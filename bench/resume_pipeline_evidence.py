"""Resume a crashed pipeline-evidence run and finish the evidence bundle.

The §7.1 crash-recovery path, exercised for real: run_job on the same store
re-polls in-flight waves (or adopts a FORMED-but-unSUBMITTED batch), processes
results, verifies, applies — no resubmission, no double spend.

  python bench/resume_pipeline_evidence.py <evidence-dir> --live --yes-spend-real-money
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from bench.run_openrouter_t1 import _load_env, check_budget
from bench.run_pipeline_evidence import MODEL, dump_tables, fetch_receipts, write_evidence_md
from bench.task_spec import load_task
from lazycode.providers.openrouter_batch import MESSAGES_ENDPOINT, OpenRouterBatchAdapter
from lazycode.scheduler import Orchestrator, SchedulerConfig
from lazycode.store import Store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("evidence_dir", type=Path)
    ap.add_argument("--task", default="add-type-hints")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--budget-usd", type=float, default=25.0)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--yes-spend-real-money", action="store_true")
    args = ap.parse_args()
    if not (args.live and args.yes_spend_real_money):
        sys.exit("refusing: pass --live --yes-spend-real-money")

    _load_env()
    client = httpx.Client(base_url="https://openrouter.ai/api",
                          headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
                          timeout=120.0)
    before = check_budget(client, args.budget_usd)

    repo_root = args.evidence_dir / "workdir" / "repo"
    task = load_task(args.task)
    store = Store.open(repo=repo_root)
    try:
        job = store.conn.execute("SELECT id, status FROM jobs").fetchone()
        print(f"[resume] job {job['id']} was {job['status']}")
        batch = OpenRouterBatchAdapter.from_env(
            provider_name="openrouter", endpoint=MESSAGES_ENDPOINT
        )
        config = SchedulerConfig(provider="openrouter", model=args.model,
                                 verify_command=task.verify_command)
        orch = Orchestrator(store, {"openrouter": batch}, repo_root, config)
        t0 = time.monotonic()
        result = orch.run_job(job["id"])
        wall = time.monotonic() - t0
        print(f"[resume] finished: {result.status} in {wall:.1f}s, waves={result.waves}")
    finally:
        store.close()

    tables = dump_tables(repo_root, args.evidence_dir)
    receipts = fetch_receipts(client, tables["waves"])
    (args.evidence_dir / "receipts.json").write_text(json.dumps(receipts, indent=1))
    llm = tables["llm_calls"]
    summary = {
        "status": result.status, "waves": result.waves, "wall_clock_s": round(wall, 2),
        "tokens_in": sum(r.get("tokens_in") or 0 for r in llm),
        "tokens_out": sum(r.get("tokens_out") or 0 for r in llm),
        "llm_calls": len(llm),
    }
    (args.evidence_dir / "result.json").write_text(json.dumps(summary, indent=1))
    time.sleep(5)
    after = float(client.get("/v1/key").json()["data"]["usage"] or 0)
    write_evidence_md(args.evidence_dir, args.task, summary, tables, receipts, after - before)
    print(f"[receipts] {receipts}")
    print(f"[evidence] {args.evidence_dir}")


if __name__ == "__main__":
    main()
