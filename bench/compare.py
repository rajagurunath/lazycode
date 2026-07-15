#!/usr/bin/env python3
"""Compare lazycode's actuals against the Claude Code CLI baseline for one
or more benchmark tasks and render the M0 accept criterion (b) verdict
(DESIGN.md §14: "total token cost < 50% of the pinned baseline"; Appendix
B7). Reads the two JSON files ``run_lazycode.py``/``run_baseline.py``
already wrote to ``bench/results/`` -- this script does no execution of its
own, only comparison.

Usage::

    uv run python bench/run_lazycode.py add-type-hints --provider mock \\
        --fixture bench/tasks/add-type-hints/mock_fixture.json
    uv run python bench/run_baseline.py add-type-hints
    uv run python bench/compare.py add-type-hints
    uv run python bench/compare.py                 # all tasks with a lazycode result
    uv run python bench/compare.py --json           # machine-readable
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from .task_spec import list_tasks
except ImportError:  # running as a plain script (`python bench/compare.py`)
    from task_spec import list_tasks  # type: ignore[no-redef]

RESULTS_DIR = Path(__file__).parent / "results"
_PASS_THRESHOLD = 0.5  # DESIGN.md §14 M0 accept (b): < 50% of the baseline.


class ComparisonError(Exception):
    """A comparison can't be produced (missing the lazycode side)."""


def load_result(task: str, suffix: str) -> dict[str, Any] | None:
    path = RESULTS_DIR / f"{task}-{suffix}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def compare_task(task: str) -> dict[str, Any]:
    """One comparison row for ``task``. The lazycode side is required
    (raises :class:`ComparisonError` if missing -- run ``run_lazycode.py``
    first). The baseline side is optional: a task whose baseline is
    missing, ``status="unavailable"`` (no ``claude`` CLI -- see
    ``run_baseline.py``), or otherwise non-``DONE`` still gets a row, just
    with ``verdict=None`` and a ``note`` explaining why, instead of raising."""
    lazycode = load_result(task, "lazycode")
    if lazycode is None:
        raise ComparisonError(f"no lazycode result for {task!r} -- run bench/run_lazycode.py {task} first")

    lc_tokens_in = lazycode.get("tokens_in", 0) or 0
    lc_tokens_out = lazycode.get("tokens_out", 0) or 0
    row: dict[str, Any] = {
        "task": task,
        "lazycode": {
            "status": lazycode.get("status"),
            "waves": lazycode.get("waves"),
            "tokens_in": lc_tokens_in,
            "tokens_out": lc_tokens_out,
            "tokens_total": lc_tokens_in + lc_tokens_out,
            "cost_usd": lazycode.get("cost_usd", 0.0),
            "wall_clock_s": lazycode.get("wall_clock_s"),
        },
        "baseline": None,
        "token_ratio": None,
        "cost_ratio": None,
        "verdict": None,
    }

    baseline = load_result(task, "baseline")
    if baseline is None:
        row["note"] = "no baseline result on disk -- run bench/run_baseline.py first"
        return row
    if baseline.get("status") != "DONE":
        row["note"] = f"baseline status={baseline.get('status')!r}: {baseline.get('note', 'no detail')}"
        return row

    base_tokens_in = baseline.get("tokens_in", 0) or 0
    base_tokens_out = baseline.get("tokens_out", 0) or 0
    base_tokens_total = base_tokens_in + base_tokens_out
    row["baseline"] = {
        "tokens_in": base_tokens_in,
        "tokens_out": base_tokens_out,
        "tokens_total": base_tokens_total,
        "cost_usd": baseline.get("cost_usd", 0.0),
        "wall_clock_s": baseline.get("wall_clock_s"),
    }

    if base_tokens_total:
        row["token_ratio"] = round(row["lazycode"]["tokens_total"] / base_tokens_total, 4)
        row["verdict"] = "PASS" if row["token_ratio"] < _PASS_THRESHOLD else "FAIL"
    if baseline.get("cost_usd"):
        row["cost_ratio"] = round(row["lazycode"]["cost_usd"] / baseline["cost_usd"], 4)
    return row


def render_table(rows: list[dict[str, Any]]) -> str:
    header = f"{'task':<22}{'waves':>7}{'lc tokens':>12}{'base tokens':>13}{'ratio':>9}{'verdict':>9}"
    lines = [header, "-" * len(header)]
    for r in rows:
        lc = r["lazycode"]
        if r["baseline"] is None:
            lines.append(f"{r['task']:<22}{str(lc['waves']):>7}{lc['tokens_total']:>12}{'—':>13}{'—':>9}{'—':>9}  {r.get('note', '')}")
            continue
        base = r["baseline"]
        ratio = r["token_ratio"]
        ratio_txt = f"{ratio * 100:.1f}%" if ratio is not None else "—"
        verdict = r["verdict"] or "—"
        lines.append(
            f"{r['task']:<22}{str(lc['waves']):>7}{lc['tokens_total']:>12}{base['tokens_total']:>13}"
            f"{ratio_txt:>9}{verdict:>9}"
        )
    return "\n".join(lines)


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "tasks", nargs="*", help="task names to compare (default: every task with a lazycode result on disk)"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the text table")
    args = parser.parse_args()

    task_names = args.tasks or [t for t in list_tasks() if load_result(t, "lazycode") is not None]
    if not task_names:
        raise SystemExit(
            "no lazycode results found in bench/results/ -- run bench/run_lazycode.py <task> first"
        )

    rows = [compare_task(t) for t in task_names]
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print(render_table(rows))

    failures = [r["task"] for r in rows if r["verdict"] == "FAIL"]
    if failures:
        raise SystemExit(f"FAIL: {', '.join(failures)} did not beat the <{int(_PASS_THRESHOLD * 100)}% token baseline")


if __name__ == "__main__":
    _main()
