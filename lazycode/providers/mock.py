"""Mock adapters for tests and Phase-5 end-to-end runs (module brief item 4).

:class:`MockBatchAdapter` and :class:`MockRealtimeAdapter` implement the same
protocols as the real Anthropic adapters (:mod:`anthropic_batch`,
:mod:`realtime`) with zero network I/O: deterministic, instant completion,
configurable canned responses, and a record of everything submitted for test
assertions. This is a first-class deliverable -- the scheduler's own tests
(later milestones) depend on these, not just this module's tests.

Canned responses are keyed by ``custom_id`` (a ``dict[str, ItemResult]``) or
computed on the fly (a ``Callable[[RenderedCall], ItemResult]``); either way,
a call/item with no configured response gets a synthesized default
``completed`` result carrying a small Anthropic-Message-shaped payload, so
tests that don't care about content still get something plausible to parse.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, Union

from lazycode.ir import (
    BatchRef,
    BatchStatus,
    Caps,
    ItemResult,
    ItemStatus,
    RenderedCall,
    TokenEstimate,
    canonical_json,
)

from .base import FatalError

ResponseMap = Union[dict[str, ItemResult], Callable[[RenderedCall], ItemResult]]

_DEFAULT_CAPS = Caps(
    max_items=100_000,
    max_bytes=256 * 1024 * 1024,
    result_ttl_days=29,
    supports_cache=True,
    supports_webhooks=False,
)


def _default_result(call: RenderedCall) -> ItemResult:
    """A synthetic, Anthropic-Message-shaped completed result for ``call``."""
    return ItemResult(
        custom_id=call.custom_id,
        status=ItemStatus.COMPLETED,
        payload={
            "id": f"mock_msg_{call.custom_id}",
            "type": "message",
            "role": "assistant",
            "model": call.model,
            "content": [{"type": "text", "text": f"mock response for {call.custom_id}"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": _heuristic_tokens(call), "output_tokens": 8},
        },
    )


def _heuristic_tokens(call: RenderedCall) -> int:
    text = "".join(b.text for b in call.system) + "".join(m.content for m in call.messages)
    return max(1, len(text) // 4)


def _resolve(responses: ResponseMap | None, call: RenderedCall) -> ItemResult:
    if responses is None:
        return _default_result(call)
    if callable(responses):
        return responses(call)
    if call.custom_id in responses:
        return responses[call.custom_id]
    return _default_result(call)


class MockBatchAdapter:
    """In-memory :class:`~lazycode.providers.base.BatchAdapter` for tests.

    ``responses`` — canned results, see module docstring. ``caps`` overrides
    the generous default (useful for testing Caps-rejection paths without
    needing 100k fake items).

    Records:

    * ``self.submitted_items`` -- every :class:`RenderedCall` ever accepted by
      ``submit`` (flat list, across all batches, in submission order).
    * ``self.submitted_batches`` -- ``batch_id -> list[RenderedCall]`` for
      that batch.
    * ``self.cancelled_batch_ids`` -- batch ids passed to ``cancel``.
    """

    def __init__(self, responses: ResponseMap | None = None, *, caps: Caps | None = None) -> None:
        self._responses = responses
        self._caps = caps or _DEFAULT_CAPS
        self.submitted_items: list[RenderedCall] = []
        self.submitted_batches: dict[str, list[RenderedCall]] = {}
        self.cancelled_batch_ids: set[str] = set()
        self._batch_counter = 0
        # idempotency_key -> BatchRef registry, emulating the real adapter's
        # metadata stamping at create time (find_batch's lookup source).
        self._idem_to_ref: dict[str, BatchRef] = {}

    @property
    def caps(self) -> Caps:
        return self._caps

    def count_tokens(self, items: list[RenderedCall]) -> TokenEstimate:
        total = sum(_heuristic_tokens(c) for c in items)
        return TokenEstimate(input_tokens=total, output_tokens=0, item_count=len(items))

    def submit(
        self,
        items: list[RenderedCall],
        idempotency_key: str,
        *,
        known_refs: dict[str, BatchRef] | None = None,
    ) -> BatchRef:
        if known_refs is not None and idempotency_key in known_refs:
            return known_refs[idempotency_key]

        if len(items) > self._caps.max_items:
            raise FatalError(
                f"batch of {len(items)} items exceeds Caps.max_items={self._caps.max_items}"
            )
        size = len(canonical_json(items).encode("utf-8"))
        if size > self._caps.max_bytes:
            raise FatalError(
                f"batch payload of {size} bytes exceeds Caps.max_bytes={self._caps.max_bytes}"
            )

        batch_id = f"mock-batch-{self._batch_counter}"
        self._batch_counter += 1
        self.submitted_batches[batch_id] = list(items)
        self.submitted_items.extend(items)
        ref = BatchRef(provider="mock", batch_id=batch_id, idempotency_key=idempotency_key)
        self._idem_to_ref[idempotency_key] = ref
        return ref

    def find_batch(self, idempotency_key: str) -> BatchRef | None:
        """Registry lookup — emulates matching the idempotency key stamped in
        provider batch metadata (see ``base.BatchAdapter.find_batch``)."""
        return self._idem_to_ref.get(idempotency_key)

    def poll(self, ref: BatchRef) -> BatchStatus:
        items = self.submitted_batches.get(ref.batch_id, [])
        if ref.batch_id in self.cancelled_batch_ids:
            # Whole-batch cancel returns partials (§7.6): everything not yet
            # resolved counts as errored (canceled folds into errored, same
            # as the real adapter -- ItemStatus has no CANCELED).
            return BatchStatus(
                batch_status="ended", completed=0, errored=len(items), expired=0, processing=0
            )

        completed = errored = expired = 0
        for call in items:
            result = _resolve(self._responses, call)
            if result.status == ItemStatus.COMPLETED:
                completed += 1
            elif result.status == ItemStatus.EXPIRED:
                expired += 1
            else:
                errored += 1
        # Deterministic instant completion: always terminal.
        return BatchStatus(
            batch_status="ended", completed=completed, errored=errored, expired=expired, processing=0
        )

    def fetch(self, ref: BatchRef) -> Iterator[ItemResult]:
        items = self.submitted_batches.get(ref.batch_id, [])
        if ref.batch_id in self.cancelled_batch_ids:
            for call in items:
                yield ItemResult(
                    custom_id=call.custom_id,
                    status=ItemStatus.ERRORED,
                    error={"type": "canceled", "message": "batch was canceled"},
                )
            return
        for call in items:
            yield _resolve(self._responses, call)

    def cancel(self, ref: BatchRef) -> None:
        self.cancelled_batch_ids.add(ref.batch_id)


class MockRealtimeAdapter:
    """In-memory :class:`~lazycode.providers.base.RealtimeAdapter` for tests.

    ``self.calls`` records every :class:`RenderedCall` passed to ``complete``,
    in order, for test assertions.
    """

    def __init__(self, responses: ResponseMap | None = None) -> None:
        self._responses = responses
        self.calls: list[RenderedCall] = []

    def complete(self, call: RenderedCall, **kwargs: Any) -> ItemResult:
        self.calls.append(call)
        return _resolve(self._responses, call)
