# lazycode benchmark harness

DESIGN.md §14 "Benchmark suite" / Appendix B7: the harness that measures
both sides of the M0 accept criterion (b) -- *"total token cost < 50% of
the pinned baseline: Claude Code CLI, same model family, same task prompt,
single run, tokens × list price"* -- and, longer-term, the dev feedback loop
and launch-blog-post numbers for M2's cost model calibration.

Built **before** `tests/e2e/test_crash_resume.py` / `test_happy_path.py`,
per B7 ("the benchmark harness that measures both sides is built before
this test, not alongside").

## Layout

```
bench/
├── tasks/<name>/
│   ├── task.yaml        # name, goal, generator, verify_command
│   └── generate.py       # committed fixture generator: def build(repo_root: Path) -> None
├── task_spec.py           # loads task.yaml + materializes a fresh committed git repo per run
├── pricing.py             # illustrative list-price table (§0's "prices at list")
├── run_lazycode.py        # runs a task through lazycode itself (mock or real anthropic)
├── run_baseline.py        # runs a task through the Claude Code CLI headless (the pinned baseline)
├── compare.py             # reads both results, renders the comparison table + <50% verdict
└── results/               # *-lazycode.json / *-baseline.json (gitignored except .gitkeep)
```

Three tasks ship today, each a small, self-contained fixture repo generated
fresh per run (nothing checked in but the generator):

| task | shape | verify |
|---|---|---|
| `add-type-hints` | 3 independent files, per-file fan-out (Generate) | `python -m compileall` |
| `docstring-pass` | 2 independent files, per-file fan-out (Generate) | `python -m compileall` |
| `coverage-a-module` | single Generate against one module with an existing sibling test | `pytest` |

`add-type-hints` also ships a committed `mock_fixture.json` (canned planner
response + per-node diffs) so its lazycode side can run deterministically
with zero network I/O -- see `tests/bench/` for how the unit tests use it,
and the "Mock run" section below for the manual equivalent.

## Running a task

**1. lazycode side.** Deterministic / zero network, using the
[config-constructible mock provider](../lazycode/cli/mock_provider.py):

```bash
uv run python bench/run_lazycode.py add-type-hints \
    --provider mock --fixture bench/tasks/add-type-hints/mock_fixture.json
```

Or for real, against Anthropic batch (needs `ANTHROPIC_API_KEY`; this is a
real, billed run and is **not** exercised by any test):

```bash
uv run python bench/run_lazycode.py add-type-hints --provider anthropic
```

Either way this writes `bench/results/add-type-hints-lazycode.json` with
`waves`, `tokens_in`/`tokens_out` (from `llm_calls`, scoped to the job's
nodes), `cost_usd` (at the batch-discounted list price), `wall_clock_s`, and
`status`.

**2. Baseline side.** Needs the `claude` CLI installed and authenticated;
runs a real (billed) headless Claude Code session against a fresh copy of
the same fixture repo, same goal, same model family:

```bash
uv run python bench/run_baseline.py add-type-hints
```

Writes `bench/results/add-type-hints-baseline.json` with token usage parsed
from Claude Code's own `--output-format json` result and cost at
*uncached realtime list price* (the honest baseline -- §0). If `claude`
isn't on `PATH`, this **degrades gracefully**: it writes
`{"status": "unavailable", ...}` instead of raising, so a sweep across
tasks (or step 3) still completes and just reports that task's baseline as
not run.

**3. Compare.**

```bash
uv run python bench/compare.py add-type-hints
# or, across every task with a lazycode result on disk:
uv run python bench/compare.py
```

Renders a table (waves, token counts, ratio, PASS/FAIL against the <50%
threshold) and exits non-zero if any compared task fails it. `--json` emits
the same rows machine-readable for downstream tooling.

## Adding a task

1. `bench/tasks/<name>/generate.py` — a pure-filesystem `build(repo_root:
   Path) -> None` (no git calls; `task_spec.build_repo` handles `git init`
   + commit).
2. `bench/tasks/<name>/task.yaml` — `name`, `goal` (a `>-` folded scalar is
   fine), `generator: generate.py`, `verify_command`.
3. Optionally a `mock_fixture.json` next to it if you want a deterministic,
   zero-network `run_lazycode.py --provider mock` run for that task (see
   `add-type-hints/mock_fixture.json` for the shape, or
   `tests/e2e/_harness.py` for the fixture-authoring helpers).

`bench/task_spec.py` has no hardcoded task list — `list_tasks()` just scans
`bench/tasks/*/task.yaml`, so a new task directory is picked up
automatically by `run_lazycode.py --help`, `run_baseline.py --help`, and
`compare.py`'s no-args mode.

## Tests

`tests/bench/` unit-tests the harness plumbing itself (task loading, repo
materialization, pricing math, result parsing, comparison/verdict logic)
against the mock provider and synthetic result files -- no network calls,
no `claude` CLI required, safe to run in CI. Run with:

```bash
uv run pytest tests/bench
```
