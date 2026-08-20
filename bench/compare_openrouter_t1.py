"""Aggregate T1 result files into the three-arm table for the paper.

Reads every JSON under bench/results/openrouter-2026-08/t1/, dedupes by
(arm, model, task_id) keeping the LATEST row (so a re-run supersedes), and
prints per-model: pass rate, receipted cost, cost per solved problem, and
the sync-vs-batch discount actually charged.

  python bench/compare_openrouter_t1.py            # table
  python bench/compare_openrouter_t1.py --json out.json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

RESULTS = Path(__file__).parent / "results" / "openrouter-2026-08" / "t1"


def load_rows() -> tuple[dict, dict]:
    rows: dict[tuple, dict] = {}
    batch_usage: dict[str, dict] = {}  # model -> latest batch usage receipt
    for path in sorted(RESULTS.glob("*.json")):
        ts = path.stat().st_mtime
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        for row in data:
            if not isinstance(row, dict):
                continue
            if "task_id" in row and "arm" in row:
                key = (row["arm"], row.get("model"), row["task_id"])
                prev = rows.get(key)
                if prev is None or prev["_ts"] <= ts:
                    rows[key] = {**row, "_ts": ts}
            elif "usage" in row and "batch_id" in row and row.get("usage"):
                model = row.get("model") or _model_from_name(path.name)
                batch_usage[model] = {**row, "_ts": ts}
    return rows, batch_usage


def _model_from_name(name: str) -> str:
    # batch-collect-<model-tail>-<seed>-<ts>.json
    parts = name.split("-")
    return "-".join(parts[2:-2]) if len(parts) > 4 else name


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    rows, batch_usage = load_rows()
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for (arm, model, _tid), row in rows.items():
        cells[(model or "?", arm)].append(row)

    table = []
    for (model, arm), rws in sorted(cells.items()):
        n = len(rws)
        npass = sum(r.get("passed") is True for r in rws)
        receipted = sum((r.get("receipt") or {}).get("total_cost") or 0 for r in rws)
        cli = sum(r.get("cli_cost_usd") or 0 for r in rws)
        turns = [r["num_turns"] for r in rws if r.get("num_turns")]
        entry = {
            "model": model, "arm": arm, "n": n,
            "pass": npass, "pass_rate": round(npass / n, 3) if n else None,
            "receipted_usd": round(receipted, 6) or None,
            "cli_reported_usd": round(cli, 4) or None,
            "mean_turns": round(sum(turns) / len(turns), 1) if turns else None,
        }
        table.append(entry)

    # batch receipts are batch-level, attach + derive per-model discount
    for model, u in batch_usage.items():
        usage = u["usage"] if isinstance(u.get("usage"), dict) else {}
        for entry in table:
            if entry["arm"] == "batch" and model in (entry["model"] or ""):
                entry["receipted_usd"] = usage.get("cost")
                entry["batch_tokens"] = {
                    "prompt": usage.get("prompt_tokens"),
                    "completion": usage.get("completion_tokens"),
                }

    print(f"{'model':<28} {'arm':<12} {'n':>3} {'pass':>5} {'receipt $':>10} {'cli $':>8} {'turns':>6}")
    for e in table:
        print(f"{e['model'] or '?':<28} {e['arm']:<12} {e['n']:>3} "
              f"{e['pass']:>3}/{e['n']:<3} "
              f"{e['receipted_usd'] if e['receipted_usd'] is not None else '-':>10} "
              f"{e['cli_reported_usd'] or '-':>8} {e['mean_turns'] or '-':>6}")

    if args.json:
        args.json.write_text(json.dumps(table, indent=1))
        print(f"[out] {args.json}")


if __name__ == "__main__":
    main()
