#!/usr/bin/env python3
"""Run one benchmark task through lazycode end to end (DESIGN.md §14
benchmark suite, Appendix B7) and write ``bench/results/<task>-lazycode.json``.

Uses the same Python API ``lazycode run`` does (``propose_plan`` +
``Orchestrator.create_job``/``run_job`` -- see ``cli/app.py::_run_in_process``)
directly, in-process, rather than shelling out to the CLI: a benchmark run
wants the token/wave/wall-clock actuals straight out of the store, and this
avoids a subprocess round-trip for every task run.

Usage::

    # deterministic, zero network -- what tests/bench/ exercises:
    uv run python bench/run_lazycode.py add-type-hints \\
        --provider mock --fixture bench/tasks/add-type-hints/mock_fixture.json

    # real run against Anthropic batch (needs ANTHROPIC_API_KEY; manual --
    # see bench/README.md):
    uv run python bench/run_lazycode.py add-type-hints --provider anthropic
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from .pricing import batch_cost_usd
    from .task_spec import build_repo, list_tasks, load_task
except ImportError:  # running as a plain script (`python bench/run_lazycode.py`)
    from pricing import batch_cost_usd  # type: ignore[no-redef]
    from task_spec import build_repo, list_tasks, load_task  # type: ignore[no-redef]

from lazycode.cli import mock_provider
from lazycode.ir import Plan
from lazycode.planner import propose_plan
from lazycode.providers.anthropic_batch import AnthropicBatchAdapter
from lazycode.providers.realtime import AnthropicRealtimeAdapter
from lazycode.scheduler import Orchestrator, SchedulerConfig
from lazycode.store import Store

RESULTS_DIR = Path(__file__).parent / "results"
DEFAULT_MODEL = "claude-haiku-4-5"


def run_task(
    task_name: str,
    *,
    provider: str = "mock",
    model: str = DEFAULT_MODEL,
    fixture: dict[str, Any] | None = None,
    fixture_path: Path | None = None,
    workdir: Path | None = None,
    write_results: bool = True,
) -> dict[str, Any]:
    """Run ``task_name`` through lazycode; return (and, by default, persist)
    the results dict.

    ``provider="mock"`` needs either ``fixture`` (a dict, for programmatic/
    test callers -- see ``tests/bench``) or ``fixture_path`` (a JSON file, for
    the CLI). ``provider="anthropic"`` runs for real and needs
    ``ANTHROPIC_API_KEY`` set (manual only -- see ``bench/README.md``).
    ``workdir``, if given, is used (and left in place) instead of a
    temp directory the caller doesn't control -- useful for a test that wants
    to inspect the resulting worktree afterwards.
    """
    task = load_task(task_name)
    owns_workdir = workdir is None
    tmp = workdir or Path(tempfile.mkdtemp(prefix=f"lazycode-bench-{task_name}-"))
    repo_root = tmp / "repo"
    try:
        base_commit = build_repo(task, repo_root)
        store = Store.open(repo=repo_root)
        try:
            sched_config = SchedulerConfig(provider=provider, model=model, verify_command=task.verify_command)

            if provider == "mock":
                if fixture is None:
                    if fixture_path is None:
                        raise ValueError("provider='mock' requires fixture= or fixture_path=")
                    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
                realtime = mock_provider.build_mock_realtime_adapter(fixture)
                batch = mock_provider.FixtureBatchAdapter(fixture)
                plan = Plan.model_validate(fixture["planner_response"])
            else:
                api_key_env = "ANTHROPIC_API_KEY"
                if not os.environ.get(api_key_env):
                    raise RuntimeError(
                        f"provider={provider!r} needs {api_key_env} set for a real benchmark run "
                        "(bench/README.md documents the manual real-provider workflow)"
                    )
                realtime = AnthropicRealtimeAdapter.from_env(api_key_env=api_key_env)
                batch = AnthropicBatchAdapter.from_env(api_key_env=api_key_env)
                plan = propose_plan(task.goal, str(repo_root), realtime, model)

            orch = Orchestrator(store, {provider: batch}, repo_root, sched_config)
            job_id = orch.create_job(task.goal, plan, base_commit, slider=100)

            t0 = time.monotonic()
            job_result = orch.run_job(job_id)
            wall_clock_s = time.monotonic() - t0

            stats = _collect_stats(store, job_id, model=model)
        finally:
            store.close()
    finally:
        if owns_workdir:
            shutil.rmtree(tmp, ignore_errors=True)

    payload = {
        "task": task_name,
        "provider": provider,
        "model": model,
        "job_id": job_id,
        "status": job_result.status,
        "waves": job_result.waves,
        "wall_clock_s": round(wall_clock_s, 4),
        **stats,
    }
    if write_results:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = RESULTS_DIR / f"{task_name}-lazycode.json"
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _collect_stats(store: Store, job_id: str, *, model: str) -> dict[str, Any]:
    """Token/call actuals from ``llm_calls`` (B7: "lazycode's own actuals
    come from llm_calls"), scoped to this job's nodes."""
    node_ids = [r["id"] for r in store.conn.execute("SELECT id FROM nodes WHERE job_id = ?", (job_id,)).fetchall()]
    if not node_ids:
        return {"tokens_in": 0, "tokens_out": 0, "llm_calls": 0, "cost_usd": 0.0}
    placeholders = ",".join("?" for _ in node_ids)
    rows = store.conn.execute(
        f"SELECT tokens_in, tokens_out FROM llm_calls WHERE node_id IN ({placeholders})", node_ids
    ).fetchall()
    tokens_in = sum(r["tokens_in"] or 0 for r in rows)
    tokens_out = sum(r["tokens_out"] or 0 for r in rows)
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "llm_calls": len(rows),
        "cost_usd": round(batch_cost_usd(model, tokens_in, tokens_out), 6),
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", choices=list_tasks())
    parser.add_argument("--provider", default="mock", choices=["mock", "anthropic"])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--fixture", type=Path, default=None, help="mock fixture JSON (required for --provider mock)"
    )
    args = parser.parse_args()

    result = run_task(args.task, provider=args.provider, model=args.model, fixture_path=args.fixture)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    _main()
