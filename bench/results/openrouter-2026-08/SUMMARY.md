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
| gpt-5.4-mini | batch | (in flight) | | |

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

Receipts (resolution rates pending official docker evaluation):

| arm | receipted $ | $/instance | notes |
|---|---|---|---|
| interactive (Claude Code in checkout) | 5.6923 | 0.569 | 10/10 non-empty patches, mean 15.8 turns, 91 s/instance |
| sync single-shot (oracle file) | 0.2582 | 0.026 | 10/10 patches |
| batch wave (oracle file) | 0.1291 | 0.013 | 8/10 patches; 2 returned the file unchanged |

- batch:sync = 0.500 exactly, receipted — the discount holds at
  5k-token prompts, not just toy ones.
- interactive:batch = 44× per instance BEFORE quality adjustment; the
  deep tier is where agent iteration should buy the most, so judge only
  with resolution rates (pending).
