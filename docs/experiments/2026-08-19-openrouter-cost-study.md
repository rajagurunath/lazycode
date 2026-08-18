# OpenRouter cost study — the receipts the LazyCode paper is missing

*Plan v1, 2026-08-19. Status: designed, not yet run. No money spent.*

## 1. Why this experiment, in one paragraph

The paper's own §Limitations names its biggest hole: the economics are
*derived and instrumented, not measured* — no real-provider invoice, no
realized `rounds_per_node`, no cache-stacking line item, no batch latency
tail. Every one of those is a number OpenRouter can hand us with **one API
key and one billing ledger** across several model families, because
OpenRouter now sells both lanes: the sync marketplace (`/api/v1/chat/
completions`, prompt-cached) and a beta Batch API (`POST /api/beta/batches`,
24 h window, ~50 % off, results inline) with 61 `:batch` model variants
across Anthropic, OpenAI, Google, Z.ai, Moonshot, MiniMax, NVIDIA. Every
completed batch reports `usage.cost` — the amount OpenRouter actually
charged — and every sync generation is priced per request via
`GET /api/v1/generation?id=…` (cost incl. cache discounts). That is a
*receipt*, not list-price arithmetic. The pitch line for the paper section:
**"same model, same verifier, same artifacts — here is the invoice for both
lanes."**

## 2. What OpenRouter actually offers (verified 2026-08-19)

| Fact | Source |
|---|---|
| Batch endpoint `POST https://openrouter.ai/api/beta/batches`, inline `requests[]` (no JSONL upload); `endpoint`+`model` must precede `requests` in the body | docs/batch-quickstart |
| Shapes: `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/embeddings`; **text-only** | same |
| Only completion window: `24h`; statuses validating→in_progress→finalizing→completed (failed/expired/cancelling/cancelled); whole-batch cancel only — **no per-item cancel** (matches the paper's §Provider reality) | same |
| Results returned **inline** in the GET response when completed; artifacts kept 30 d in GCS | same |
| Pricing "typically 50 % of the model's standard per-token pricing"; caching rates *vary by model*; `usage.cost` on completed batch is the charge | same |
| Claude Code runs unmodified against OpenRouter: `ANTHROPIC_BASE_URL=https://openrouter.ai/api`, `ANTHROPIC_AUTH_TOKEN=sk-or-…`, `ANTHROPIC_API_KEY=""` (Anthropic-skin `/v1/messages`) | openrouter.ai/docs/cookbook/coding-agents/claude-code-integration |
| 414 models listed, 61 `:batch` variants (`/api/v1/models`, no auth) | pulled 2026-08-19 |

Selected `:batch` vs sync list prices ($/Mtok in / out), from `/api/v1/models`:

| model | batch | sync | batch discount |
|---|---|---|---|
| anthropic/claude-haiku-4.5 | 0.50 / 2.50 | 1.00 / 5.00 | 50 % |
| anthropic/claude-sonnet-5 | 1.00 / 5.00 | 2.00 / 10.00 | 50 % |
| openai/gpt-5.4-mini | 0.375 / 2.25 | 0.75 / 4.50 | 50 % |
| openai/gpt-5.6-terra | 1.00 / 6.00 | 2.00 / 12.00 | 50 % |
| google/gemini-3.7-flash | 0.188 / 0.938 | 0.375 / 1.875 | 50 % |
| moonshotai/kimi-k2.7-code | 0.475 / 2.00 | 0.71 / 3.50 | **33 % / 43 %** |
| **z-ai/glm-5.2** | **0.70 / 2.20** | **0.476 / 1.496** | **batch is 47 % MORE expensive** |

The last two rows are already a finding: on a router marketplace the batch
lane is discounted against the *first-party* provider's price, while the
sync price is the cheapest *competing* host — so for open-weight models the
"batch is half price" premise of §Economics can be false. That directly
feeds the paper's existing theme that the size of the win is a property of
the baseline, and it is a fresh, checkable observation nobody has written up.

## 3. Questions the run must answer (each maps to a paper gap)

| # | Question | Paper gap it closes | Metric |
|---|---|---|---|
| Q1 | Receipted $ per task: LazyCode-batch vs Claude Code interactive, same model | "no real-provider invoice" | `usage.cost` sum (batch) + per-generation cost (sync, planner) vs Claude Code `total_cost_usd` cross-checked against OR activity |
| Q2 | Does compilation itself save tokens, independent of the tier? | R < T/5 is *derived* | ablation: same LazyCode plan executed on **sync** API — isolates plan-vs-agent token volume from the 50 % lane discount |
| Q3 | Realized R (waves/rounds per node) vs T (interactive turns) | `rounds_per_node` never measured | waves per job, rounds per node (instrument the counter — today it can only say 1); T from Claude Code `num_turns` |
| Q4 | Does quality hold? | "same quality" claim untested | `verify_command` pass rate; unified-diff size; assumption-ledger entries; blind LLM-judge tie-break only where verify passes on both |
| Q5 | Do batch and cache discounts stack, and at what hit rate? | §Width "unverified line item" | `usage.prompt_tokens_details.cached_tokens` per batch item vs cost; per-provider |
| Q6 | Batch turnaround distribution | "latency tails unpublished" | submit→completed per batch, per model, time-of-day tagged; expiry count |
| Q7 | Cross-family generality across three closed 50 %-off providers | economics stated Anthropic/OpenAI-only | repeat Q1–Q6 on 3 families |

## 4. Design

**Arms** (per task × model × seed):

1. `interactive` — Claude Code CLI headless (`bench/run_baseline.py`), routed
   through OpenRouter (env above), same model. The pinned M0 baseline.
   Non-Anthropic models: Claude Code is unreliable off-Anthropic (OpenRouter's
   own caveat), so the cross-family baseline is `aider` or `mini-swe-agent`
   pointed at OpenRouter — recorded as a *different* agent, never averaged
   with Claude Code.
2. `lazycode-sync` — LazyCode plan (planner on OR sync) executed with the
   realtime adapter against OR sync at full price. **The ablation.**
3. `lazycode-batch` — same plan, waves submitted to `/api/beta/batches`.
   **The product.** Planner cost is *included* in this arm's bill.

(1→2) is the compilation effect — it may be *negative* (self-sufficient
prompts are fatter, no conversation cache); (2→3) is the lane discount plus
Q5/Q6. Reporting both keeps the paper honest about which half of the saving
is architecture and which half is a price list.

**Tasks.** Extend `bench/tasks/` from 3 to ~12 fixtures, four shapes × three
instances, all with a mechanical verifier (compileall / pytest / ruff /
mypy): per-file fan-out (type hints, docstrings, logging), single-module
generation (coverage), cross-file refactor (rename + call sites; the shape
that *should* need R>1), and doc/README synthesis. Two of the fixture
generators should be cut from real repos the user owns (tidal, lazycode
itself) so the "particular repo" flavour is genuine, and the task prompt is
identical byte-for-byte across arms.

**Models (3 closed families, each with a clean 50 % batch lane — user
decision 2026-08-19: only 50 %-off models are used to prove the point):**
`anthropic/claude-haiku-4.5` (explicit `cache_control`, 0.1× reads),
`openai/gpt-5.4-mini` (positional cache), `google/gemini-3.7-flash`
(implicit cache). Optionally `anthropic/claude-sonnet-5` on a 3-task subset
for a "frontier" row. The open-weight marketplace inversion (GLM-5.2, Kimi)
is **not** an arm — it is reported as a one-paragraph observation from the
price list, and left out of every saving figure so no reader can say the
result was diluted or cherry-picked.

**Protocol.** 2 seeds per cell (batch and agents are non-deterministic;
report mean ± range, not one run). Fresh fixture repo per run. Batches
submitted at three times of day (morning / evening / overnight IST) to
tag Q6. All raw responses, ledgers, batch ids and generation ids archived
under `bench/results/openrouter-2026-08/` (gitignored raw, committed
summary JSON + CSV).

**Budget.** Cells: 12 tasks × 3 arms × 2 seeds ≈ 72 runs on the Anthropic
family; ~$0.30–1.50 per interactive Haiku run, ≤ $0.20 per lazycode run →
≈ $25 for Haiku, ≈ $15 GPT-5.4-mini, ≈ $8 Gemini 3.7 Flash. **Cap $60; Phase 0
smoke ≤ $2.** Every live invocation stays behind `--live
--yes-spend-real-money` and is never run without an explicit OK (standing
rule).

## 5. Engineering before any money moves

| # | Work | Size |
|---|---|---|
| E1 | `lazycode/providers/openrouter_batch.py` — `BatchAdapter` for the OR wire: inline `requests[]`, `endpoint`/`model` ordering, poll `GET /api/beta/batches/:id`, inline `results[]` → `ItemResult`, `usage.cost` captured on the batch ref, idempotency key in `metadata` (verify round-trip live in Phase 0), whole-batch cancel. Mirrors `openai_batch.py`; ~300 lines + tests with injected HTTP client. | ½ day |
| E2 | Receipt plumbing: sync arm records OR `id` per call and resolves `/api/v1/generation?id=` cost + cached-token counts; batch arm records `usage.cost`; ledger schema gains `receipt_usd`, `cached_tokens`, `provider_reported_model`. | ¼ day |
| E3 | `bench/run_baseline.py`: OpenRouter env passthrough for Claude Code, model flag, `num_turns` retained; second baseline runner for aider (cross-family). | ¼ day |
| E4 | Instrument realized `rounds_per_node` (the counter that "can only report 1"). | ¼ day |
| E5 | 9 new fixture generators + task.yaml + verifiers. | ½ day |
| E6 | `bench/compare.py`: three-arm table, per-model, with Q1–Q7 columns; matplotlib figure for the paper (receipted $ per task, three arms, three families) + batch-latency ECDF. | ¼ day |
| Phase 0 | Smoke on `add-type-hints` × Haiku, all three arms, 1 seed (≤ $2): confirms receipts reconcile with the OR activity page to the cent, batch metadata round-trips, `:batch` results carry `usage`. | 1 h + wait for batch |
| Phase 1 | Full Anthropic-family run (Q1–Q6). | 1 day wall (batches) |
| Phase 2 | GPT-5.4-mini + Gemini 3.7 Flash (Q7). | 1 day wall |
| Phase 3 | Analysis; paper §"Measured economics" replacing the "named next step" language in §Implementation and §Limitations; update site. | ½ day |

Blocked on: an OpenRouter key with ~$60 credit (none in any `.env` today —
the io.net OpenRouter *provider* account is not a consumer key and must not
be used for this).

## 6. What this experiment cannot claim, stated up front

* The 50 % per-token discount is by construction; the experiment does not
  "discover" it. What it measures is whether LazyCode's *compiled* token
  volume and realized rounds keep the R < T/5 side of the ledger, and what
  the blended, receipted saving is per task class.
* Quality: same weights per token, so per-token quality is fixed; the risk is
  task-level (batch cannot ask questions). Q4 is measured, and a lower pass
  rate is reported as-is, not explained away.
* OpenRouter's Batch API is beta; prices and model coverage move. The report
  pins the `/api/v1/models` snapshot date and the exact `:batch` slugs.
* Router-specific: the GLM inversion is a marketplace phenomenon and is
  labelled as such — it says nothing about Z.ai's own batch price.

## 7. Paper placement

New §"Measured economics" between §Provider reality and §Implementation
truth: one table (three arms × three families, receipted $, R vs T, pass
rate), one figure (per-task cost bars + batch-turnaround ECDF), and a short
paragraph on the marketplace inversion feeding back into §Economics ("the
baseline's price list, not batch, sets the size of the win"). §Limitations
loses its first two sentences; §Conclusion's "receipts to show it" gets
its receipts. Companion Tidal paper is unaffected.
