"""Adapter protocols and the error hierarchy every provider adapter uses (DESIGN.md §10).

Two protocols:

* :class:`BatchAdapter` — the §10 ``BatchAdapter`` Protocol: ``count_tokens``,
  ``submit``, ``poll``, ``fetch``, ``cancel``, plus a ``caps`` property. This is
  what batch providers (``anthropic_batch``, later ``openai_batch``/``gemini``)
  implement.
* :class:`RealtimeAdapter` — a single-call ``complete(call) -> ItemResult``
  surface for the realtime planner (M0) and later hedges (M2)/slider-0 (§10:
  "realtime: same ``RenderedCall`` shape, for the planner (M0), hedges (M2), and
  slider-0").

Error hierarchy (:class:`AdapterError` and subclasses) lets callers (the
scheduler, in later milestones) distinguish retryable failures (rate limits,
5xx, transient network errors) from fatal ones (bad request, contract
violations the adapter itself can detect, e.g. Caps overflow) without string-
matching provider exception messages.

Also here: :func:`backoff_delays`, a small stateless exponential-backoff
helper for ``poll`` loops (per the module brief: "no retries beyond a simple
exponential-backoff helper for poll"). It is a pure generator of delays — it
does not sleep or retry itself; callers drive the loop.

Pure interfaces + one pure helper — no I/O, no provider imports. Concrete
adapters (``anthropic_batch.py``, ``realtime.py``, ``mock.py``) import the
value types from :mod:`lazycode.ir` and implement these protocols.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable

from lazycode.ir import BatchRef, BatchStatus, Caps, ItemResult, RenderedCall, TokenEstimate

# --- error hierarchy ----------------------------------------------------------


class AdapterError(Exception):
    """Base class for every error a provider adapter raises.

    Carries the optional original provider exception as ``__cause__`` (raise
    ``... from original``) so callers can inspect it without every adapter
    reinventing the attribute.
    """


class RetryableError(AdapterError):
    """A transient failure — network error, 5xx, timeout. Safe to retry."""


class FatalError(AdapterError):
    """A non-retryable failure — bad request, Caps violation, malformed
    response, 4xx other than rate limiting. Retrying with the same input
    will not help."""


class RateLimited(RetryableError):
    """The provider rejected the request with a rate-limit error (HTTP 429).

    ``retry_after`` is the provider-suggested backoff in seconds, when known
    (e.g. from a ``retry-after`` response header); ``None`` if the provider
    didn't say.
    """

    def __init__(self, message: str = "rate limited", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# --- backoff helper -------------------------------------------------------


def backoff_delays(
    *, base: float = 1.0, cap: float = 60.0, jitter: bool = True
) -> Iterator[float]:
    """Yield an unbounded sequence of exponential-backoff delays (seconds).

    ``base * 2**attempt``, capped at ``cap``, with up to ±25% jitter when
    ``jitter`` is true (avoids thundering-herd re-polls across many jobs).
    Pure generator — does not sleep. Typical use in a poll loop::

        for delay in backoff_delays():
            status = adapter.poll(ref)
            if status.is_terminal:
                break
            time.sleep(delay)
    """
    attempt = 0
    while True:
        delay = min(base * (2**attempt), cap)
        if jitter:
            delay *= random.uniform(0.75, 1.25)
        yield delay
        attempt += 1


# --- protocols -----------------------------------------------------------


@runtime_checkable
class BatchAdapter(Protocol):
    """The §10 batch-provider Protocol.

    ``submit`` additionally accepts ``known_refs`` (not in the §10 pseudocode,
    resolved here per the module brief): a caller-owned map of
    ``idempotency_key -> BatchRef`` for previously-submitted batches. When the
    key is already present, implementations return the existing ref instead of
    resubmitting — the local half of "at minimum, dedupe locally" (idempotency
    keys are not accepted by the Anthropic Batches API itself; see
    ``anthropic_batch.py``). Passing ``None`` (the default) means "no known
    batches" — always submit.
    """

    @property
    def caps(self) -> Caps:
        """Provider width/feature constraints (§5.3, §10)."""
        ...

    def count_tokens(self, items: list[RenderedCall]) -> TokenEstimate:
        """Pre-submit sizing for the whole ``items`` group (§5.1/§5.3)."""
        ...

    def submit(
        self,
        items: list[RenderedCall],
        idempotency_key: str,
        *,
        known_refs: dict[str, BatchRef] | None = None,
    ) -> BatchRef:
        """Submit ``items`` as one provider batch."""
        ...

    def poll(self, ref: BatchRef) -> BatchStatus:
        """Provider-level status + per-item-state counts."""
        ...

    def fetch(self, ref: BatchRef) -> Iterator[ItemResult]:
        """Stream per-item results. Only meaningful once ``poll`` reports terminal."""
        ...

    def cancel(self, ref: BatchRef) -> None:
        """Cancel the WHOLE batch (§7.6 — no per-item cancellation exists)."""
        ...


@runtime_checkable
class RealtimeAdapter(Protocol):
    """Single-call realtime completion (planner, M0; hedges, M2; slider-0)."""

    def complete(self, call: RenderedCall, **kwargs: Any) -> ItemResult:
        """Execute ``call`` synchronously and return its result.

        ``**kwargs`` is an adapter-specific escape hatch — e.g. the Anthropic
        realtime adapter accepts ``tool_choice`` here (see ``realtime.py``)
        since :class:`~lazycode.ir.RenderedCall` has no ``tool_choice`` field.
        Raises an :class:`AdapterError` subclass on failure rather than
        returning an errored :class:`~lazycode.ir.ItemResult` — realtime calls
        are synchronous and single-shot, so the caller is already in the best
        position to decide whether/how to retry.
        """
        ...
