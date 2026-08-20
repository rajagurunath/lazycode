"""Run one fixture task through the REAL LazyCode pipeline against OpenRouter
and export the store's own records as a shareable evidence bundle.

This is the run that proves the centerpiece claim: LazyCode takes an
online-agent-shaped goal, plans it, converts the plan into batch waves,
submits them to a real half-price batch lane, collects results, verifies,
and applies — and every step is in the store's event log, not in our prose.

Bundle layout (bench/results/openrouter-2026-08/t3-pipeline/<task>-<ts>/):
  result.json     — run_task() payload (status, waves, tokens, wall clock)
  events.json     — the §11 event log, the submit→poll→fetch→verify narrative
  nodes.json      — the plan's nodes as scheduled
  waves.json      — wave rows incl. provider batch ids + idempotency keys
  llm_calls.json  — per-call token actuals
  call_items.json — per-item batch statuses
  applied_diffs.json — what landed in the repo
  receipts.json   — OpenRouter's usage.cost per batch id + key-usage delta
  evidence.md     — human-readable walkthrough stitched from the above

  python bench/run_pipeline_evidence.py add-type-hints --live --yes-spend-real-money
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from bench.run_lazycode import run_task
from bench.run_openrouter_t1 import _load_env, check_budget
from lazycode.store import Store

OUT_ROOT = Path(__file__).parent / "results" / "openrouter-2026-08" / "t3-pipeline"
MODEL = "anthropic/claude-haiku-4.5"


def dump_tables(repo_root: Path, out: Path) -> dict:
    store = Store.open(repo=repo_root)
    tables = {}
    try:
        for table in ("events", "jobs", "nodes", "waves", "llm_calls",
                      "call_items", "applied_diffs"):
            rows = [dict(r) for r in store.conn.execute(f"SELECT * FROM {table}").fetchall()]
            for r in rows:  # decode JSON payload columns for readability
                for k, v in list(r.items()):
                    if isinstance(v, str) and v[:1] in "{[":
                        try:
                            r[k] = json.loads(v)
                        except Exception:
                            pass
            (out / f"{table}.json").write_text(json.dumps(rows, indent=1, default=str))
            tables[table] = rows
    finally:
        store.close()
    return tables


def fetch_receipts(client: httpx.Client, waves: list[dict]) -> list[dict]:
    receipts = []
    for w in waves:
        bid = w.get("batch_id")
        if not bid:
            continue
        r = client.get(f"/beta/batches/{bid}")
        if r.status_code == 200:
            d = r.json()
            receipts.append({"batch_id": bid, "status": d.get("status"),
                             "request_counts": d.get("request_counts"),
                             "usage": d.get("usage")})
    return receipts


def write_evidence_md(out: Path, task: str, result: dict, tables: dict,
                      receipts: list[dict], usage_delta: float) -> None:
    ev = tables["events"]
    lines = [
        f"# LazyCode pipeline evidence — `{task}` over OpenRouter batch",
        "",
        f"- model: `{MODEL}` · provider: `openrouter` (`/api/beta/batches`, `/v1/messages` skin)",
        f"- job status: **{result['status']}** · waves: {result['waves']} · "
        f"wall clock: {result['wall_clock_s']}s",
        f"- tokens (from `llm_calls`): {result['tokens_in']} in / {result['tokens_out']} out "
        f"across {result['llm_calls']} calls",
        f"- OpenRouter receipts: " + "; ".join(
            f"`{r['batch_id']}` → {r['status']}, ${((r.get('usage') or {}).get('cost') or 0):.6f}"
            for r in receipts) if receipts else "- receipts: none found",
        f"- key-usage delta for the whole run (planner + waves): ${usage_delta:.6f}",
        "",
        "## The conversion, in the store's own events",
        "",
        "| # | event | detail |",
        "|---|---|---|",
    ]
    for i, e in enumerate(ev):
        payload = e.get("payload") or {}
        keys = ("node_id", "wave_id", "batch_id", "idempotency_key", "passed",
                "exit_code", "item_count", "status")
        detail = ", ".join(f"{k}={payload[k]}" for k in keys if k in payload)
        lines.append(f"| {i} | {e.get('kind') or e.get('type')} | {detail[:120]} |")
    lines += [
        "",
        "## Waves (plan → provider batches)",
        "",
        "```json",
        json.dumps([{k: w.get(k) for k in ("id", "batch_id", "idempotency_key",
                                            "status", "provider")} for w in tables["waves"]],
                   indent=1),
        "```",
        "",
        "## Applied diffs",
        "",
        f"{len(tables['applied_diffs'])} diff(s) applied to the repo after verify.",
    ]
    (out / "evidence.md").write_text("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--budget-usd", type=float, default=25.0)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--yes-spend-real-money", action="store_true")
    args = ap.parse_args()
    if not (args.live and args.yes_spend_real_money):
        sys.exit("refusing to spend: pass --live --yes-spend-real-money")

    _load_env()
    import os
    client = httpx.Client(base_url="https://openrouter.ai/api",
                          headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
                          timeout=120.0)
    before = check_budget(client, args.budget_usd)

    out = OUT_ROOT / f"{args.task}-{int(time.time())}"
    workdir = out / "workdir"
    workdir.mkdir(parents=True)

    result = run_task(args.task, provider="openrouter", model=args.model,
                      workdir=workdir, write_results=False)
    (out / "result.json").write_text(json.dumps(result, indent=1, default=str))

    tables = dump_tables(workdir / "repo", out)
    receipts = fetch_receipts(client, tables["waves"])
    (out / "receipts.json").write_text(json.dumps(receipts, indent=1))
    time.sleep(5)
    after = float(client.get("/v1/key").json()["data"]["usage"] or 0)
    write_evidence_md(out, args.task, result, tables, receipts, after - before)

    print(json.dumps({k: result[k] for k in ("status", "waves", "wall_clock_s",
                                              "tokens_in", "tokens_out", "llm_calls")}, indent=1))
    print(f"[receipts] {receipts}")
    print(f"[evidence] {out}")


if __name__ == "__main__":
    main()
