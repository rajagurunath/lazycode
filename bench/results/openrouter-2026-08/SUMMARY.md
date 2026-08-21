# OpenRouter cost study — receipted results (2026-08-21, single seed)

All dollars are provider receipts (batch `usage.cost`; per-generation
billing records for sync; account usage-meter deltas for the agent),
never list prices. Verifier identical across arms within a tier.

## T1 · fan-out (HumanEval subset-50, seed-7 problem list)

| model | arm | pass | receipted $ | batch:sync |
|---|---|---|---|---|
| claude-haiku-4.5 | interactive (Claude Code) | 49/50 | 3.3113 | — |
| claude-haiku-4.5 | sync single-shot | 46/50 | 0.0721 | — |
| claude-haiku-4.5 | batch wave | 47/50 | 0.0367 | 0.509 |
| gemini-3.7-flash | sync | 47/50 | 0.0552 | — |
| gemini-3.7-flash | batch | 49/50 | 0.0281 | 0.510 |
| gpt-5.4-mini | sync | 46/50 | 0.0520 | — |
| gpt-5.4-mini | batch | 47/50 | 0.0255 | 0.491 |

- Interactive → sync (compilation effect): ~46× per solved problem.
- Sync → batch (lane discount): ×1.96. Blended: ~90×.
- Agent iteration bought +2–3 problems accuracy — real, and priced.
- Batch turnaround 90 s – 9 min (2–50 items), 24 h window unused.
- Claude Code telemetry $15.84 vs OR receipts $3.31 (4.8× overstatement).

## T3 · real pipeline end-to-end (add-type-hints fixture)

plan → 1 wave (3 nodes) → OpenRouter `/v1/messages` batch →
3/3 contracts passed → 3/3 diffs applied → repo verify green →
JOB_DONE in 193 s. Wave receipt $0.00309.
Evidence bundle: `t3-pipeline/add-type-hints-1787258012/` (store event
log, waves, llm_calls, applied diffs, receipts).

Live findings fed back into the code:
1. Post-create batch 404 visibility lag → bounded retry in adapter poll.
2. Crash after submit → resume adopted the paid batch via idempotency
   key; zero double spend (receipts show one batch).
3. Model diffs with wrong hunk counts ("corrupt patch") → git apply
   --recount fallback; wave went 0/3 → 3/3 applied.

## T2 · deep tier (SWE-bench Verified, 10 × "<15 min fix")

Resolution via the official harness (docker, x86 images under emulation):

| arm | resolved | receipted $ | $/instance | $/resolved |
|---|---|---|---|---|
| interactive (Claude Code in checkout) | 6/10 | 5.6923 | 0.569 | 0.949 |
| sync single-shot (oracle file) | 8/10 | 0.2582 | 0.026 | 0.032 |
| batch wave (oracle file) | 8/10 | 0.1291 | 0.013 | 0.016 |

- batch:sync = 0.500 exactly, receipted; **identical resolved sets**
  (both miss django-10097 and django-10999).
- The single-shot arms BEAT the agent (8 vs 6) on this easiest band —
  with the honest caveat that oracle retrieval hands them the right
  file, which is part of what the agent's 15.8 mean turns pay for.
- interactive $/resolved is 59× batch's.
- Harness-side bug found & fixed during collection: batch diffs were
  first computed against worktrees the interactive arm had edited
  (5 bogus apply-errors); baselines now read the pristine base_commit
  blob via `git show HEAD:path`. Prompts were always pristine (built
  before the interactive arm ran) — no contamination of any arm's
  inputs.

## Turnaround asymmetry (T1, same 50-request shape per family)

anthropic 8 min · google ~7 min · openai 209 min (3.5 h) — a 25×
spread on one workload. The 24 h window is the contract; the realized
tail is provider-specific.

## Head-to-head: online agent vs LazyCode (Haiku 4.5, receipted)

| tier | arm | quality | receipted $ | $/solved | wall |
|---|---|---|---|---|---|
| fan-out (HumanEval-50) | agent (Claude Code) | 49/50 | 3.3113 | 0.0676 | 18 s/item |
| | compiled, sync price (ablation) | 46/50 | 0.0721 | 0.0016 | 2.2 s/item |
| | **LazyCode** (batch lane) | 47/50 | 0.0367 | 0.0008 | 8.1 min/wave |
| deep (SWE-bench ×10) | agent (Claude Code) | 6/10 | 5.6923 | 0.9487 | 91 s/item |
| | compiled, sync price (ablation) | 8/10 | 0.2582 | 0.0323 | 39 s/item |
| | **LazyCode** (batch lane) | 8/10 | 0.1291 | 0.0161 | 9.6 min/wave |
