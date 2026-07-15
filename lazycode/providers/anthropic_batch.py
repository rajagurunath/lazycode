"""Anthropic Message Batches adapter (DESIGN.md §10, Appendix A).

Implements :class:`~lazycode.providers.base.BatchAdapter` against the
``anthropic`` SDK's Batches API (``client.messages.batches.*``). No live API
calls happen in this module's tests — the SDK client is injected (directly, or
via a zero-arg ``client_factory``), and tests pass a mock.

Key mapping decisions (see the class docstring and inline comments for the
full rationale):

* **``custom_id`` / ``params`` request shape** follows the SDK's
  ``batch_create_params.Request`` / ``MessageCreateParamsNonStreaming``
  TypedDicts. These are ``TypedDict``\\ s (not runtime-enforced), so this
  module builds them as plain ``dict``\\ s — no SDK-side construction needed,
  which keeps golden request-mapping tests simple (compare against plain
  dicts).
* **``cache_control`` placement:** a :class:`~lazycode.ir.PrefixBlock` with
  ``cache_hint=True`` becomes a ``system`` text block carrying
  ``cache_control: {"type": "ephemeral"}`` (§5.2 R4 — best-effort, 5-minute
  ephemeral on Anthropic batch). Blocks with ``cache_hint=False`` get no
  ``cache_control`` key at all (omitted, not ``None``), matching the SDK's
  ``total=False`` TypedDict shape.
* **Idempotency:** the Anthropic Batches API's ``create`` call has no
  ``metadata`` parameter (verified against the installed SDK — Batches.create
  only accepts ``requests`` and ``user_profile_id``), so the submit
  idempotency key (§B5) cannot be stored server-side. ``submit`` therefore
  dedupes *locally only*: the caller passes ``known_refs`` (a
  ``idempotency_key -> BatchRef`` map it owns, e.g. rebuilt from the event
  log on crash-replay); if the key is already present, the existing ref is
  returned and no request is made.
* **Status mapping:** ``processing_status`` (``in_progress`` / ``canceling`` /
  ``ended``) passes straight through as ``BatchStatus.batch_status``.
  ``request_counts.canceled`` has no ``ItemStatus`` counterpart (the IR's
  ``ItemStatus`` enum is ``completed | errored | expired`` — no ``canceled``,
  since §7.6 makes whole-batch cancellation return *partials*, not a steady
  per-item state) so canceled requests are folded into ``errored`` at both
  the poll-count level and the per-item ``fetch`` level, per the module
  brief's explicit mapping table.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from lazycode.ir import (
    BatchRef,
    BatchStatus,
    Caps,
    ItemResult,
    ItemStatus,
    RenderedCall,
    TokenEstimate,
)

from .base import AdapterError, FatalError, RateLimited, RetryableError

# Appendix A, verified July 2026.
_ANTHROPIC_CAPS = Caps(
    max_items=100_000,
    max_bytes=256 * 1024 * 1024,
    enqueued_token_cap=None,
    creation_rate_limit=None,
    disallowed_params=["stream"],
    supports_cache=True,
    supports_webhooks=False,
    result_ttl_days=29,
    # Appendix A: "most < 1h (no published p95)" -- only book what's published.
    typical_latency_dist={"p50": 0.5},
)


def _prefix_block_param(block: Any) -> dict[str, Any]:
    param: dict[str, Any] = {"type": "text", "text": block.text}
    if block.cache_hint:
        param["cache_control"] = {"type": "ephemeral"}
    return param


def _message_param(message: Any) -> dict[str, Any]:
    return {"role": message.role, "content": message.content}


def _tool_param(tool: Any) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def build_message_params(call: RenderedCall) -> dict[str, Any]:
    """Map a :class:`RenderedCall` to ``MessageCreateParamsNonStreaming``-shaped dict.

    Shared by the batch adapter (wrapped in a ``{custom_id, params}`` Request)
    and the realtime adapter (passed straight to ``messages.create``).
    """
    params: dict[str, Any] = {
        "model": call.model,
        "max_tokens": call.max_tokens,
        "temperature": call.temperature,
        "messages": [_message_param(m) for m in call.messages],
    }
    if call.system:
        params["system"] = [_prefix_block_param(b) for b in call.system]
    if call.tools:
        params["tools"] = [_tool_param(t) for t in call.tools]
    return params


def build_batch_request(call: RenderedCall) -> dict[str, Any]:
    """Map a :class:`RenderedCall` to a Batches API ``Request``-shaped dict."""
    return {"custom_id": call.custom_id, "params": build_message_params(call)}


def _heuristic_token_count(call: RenderedCall) -> int:
    """chars/4 fallback estimate when the SDK's ``count_tokens`` isn't usable."""
    text = "".join(b.text for b in call.system) + "".join(m.content for m in call.messages)
    if call.tools:
        text += json.dumps([t.model_dump(mode="json") for t in call.tools], sort_keys=True)
    return max(1, len(text) // 4)


class AnthropicBatchAdapter:
    """§10 ``BatchAdapter`` implementation for Anthropic Message Batches.

    ``client`` is an already-constructed SDK client (e.g. a test mock, or a
    real ``anthropic.Anthropic()``). Alternatively pass ``client_factory``, a
    zero-arg callable invoked lazily on first use — this is how :meth:`from_env`
    resolves credentials without instantiating a client at import time. Exactly
    one of ``client`` / ``client_factory`` must be given.

    No API keys are read here except inside :meth:`from_env`, which is a tiny
    convenience — normal construction always takes a caller-supplied client.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if client is None and client_factory is None:
            raise ValueError("AnthropicBatchAdapter requires either client or client_factory")
        if client is not None and client_factory is not None:
            raise ValueError("pass only one of client / client_factory")
        self._client_value = client
        self._client_factory = client_factory
        # Set by count_tokens(); exposed for tests/observability since
        # TokenEstimate (frozen ir schema) has no field to carry it.
        self.last_count_tokens_source: str | None = None

    @classmethod
    def from_env(cls, *, api_key_env: str = "ANTHROPIC_API_KEY") -> AnthropicBatchAdapter:
        """Build an adapter backed by a real client, reading ``api_key_env`` lazily.

        The environment variable is read only when the client is first used
        (inside the ``client_factory``), not at construction time.
        """

        def _make_client() -> Any:
            import os

            import anthropic

            api_key = os.environ.get(api_key_env)
            if not api_key:
                raise FatalError(f"environment variable {api_key_env!r} is not set")
            return anthropic.Anthropic(api_key=api_key)

        return cls(client_factory=_make_client)

    @property
    def _client(self) -> Any:
        if self._client_value is None:
            self._client_value = self._client_factory()  # type: ignore[misc]
        return self._client_value

    @property
    def caps(self) -> Caps:
        return _ANTHROPIC_CAPS

    # --- count_tokens ---------------------------------------------------

    def count_tokens(self, items: list[RenderedCall]) -> TokenEstimate:
        """Sum per-item input-token counts via the SDK's ``count_tokens``
        endpoint; fall back to a chars/4 heuristic for every item if the
        endpoint is unavailable or errors on the first item.

        Which path was used is recorded on ``self.last_count_tokens_source``
        (``"api"`` or ``"heuristic"``) rather than in the returned
        :class:`TokenEstimate` — that type is a frozen ``ir`` schema
        (``extra="forbid"``) this module must not redefine.
        """
        if not items:
            self.last_count_tokens_source = "api"
            return TokenEstimate(input_tokens=0, output_tokens=0, item_count=0)

        try:
            total_in = 0
            for call in items:
                kwargs: dict[str, Any] = {
                    "model": call.model,
                    "messages": [_message_param(m) for m in call.messages],
                }
                if call.system:
                    kwargs["system"] = [_prefix_block_param(b) for b in call.system]
                if call.tools:
                    kwargs["tools"] = [_tool_param(t) for t in call.tools]
                result = self._client.messages.count_tokens(**kwargs)
                total_in += result.input_tokens
            self.last_count_tokens_source = "api"
            return TokenEstimate(input_tokens=total_in, output_tokens=0, item_count=len(items))
        except Exception:
            self.last_count_tokens_source = "heuristic"
            total_in = sum(_heuristic_token_count(c) for c in items)
            return TokenEstimate(input_tokens=total_in, output_tokens=0, item_count=len(items))

    # --- submit -----------------------------------------------------------

    def _validate_caps(self, items: list[RenderedCall], requests: list[dict[str, Any]]) -> None:
        """Validate against Caps *before* submitting (§10: "validate and raise
        before submit").

        Byte size is measured on the mapped ``requests`` payload (what
        actually goes over the wire to the Batches API), not on the raw
        :class:`RenderedCall` objects — those carry ir-only bookkeeping
        fields (``memo_key``, ``node_ids``) that never reach the API and
        would make the size check inaccurate in both directions.
        """
        caps = self.caps
        if len(items) > caps.max_items:
            raise FatalError(
                f"batch of {len(items)} items exceeds Caps.max_items={caps.max_items}"
            )
        size = len(json.dumps(requests, default=str).encode("utf-8"))
        if size > caps.max_bytes:
            raise FatalError(
                f"batch payload of {size} bytes exceeds Caps.max_bytes={caps.max_bytes}"
            )

    def submit(
        self,
        items: list[RenderedCall],
        idempotency_key: str,
        *,
        known_refs: dict[str, BatchRef] | None = None,
    ) -> BatchRef:
        if known_refs is not None and idempotency_key in known_refs:
            return known_refs[idempotency_key]

        requests = [build_batch_request(call) for call in items]
        self._validate_caps(items, requests)

        try:
            # The idempotency key is stamped into the batch's server-side
            # metadata at create time so a crash between create and the
            # WAVE_SUBMITTED event can be reconciled via find_batch() instead
            # of paying for a second batch. The installed SDK's typed create()
            # has no ``metadata`` parameter, so it rides ``extra_body``.
            batch = self._client.messages.batches.create(
                requests=requests,
                extra_body={"metadata": {"idempotency_key": idempotency_key}},
            )
        except Exception as exc:
            raise _map_error(exc) from exc

        return BatchRef(provider="anthropic", batch_id=batch.id, idempotency_key=idempotency_key)

    # --- find_batch -------------------------------------------------------

    _FIND_BATCH_SCAN_LIMIT = 200

    def find_batch(self, idempotency_key: str) -> BatchRef | None:
        """Scan recent batches for one whose metadata carries ``idempotency_key``.

        Crash-recovery lookup (see ``base.BatchAdapter.find_batch``): lists the
        most recent batches (newest first) and matches the ``idempotency_key``
        stamped by :meth:`submit`. Bounded scan — an orphaned batch is at most
        minutes old on resume, so it is always near the head of the list.
        """
        try:
            page = self._client.messages.batches.list(limit=100)
        except Exception as exc:
            raise _map_error(exc) from exc

        scanned = 0
        for batch in page:
            meta = getattr(batch, "metadata", None) or {}
            if isinstance(meta, dict) and meta.get("idempotency_key") == idempotency_key:
                return BatchRef(
                    provider="anthropic", batch_id=batch.id, idempotency_key=idempotency_key
                )
            scanned += 1
            if scanned >= self._FIND_BATCH_SCAN_LIMIT:
                break
        return None

    # --- poll ---------------------------------------------------------------

    def poll(self, ref: BatchRef) -> BatchStatus:
        try:
            batch = self._client.messages.batches.retrieve(ref.batch_id)
        except Exception as exc:
            raise _map_error(exc) from exc

        counts = batch.request_counts
        # canceled has no ItemStatus counterpart -- fold into errored (see
        # module docstring "Status mapping").
        errored = counts.errored + getattr(counts, "canceled", 0)
        return BatchStatus(
            batch_status=batch.processing_status,
            completed=counts.succeeded,
            errored=errored,
            expired=counts.expired,
            processing=counts.processing,
        )

    # --- fetch ----------------------------------------------------------

    def fetch(self, ref: BatchRef) -> Iterator[ItemResult]:
        try:
            results = self._client.messages.batches.results(ref.batch_id)
        except Exception as exc:
            raise _map_error(exc) from exc

        for raw in results:
            yield _map_item_result(raw)

    # --- cancel ---------------------------------------------------------

    def cancel(self, ref: BatchRef) -> None:
        try:
            self._client.messages.batches.cancel(ref.batch_id)
        except Exception as exc:
            raise _map_error(exc) from exc


def _map_item_result(raw: Any) -> ItemResult:
    """Map one ``MessageBatchIndividualResponse``-shaped object to ``ItemResult``.

    ``succeeded -> completed``, ``errored -> errored``, ``expired -> expired``,
    ``canceled -> errored`` (per the module brief's explicit table).
    """
    custom_id = raw.custom_id
    result = raw.result
    result_type = result.type

    if result_type == "succeeded":
        return ItemResult(
            custom_id=custom_id,
            status=ItemStatus.COMPLETED,
            payload=result.message.model_dump(mode="json"),
        )
    if result_type == "errored":
        return ItemResult(
            custom_id=custom_id,
            status=ItemStatus.ERRORED,
            error=result.error.model_dump(mode="json"),
        )
    if result_type == "expired":
        return ItemResult(custom_id=custom_id, status=ItemStatus.EXPIRED)
    if result_type == "canceled":
        return ItemResult(
            custom_id=custom_id,
            status=ItemStatus.ERRORED,
            error={"type": "canceled", "message": "batch was canceled before this item processed"},
        )
    raise FatalError(f"unknown batch result type: {result_type!r}")


def _map_error(exc: Exception) -> AdapterError:
    """Map an SDK/network exception to the :mod:`base` error hierarchy."""
    if isinstance(exc, AdapterError):
        return exc

    try:
        import anthropic
    except ImportError:
        return RetryableError(str(exc))

    if isinstance(exc, anthropic.RateLimitError):
        retry_after: float | None = None
        try:
            header = exc.response.headers.get("retry-after")
            retry_after = float(header) if header is not None else None
        except Exception:
            retry_after = None
        return RateLimited(str(exc), retry_after=retry_after)
    if isinstance(exc, anthropic.APIConnectionError):
        return RetryableError(str(exc))
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return RetryableError(str(exc))
        return FatalError(str(exc))
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return RetryableError(str(exc))
    return FatalError(str(exc))
