"""T1 (HumanEval subset-50) runner for the OpenRouter cost study.

Arms:
  sync   — one OpenRouter sync chat completion per problem, full price.
           The lazycode-sync ablation (T1's plan is degenerate: one node per
           problem, planner cost $0 by construction — recorded as such).
  batch  — the same RenderedCalls as ONE wave through
           lazycode.providers.openrouter_batch (the product arm).
           submit-only by default; poll/collect later with --collect.
  (interactive arm lives in run_baseline.py — a coding agent, not this file.)

Receipts, not list prices: sync records each generation id and resolves
GET /api/v1/generation for the actual charge + cached tokens; batch records
the completed batch's usage.cost.

Spend guard: refuses to start if the key's OpenRouter-reported usage would
exceed --budget-usd (default 25). Live calls require --live
--yes-spend-real-money, same contract as cost_report.py.

Usage:
  python bench/run_openrouter_t1.py --arm sync  --model anthropic/claude-haiku-4.5 -n 2 --live --yes-spend-real-money
  python bench/run_openrouter_t1.py --arm batch --model anthropic/claude-haiku-4.5 -n 2 --live --yes-spend-real-money
  python bench/run_openrouter_t1.py --collect <batch_id> --model ...   # poll+verify+receipt
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

from bench.datasets import humaneval as he
from lazycode.ir import BatchRef, Message, RenderedCall
from lazycode.providers.openrouter_batch import BASE_URL, OpenRouterBatchAdapter

RESULTS_DIR = Path(__file__).parent / "results" / "openrouter-2026-08" / "t1"
MAX_TOKENS = 1200
TEMPERATURE = 0.0


def _load_env() -> None:
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def _client() -> httpx.Client:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("OPENROUTER_API_KEY not set (checked env and lazycode/.env)")
    return httpx.Client(
        base_url=BASE_URL, headers={"Authorization": f"Bearer {key}"}, timeout=180.0
    )


def check_budget(client: httpx.Client, budget_usd: float) -> float:
    r = client.get("/v1/key").json().get("data", {})
    used = float(r.get("usage") or 0.0)
    if used >= budget_usd:
        sys.exit(f"REFUSING: key usage ${used:.2f} >= budget ${budget_usd:.2f}")
    print(f"[budget] key usage ${used:.4f} of ${budget_usd:.2f} cap")
    return used


def rendered_calls(model: str, n: int | None) -> list[tuple[dict, RenderedCall]]:
    probs = he.load_problems()
    ids = he.subset_ids()[: n or None]
    out = []
    for tid in ids:
        p = probs[tid]
        call = RenderedCall(
            custom_id=tid.replace("/", "-"),
            model=model,
            messages=[Message(role="user", content=he.build_prompt(p))],
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            memo_key=f"t1-{tid}",
        )
        out.append((p, call))
    return out


def generation_receipt(client: httpx.Client, gen_id: str, retries: int = 5) -> dict:
    for i in range(retries):
        r = client.get("/v1/generation", params={"id": gen_id})
        if r.status_code == 200:
            d = r.json().get("data", {})
            return {
                "total_cost": d.get("total_cost"),
                "tokens_prompt": d.get("tokens_prompt"),
                "tokens_completion": d.get("tokens_completion"),
                "cache_discount": d.get("cache_discount"),
                "provider_name": d.get("provider_name"),
                "model": d.get("model"),
            }
        time.sleep(1.5 * (i + 1))
    return {"error": f"receipt unavailable after {retries} tries ({r.status_code})"}


def run_sync(client: httpx.Client, model: str, n: int | None, seed_tag: str) -> None:
    rows = []
    for p, call in rendered_calls(model, n):
        body = {
            "model": model,
            "max_tokens": call.max_tokens,
            "temperature": call.temperature,
            "messages": [{"role": m.role, "content": m.content} for m in call.messages],
        }
        t0 = time.time()
        resp = client.post("/v1/chat/completions", json=body)
        latency = time.time() - t0
        resp.raise_for_status()
        rj = resp.json()
        text = rj["choices"][0]["message"]["content"] or ""
        code = he.extract_code(text)
        passed, detail = he.verify(p, code)
        receipt = generation_receipt(client, rj["id"])
        rows.append({
            "task_id": p["task_id"], "arm": "sync", "model": model,
            "gen_id": rj["id"], "latency_s": round(latency, 2),
            "usage": rj.get("usage"), "receipt": receipt,
            "passed": passed, "verify_detail": None if passed else detail,
            "response_text": text,
        })
        print(f"  {p['task_id']}: {'PASS' if passed else 'FAIL'} "
              f"${(receipt.get('total_cost') or 0):.6f} {latency:.1f}s")
    _write(rows, f"sync-{model.split('/')[-1]}-{seed_tag}")
    total = sum(r["receipt"].get("total_cost") or 0 for r in rows)
    npass = sum(r["passed"] for r in rows)
    print(f"[sync] {npass}/{len(rows)} pass, receipted total ${total:.4f}")


def run_batch_submit(model: str, n: int | None, seed_tag: str) -> None:
    adapter = OpenRouterBatchAdapter.from_env()
    calls = [c for _, c in rendered_calls(model, n)]
    ref = adapter.submit(calls, idempotency_key=f"t1-{model}-{seed_tag}-n{len(calls)}")
    meta = {"batch_id": ref.batch_id, "model": model, "n": len(calls),
            "submitted_unix": time.time(), "seed_tag": seed_tag}
    _write([meta], f"batch-submit-{model.split('/')[-1]}-{seed_tag}")
    print(f"[batch] submitted {len(calls)} items -> {ref.batch_id}")
    print(f"        collect with: --collect {ref.batch_id} --model {model} -n {len(calls)}")


def run_batch_collect(client: httpx.Client, batch_id: str, model: str,
                      n: int | None, seed_tag: str, wait: bool) -> None:
    adapter = OpenRouterBatchAdapter.from_env()
    ref = BatchRef(provider="openrouter-batch", batch_id=batch_id)
    while True:
        st = adapter.poll(ref)
        print(f"[batch {batch_id}] {st.batch_status} "
              f"done={st.completed} err={st.errored} inflight={st.processing}")
        if st.is_terminal:
            break
        if not wait:
            return
        time.sleep(30)
    probs = {p["task_id"].replace("/", "-"): p for p, _ in rendered_calls(model, n)}
    rows = []
    for item in adapter.fetch(ref):
        p = probs.get(item.custom_id)
        if p is None or item.payload is None:
            rows.append({"custom_id": item.custom_id, "arm": "batch",
                         "status": str(item.status), "error": item.error})
            continue
        text = (p and item.payload["choices"][0]["message"]["content"]) or ""
        code = he.extract_code(text)
        passed, detail = he.verify(p, code)
        rows.append({
            "task_id": p["task_id"], "arm": "batch", "model": model,
            "gen_id": item.payload.get("id"), "status": str(item.status),
            "passed": passed, "verify_detail": None if passed else detail,
            "response_text": text,
        })
        print(f"  {p['task_id']}: {'PASS' if passed else 'FAIL'}")
    summary = {"batch_id": batch_id, "usage": adapter.last_usage,
               "n": len(rows), "passed": sum(r.get("passed") is True for r in rows)}
    _write(rows + [summary], f"batch-collect-{model.split('/')[-1]}-{seed_tag}")
    print(f"[batch] {summary['passed']}/{summary['n']} pass, "
          f"receipt: {adapter.last_usage}")


def _write(rows: list[dict], name: str) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{name}-{int(time.time())}.json"
    path.write_text(json.dumps(rows, indent=1, default=str))
    print(f"[out] {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["sync", "batch"])
    ap.add_argument("--collect", metavar="BATCH_ID")
    ap.add_argument("--wait", action="store_true", help="poll --collect to terminal")
    ap.add_argument("--model", required=True)
    ap.add_argument("-n", type=int, default=None, help="first n subset problems")
    ap.add_argument("--seed-tag", default="s1")
    ap.add_argument("--budget-usd", type=float, default=25.0)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--yes-spend-real-money", action="store_true")
    args = ap.parse_args()

    _load_env()
    if not (args.live and args.yes_spend_real_money) and not args.collect:
        sys.exit("refusing to spend: pass --live --yes-spend-real-money")

    client = _client()
    check_budget(client, args.budget_usd)

    if args.collect:
        run_batch_collect(client, args.collect, args.model, args.n, args.seed_tag, args.wait)
    elif args.arm == "sync":
        run_sync(client, args.model, args.n, args.seed_tag)
    elif args.arm == "batch":
        run_batch_submit(args.model, args.n, args.seed_tag)
    else:
        sys.exit("pass --arm sync|batch or --collect BATCH_ID")


if __name__ == "__main__":
    main()
