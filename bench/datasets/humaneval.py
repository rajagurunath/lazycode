"""HumanEval(+) as LazyCode bench tier T1 — fan-out, standard, verifiable.

Loads the cached HumanEvalPlus release JSONL (bench/datasets/cache/), selects
the committed 50-problem subset (deterministic sample, seed 7), builds the
byte-identical prompt used by every arm, and verifies completions with the
original assertion test suite (same verifier for all arms — the equal-quality
axis of the study; EvalPlus plus_inputs upgrade is optional later).

Executing model-written code is inherent to this benchmark; verification runs
in a subprocess with a hard timeout, exactly as the upstream harness does.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CACHE = Path(__file__).parent / "cache"
SUBSET_FILE = Path(__file__).parent / "humaneval_subset50.json"
SUBSET_SEED = 7
SUBSET_N = 50

PROMPT_TEMPLATE = """\
Complete the following Python function. Write the complete function \
including the signature exactly as given (keep the docstring). Output ONLY \
a single Python code block, no explanation.

```python
{prompt}
```"""


def load_problems() -> dict[str, dict]:
    path = CACHE / "HumanEvalPlus.jsonl"
    if not path.exists():
        path = CACHE / "HumanEval.jsonl"
    rows = [json.loads(line) for line in path.open()]
    return {r["task_id"]: r for r in rows}


def subset_ids() -> list[str]:
    """The committed 50-problem subset; created once, then read from disk."""
    if SUBSET_FILE.exists():
        return json.loads(SUBSET_FILE.read_text())
    ids = sorted(load_problems().keys(), key=lambda t: int(t.split("/")[1]))
    picked = sorted(
        random.Random(SUBSET_SEED).sample(ids, SUBSET_N),
        key=lambda t: int(t.split("/")[1]),
    )
    SUBSET_FILE.write_text(json.dumps(picked, indent=1))
    return picked


def build_prompt(problem: dict) -> str:
    return PROMPT_TEMPLATE.format(prompt=problem["prompt"].rstrip())


_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def extract_code(response_text: str) -> str:
    m = _FENCE.search(response_text)
    return (m.group(1) if m else response_text).strip()


def verify(problem: dict, code: str, *, timeout: float = 15.0) -> tuple[bool, str]:
    """Run the assertion suite against ``code``. Returns (passed, detail)."""
    if f"def {problem['entry_point']}" not in code:
        # model returned a bare body — graft it onto the given signature
        code = problem["prompt"] + "\n" + code
    program = (
        code
        + "\n\n"
        + problem["test"]
        + f"\n\ncheck({problem['entry_point']})\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(program)
        path = f.name
    try:
        proc = subprocess.run(
            [sys.executable, path], capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode == 0, (proc.stderr or proc.stdout)[-500:]
    except subprocess.TimeoutExpired:
        return False, "timeout"
    finally:
        Path(path).unlink(missing_ok=True)
