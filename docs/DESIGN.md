# lazycode — a batch-API-native coding agent

**Status:** Design spec v2 (post-review), ready for implementation
**Date:** 2026-07-14 (revised same day after 3-lens review: architecture, red-team feasibility, implementability)
**One-liner:** A coding agent that plans with a realtime model and executes with 24h-SLA batch APIs, treating an engineering task the way a database treats a query: parse → logical plan → optimize → physical plan → staged execution. Trade latency for cost, controlled by a single slider.

---

## 0. Verdict first: will this model work?

**Yes — conditionally.** The idea is sound and the niche is empty, but only if you accept what the physics allows and state the economics honestly.

### The physics (latency)

The classic agent loop is `LLM → tool → LLM → tool → …`, 30–200 sequential LLM rounds per task. Naively swapping each realtime call for a batch call makes a 30-round task take 30 batch round-trips — up to 30 days worst case. **Dead on arrival.**

What saves it: batch latency is per-*wave*, not per-*call*. A batch of 10,000 requests costs the same wall-clock as a batch of 1 (up to provider enqueued-token caps — see §10). So the engine's single most important job is to restructure the agent loop from a deep chain into a shallow, wide DAG:

```
wall_clock ≈ DAG_depth × wave_latency        (width is free up to enqueued-token caps)
```

- Provider reality (verified July 2026): OpenAI and Anthropic both offer flat **50% off all models** on batch, 24h processing window, with **most batches completing in under 1–6 hours** (Anthropic: most < 1 hour). Anthropic accepts up to 100k requests / 256 MB per batch; OpenAI 50k requests / 200 MB per file, per-model enqueued-token caps, 2,000 batch creations/hr.
- **The latency tail is heavy and undocumented.** Providers publish only "most < 1h" — no p95/p99 — and the hard ceiling is 24h *expiry* (Anthropic explicitly warns that under load more requests expire at 24h). Overnight success is a *product over the critical path*: P(job done by 9am) = Π P(wave_i fast) across 3–8 sequential waves. One stalled early wave starves everything behind it. The design must therefore treat wave latency as a heavy-tailed random variable, detect stalled waves early, and hedge (§7.6) — and be honest that a badly stalled critical path degrades to realtime pricing for the remainder (§7.6). With that machinery, "submit at 6 pm, review at 9 am" holds *most* nights, with a priced fallback on the bad ones — not a guarantee.
- The product framing that survives this: **the night shift**, not the pair programmer.

### The economics (honest version)

The naive pitch — "batch is 50% off, so the agent costs half" — is wrong, because the real competitor is not an uncached realtime agent. Modern interactive agents (Claude Code, Codex CLI) use prompt caching, and **cache-read input is billed at 0.1× base — 5× cheaper than batch's 0.5×** on repeated context. Any honest accounting must beat *that* baseline:

**Input tokens.** An interactive agent re-sends the shared context (system prompt, repo context, growing transcript) on *every* turn: T turns ≈ `0.1 × C × T` for the cached prefix (plus 1.25–2× cache-write premiums, plus the growing uncached suffix each turn). lazycode sends front-loaded context once per round at batch price: `0.5 × C × R` for R rounds. **Batch wins on input when R < T/5.** A 30-turn interactive task collapsed to 2–3 batch rounds pays ~0.5–0.75 C-equivalents versus ~3 C-equivalents cached-realtime. The depth-collapse is therefore not just the latency mechanism — *it is the input-cost mechanism*. Conversely, if `rounds_per_node` degrades toward interactive-like depth, the input economics invert and batch *loses* to a cached agent. Front-loading is a bet the optimizer must keep winning, and the stats table (§5.1) must watch it.

**Output tokens.** Flat, unconditional ~50% win — output is never cacheable. Coding-agent spend is typically output-heavy, which is why the overall economics still land well.

**Stacking levers and honest overheads:**

| Lever | Effect | Caveat |
|---|---|---|
| Batch pricing on output | −50% on all output | unconditional |
| Depth-collapse on input | pays context R× at 0.5 instead of T× at 0.1 (win iff R < T/5) | collapses if rounds/node degrades |
| Model tiering (§5.2 R5) | −30–70% on mechanically-routed nodes | verify-pass threshold gates it |
| Within-wave prompt caching (Anthropic batch) | best-effort, 30–98% hit, 5-min ephemeral | model probabilistically; never book a flat % — cache *writes* cost 1.25–2× |
| **Overheads (subtract!)** | speculation losers (R7), repair rounds (R9, ≤2), expiry resubmits, hedge double-pays | must appear in `explain analyze` actuals |

**Honest headline: ~50% on output plus a conditional input win, blended 30–70% vs a well-cached realtime agent, task-class dependent, net of overheads.** Against an *uncached* baseline (many API users still are), 60–85% is real. The benchmark suite (§14) measures both baselines and the marketing must quote the cached one.

A second-order effect remains true and valuable: **at 50% off, tokens buy latency.** Speculative execution (§5.2 R7 — submitting multiple enumerable continuations in the same wave and letting a later step pick the survivor) doubles tokens on that node but removes a whole wave, at roughly the price a realtime agent pays for one copy. No realtime agent can afford this habit. (Scope note: speculation only pays when the branch decider is itself a remote step — see R7's restriction.)

### Where it wins / where it loses

**Wins (design center):** backlog burn-down. Test-coverage sweeps, lint/type-error eradication, framework migrations across hundreds of call sites, dependency upgrades, docstring/i18n passes, TODO/FIXME resolution, large-scale refactors, reviewing 40 open PRs, "fix every flaky test." Embarrassingly parallel, verifiable by local tooling, output-heavy, and nobody wants to babysit them.

**Loses (out of scope):** interactive debugging, exploratory "what does this codebase do," design conversations, deeply sequential tasks where each step's content depends on the previous step's *output text* (limits both fan-out and speculation). The slider (§8) lets those nodes go realtime, but the honest answer is: use Claude Code for that.

**Positioning:** not "cheaper Claude Code." A *new execution tier* — the way data teams have both a dashboard database and an overnight warehouse. Tagline candidates: *"Your backlog, done by morning."* / *"CI for intelligence."*

### Why nobody has built this (novelty check, July 2026)

- Realtime CLI agents (Claude Code, Codex CLI, Aider, OpenCode, Goose): all realtime APIs.
- Async cloud agents (Devin, OpenAI Codex cloud tasks, Google Jules): asynchronous *UX*, realtime *pricing* — they hide latency but don't monetize it.
- Batch tooling (Bespoke Curator, DagEngine, provider SDK helpers): data generation pipelines — no tools, no repo, no agency, no planner.
- Durable-execution frameworks (Temporal, DBOS, Restate): substrate, not product.

The gap is precisely the intersection: **agent harness × batch pricing × cost-based DAG optimizer.** Related research ("sleep-time compute") validates the direction but has no OSS product. First-mover window is real but short — providers could bundle batch pricing into their own cloud agents at any time, so the moat is open source + multi-provider + the optimizer.

### Naming

`lazycode` is the right name — the semantics literally are **lazy evaluation**: building the plan is a transformation, nothing executes until an action forces it, exactly Spark's model. Caveat: `lazycodex` (unrelated Codex harness) exists on GitHub; the collision is survivable but check PyPI/npm/crates before locking in. Fallbacks: `nightshift`, `batchwork`. `pandascode` is misleading (pandas is eager). Rest of this doc says **lazycode**.

---

## 1. Product definition

### What the user experiences (end-state UX — M0 subset noted in §14/Appendix B)

```bash
pipx install lazycode          # zero infra — one binary+daemon, SQLite state
cd my-repo

lazycode run "raise test coverage of src/billing to 90%" \
    --mode overnight --budget 10 --deadline 9am

# ┌─ Plan (logical) ────────────────────────────────────────────┐
# │ Explore(coverage gaps) → Decompose → 23× [Generate(test) →  │
# │ Verify(pytest)] → Reduce(dedupe fixtures, summarize)        │
# └─────────────────────────────────────────────────────────────┘
# Estimated: 4 waves · ~2.1M tokens · $3.80 batch  (vs $9.20 realtime)   ← M2+; M0 shows plan + y/N only
# ETA: ~03:40 · Deadline hedge armed at 07:30
# Proceed? [y/N/edit]

lazycode status                # live wave/node states
lazycode explain <job>         # EXPLAIN / EXPLAIN ANALYZE — plan tree with costs
lazycode ui                    # web UI: Spark-style DAG viewer + review queue
lazycode review <job>          # morning: diffs + verification reports per branch
lazycode merge <job>
```

Morning deliverable per job: a **git branch per task group**, each with passing verification (tests/lint/build run locally, free), a machine-written report of what was done, an **assumption ledger** (every judgment call the batch nodes made without being able to ask), and cost/token actuals vs. estimates.

- `--budget N` is a **hard pre-submission gate**: the scheduler refuses to submit any wave whose worst-case estimated cost would exceed the remaining budget; already-submitted batch spend is sunk and counted. It is not an in-flight kill switch.
- `--deadline 9am` is local-timezone, next occurrence (9am tomorrow if it's 6pm today). Stored as an absolute UTC timestamp at job creation.

### The two execution modes the user described are one mechanism

- "Everything via batch" = slider at 100.
- "Realtime planner + batch executors" = slider anywhere below 100 — the planner is *always* realtime (it's a few small calls; planning cost is noise next to execution cost), and the slider governs how much of *execution* may escape to realtime.

---

## 2. Architecture overview

```
┌────────────┐   ┌──────────────────────────── lazycode core ───────────────────────────┐
│  CLI / TUI │──▶│  Planner (realtime LLM, small)                                        │
│  (typer/   │   │     └─▶ Logical Plan (operator DAG, typed IR)                         │
│   textual) │   │  Optimizer (rule + cost based, re-runs after every wave = AQE)        │
└────────────┘   │     └─▶ Physical Plan (waves × batch groups × model assignments)      │
┌────────────┐   │  Scheduler / Orchestrator (event-sourced, crash-safe, resumable,      │
│  Web UI    │◀──│                            single-writer + job lease — §7.1)          │
│  (React,   │   │     ├─▶ Provider adapters: anthropic-batch │ openai-batch │ gemini │  │
│  DAG viewer│   │     │                      realtime │ pseudo-batch (off-peak/self-host)│
│  + review  │   │     ├─▶ Local Tool Executor (rg, tree-sitter repo map, LSP, shell)    │
│  queue)    │   │     ├─▶ Workspace Manager (worktree per task group, serialized applies)│
└────────────┘   │     └─▶ Verifier Runner (pytest/tsc/eslint/build — local, free)       │
      ▲          │  Store: SQLite event log (source of truth) + content-addressed blobs  │
      └──────────│  Notifier: desktop / Slack / webhook                                  │
                 │  Server mode (FastAPI + websockets; also serves the UI)               │
                 └───────────────────────────────────────────────────────────────────────┘
```

**Process model (single-writer rule).** SQLite WAL allows many readers but ONE writer. Therefore: the **orchestrator process is the sole event-log writer**. When the daemon is running, CLI and FastAPI server are *clients* — they talk to the daemon over a local HTTP/unix socket and never write the DB. When no daemon is running, `lazycode run` hosts an in-process orchestrator that first acquires the **job lease** (§7.1); `status`/`explain` are read-only and always safe. No configuration ever has two coequal writers.

**Tech stack (recommended):** Python 3.12, `typer` + `rich`/`textual` (CLI/TUI), `pydantic` (all IR/schemas), `sqlite3` WAL (state), FastAPI + websockets (server/UI backend), React + Vite + a DAG lib (e.g. `@xyflow/react`) for the UI. Rationale: the hot path is I/O-bound orchestration, not compute; Python maximizes contributor surface and ships fastest. (Alternative considered: TypeScript end-to-end — better CLI-ecosystem gravity, worse for the eventual vLLM/serving phase and for this team's strengths. A Rust core was rejected as premature.)

**Key inversion vs. classic agents:** tools run **locally, free, between waves**; only LLM inference is remote and batched. A "turn" of the agent is a *wave*, and the engine's whole optimization problem is minimizing the number of waves.

---

## 3. The IR: logical plan

A logical plan is a typed DAG over a small, closed operator algebra (the "relational algebra" of engineering work). Planner output is constrained to this schema (structured output), which is what makes optimization possible — you can't rewrite free-form prose. **The normative schema is code**: `ir/operators.py` defines one pydantic model per operator in a discriminated union on `op`, and the planner's structured-output target is generated from it. §3.2 is an example, not the schema; Appendix B pins the per-operator field table for M0.

### 3.1 Operators

| Operator | Signature | Executed by | Notes |
|---|---|---|---|
| `Explore(question, scope)` | → KnowledgeArtifact | **local first** (harvester), LLM only if judgment needed | codebase Q&A, coverage analysis |
| `Decompose(goal, context)` | → SubPlan | realtime LLM (planner recursion) | dynamic — DAG grows at runtime |
| `Generate(spec, context)` | → CodeArtifact | batch LLM | new files/tests/docs |
| `Edit(files, instruction, context)` | → Diff | batch LLM | modify existing code; file-scoped for parallelism |
| `Verify(artifact, checks)` | → Report | **local** (pytest/tsc/lint/build); LLM-judge only for un-runnable checks | **first-class node**, not a substate — its failure output feeds Repair nodes |
| `Judge(candidates, rubric)` | → Selection | batch LLM or local heuristic | picks among N speculative candidates |
| `Reduce(artifacts, instruction)` | → MergedArtifact | batch LLM | integrate diffs, resolve conflicts, write summary/PR body |
| `Gate(policy)` | → Approval | human or auto-policy | an executable DAG node (states in §7.4). Distinct from the pre-flight CLI y/N confirm, which is not a DAG node. |

Every operator node carries: `inputs` (dep edges), `context_spec` (what the harvester must gather — §6), `output_contract` (a typed union — Appendix B), `difficulty_hint`, `budget_hint`.

### 3.2 Plan example (illustrative; schema lives in `ir/operators.py`)

```json
{
  "goal": "raise coverage of src/billing to 90%",
  "assumptions": ["tests use pytest", "no network in tests"],
  "nodes": [
    {"id": "n1", "op": "Explore", "question": "which functions in src/billing lack coverage",
     "scope": ["src/billing/**"], "prefer_local": true},
    {"id": "n2", "op": "Decompose", "goal": "one test-writing task per uncovered module",
     "deps": ["n1"], "fanout_hint": "per-file"},
    {"id": "n3.*", "op": "Generate", "template": true, "deps": ["n2"],
     "spec": "write tests for {module} targeting {gaps}",
     "context_spec": {"files": ["{module}", "existing tests for {module}"], "repo_map": true},
     "output_contract": {"type": "diff", "must_pass": ["pytest {testfile}"]}},
    {"id": "n4", "op": "Reduce", "deps": ["n3.*"], "instruction": "dedupe fixtures, write summary"},
    {"id": "n5", "op": "Gate", "deps": ["n4"], "policy": "human-review"}
  ]
}
```

`"template": true` nodes are *unresolved fan-outs* — cardinality unknown until an upstream node completes. Resolved children are minted deterministically as `{parent_id}.{index}` in the order they appear in the resolving node's output, recorded in a `FANOUT_RESOLVED` event (replay-deterministic). Children carry `template_parent_id` + a `bindings` JSON (e.g. `{module: "src/billing/tax.py"}`). The DAG is dynamic: `Decompose`, tool-continuations, verify-failures (Repair), and speculation all add nodes at runtime — which is why the optimizer re-runs after every wave (§5.4).

### 3.3 Lazy semantics

Exactly Spark: `lazycode plan`/`add` build transformations; nothing is submitted until an **action** (`run`, or a scheduled trigger like "every night at 1 am, drain the queue"). Queued micro-tasks accumulate and are compacted together (§7.3, M4) — the LSM analogy is real: a memtable of pending intents, flushed and compacted into batch submissions in urgency tiers (L0 realtime, L1 next-wave, L2 overnight).

---

## 4. The IR: physical plan

**Wave semantics (normative).** A **wave is a hard, per-job barrier**: the scheduler submits the entire ready frontier as one wave (one provider batch per (provider, model) group), waits for all of the wave's batches to complete (or hedge/expire per §7.6), processes results, then forms the next wave. This is the Spark analogy exactly — *job → stages → tasks* becomes *job → waves → nodes*, with wave boundaries at LLM materialization points. It makes `wall_clock ≈ depth × wave_latency` literal, makes "wave count" well-defined for acceptance tests (count of `waves` rows submitted for the job), and keeps M0 simple. **Rolling / pipelined flush** (a node whose deps are met not waiting for unrelated stragglers) and **multi-job compaction** are throughput optimizations deliberately deferred to M4, where "wave" becomes a display grouping and the cost model switches to critical-path depth; until then, barriers are the semantics, not just the UI.

The physical plan assigns every logical node to:

- a **wave** (topological layer, minimum-depth),
- an **execution class**: `batch` | `realtime` | `local` | `speculative` (with `spec_group_id` + branch label linking sibling speculations),
- a **provider + model**: e.g. `anthropic/claude-haiku-4-5` for mechanical edits, `anthropic/claude-opus-4-*` for design-heavy nodes — multi-provider per wave is fine (one batch per (provider, model) group per wave),
- a **prompt-assembly recipe**: shared prefix block id (§5.2 R4), packed-group id if vectorized (§5.2 R6),
- **deadline + hedge policy** (§7.6).

`lazycode explain` renders both plans Postgres-style:

```
Wave 2  (batch · anthropic · est 41m · $1.92)
├─ 23× Generate(tests)   haiku-4.5   shared-prefix P1 (repo map, 18k tok, best-effort cached)
│    est: 23 × (9k in / 3k out)      contract: diff + pytest-green
└─ 1× Edit(conftest)     sonnet      hedge: stall-check at T+3h, realtime at deadline−margin
Wave 3  (local · free)
└─ 24× Verify(pytest)  → failures spawn Repair nodes into Wave 4
```

---

## 5. The optimizer

Structured as Catalyst: a sequence of **rewrite rules** over the logical plan, then **cost-based physical planning**, then **adaptive re-optimization** at every wave boundary. Every rule below maps to a database optimization on purpose — the mapping is the design, not decoration.

### 5.1 Cost model

Per node: `est_tokens_in` (from context_spec size + operator prior), `est_tokens_out` (operator prior, updated online), `p_extra_round` (probability the node needs a tool continuation — learned per operator type from history), `difficulty`. Per plan:

```
cost($)   = Σ node_tokens × price(model, class)          class ∈ {batch: 0.5×, realtime: 1×, realtime-cached: modeled}
makespan  = Σ over critical path of wave_latency(class)  (batch ~ heavy-tailed, E[0.5–6h], ceiling 24h; realtime ~ seconds)
objective = cost + λ · makespan
```

**λ is the slider** (§8). Slider 100 → λ≈0 (pure cost → everything batch, maximum width); slider 0 → λ→∞ (pure latency → everything realtime, a normal agent). The optimizer is honest about being a Lagrangian relaxation — presets are named λ values.

Physical planning must also respect **provider width constraints**: per-model enqueued-token caps and batch-creation rate limits (§10 Caps). "Width is free" holds only under those ceilings; an over-cap wave is split across flushes or providers.

Statistics (an ANALYZE equivalent): a local `stats` table records actuals per (operator, model, repo) — tokens, rounds, verify-pass-rate. Estimates start from **hardcoded cold-start priors** (Appendix B table) and converge per-repo. Shown in `explain analyze`, including the overhead lines (speculation losers, repair rounds, expiry resubmits, hedge double-pays).

### 5.2 Rewrite rules (rule name → DB analogue)

- **R1 `LocalPushdown`** (predicate pushdown): any `Explore` answerable by deterministic tooling — ripgrep, tree-sitter repo map, LSP refs/defs, coverage reports, `git log` — is rewritten to a local node. Free and instant. This rule alone removes most "exploration" LLM rounds.
- **R2 `ContextPruning`** (projection pruning): each node ships only the columns it reads — the minimal file set, symbol neighborhoods (LSP-sliced), not the whole repo. Directly cuts `est_tokens_in`, which matters doubly given the honest input economics (§0).
- **R3 `FanoutWidening`** (join reordering / false-dependency elimination): split coarse nodes along independence boundaries (per-file, per-module, per-test) to convert depth into width. Conflict-prone splits (two edits touching one file) are re-merged or serialized via `Reduce`.
- **R4 `PrefixFactoring`** (common subexpression elimination): hoist shared context (system prompt, repo map, house style, task-family instructions) into one prefix block per wave, ordered to exploit provider prompt caching. Caveats the estimator must encode: Anthropic batch caching is **best-effort (30–98% hit), 5-minute ephemeral**, and cache writes cost 1.25–2×; across waves hours apart the cache is always cold. Model as a probabilistic bonus, never a booked saving.
- **R5 `ModelTiering`** (access-path selection): choose the cheapest model whose predicted verify-pass-rate for this (operator, difficulty) clears a threshold; verification failure escalates the repair node one tier (index scan → seq scan fallback).
- **R6 `Vectorize`** (batch row processing): pack k tiny homogeneous tasks (rename, one-line fixes, docstrings) into one request with structured multi-item output. Amortizes prefix/overhead. The `call_items` table (§11) maps the one `llm_call` back to its k source nodes; any item failing its contract is re-issued solo.
- **R7 `Speculate`** (branch prediction / hedged reads): **applies only when the branch decider is itself a remote (LLM) step and the continuations are enumerable a priori without the decider's output *content*** — e.g. an `Edit` whose approach depends on an LLM-judged design choice A/B: submit both A- and B-continuations in the same wave as `speculative` siblings sharing a `spec_group_id`; when the Judge resolves, the loser transitions to `SUPERSEDED`. Do **not** speculate on locally-decidable branches (a pytest pass/fail resolves free and instantly between waves — speculating on it removes zero waves and only burns tokens; and a repair branch can't be pre-built anyway since it needs the failure output). Also covers N-best sampling for hard `Generate` nodes + a `Judge` pick (sample index is part of the memo key). Budgeted by the slider's speculation allowance.
- **R8 `HedgeInsertion`**: attach stall-detection + realtime fallbacks to critical-path nodes (§7.6).
- **R9 `RepairBudget`**: bound Verify→Repair loops (default 2 rounds; then mark node `NEEDS_HUMAN`, deliver partial with the failure report). Prevents overnight token runaways.
- **R10 `Memoize`** (materialized views): every LLM call is keyed by `memo_key = hash(model, rendered_prompt, mode, sample_idx)`; identical calls (retries, re-runs, resubmitted expired items, unchanged plan re-executions) hit the local result cache instead of the API. Note the key includes `mode` and `sample_idx` so a realtime hedge of a batch item, and N-best samples of one prompt, are distinct rows. Memoization caches **LLM results**; it is *not* the side-effect idempotency mechanism (that's the applied-diff ledger, §9).

### 5.3 Physical planning

Topological layering of the rewritten DAG into minimum-depth waves; within each wave, group by (provider, model, shared-prefix); split groups that exceed provider Caps (enqueued tokens, item count, bytes); solve model assignment greedily against the §5.1 objective (an ILP is overkill at v1 — nodes are few hundred, greedy + local search is fine).

### 5.4 Adaptive re-optimization (Spark AQE)

At every wave boundary the optimizer re-runs on the *remaining* DAG with actuals: resolved fan-out cardinalities, real token counts, verify outcomes, elapsed budget/deadline. Consequences: re-tiering (cheap model kept failing → escalate family), re-hedging (behind schedule → pull critical path to realtime), pruning (goal already satisfied → cancel *not-yet-submitted* nodes; for in-flight work, cancellation is **whole-batch only** on both providers — worth it only when an entire wave-group is abandoned; individual unwanted results are simply discarded on return, already billed).

Re-planning calls (when `Decompose` runs or the plan needs semantic revision) go to the realtime planner but with a *tiny* context — the plan state and wave summaries, never raw code. Planner spend stays < 5% of job spend.

---

## 6. Context harvesting (the front-loading engine)

The reason classic agents are deep is that they *discover* context incrementally. lazycode moves discovery to a deterministic, local, free **harvester** that runs before submission, so each batched node is **self-sufficient** — it should rarely need to ask for more.

Per node, driven by `context_spec`:

1. Repo map (tree-sitter symbol outline, cached, incrementally updated) — the shared prefix.
2. Target files in full; LSP-sliced neighborhoods (defs/refs/types) of touched symbols (M1+; M0 ships repo map + whole target files only); nearest existing analogue (e.g. "an existing good test file for a sibling module" — few-shot by construction).
3. House rules: lint config, CI config, CONTRIBUTING, style exemplars.
4. Task-specific harvests: coverage XML for test tasks, failing-test output for repair tasks, `git blame`/log for context on why code is the way it is.

If a batched response *does* emit tool calls anyway (the contract allows it as an escape hatch), the orchestrator executes them locally and the continuation rides the next wave — costing exactly +1 depth. **`rounds_per_node` is the engine's north-star metric**, tracked per operator class — and the targets are class-specific, because published agent-trajectory data (SWE-bench-style harnesses) says integration-heavy work does *not* land in one round:

| Node class | single-round target | note |
|---|---|---|
| Vectorized / mechanical Edit (rename, docstring, lint-fix) | ≥ 90% | realistic |
| Generate (tests, new files) | ≥ 70% | verify+repair loop absorbs the rest |
| Repair / integration / cross-file refactor | ≥ 40% | inherently iterative; R9 budget applies |

If a class's rounds trend above target, its harvest recipe needs enriching — or the optimizer should stop batching that class at high slider values (this feedback is exactly what the stats table is for). The whole-job economics survive because the mechanical classes dominate node *count* in the design-center workloads.

Nodes cannot ask the user anything mid-flight. Instead the prompt contract requires an **assumption ledger**: any judgment call made in lieu of asking is recorded in the node output and surfaced in the morning review. This converts "blocked on human" into "decided + flagged," which is what makes overnight autonomy viable.

---

## 7. Scheduler / orchestrator

### 7.1 Durability, single-writer, and the job lease

Three durability options were considered; **embedded event-sourced core on SQLite** wins because `pipx install lazycode` must work with zero infrastructure. The workload is a *specific* state machine (a job DAG), not arbitrary durable code, so the core is small: an append-only `events` table is the source of truth; `jobs/nodes/waves` tables are projections rebuilt from it; every side effect (batch submitted, diff applied) is recorded with idempotency keys. Crash → replay → resume. (Temporal/Restate remain pluggable backends behind the same `Orchestrator` interface for hosted/team deployments (M5); DBOS rejected locally for its Postgres dependency.)

Two rules make this safe in practice:

- **Single writer.** Exactly one process writes the event log (§2 process model). CLI/UI are clients of the daemon, or the CLI hosts the orchestrator itself when no daemon runs.
- **Job lease.** Any orchestrator (daemon, in-process CLI run, GHA runner, hosted relay) must hold an exclusive per-job lease before advancing that job — a `lease(job_id, holder_id, expires_at)` row updated transactionally, with a heartbeat and takeover-on-expiry. This is what prevents a woken laptop daemon and a GHA cron run from **double-submitting the same wave**. For the GHA runner, the lease lives in the same event-log store it resumes from; while GHA mode is enabled for a job, it is the *exclusive* runner for that job.

### 7.2 The wave loop

```python
while job.active:
    frontier = ready_nodes(dag)                        # deps satisfied
    for n in frontier:
        if n.exec_class == LOCAL: run_local(n); continue   # Explore-local, Verify — free, immediate
        harvest(n); render_prompt(n)                   # local, free
    groups = group_by(remote(frontier), provider, model, prefix)  # split per Caps
    for g in groups:
        key = hash(rendered_items(g)) + flush_ordinal  # content-derived idempotency key
        batch_ref = adapter.submit(g.items, idempotency_key=key)   # event: WAVE_SUBMITTED
    await wave_complete(poll | webhook, stall_checks_armed=True)   # §7.6
    for result in results:                             # per-item: completed | errored | expired
        validate_contract(result) or spawn_repair(result)
        apply_artifacts(result, group_worktree)        # serialized per worktree, ledgered — §9
        spawn_followups(result)                        # Verify nodes, tool continuations, resolved fanouts
    optimizer.reoptimize(remaining_dag, actuals)       # §5.4
notify(user)                                           # M0: log line; M3: desktop/Slack
package_review(branches, reports, ledger)
```

Waves are **hard per-job barriers** (§4). M0 note: `spawn_repair` here is the M1 repair loop; in M0 a contract/verify failure transitions the node straight to `NEEDS_HUMAN` (Appendix B).

### 7.3 Compaction (multi-job — M4)

All queued work across jobs shares a flush pipeline: pending nodes accumulate in tiers (L0 realtime-now, L1 flush-soon, L2 overnight window), and the flusher compacts co-tier nodes into shared provider batches — many small jobs ride one batch submission. At M4 this replaces strict per-job barriers with rolling flushes, and the accounting metric becomes critical-path depth. This is where the tool gets *better* the more a team uses it.

### 7.4 Node state machine

```
                    ┌─────────────── local path ────────────────┐
PENDING → READY ────┤ EXECUTING_LOCAL → COMPLETED_LOCAL → DONE  │   (Explore-local, Verify)
                    └───────────────────────────────────────────┘
PENDING → READY → HARVESTED → ENQUEUED → SUBMITTED → RETURNED           (remote path)
   RETURNED(contract ok)   → APPLIED → DONE          (Verify runs as its OWN downstream node)
   RETURNED(contract fail) → REPAIR_SPAWNED          (M1+; M0: → NEEDS_HUMAN)
   Verify-node failure     → spawns Repair node (≤ R9 budget) → … → NEEDS_HUMAN
   NEEDS_HUMAN → READY                                (send-back from review, with human note attached)
   SUBMITTED → EXPIRED → RE_ENQUEUED (memo-checked) | HEDGED (§7.6)
   Gate: READY → WAITING_APPROVAL → APPROVED(=DONE) | REJECTED(→ job pauses / re-plan)
   any → SUPERSEDED (speculation loser, via spec_group resolution) | CANCELLED | ABANDONED
```

Verification is **not** a substate of code nodes — `Verify` is a first-class node (§3.1) whose deps are the artifacts it checks; its failure report is the input context of the Repair node it spawns.

### 7.5 The always-on problem (important, easy to miss)

Batches complete **server-side** regardless of the client; results persist (OpenAI: output files ~30 days; Anthropic: results 29 days). But *advancing to the next wave* requires an awake orchestrator. A lid-closed laptop stalls the pipeline between waves. Mitigations, in order:

1. **M0:** local daemon (`lazycode daemon`, launchd/systemd unit) + **sleep inhibition with consent**: while jobs are in flight, the daemon wraps itself in `caffeinate -i` (macOS) / `systemd-inhibit` (Linux) so the machine doesn't idle-sleep overnight; governed by config `[daemon] keep_awake = "ask" | true | false` (default `ask`: the pre-flight prompt asks once per job, e.g. "Keep this Mac awake until ~9am? [y/N]"). Lid-close on battery still sleeps — docs say so honestly, and `lazycode status` shows daemon health + inhibition state loudly.
2. **M2 (best-effort fallback, not a promise):** `--runner github-actions` — a scheduled workflow resumes the orchestrator from an event log stored in a repo branch/artifact, under the job lease (§7.1). Known caveats, stated in docs: GHA cron is *not* punctual (10–60+ min delays at peak, runs can be dropped), schedules auto-disable after 60 days of repo inactivity, provider keys become repo secrets (audit your collaborator trust), and state-in-branch needs care with concurrent runs (the lease handles correctness; history bloat is cosmetic). Good enough for thrifty weekend jobs; **not** for deadline-bound overnight work.
3. **M5:** hosted relay/server mode (the Temporal/Restate backend slot) — the real answer for teams.

### 7.6 Deadline hedging (rewritten for real provider semantics)

Facts this design must respect: **there is no per-item cancellation** — both providers cancel whole batches only (Anthropic returns partials; OpenAI drains in-flight up to 10 min, completed items in the output file). So a hedge can never "cancel the batch copy"; both copies may complete and be billed. Hedging is therefore **duplicate-and-discard**, and its cost must be budgeted.

Mechanics:

- **Stall detection (early):** each wave arms a stall check at `T_submit + stall_threshold` (default: p50-completion × 4, min 2h). A wave still unstarted/unfinished at the check, with deadline pressure, triggers hedging of its *critical-path* items to realtime.
- **Deadline guard (late):** at `deadline − est_remaining_waves × wave_latency_p50 − margin`, critical-path items of the current wave are hedged unconditionally.
- **Resolution:** first result to arrive per node wins **at the node level** (`NODE_RESULT_CHOSEN` event; the loser's later arrival is discarded — its `llm_calls` row is recorded for cost accounting but never applied; the applied-diff ledger (§9) makes double-apply impossible). Whole-batch cancel is used only if the *entire remaining wave* is being abandoned.
- **Honest failure mode:** if wave 1 of an 8-wave critical path stalls for 20h, hedging rescues the deadline only by pulling the whole remaining critical path to realtime — i.e., that job degrades to realtime pricing (what you'd have paid without lazycode) plus the sunk batch spend. The slider sets how willing the scheduler is to do this vs. blowing the deadline. `explain analyze` reports hedge double-pay explicitly.

### 7.7 Failure taxonomy

| Failure | Handling |
|---|---|
| Batch item errored | memo-checked resubmit next wave; hedge if critical |
| Batch item expired (24h) | same as errored; counted in overhead accounting |
| Malformed/contract-violating output | 1 cheap repair attempt (re-prompt with violation), then escalate tier (M1+; M0 → NEEDS_HUMAN) |
| Verify fail | Repair node ≤ R9 budget, then `NEEDS_HUMAN` with report |
| Provider outage | adapter circuit breaker; reroute *not-yet-submitted* groups to alternate provider **after re-validating against the target's Caps** (size/token ceilings differ) |
| Crash / power loss | event-log replay; submit idempotency keys + applied-diff ledger make effects exactly-once |
| Repo drifted overnight | worktrees pinned to `base_commit`; rebase at merge time, conflicts → `NEEDS_HUMAN` |

---

## 8. The cost slider

One user-facing dial, 0–100 → the λ in §5.1 plus derived sub-policies:

| Slider | Preset | Critical path | Off-path | Speculation | Hedging | Feel |
|---|---|---|---|---|---|---|
| 0–10 | `interactive` | realtime | realtime | none | n/a | a normal agent |
| ~30 | `hybrid` | realtime | batch | low | aggressive | hours, big tasks cheap |
| ~70 | `overnight` | batch | batch | high | stall-check + deadline guard | submit 6pm → review 9am |
| 100 | `thrifty` | batch | batch, cheapest models | max | off (accept 24h tail) | weekend-sized, max savings |

The pre-flight estimate always shows both sides of the trade — `$3.80 & ~7h` vs `$9.20 & ~25min` — so the slider is felt, not abstract (M2+).

## 9. Sandbox & delivery model

- One **git worktree per task group** (independent subtree of the DAG), branched from pinned `base_commit`; recorded in `task_groups` (§11). Never touches the user's checkout.
- **Applies are serialized per worktree**: the scheduler applies returned diffs one at a time per worktree in deterministic (topological, then node-id) order using `git apply --3way`; an apply conflict spawns an integration Repair node rather than corrupting the tree. Parallelism lives in the LLM calls, not in local applies (applies are milliseconds).
- **Applied-diff ledger (side-effect idempotency):** before applying, the scheduler appends an `ARTIFACT_APPLY_INTENT(diff_hash, worktree)` event; after applying, `ARTIFACT_APPLIED`. On crash-replay, a diff whose hash is already in the ledger for that worktree is skipped (`git apply --check` as belt-and-braces). This — not the LLM memo cache — is what makes applies exactly-once (§5.2 R10 note).
- Cross-group `Reduce` runs in a fresh **integration worktree**: branched from `base_commit`, group branches merged into it, the Reduce node's output applied there; it becomes the delivery branch for the merged result.
- Verifiers run in the relevant worktree, optionally inside a container (config: `verify.command`, `verify.container`); network-off by default for generated-code execution.
- Delivery = branch + structured report (`report.md` + machine-readable `report.json` — skeleton in Appendix B): what/why/assumption ledger/verification transcript/cost actuals. `lazycode review` walks them; `lazycode merge` rebases onto current head.
- Full-auto mode (`--yolo`) can push branches / open PRs via `gh`, but merge always stays behind a `Gate` unless explicitly disabled.

## 10. Provider adapters

```python
class BatchAdapter(Protocol):
    def count_tokens(self, items: list[RenderedCall]) -> TokenEstimate   # pre-submit sizing for §5.1/§5.3
    def submit(self, items: list[RenderedCall], idempotency_key: str) -> BatchRef
    def poll(self, ref: BatchRef) -> BatchStatus                         # counts by per-item state
    def fetch(self, ref: BatchRef) -> Iterator[ItemResult]               # ItemResult: custom_id, status ∈ {completed, errored, expired}, payload | error
    def cancel(self, ref: BatchRef) -> None                              # WHOLE batch only — see §7.6
    caps: Caps
# Caps: max_items, max_bytes, enqueued_token_cap, creation_rate_limit,
#       disallowed_params, supports_cache, supports_webhooks, result_ttl_days,
#       typical_latency_dist (p50/p90 priors, updated from observed stats)
```

`RenderedCall` (defined in `ir/`, Appendix B): `custom_id`, `model`, `system` (ordered prefix blocks, cache-control-taggable), `messages`, `tools`, `max_tokens`, `temperature`, `memo_key`, `node_ids` (plural — a vectorized call maps to k nodes via `call_items`).

- **anthropic-batch:** Message Batches API — up to 100k requests / 256 MB, poll `processing_status`, results 29 days, prompt caching best-effort (5-min ephemeral, 30–98% hit), whole-batch cancel returns partials, some params disallowed in batch. Fastest typical completion (< 1h majority) → default provider. Poll-only today.
- **openai-batch:** JSONL file upload → `/v1/batches` → output file; 50k items / 200 MB per file; per-model enqueued-token caps; 2,000 batch creations/hr; expired batches return the completed subset; **supports batch webhooks** (`batch.completed`/`batch.failed`/`batch.expired`, signed, ≤72h retries) — the adapter should prefer webhooks (delivered to the daemon or relay) and fall back to polling.
- **gemini-batch:** Vertex/GenAI batch mode, 50% off — M4.
- **realtime:** same `RenderedCall` shape, for the planner (M0), hedges (M2), and slider-0.
- **pseudo-batch:** rate-limited async realtime against cheap/off-peak providers (DeepSeek off-peak, self-hosted vLLM, io.net endpoints) exposed through the same interface — makes *any* OpenAI-compatible endpoint a "batch" backend; the bridge to Phase 2.

Results, requests, and artifacts are stored content-addressed; `llm_calls` rows carry the memo key (§5.2 R10).

## 11. Storage schema (SQLite, WAL — single writer per §7.1)

```sql
events(seq INTEGER PK, job_id, ts, type, payload JSON)   -- source of truth, append-only; type vocabulary in Appendix B
jobs(id, goal, repo, base_commit, slider, budget_usd, deadline_utc, status, created_at)
leases(job_id PK, holder_id, expires_at)                 -- orchestrator mutual exclusion (§7.1)
task_groups(id, job_id, worktree_path, branch)           -- workspace mapping (§9)
nodes(id, job_id, group_id → task_groups, op, spec JSON, deps JSON, status, attempt,
      wave_id, exec_class, spec_group_id NULL, branch_label NULL,      -- speculation bookkeeping
      template_parent_id NULL, bindings JSON NULL,                      -- fan-out provenance
      provider, model, est_in, est_out, act_in, act_out, cost_usd, rounds)
waves(id, job_id, provider, model, batch_ref, idempotency_key, submitted_at, completed_at, status)
llm_calls(id, node_id NULL, memo_key, mode, sample_idx, provider, request_ref, response_ref,
          tokens_in, tokens_out, cost_usd, cached BOOL,
          UNIQUE(memo_key))                              -- memo_key already hashes (model, prompt, mode, sample_idx)
call_items(call_id → llm_calls, node_id → nodes, custom_id, item_status)   -- vectorized k-to-1 mapping (R6)
artifacts(hash PK, kind, meta JSON, blob_path)           -- content-addressed store
applied_diffs(worktree, diff_hash, node_id, applied_at, PRIMARY KEY(worktree, diff_hash))  -- side-effect ledger (§9)
stats(op, model, repo, n, avg_in, avg_out, avg_rounds, verify_pass_rate)   -- the ANALYZE table
```

`jobs/nodes/waves/...` are projections; `lazycode doctor --rebuild` replays `events` to reconstruct them.

## 12. UI

**CLI/TUI:** `run · status · watch · plan · explain [analyze] · review · merge · cancel · daemon · ui · stats`. Milestone availability table in Appendix B (M0 ships `run/status/explain/review/daemon`). `explain` is deliberately Postgres-flavored (§4). `watch` is a textual TUI of live node states.

**Web UI (`lazycode ui`, served by the daemon's FastAPI — M3):**
1. **DAG view** — the flagship, explicitly Spark-UI-inspired: logical plan graph ↔ physical plan toggle; waves as swimlanes/Gantt; node chips colored by state; click → prompt, context manifest, result, verify transcript, cost.
2. **Cost dashboard** — spend vs. *both* counterfactuals (uncached realtime and cached realtime — §0 honesty), per-model breakdown, overhead lines, estimate-vs-actual calibration.
3. **Review queue** — per-branch diffs, verification reports, assumption ledgers; approve/merge/send-back (send-back = `NEEDS_HUMAN → READY` with the human note as added context).
4. Live via websocket from the event log — the UI is just another event-log projection, no separate state.

Design language for the eventual landing page + UI: lean into the database metaphor (plans, EXPLAIN, waves) and the "night shift" identity — dark, calm, scheduler-like; the anti-"chat with your code" aesthetic. (Detailed landing/UX design is a follow-on spec once the engine exists.)

## 13. Repository layout (create each subpackage when its milestone starts)

```
lazycode/
├── pyproject.toml
├── lazycode/
│   ├── ir/             # M0 — pydantic: operators, plans, RenderedCall, contracts, events
│   ├── store/          # M0 — sqlite event log, projections, CAS blobs, memo cache, leases
│   ├── providers/      # M0: anthropic_batch, realtime · M2: hedging · M4: openai_batch, gemini, pseudo_batch
│   ├── harvest/        # M0: repo map + files · M1: LSP slice, coverage
│   ├── planner/        # M0 — realtime planning calls, schema enforcement; M2: re-planning
│   ├── scheduler/      # M0 — wave loop, state machine, lease, event sourcing · M2: hedging, AQE hooks
│   ├── workspace/      # M0 — worktree manager, serialized applies, ledger
│   ├── verify/         # M0 — verifier runners (pass/fail only) · M1: contracts + repair
│   ├── optimizer/      # M0: R1/R2 only · M2: cost model, R3–R5, R8–R10, AQE · M4: R6/R7
│   ├── cli/            # M0 — typer app · M3: textual watch TUI
│   ├── server/         # M3 — fastapi + websockets, serves ui/dist
│   └── notify/         # M0: stdout stub · M3: desktop/Slack
├── ui/                 # M3 — React + Vite DAG viewer & review queue
├── runners/gha/        # M2 — GitHub Actions best-effort resumer
└── docs/
```

**Build order for M0 (no circularities):** `ir` → `store` → `providers` → `harvest`/`workspace`/`verify` → `planner` → `scheduler` → `cli`.

## 14. Milestones

Each milestone is shippable with a hard acceptance test. M0 is the existence proof; its precise work order (schemas, degenerate paths, baselines) is **Appendix B** — an implementing agent starts there.

- **M0 — the loop (existence proof).** Anthropic batch + minimal realtime adapter (planner only). Optimizer = R1/R2 only. Strict wave barriers. No repair loop (fail → NEEDS_HUMAN), shape-only contract validation, no UI, no speculation, no hedging, notify = log line. `lazycode run` → realtime plan → CLI y/N (plan tree, no cost estimate) → waves → worktree branch + report. **Accept:** (a) a real multi-file task ("add type hints to package X", ≥10 files) completes in ≤ 4 waves — counted as rows in the `waves` table for the job; (b) total token cost < 50% of the pinned baseline: *Claude Code CLI, same model family, same task prompt, single run, tokens × list price* (the benchmark harness that measures both sides is built **before** this test, not alongside); (c) `kill -9` mid-wave, restart, job resumes with no double-submit (verified via provider dashboard) and no double-apply. `rounds_per_node` instrumented per class from day one.
- **M1 — quality loop.** Verifier contracts (typed union), Repair nodes + R9 budget, assumption ledger, review/merge flow, LSP harvesting. **Accept:** test-coverage task where ≥ 70% of generated tests pass locally without human touch-ups (target per §6 class table).
- **M2 — the optimizer + slider.** Cost model + cold-start priors, R3–R5, R8, R10, AQE, `explain [analyze]` with overhead lines, pre-flight dual estimate (both baselines), stall-detection + deadline hedging, GHA best-effort runner. **Accept:** estimates within ±30% of actuals on 10 benchmark tasks; slider presets produce measurably different cost/latency on the same task; a synthetically-stalled wave triggers hedge and the job still meets its deadline.
- **M3 — the face.** Web UI (DAG viewer, cost dashboard, review queue), notifications, TUI watch. **Accept:** a full overnight job is followable and reviewable without touching the CLI.
- **M4 — breadth + cleverness.** OpenAI (webhook-first) + Gemini adapters, provider failover with Caps re-validation, R6 vectorization (+ `call_items`), R7 speculation (remote-decider only), rolling flush + multi-job compaction (§7.3). **Accept:** speculation demonstrably removes ≥ 1 wave on a benchmark task at ≤ 1.2× its token cost; a mixed 3-provider job completes; two queued jobs share one batch submission.
- **M5 — team scale.** Hosted relay/server mode, Temporal-or-Restate backend option, shared queue + compaction across users, org policies (budgets, model allow-lists). **Accept:** two users share a daemon; combined jobs compact into shared batches.

**Benchmark suite (prerequisite for M0's accept, then grows):** ~10 reproducible repo tasks (coverage sweep, migration, lint eradication, docstring pass, dependency bump…), each scored on cost (vs *both* baselines), wall-clock, waves, human-touch-up count. This is both the dev feedback loop and the launch blog post.

## 15. Phase 2 — batch-optimized serving (separate spec later; direction locked now)

Once lazycode generates real batch traffic, the serving side becomes the opportunity — and it's directly relevant to io.net:

1. **vLLM is already throughput-first** (its origin is offline batch); don't rebuild it. The gaps are *scheduling policy above the engine*:
   - **Prefix-aware batch reordering:** lazycode waves have massive shared prefixes by construction (R4). A scheduler that groups/orders items by shared prefix before feeding vLLM turns prompt-cache hits from incidental to structural. MergeTree-flavored: sort the batch by prefix key so adjacent items merge their prefill — the single highest-leverage serving optimization for this workload, and plausibly an upstream vLLM contribution.
   - **SLA-tiered admission:** batch items backfill idle capacity under realtime traffic, preemptible at token granularity — 24h-SLA work is the perfect load-smoothing sponge.
2. **Idle/spot GPU economics:** 24h-SLA, checkpointable, retry-tolerant work is the ideal load for preemptible and decentralized capacity — batch inference on idle io.net GPUs could undercut hyperscaler batch pricing, and lazycode's `pseudo-batch` adapter (§10) is the ready-made client. Engine → adapter → demand → serving product: each phase feeds the next.
3. A transformers-wrapper rebuild is **not** justified; policy layers above vLLM (or an aibrix-style gateway) capture the value at 1% of the effort.

## 16. Risks (honest list)

1. **Depth blowup / rounds degradation:** if rounds/node drifts above the §6 class targets, both the latency story *and the input economics* (§0) collapse. Measure from M0; be willing to declare task classes out of scope.
2. **Cached-realtime economics:** the competitor is a 0.1×-cache-read agent, not list price. The whole input-side win rides on depth-collapse (R < T/5). If that ratio doesn't hold in practice, the product is an *output-token discount + overnight autonomy* product — still viable, but the pitch must change. The benchmark suite decides.
3. **Latency tail:** undocumented p95, 24h expiry ceiling, compounding across waves. Hedging bounds the damage but at realtime prices; a stall-heavy provider period makes lazycode temporarily pointless. Track per-provider latency dists in stats.
4. **Provider bundling:** OpenAI/Anthropic could ship batch-priced cloud agents. Mitigation: speed, open source, multi-provider optimizer, pseudo-batch (can't be bundled away).
5. **Quality without a human in the loop:** bad overnight output erodes trust fast. Mitigation: verification-first, assumption ledger, repair budgets, per-node contracts; M1 before breadth.
6. **The always-on gap** (§7.5): silently stalled overnight jobs are the worst first-run experience. Daemon health surfaced loudly; GHA positioned honestly as best-effort.
7. **Name collision:** `lazycodex` exists; clear `lazycode` on PyPI/npm/crates before announcing.

## 17. Success metrics

- **Engine:** rounds/node per class vs §6 targets; waves/job (≤ 5 typical); estimate calibration (±30%); verify-pass-without-human ≥ 70% (M1) → 80% (M2+); hedge double-pay < 10% of job spend at `overnight` preset.
- **Economics:** measured $ vs *both* baselines on the benchmark suite (target ≥ 30–70% vs cached-realtime, ≥ 60% vs uncached); $ saved surfaced per user.
- **Adoption:** overnight jobs/week per active user; % of jobs merged without send-back.

---

## Appendix A — verified provider facts (July 2026)

| | Anthropic Message Batches | OpenAI Batch |
|---|---|---|
| Discount | 50% all models (incl. Bedrock/Vertex) | 50% all models |
| Window | 24h; **requests may expire at 24h under load** | 24h; expired batches return completed subset |
| Size caps | 100k requests / 256 MB per batch | 50k requests / 200 MB per file; per-model **enqueued-token cap**; 2,000 batch creations/hr |
| Typical latency | most < 1h (no published p95) | most 1–6h (no published p95) |
| Results retention | 29 days | output files ~30 days |
| Cancellation | **whole batch only**; returns partials | **whole batch only**; ≤10-min drain; completed items in output file |
| Prompt caching | supported, **best-effort** (30–98% hit, 5-min ephemeral; writes cost 1.25–2×) | standard automatic caching semantics; no batch-specific guarantee |
| Webhooks | none (poll `processing_status`) — re-verify at implementation | **yes**: `batch.completed/failed/expired`, signed, ≤72h retries — prefer over polling |
| Disallowed in batch | some params (e.g. streaming; verify current list) | streaming; verify current list |
| Cache-read realtime price (for §0 comparison) | 0.1× base input | ~0.1–0.25× per current pricing page |

## Appendix B — M0 work order (pin-downs an implementing agent needs on day one)

**B1. `RenderedCall` (ir/):** `custom_id: str`, `model: str`, `system: list[PrefixBlock]` (each: `text`, `cache_hint: bool`), `messages: list[Message]`, `tools: list[ToolDef] | None`, `max_tokens: int`, `temperature: float`, `memo_key: str`, `node_ids: list[str]`.

**B2. Config.** Repo-local `lazycode.toml` (checked in, no secrets): `[verify] command`, `container`; `[defaults] slider`, `model_map`; `[providers] anthropic.model_default`, … . User-global `~/.config/lazycode/config.toml`: provider keys **by env-var reference** (`api_key_env = "ANTHROPIC_API_KEY"`), notification settings, `[daemon] keep_awake = "ask" | true | false` (§7.5 sleep inhibition). Precedence: CLI flag > repo > global.

**B3. Plan schema (ir/operators.py, discriminated union on `op`).** Required/optional fields per operator:
| op | required | optional |
|---|---|---|
| Explore | id, question, scope | prefer_local (default true) |
| Decompose | id, goal, deps | fanout_hint |
| Generate | id, spec, context_spec, output_contract, deps | difficulty_hint, budget_hint |
| Edit | id, files, instruction, context_spec, output_contract, deps | difficulty_hint |
| Verify | id, checks (list[Contract]), deps | — |
| Judge | id, candidates (node ids), rubric, deps | — |
| Reduce | id, instruction, deps | — |
| Gate | id, policy ∈ {human-review, auto}, deps | — |
Plans carry `schema_version: 1`. Unknown fields = validation error (planner retries on schema failure).

**B4. `output_contract` typed union:** `DiffContract{files_within: list[glob]}` (must parse as unified diff, apply with --3way, touch only allowed paths) · `CommandContract{cmd, timeout_s, expect_exit=0}` · `JsonContract{schema}`. M0 enforces DiffContract shape + apply-check only; CommandContract execution is M1 (verify runners exist in M0 but run job-level `verify.command`, pass/fail).

**B5. Event vocabulary (store/):** `JOB_CREATED, PLAN_PROPOSED, PLAN_APPROVED, NODE_ADDED, FANOUT_RESOLVED, NODE_READY, NODE_HARVESTED, WAVE_FORMED, WAVE_SUBMITTED, WAVE_COMPLETED, ITEM_RETURNED, CONTRACT_RESULT, ARTIFACT_APPLY_INTENT, ARTIFACT_APPLIED, VERIFY_RESULT, NODE_RESULT_CHOSEN, NODE_DONE, NODE_NEEDS_HUMAN, NODE_STATE_CHANGED, LEASE_ACQUIRED, LEASE_RENEWED, JOB_DONE, JOB_CANCELLED`. Submit idempotency key = `sha256(canonical_json(rendered items))[:16] + ":" + flush_ordinal`; apply idempotency = `applied_diffs` ledger (§9, §11).

**B6. "Wave" for the accept test** = rows in `waves` with status ≥ SUBMITTED for the job (barriers make this unambiguous — §4).

**B7. Cost baseline harness** (build first): a script that runs the same task prompt through Claude Code CLI (same model family), captures its token usage from its own telemetry/logs, prices at list, and writes a comparable `report.json`. lazycode's own actuals come from `llm_calls`.

**B8. `report.md` skeleton** (+ `report.json` sibling with the same fields): `# Job <id>: <goal>` · `## What was done` (per task group: files, node count) · `## Assumption ledger` (table: node, assumption, risk) · `## Verification` (per Verify node: command, exit, tail of output) · `## Cost` (est vs actual, per model, vs baselines) · `## Follow-ups / NEEDS_HUMAN`.

**B9. CLI × milestone:** M0: `run, status, explain, review, daemon` · M1: `merge, cancel` · M2: `explain analyze, stats` · M3: `watch, ui`. `plan`/`add` (lazy queueing) land with M4 compaction.

**B10. Cold-start priors (stats fallback until n ≥ 20 per (op, model)):** Explore-LLM 12k/1.5k, Generate 9k/3k, Edit 8k/1.5k, Judge 4k/0.3k, Reduce 15k/2k (in/out tokens); `p_extra_round`: mechanical 0.05, Generate 0.2, Repair 0.5.

**B11. M0 degenerate paths (explicit):** Gate operator not executed as a DAG node (planner told not to emit it; pre-flight CLI y/N covers approval); contract fail / verify fail → `NEEDS_HUMAN` (no Repair nodes); harvester = repo map + whole target files + house rules (no LSP); notify = log line; single provider (Anthropic) + realtime planner adapter sharing the `RenderedCall` shape.

## Appendix C — implementation model assignment (Opus vs Sonnet)

Owner intent: implement with Opus and Sonnet subagents (Fable plans/reviews only). Sequencing rule: **Opus lands `ir/` + the event vocabulary first**; everything Sonnet builds depends on those shapes being frozen.

| Module | Model | Why |
|---|---|---|
| `ir/` (operators, plans, RenderedCall, contracts, events) | **Opus** | every module depends on it; expensive to change later |
| `scheduler/` (wave loop, state machine, lease, event sourcing, replay) | **Opus** | durability/concurrency bugs are silent and costly |
| `planner/` (structured-output prompting against the schema) | **Opus** | subtle prompt/schema-retry engineering |
| `optimizer/` (M2+: cost model, rules, AQE) | **Opus** | genuine algorithmic judgment |
| `providers/` (anthropic_batch, realtime; later openai/gemini) | Sonnet | mechanical HTTP/JSON against documented APIs, once shapes are pinned |
| `store/` (projections, CAS, memo cache) | Sonnet | mechanical once event types are enumerated |
| `harvest/`, `workspace/`, `verify/` | Sonnet | ripgrep/tree-sitter/git/subprocess plumbing |
| `cli/`, `notify/`, later `server/` + `ui/` | Sonnet | boilerplate-heavy, well-specified |
| Benchmark harness (B7) | Sonnet | scripted measurement |
