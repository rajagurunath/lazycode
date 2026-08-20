"""T1 interactive arm: each HumanEval problem through Claude Code headless,
routed via OpenRouter (same model slug as the other two arms).

Routing: ANTHROPIC_BASE_URL=https://openrouter.ai/api +
ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY + ANTHROPIC_API_KEY="" (blank on
purpose — forces the CLI onto the auth token). Cost is receipted two ways:
the CLI's own total_cost_usd (list-price telemetry) AND the OpenRouter
key-usage delta around the whole run (the actual charge — the number the
paper uses).

Per problem the agent gets the same task text as the other arms plus one
instruction: write the complete function to solution.py. The verifier is
byte-identical across arms (bench.datasets.humaneval.verify).

  python bench/run_openrouter_t1_interactive.py --model anthropic/claude-haiku-4.5 -n 2 --live --yes-spend-real-money
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from bench.datasets import humaneval as he
from bench.run_openrouter_t1 import RESULTS_DIR, _load_env, check_budget

TIMEOUT_S = 300.0


def key_usage(client: httpx.Client) -> float:
    return float(client.get("/v1/key").json().get("data", {}).get("usage") or 0.0)


def run_one(claude_bin: str, problem: dict, model: str, env: dict) -> dict:
    prompt = (
        he.build_prompt(problem)
        + "\n\nWrite the complete function (with any imports it needs) to a file "
          "named solution.py in the current directory. Then re-read solution.py "
          "to confirm it contains the full function."
    )
    with tempfile.TemporaryDirectory() as tmp:
        t0 = time.time()
        try:
            proc = subprocess.run(
                [claude_bin, "-p", prompt, "--output-format", "json",
                 "--model", model, "--permission-mode", "bypassPermissions"],
                capture_output=True, text=True, timeout=TIMEOUT_S, cwd=tmp, env=env,
            )
        except subprocess.TimeoutExpired:
            return {"task_id": problem["task_id"], "arm": "interactive",
                    "status": "timeout", "passed": False}
        wall = time.time() - t0
        sol = Path(tmp) / "solution.py"
        code = sol.read_text() if sol.exists() else ""
    passed, detail = (False, "no solution.py") if not code else he.verify(problem, code)
    row = {"task_id": problem["task_id"], "arm": "interactive", "model": model,
           "wall_s": round(wall, 1), "passed": passed,
           "verify_detail": None if passed else detail[-300:]}
    try:
        rj = json.loads(proc.stdout)
        row["cli_cost_usd"] = rj.get("total_cost_usd")
        row["num_turns"] = rj.get("num_turns")
        row["cli_usage"] = rj.get("usage")
    except Exception:
        row["cli_parse_error"] = (proc.stdout or proc.stderr)[-300:]
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("-n", type=int, default=None)
    ap.add_argument("--seed-tag", default="s1")
    ap.add_argument("--budget-usd", type=float, default=25.0)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--yes-spend-real-money", action="store_true")
    args = ap.parse_args()

    if not (args.live and args.yes_spend_real_money):
        sys.exit("refusing to spend: pass --live --yes-spend-real-money")
    _load_env()
    claude_bin = shutil.which("claude") or sys.exit("claude CLI not on PATH")
    key = os.environ["OPENROUTER_API_KEY"]
    client = httpx.Client(base_url="https://openrouter.ai/api",
                          headers={"Authorization": f"Bearer {key}"}, timeout=60.0)
    check_budget(client, args.budget_usd)

    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = "https://openrouter.ai/api"
    env["ANTHROPIC_AUTH_TOKEN"] = key
    env["ANTHROPIC_API_KEY"] = ""
    for var in ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(var, None)

    probs = he.load_problems()
    ids = he.subset_ids()[: args.n or None]
    usage_before = key_usage(client)
    rows = []
    for tid in ids:
        row = run_one(claude_bin, probs[tid], args.model, env)
        rows.append(row)
        print(f"  {tid}: {'PASS' if row.get('passed') else 'FAIL'} "
              f"cli=${row.get('cli_cost_usd') or 0:.4f} turns={row.get('num_turns')} "
              f"{row.get('wall_s', '?')}s")
    time.sleep(5)
    usage_after = key_usage(client)
    summary = {"arm": "interactive", "model": args.model, "n": len(rows),
               "passed": sum(r.get("passed") is True for r in rows),
               "or_receipted_usd": round(usage_after - usage_before, 6),
               "cli_reported_usd": round(sum(r.get("cli_cost_usd") or 0 for r in rows), 6)}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"interactive-{args.model.split('/')[-1]}-{args.seed_tag}-{int(time.time())}.json"
    out.write_text(json.dumps(rows + [summary], indent=1, default=str))
    print(f"[out] {out}")
    print(f"[interactive] {summary['passed']}/{summary['n']} pass, "
          f"OR receipt ${summary['or_receipted_usd']:.4f}, "
          f"CLI-reported ${summary['cli_reported_usd']:.4f}")


if __name__ == "__main__":
    main()
