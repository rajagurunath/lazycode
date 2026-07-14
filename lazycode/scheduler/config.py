"""Scheduler configuration (DESIGN.md §4, §7, Appendix B2 subset).

A small dataclass the CLI loads from ``lazycode.toml`` later and tests construct
directly. M0 only needs a default (provider, model), the job-level verify
command, wave/poll bounds, and lease TTL — the cost model, slider λ, and hedge
policy are M2.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SchedulerConfig:
    """Everything the M0 wave loop needs to run a job.

    Attributes:
        provider: default batch provider key (M0: ``"anthropic"``). Adapters are
            keyed by this in ``Orchestrator``'s adapter map.
        model: default model for every remote node (M0 has no tiering).
        verify_command: job-level verify command run by local ``Verify`` nodes
            (Appendix B4/B11 — command contracts are M1).
        verify_timeout_s: timeout for one verify run.
        max_waves: hard ceiling on wave-loop iterations (runaway guard).
        poll_base_s / poll_cap_s: exponential-backoff bounds for wave polling.
        lease_ttl_s: job-lease TTL; renewed each wave iteration (§7.1).
        max_tokens / temperature: rendered-call defaults for remote nodes.
    """

    provider: str = "anthropic"
    model: str = "claude-haiku-4-5"
    verify_command: str = "true"
    verify_timeout_s: float = 300.0
    max_waves: int = 8
    poll_base_s: float = 2.0
    poll_cap_s: float = 60.0
    lease_ttl_s: float = 300.0
    max_tokens: int = 4096
    temperature: float = 0.0
