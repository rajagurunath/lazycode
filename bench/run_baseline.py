#!/usr/bin/env python3
"""Run one benchmark task through the Claude Code CLI headless -- the pinned
cost baseline for the M0 accept criterion (b) (DESIGN.md §14, Appendix B7:
"a script that runs the same task prompt through Claude Code CLI (same model
family), captures its token usage from its own telemetry/logs, prices at
list, and writes a comparable report.json"). Built alongside
``run_lazycode.py`` so ``bench/compare.py`` has both sides of the trade
before the M0 accept test exists (B7: "built before this test, not
alongside").

Usage::

    # needs the `claude` CLI on PATH and its own auth configured; runs a
    # real (billed) Claude Code session against a fresh copy of the task
    # fixture repo:
    uv run python bench/run_baseline.py add-type-hints

    # if `claude` isn't installed, this degrades gracefully -- writes a
    # status="unavailable" result instead of raising, so a benchmark sweep
    # (or `bench/compare.py`) can still run and report "baseline not
    # available" rather than crashing.

Deliberately shells out rather than importing an SDK: Claude Code's own
CLI *is* the artifact being priced against (list price, no cache-read
discount -- DESIGN.md §0's honest baseline), so this baseline must measure
the actual CLI's own reported usage, not a hand-rolled equivalent call.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from .pricing import realtime_cost_usd
    from .task_spec import build_repo, list_tasks, load_task
except ImportError:  # running as a plain script (`python bench/run_baseline.py`)
    from pricing import realtime_cost_usd  # type: ignore[no-redef]
    from task_spec import build_repo, list_tasks, load_task  # type: ignore[no-redef]

RESULTS_DIR = Path(__file__).parent / "results"
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TIMEOUT_S = 900.0


def _claude_bin() -> str | None:
    return shutil.which("claude")


def _write(task_name: str, payload: dict[str, Any]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{task_name}-baseline.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _invoke_claude(
    claude_bin: str, goal: str, *, model: str, repo_root: Path, timeout_s: float
) -> subprocess.CompletedProcess[str]:
    """The one place that actually shells out to ``claude``. Kept as its own
    function (rather than an inline ``subprocess.run`` call) so tests can
    monkeypatch *this* symbol -- patching ``subprocess.run`` directly would
    also intercept ``task_spec.build_repo``'s ``git`` calls, since both
    modules share the one ``subprocess`` module object."""
    return subprocess.run(
        [
            claude_bin,
            "-p",
            goal,
            "--output-format",
            "json",
            "--model",
            model,
            "--permission-mode",
            "bypassPermissions",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def _parse_claude_json(stdout: str) -> tuple[dict[str, Any], float | None]:
    """Parse Claude Code's ``--output-format json`` single-result payload.
    Tolerant of shape drift across Claude Code versions -- this baseline
    only needs the ``usage`` token counts (and, best-effort, the CLI's own
    reported cost for a sanity cross-check); anything else in the payload
    is ignored."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {}, None
    usage = data.get("usage") or {}
    reported_cost = data.get("total_cost_usd", data.get("cost_usd"))
    return usage, reported_cost


def run_task(
    task_name: str,
    *,
    model: str = DEFAULT_MODEL,
    workdir: Path | None = None,
    write_results: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    claude_bin: str | None = None,
) -> dict[str, Any]:
    """Run ``task_name``'s goal through the Claude Code CLI in headless
    (``--print --output-format json``) mode against a fresh copy of the
    task's fixture repo. Returns (and, by default, persists) a result dict
    shaped comparably to ``run_lazycode.run_task``'s (``tokens_in``,
    ``tokens_out``, ``cost_usd``, ``wall_clock_s``, ``status``).

    ``claude_bin`` overrides PATH lookup (mainly for tests); when neither
    that nor ``shutil.which("claude")`` resolves, this **degrades
    gracefully**: it returns/writes a ``status="unavailable"`` result
    instead of raising, so a benchmark sweep or ``bench/compare.py`` can
    still complete and just report the baseline as not run.
    """
    task = load_task(task_name)
    resolved_bin = claude_bin if claude_bin is not None else _claude_bin()
    if resolved_bin is None:
        payload: dict[str, Any] = {
            "task": task_name,
            "provider": "claude-code",
            "model": model,
            "status": "unavailable",
            "note": "`claude` CLI not found on PATH -- install Claude Code to run this baseline.",
        }
        if write_results:
            _write(task_name, payload)
        return payload

    owns_workdir = workdir is None
    tmp = workdir or Path(tempfile.mkdtemp(prefix=f"lazycode-bench-baseline-{task_name}-"))
    repo_root = tmp / "repo"
    try:
        build_repo(task, repo_root)

        t0 = time.monotonic()
        try:
            result = _invoke_claude(
                resolved_bin, task.goal, model=model, repo_root=repo_root, timeout_s=timeout_s
            )
        except subprocess.TimeoutExpired:
            payload = {
                "task": task_name,
                "provider": "claude-code",
                "model": model,
                "status": "timeout",
                "note": f"claude CLI exceeded {timeout_s}s timeout",
            }
            if write_results:
                _write(task_name, payload)
            return payload
        wall_clock_s = time.monotonic() - t0

        if result.returncode != 0:
            payload = {
                "task": task_name,
                "provider": "claude-code",
                "model": model,
                "status": "error",
                "wall_clock_s": round(wall_clock_s, 4),
                "note": (result.stderr or result.stdout)[-2000:],
            }
            if write_results:
                _write(task_name, payload)
            return payload

        usage, reported_cost = _parse_claude_json(result.stdout)
        tokens_in = int(
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        tokens_out = int(usage.get("output_tokens", 0))
        payload = {
            "task": task_name,
            "provider": "claude-code",
            "model": model,
            "status": "DONE",
            "wall_clock_s": round(wall_clock_s, 4),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round(realtime_cost_usd(model, tokens_in, tokens_out), 6),
            "reported_cost_usd": reported_cost,
        }
    finally:
        if owns_workdir:
            shutil.rmtree(tmp, ignore_errors=True)

    if write_results:
        _write(task_name, payload)
    return payload


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", choices=list_tasks())
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args()

    result = run_task(args.task, model=args.model, timeout_s=args.timeout_s)
    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("status") == "unavailable":
        raise SystemExit(1)


if __name__ == "__main__":
    _main()
