"""OpenAI-compatible Batch API adapter (DESIGN.md §10, §15).

Implements :class:`~lazycode.providers.base.BatchAdapter` against the OpenAI
Batch API wire (``files.create(purpose="batch")`` → ``batches.create`` →
``batches.retrieve`` → output/error file download) using the ``openai`` SDK
with a configurable ``base_url``. Because the wire is the *OpenAI-compatible*
one rather than OpenAI-the-vendor, the same adapter drives:

* api.openai.com's real Batch API, and
* a **self-hosted gateway** speaking the same surface — vLLM behind a Tidal
  batch gateway, io.net endpoints, any OpenAI-compatible "pseudo-batch"
  backend (§10's ``pseudo-batch`` bullet, §15's "the ready-made client").

Structure deliberately mirrors ``anthropic_batch.py`` — same client-injection
contract, same local+server idempotency pattern, same error mapping — so the
two adapters stay diffable. No live API calls happen in this module's tests:
the SDK client is injected (directly, or via a zero-arg ``client_factory``).

Key mapping decisions:

* **Request shape:** one JSONL line per :class:`~lazycode.ir.RenderedCall`,
  ``{"custom_id", "method": "POST", "url": "/v1/chat/completions", "body"}``,
  uploaded as an **in-memory** file (``files.create(file=(name, bytes),
  purpose="batch")``) — lazycode never writes the batch payload to disk.
* **``system`` prefix blocks** are joined, in order, into a single ``system``
  chat message. The OpenAI chat wire has no per-block ``cache_control``
  (Anthropic's R4 hint has no counterpart), and prefix caching on both
  api.openai.com and vLLM is *automatic and position-based* — so preserving
  block order is what actually buys the cache hit. ``cache_hint`` is therefore
  accepted and ignored; ``Caps.supports_cache`` stays ``True`` because the
  caching happens, just not under lazycode's explicit control.
* **Idempotency:** unlike Anthropic Batches, ``batches.create`` *does* accept
  ``metadata``, so the §B5 submit idempotency key is stamped server-side
  (``metadata={"idempotency_key": ...}``) and :meth:`find_batch` can adopt an
  orphaned batch after a crash. ``submit`` still short-circuits on
  ``known_refs`` first (the local half), exactly like the Anthropic adapter.
* **Status mapping:** ``validating``/``in_progress``/``finalizing`` (and
  ``cancelling``) are in-progress; ``completed``/``expired``/``cancelled``/
  ``failed`` are terminal. ``BatchStatus.is_terminal`` is derived from
  ``processing == 0``, so terminal statuses always report ``processing=0``.
  OpenAI's ``request_counts`` is only ``{total, completed, failed}`` — there is
  no ``expired`` counter — so the *remainder* (``total - completed - failed``)
  is attributed by batch status: expired batches book it as ``expired``,
  cancelled/failed batches book it as ``errored`` (mirroring the Anthropic
  adapter's canceled→errored fold, since :class:`~lazycode.ir.ItemStatus` has
  no ``canceled`` member).
* **Result mapping:** output-file lines → ``COMPLETED`` (payload = the
  response ``body``), except a non-2xx ``response.status_code``, which is
  ``ERRORED``. Error-file lines → ``ERRORED``, except error code
  ``batch_expired`` → ``EXPIRED`` (an expired batch returns the completed
  subset plus one ``batch_expired`` error line per unfinished request).
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

#: Provider name this adapter registers under (also the ``BatchRef.provider``
#: default). ``selfhost`` is the documented alias for the same wire pointed at
#: a self-hosted gateway; see ``cli/config.py``.
PROVIDER_NAME = "openai-batch"

DEFAULT_BASE_URL_ENV = "OPENAI_BATCH_BASE_URL"
DEFAULT_API_KEY_ENV = "OPENAI_BATCH_API_KEY"
#: A self-hosted gateway usually does not authenticate at all, but the OpenAI
#: SDK refuses to construct a client without *some* key — so an unset
#: ``OPENAI_BATCH_API_KEY`` is not an error here (unlike Anthropic's
#: ``from_env``), it just falls back to this placeholder.
DEFAULT_API_KEY = "tidal-dev-key"

#: Chat Completions is the only endpoint lazycode renders against (§10:
#: RenderedCall is a messages+tools shape).
BATCH_ENDPOINT = "/v1/chat/completions"
BATCH_COMPLETION_WINDOW = "24h"
_UPLOAD_FILENAME = "lazycode-batch.jsonl"

# DESIGN.md §10 "openai-batch" bullet, verified July 2026.
_OPENAI_CAPS = Caps(
    max_items=50_000,
    max_bytes=200 * 1024 * 1024,
    enqueued_token_cap=None,  # per-model, not knowable without a model name
    creation_rate_limit=2_000,  # batch creations per hour
    disallowed_params=["stream"],
    supports_cache=True,  # automatic prefix caching, not lazycode-controlled
    supports_webhooks=False,  # webhook-first delivery is M4; poll-only here
    result_ttl_days=29,
    typical_latency_dist={"p50": 0.5},
)

# Batch statuses that mean "still moving". Everything else is terminal.
_IN_PROGRESS_STATUSES = frozenset({"validating", "in_progress", "finalizing", "cancelling"})
_TERMINAL_STATUSES = frozenset({"completed", "expired", "cancelled", "failed"})


def _system_message(call: RenderedCall) -> dict[str, Any] | None:
    """Fold ordered :class:`PrefixBlock`\\ s into one ``system`` chat message.

    Order is preserved because prefix caching on this wire is positional; the
    per-block ``cache_hint`` has no wire counterpart (see module docstring).
    """
    if not call.system:
        return None
    return {"role": "system", "content": "\n\n".join(b.text for b in call.system)}


def _tool_param(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def build_chat_body(call: RenderedCall) -> dict[str, Any]:
    """Map a :class:`RenderedCall` to a Chat Completions request body.

    Shared by :func:`build_batch_line` and available to callers that want the
    same rendering for a realtime hedge against the same endpoint.
    """
    messages: list[dict[str, Any]] = []
    system = _system_message(call)
    if system is not None:
        messages.append(system)
    messages.extend({"role": m.role, "content": m.content} for m in call.messages)

    body: dict[str, Any] = {
        "model": call.model,
        "max_tokens": call.max_tokens,
        "temperature": call.temperature,
        "messages": messages,
    }
    if call.tools:
        body["tools"] = [_tool_param(t) for t in call.tools]
    return body


def build_batch_line(call: RenderedCall) -> dict[str, Any]:
    """Map a :class:`RenderedCall` to one OpenAI batch-input JSONL record."""
    return {
        "custom_id": call.custom_id,
        "method": "POST",
        "url": BATCH_ENDPOINT,
        "body": build_chat_body(call),
    }


def build_jsonl(items: list[RenderedCall]) -> bytes:
    """Render ``items`` as the batch input file's bytes (newline-delimited JSON)."""
    lines = [json.dumps(build_batch_line(call), default=str) for call in items]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def _heuristic_token_count(call: RenderedCall) -> int:
    """chars/4 estimate — this wire has no ``count_tokens`` endpoint."""
    text = "".join(b.text for b in call.system) + "".join(m.content for m in call.messages)
    if call.tools:
        text += json.dumps([t.model_dump(mode="json") for t in call.tools], sort_keys=True)
    return max(1, len(text) // 4)


class OpenAIBatchAdapter:
    """§10 ``BatchAdapter`` implementation for the OpenAI-compatible Batch API.

    ``client`` is an already-constructed SDK client (a test fake, or a real
    ``openai.OpenAI(base_url=..., api_key=...)``). Alternatively pass
    ``client_factory``, a zero-arg callable invoked lazily on first use — this
    is how :meth:`from_env` resolves ``base_url``/credentials without
    instantiating a client at import time. Exactly one of ``client`` /
    ``client_factory`` must be given.

    ``provider_name`` is what lands in :attr:`BatchRef.provider`; it defaults to
    ``"openai-batch"`` but must be set to the configured provider key (e.g.
    ``"selfhost"``) when the orchestrator's adapter map is keyed by that alias,
    since resume looks adapters up by ``BatchRef.provider``.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        client_factory: Callable[[], Any] | None = None,
        provider_name: str = PROVIDER_NAME,
        completion_window: str = BATCH_COMPLETION_WINDOW,
    ) -> None:
        if client is None and client_factory is None:
            raise ValueError("OpenAIBatchAdapter requires either client or client_factory")
        if client is not None and client_factory is not None:
            raise ValueError("pass only one of client / client_factory")
        self._client_value = client
        self._client_factory = client_factory
        self.provider_name = provider_name
        self.completion_window = completion_window
        # Mirrors AnthropicBatchAdapter: TokenEstimate (frozen ir schema) has
        # no field to carry the provenance, so it rides the adapter.
        self.last_count_tokens_source: str | None = None

    @classmethod
    def from_env(
        cls,
        *,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        base_url_env: str = DEFAULT_BASE_URL_ENV,
        base_url: str | None = None,
        provider_name: str = PROVIDER_NAME,
    ) -> OpenAIBatchAdapter:
        """Build an adapter backed by a real client, reading the env lazily.

        ``base_url`` (explicit, e.g. from ``[providers.<name>].base_url``) wins
        over ``$OPENAI_BATCH_BASE_URL``; when neither is set the SDK's own
        default (api.openai.com) applies. A missing ``$OPENAI_BATCH_API_KEY``
        falls back to :data:`DEFAULT_API_KEY` rather than raising — a
        self-hosted gateway typically ignores the header entirely.
        """

        def _make_client() -> Any:
            import os

            import openai

            resolved_base_url = base_url or os.environ.get(base_url_env)
            api_key = os.environ.get(api_key_env) or DEFAULT_API_KEY
            kwargs: dict[str, Any] = {"api_key": api_key}
            if resolved_base_url:
                kwargs["base_url"] = resolved_base_url
            return openai.OpenAI(**kwargs)

        return cls(client_factory=_make_client, provider_name=provider_name)

    @property
    def _client(self) -> Any:
        if self._client_value is None:
            self._client_value = self._client_factory()  # type: ignore[misc]
        return self._client_value

    @property
    def caps(self) -> Caps:
        return _OPENAI_CAPS

    # --- count_tokens ---------------------------------------------------

    def count_tokens(self, items: list[RenderedCall]) -> TokenEstimate:
        """Estimate input tokens with a chars/4 heuristic.

        The OpenAI-compatible wire exposes no ``count_tokens`` endpoint (and a
        self-hosted gateway's tokenizer is model-specific), so unlike the
        Anthropic adapter there is no API path to try first —
        ``last_count_tokens_source`` is always ``"heuristic"``.
        """
        if not items:
            self.last_count_tokens_source = "heuristic"
            return TokenEstimate(input_tokens=0, output_tokens=0, item_count=0)
        total_in = sum(_heuristic_token_count(c) for c in items)
        self.last_count_tokens_source = "heuristic"
        return TokenEstimate(input_tokens=total_in, output_tokens=0, item_count=len(items))

    # --- submit -----------------------------------------------------------

    def _validate_caps(self, items: list[RenderedCall], payload: bytes) -> None:
        """Validate against Caps *before* submitting (§10: "validate and raise
        before submit").

        Byte size is measured on the rendered JSONL — literally the file that
        goes over the wire, which is what the 200 MB per-file limit applies to.
        """
        caps = self.caps
        if len(items) > caps.max_items:
            raise FatalError(
                f"batch of {len(items)} items exceeds Caps.max_items={caps.max_items}"
            )
        if len(payload) > caps.max_bytes:
            raise FatalError(
                f"batch payload of {len(payload)} bytes exceeds Caps.max_bytes={caps.max_bytes}"
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

        payload = build_jsonl(items)
        self._validate_caps(items, payload)

        try:
            # In-memory upload: the batch input never touches disk.
            uploaded = self._client.files.create(
                file=(_UPLOAD_FILENAME, payload),
                purpose="batch",
            )
            # Unlike Anthropic Batches, create() takes metadata natively, so
            # the §B5 key is stored server-side and find_batch() can adopt an
            # orphaned batch after a crash between create and WAVE_SUBMITTED.
            batch = self._client.batches.create(
                input_file_id=uploaded.id,
                endpoint=BATCH_ENDPOINT,
                completion_window=self.completion_window,
                metadata={"idempotency_key": idempotency_key},
            )
        except Exception as exc:
            raise _map_error(exc) from exc

        return BatchRef(
            provider=self.provider_name, batch_id=batch.id, idempotency_key=idempotency_key
        )

    # --- find_batch -------------------------------------------------------

    _FIND_BATCH_SCAN_LIMIT = 200

    def find_batch(self, idempotency_key: str) -> BatchRef | None:
        """Scan recent batches for one whose metadata carries ``idempotency_key``.

        Crash-recovery lookup (see ``base.BatchAdapter.find_batch``). Bounded
        scan — an orphaned batch is at most minutes old on resume, so it is
        always near the head of the newest-first list.
        """
        try:
            page = self._client.batches.list(limit=100)
        except Exception as exc:
            raise _map_error(exc) from exc

        scanned = 0
        for batch in page:
            meta = getattr(batch, "metadata", None) or {}
            if isinstance(meta, dict) and meta.get("idempotency_key") == idempotency_key:
                return BatchRef(
                    provider=self.provider_name,
                    batch_id=batch.id,
                    idempotency_key=idempotency_key,
                )
            scanned += 1
            if scanned >= self._FIND_BATCH_SCAN_LIMIT:
                break
        return None

    # --- poll ---------------------------------------------------------------

    def poll(self, ref: BatchRef) -> BatchStatus:
        try:
            batch = self._client.batches.retrieve(ref.batch_id)
        except Exception as exc:
            raise _map_error(exc) from exc

        status = batch.status
        counts = getattr(batch, "request_counts", None)
        total = getattr(counts, "total", 0) or 0
        completed = getattr(counts, "completed", 0) or 0
        failed = getattr(counts, "failed", 0) or 0
        remainder = max(0, total - completed - failed)

        if status in _IN_PROGRESS_STATUSES:
            return BatchStatus(
                batch_status=status,
                completed=completed,
                errored=failed,
                expired=0,
                processing=remainder,
            )
        if status not in _TERMINAL_STATUSES:
            raise FatalError(f"unknown batch status: {status!r}")

        # Terminal: nothing is still processing, so the remainder has to land
        # somewhere. Expired batches expired it; cancelled/failed ones lost it
        # (ItemStatus has no CANCELED -- folded into errored, as in
        # anthropic_batch.py).
        expired = remainder if status == "expired" else 0
        errored = failed if status == "expired" else failed + remainder
        return BatchStatus(
            batch_status=status,
            completed=completed,
            errored=errored,
            expired=expired,
            processing=0,
        )

    # --- fetch ----------------------------------------------------------

    def fetch(self, ref: BatchRef) -> Iterator[ItemResult]:
        """Stream the output file's results, then the error file's.

        Both files are keyed by ``custom_id``, so the caller reassembles the
        wave from the union; a partially-expired batch yields its completed
        subset from the output file and one ``batch_expired`` line per
        unfinished request from the error file.
        """
        try:
            batch = self._client.batches.retrieve(ref.batch_id)
        except Exception as exc:
            raise _map_error(exc) from exc

        for line in self._read_jsonl_file(getattr(batch, "output_file_id", None)):
            yield _map_output_line(line)
        for line in self._read_jsonl_file(getattr(batch, "error_file_id", None)):
            yield _map_error_line(line)

    def _read_jsonl_file(self, file_id: str | None) -> Iterator[dict[str, Any]]:
        if not file_id:
            return
        try:
            content = self._client.files.content(file_id)
        except Exception as exc:
            raise _map_error(exc) from exc

        text = _content_to_text(content)
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FatalError(f"malformed JSONL line in batch file {file_id}: {exc}") from exc
            yield parsed

    # --- cancel ---------------------------------------------------------

    def cancel(self, ref: BatchRef) -> None:
        try:
            self._client.batches.cancel(ref.batch_id)
        except Exception as exc:
            raise _map_error(exc) from exc


def _content_to_text(content: Any) -> str:
    """Coerce whatever ``files.content`` returned into JSONL text.

    The SDK returns ``HttpxBinaryResponseContent`` (``.text`` / ``.read()``);
    tests and simpler gateways may hand back a plain ``str``/``bytes``.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, (bytes, bytearray)):
        return content.decode("utf-8")
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    read = getattr(content, "read", None)
    if callable(read):
        data = read()
        return data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    raise FatalError(f"cannot read batch file content of type {type(content).__name__}")


def _map_output_line(line: dict[str, Any]) -> ItemResult:
    """Map one output-file JSONL record to an :class:`ItemResult`."""
    custom_id = line.get("custom_id")
    if not custom_id:
        raise FatalError(f"batch output line without custom_id: {line!r}")

    error = line.get("error")
    if error:
        return _errored(custom_id, error)

    response = line.get("response") or {}
    status_code = response.get("status_code", 200)
    body = response.get("body")
    if status_code is not None and status_code >= 400:
        return ItemResult(
            custom_id=custom_id,
            status=ItemStatus.ERRORED,
            error={"status_code": status_code, "body": body},
        )
    return ItemResult(custom_id=custom_id, status=ItemStatus.COMPLETED, payload=body)


def _map_error_line(line: dict[str, Any]) -> ItemResult:
    """Map one error-file JSONL record to an :class:`ItemResult`.

    ``batch_expired`` is the one code that is not an error in lazycode's model
    — it is the 24h window closing on an unfinished request (§7.6), which the
    IR calls ``EXPIRED``.
    """
    custom_id = line.get("custom_id")
    if not custom_id:
        raise FatalError(f"batch error line without custom_id: {line!r}")
    error = line.get("error") or {}
    if isinstance(error, dict) and error.get("code") == "batch_expired":
        return ItemResult(custom_id=custom_id, status=ItemStatus.EXPIRED)
    return _errored(custom_id, error)


def _errored(custom_id: str, error: Any) -> ItemResult:
    payload = error if isinstance(error, dict) else {"message": str(error)}
    return ItemResult(custom_id=custom_id, status=ItemStatus.ERRORED, error=payload)


def _map_error(exc: Exception) -> AdapterError:
    """Map an SDK/network exception to the :mod:`base` error hierarchy."""
    if isinstance(exc, AdapterError):
        return exc

    try:
        import openai
    except ImportError:  # pragma: no cover - openai is a hard dependency
        return RetryableError(str(exc))

    if isinstance(exc, openai.RateLimitError):
        retry_after: float | None = None
        try:
            header = exc.response.headers.get("retry-after")
            retry_after = float(header) if header is not None else None
        except Exception:
            retry_after = None
        return RateLimited(str(exc), retry_after=retry_after)
    if isinstance(exc, openai.APIConnectionError):
        return RetryableError(str(exc))
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return RetryableError(str(exc))
        return FatalError(str(exc))
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return RetryableError(str(exc))
    return FatalError(str(exc))
