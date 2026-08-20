"""T2 (SWE-bench Verified, 10 "<15 min fix" instances) — the deep tier.

The unfavorable shape for LazyCode: dependent repo context, where a batch
lane cannot iterate. Three arms, same rules as T1:

  sync / batch  — single-shot: problem statement + the oracle file (the one
                  file the gold patch touches; the recognized "oracle"
                  retrieval setting). The model emits the COMPLETE corrected
                  file; we compute the unified diff locally, so models are
                  graded on the fix, not on diff syntax.
  interactive   — Claude Code headless in a real checkout at base_commit,
                  free to explore; the prediction is `git diff`.

Predictions land as SWE-bench predictions JSONL; evaluation runs separately
through the official harness (docker).

  python bench/run_openrouter_t2.py --arm sync --model anthropic/claude-haiku-4.5 --live --yes-spend-real-money
  python bench/run_openrouter_t2.py --arm batch ... ; --collect BATCH_ID ...
  python bench/run_openrouter_t2.py --arm interactive --model anthropic/claude-haiku-4.5 --live --yes-spend-real-money
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from bench.run_openrouter_t1 import _client, _load_env, check_budget, generation_receipt
from lazycode.ir import BatchRef, Message, RenderedCall
from lazycode.providers.openrouter_batch import OpenRouterBatchAdapter

SUBSET_FILE = Path(__file__).parent / "datasets" / "swebench_subset10.json"
RESULTS_DIR = Path(__file__).parent / "results" / "openrouter-2026-08" / "t2"
REPO_CACHE = Path(os.environ.get(
    "LAZYCODE_T2_REPO_CACHE",
    Path(tempfile.gettempdir()) / "lazycode-t2-repos",
))
MAX_TOKENS = 16000
TIMEOUT_INTERACTIVE_S = 600.0

PROMPT_TEMPLATE = """\
You are fixing a real reported issue in {repo}.

<issue>
{problem}
</issue>

The fix belongs in `{path}`. Here is its current content:

```python
{content}
```

Output the COMPLETE corrected file in a single ```python code block, no
explanation before or after. Change only what the fix requires; keep every
other line byte-identical."""


def load_subset() -> list[dict]:
    return json.loads(SUBSET_FILE.read_text())


def oracle_path(inst: dict) -> str:
    for line in inst["patch"].splitlines():
        if line.startswith("+++ b/"):
            return line[6:]
    raise ValueError(f"no +++ path in gold patch for {inst['instance_id']}")


def repo_checkout(inst: dict) -> Path:
    """Blobless-clone cache per repo; detached worktree per instance."""
    REPO_CACHE.mkdir(parents=True, exist_ok=True)
    repo = inst["repo"]
    cache = REPO_CACHE / repo.replace("/", "__")
    if not cache.exists():
        subprocess.run(
            ["git", "clone", "--filter=blob:none", f"https://github.com/{repo}.git", str(cache)],
            check=True, capture_output=True, text=True,
        )
    wt = REPO_CACHE / "wt" / inst["instance_id"]
    if wt.exists():
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(cache), "fetch", "origin", inst["base_commit"]],
                   capture_output=True, text=True)
    r = subprocess.run(["git", "-C", str(cache), "worktree", "add", "--detach",
                        str(wt), inst["base_commit"]], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"worktree add failed for {inst['instance_id']}: {r.stderr[-300:]}")
    return wt


_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def file_to_patch(inst: dict, path: str, original: str, new_content: str) -> str:
    if not new_content.endswith("\n"):
        new_content += "\n"
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True), new_content.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))


def build_calls(model: str, n: int | None) -> list[tuple[dict, str, str, RenderedCall]]:
    out = []
    for inst in load_subset()[: n or None]:
        wt = repo_checkout(inst)
        path = oracle_path(inst)
        # Pristine blob at base_commit (worktree HEAD), NOT the working
        # tree: the interactive arm edits these same worktrees, and a
        # working-tree read after it has run computes diffs against the
        # wrong baseline (found via 5 harness apply-errors, 2026-08-21).
        original = subprocess.run(
            ["git", "-C", str(wt), "show", f"HEAD:{path}"],
            capture_output=True, text=True, check=True,
        ).stdout
        prompt = PROMPT_TEMPLATE.format(
            repo=inst["repo"], problem=inst["problem_statement"].strip(),
            path=path, content=original,
        )
        call = RenderedCall(
            custom_id=inst["instance_id"], model=model,
            messages=[Message(role="user", content=prompt)],
            max_tokens=MAX_TOKENS, temperature=0.0,
            memo_key=f"t2-{inst['instance_id']}",
        )
        out.append((inst, path, original, call))
    return out


def _write(rows: list[dict], name: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    p = RESULTS_DIR / f"{name}-{int(time.time())}.json"
    p.write_text(json.dumps(rows, indent=1, default=str))
    print(f"[out] {p}")
    return p


def _write_predictions(rows: list[dict], arm: str, model: str) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    p = RESULTS_DIR / f"preds-{arm}-{model.split('/')[-1]}.jsonl"
    with p.open("w") as f:
        for r in rows:
            if r.get("model_patch"):
                f.write(json.dumps({
                    "instance_id": r["instance_id"],
                    "model_name_or_path": f"{arm}-{model.split('/')[-1]}",
                    "model_patch": r["model_patch"],
                }) + "\n")
    print(f"[preds] {p}")


def run_sync(client: httpx.Client, model: str, n: int | None) -> None:
    rows = []
    for inst, path, original, call in build_calls(model, n):
        body = {"model": model, "max_tokens": call.max_tokens, "temperature": 0.0,
                "messages": [{"role": "user", "content": call.messages[0].content}]}
        t0 = time.time()
        resp = client.post("/v1/chat/completions", json=body)
        resp.raise_for_status()
        rj = resp.json()
        text = rj["choices"][0]["message"]["content"] or ""
        m = _FENCE.search(text)
        patch = file_to_patch(inst, path, original, m.group(1)) if m else ""
        receipt = generation_receipt(client, rj["id"])
        rows.append({"instance_id": inst["instance_id"], "arm": "sync", "model": model,
                     "latency_s": round(time.time() - t0, 1), "receipt": receipt,
                     "model_patch": patch, "extracted": bool(m)})
        print(f"  {inst['instance_id']}: patch={'ok' if patch else 'MISSING'} "
              f"${(receipt.get('total_cost') or 0):.4f}")
    _write(rows, f"sync-{model.split('/')[-1]}")
    _write_predictions(rows, "sync", model)
    print(f"[sync] total ${sum(r['receipt'].get('total_cost') or 0 for r in rows):.4f}")


def run_batch_submit(model: str, n: int | None) -> None:
    adapter = OpenRouterBatchAdapter.from_env()
    calls = [c for _, _, _, c in build_calls(model, n)]
    ref = adapter.submit(calls, idempotency_key=f"t2-{model}-n{len(calls)}")
    _write([{"batch_id": ref.batch_id, "model": model, "n": len(calls),
             "submitted_unix": time.time()}], f"batch-submit-{model.split('/')[-1]}")
    print(f"[batch] submitted {len(calls)} -> {ref.batch_id}")


def run_batch_collect(batch_id: str, model: str, n: int | None, wait: bool) -> None:
    adapter = OpenRouterBatchAdapter.from_env()
    ref = BatchRef(provider="openrouter-batch", batch_id=batch_id)
    while True:
        st = adapter.poll(ref)
        print(f"[batch {batch_id}] {st.batch_status} done={st.completed} inflight={st.processing}")
        if st.is_terminal:
            break
        if not wait:
            return
        time.sleep(60)
    ctx = {i["instance_id"]: (i, p, o) for i, p, o, _ in build_calls(model, n)}
    rows = []
    for item in adapter.fetch(ref):
        inst, path, original = ctx[item.custom_id]
        text = (item.payload or {}).get("choices", [{}])[0].get("message", {}).get("content") or ""
        m = _FENCE.search(text)
        patch = file_to_patch(inst, path, original, m.group(1)) if m else ""
        rows.append({"instance_id": inst["instance_id"], "arm": "batch", "model": model,
                     "status": str(item.status), "model_patch": patch, "extracted": bool(m)})
        print(f"  {inst['instance_id']}: patch={'ok' if patch else 'MISSING'}")
    _write(rows + [{"batch_id": batch_id, "usage": adapter.last_usage}],
           f"batch-collect-{model.split('/')[-1]}")
    _write_predictions(rows, "batch", model)
    print(f"[batch] receipt: {adapter.last_usage}")


def run_interactive(client: httpx.Client, model: str, n: int | None, skip: int = 0) -> None:
    claude_bin = shutil.which("claude") or sys.exit("claude CLI not on PATH")
    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = "https://openrouter.ai/api"
    env["ANTHROPIC_AUTH_TOKEN"] = os.environ["OPENROUTER_API_KEY"]
    env["ANTHROPIC_API_KEY"] = ""
    for var in ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(var, None)

    def usage() -> float:
        return float(client.get("/v1/key").json()["data"]["usage"] or 0.0)

    rows = []
    before = usage()
    out_stub = f"interactive-{model.split('/')[-1]}-skip{skip}"
    for inst in load_subset()[skip : (skip + n) if n else None]:
        wt = repo_checkout(inst)
        subprocess.run(["git", "-C", str(wt), "checkout", "--", "."], capture_output=True)
        prompt = (
            f"Fix this reported issue in the repository at the current directory.\n\n<issue>\n"
            f"{inst['problem_statement'].strip()}\n</issue>\n\n"
            "Edit the source files to fix it. Do NOT run the test suite (the "
            "environment is not set up for it); rely on reading the code. Do not "
            "create new files unless strictly necessary. When done, summarize the fix."
        )
        t0 = time.time()
        try:
            proc = subprocess.run(
                [claude_bin, "-p", prompt, "--output-format", "json", "--model", model,
                 "--permission-mode", "bypassPermissions"],
                capture_output=True, text=True, timeout=TIMEOUT_INTERACTIVE_S,
                cwd=wt, env=env,
            )
            cli = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
        except subprocess.TimeoutExpired:
            cli = {"timeout": True}
        diff = subprocess.run(["git", "-C", str(wt), "diff"], capture_output=True, text=True).stdout
        rows.append({"instance_id": inst["instance_id"], "arm": "interactive", "model": model,
                     "wall_s": round(time.time() - t0, 1), "model_patch": diff,
                     "num_turns": cli.get("num_turns"), "cli_cost_usd": cli.get("total_cost_usd")})
        _write(rows, out_stub) if False else None
        (RESULTS_DIR / f"{out_stub}-progress.json").write_text(json.dumps(rows, indent=1))
        print(f"  {inst['instance_id']}: patch={'ok' if diff.strip() else 'EMPTY'} "
              f"turns={cli.get('num_turns')} {rows[-1]['wall_s']}s")
    time.sleep(5)
    rows.append({"arm": "interactive", "model": model, "or_receipted_usd": round(usage() - before, 6)})
    _write(rows, out_stub)
    _write_predictions(rows[:-1], "interactive", model)
    print(f"[interactive] OR receipt ${rows[-1]['or_receipted_usd']:.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["sync", "batch", "interactive"])
    ap.add_argument("--collect", metavar="BATCH_ID")
    ap.add_argument("--wait", action="store_true")
    ap.add_argument("--model", required=True)
    ap.add_argument("-n", type=int, default=None)
    ap.add_argument("--skip", type=int, default=0)
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
        run_batch_collect(args.collect, args.model, args.n, args.wait)
    elif args.arm == "sync":
        run_sync(client, args.model, args.n)
    elif args.arm == "batch":
        run_batch_submit(args.model, args.n)
    elif args.arm == "interactive":
        run_interactive(client, args.model, args.n, args.skip)


if __name__ == "__main__":
    main()
