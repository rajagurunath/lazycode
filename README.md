# lazycode

**Your backlog, done by morning.** A coding agent that plans with a realtime model and executes on provider **batch APIs** (50% off, 24h SLA), structured like a database query engine: logical plan → optimizer → physical plan → staged wave execution.

Not a pair programmer — the night shift. Point it at backlog burn-down (test coverage, migrations, lint eradication, mass refactors), close your laptop, review branches in the morning.

Status: pre-alpha, milestone M0 in progress. Full design: [docs/DESIGN.md](docs/DESIGN.md).

## Quickstart

```bash
git clone <this repo> && cd lazycode
uv sync

cd /path/to/your-repo   # any git repo with at least one commit
export ANTHROPIC_API_KEY=sk-ant-...

uv run lazycode run "add type hints to package X" --yes
# ┌─ Plan (logical) ────────────────────────────────────┐
# │ ...                                                   │
# └────────────────────────────────────────────────────┘
# ... waves run, a branch + report.md land in .lazycode/ ...

uv run lazycode status <job-id>     # per-node detail
uv run lazycode explain <job-id>    # logical + physical plan trees
uv run lazycode review <job-id>     # branches, verification, assumption ledger
uv run lazycode resume <job-id>     # after a crash / kill -9 / restart
```

`--verify` overrides the `[verify].command` from `lazycode.toml`; `--model`/`--max-waves` override defaults; no daemon is required (`run`/`resume` host the orchestrator in-process — see `docs/DESIGN.md` §2 for the daemon-mode alternative). Repo-local config lives in `lazycode.toml` (checked in), provider keys live in `~/.config/lazycode/config.toml` (never checked in) — see Appendix B2.

Want to try it without an API key first? Point `lazycode.toml` at the [mock provider seam](lazycode/cli/mock_provider.py) (`[defaults] provider = "mock"`, `[providers.mock] fixture = "..."`) — the same mechanism `tests/e2e/` and `bench/` use for deterministic, zero-network runs.

## M0 status

What works today, end to end, verified by the test suite:

- `lazycode run "<goal>" --yes` — realtime plan → CLI approval → wave loop → git branch + `report.md`/`report.json`, all via the Anthropic batch + realtime adapters (or the mock seam for testing).
- `lazycode status` / `explain` / `review` — read-only, work whether or not a job/daemon is live.
- `lazycode resume <job-id>` — reopens the store and drives an interrupted job to completion; **crash-safe**: `kill -9` mid-wave then `resume` does not double-submit the batch or double-apply the diff (event-sourced replay + the applied-diff ledger, DESIGN.md §7.1/§9). Covered by `tests/e2e/test_crash_resume.py`.
- `lazycode daemon` — foreground-only single-writer daemon; `run` hands jobs to it over HTTP when it's up (`--background` is not implemented in M0 — run it under launchd/systemd/tmux instead).
- Multi-file fan-out — independent `Generate` nodes land in one wave (`tests/e2e/test_happy_path.py`); optimizer is R1/R2 only (local pushdown + context pruning), no model tiering or speculation yet.
- Benchmark harness (`bench/`) — three fixture-repo tasks, lazycode-vs-Claude-Code-CLI token comparison, `<50%` verdict (Appendix B7).

What doesn't work yet (by design — later milestones, Appendix B11):

- No repair loop — a failed contract or verify run goes straight to `NEEDS_HUMAN`, not a retry (M1).
- No cost estimate in the pre-flight prompt (plan tree + y/N only), no `explain analyze`, no cost/slider-driven optimizer beyond R1/R2 (M2).
- No hedging or deadline-aware fallback to realtime (M2), no speculation/vectorization (M4).
- No web UI, no desktop/Slack notifications (log line only), no `watch` TUI (M3).
- Single provider (Anthropic) + realtime planner only — no OpenAI/Gemini/pseudo-batch adapters yet (M4).
- `--background` daemon mode, `merge`/`cancel` commands, and GitHub Actions best-effort runner are all unimplemented (M1/M2).
