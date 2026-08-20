"""OpenRouter Batch API adapter (beta wire, verified 2026-08-19/21).

Implements :class:`~lazycode.providers.base.BatchAdapter` against OpenRouter's
``/api/beta/batches`` surface. The wire differs from both SDK-mediated batch
APIs this repo already speaks, which is why this is a third adapter rather
than a parameterization of ``openai_batch.py``:

* **Inline submit, no files.** One ``POST /api/beta/batches`` with a JSON body
  ``{endpoint, model, requests: [{custom_id, body}, ...]}`` — there is no
  file-upload step. ``endpoint`` and ``model`` MUST be serialized *before*
  ``requests`` (the API stream-parses and returns 400 otherwise); Python dict
  insertion order + ``json.dumps`` preserves this.
* **Batch-level model.** One model per batch, applied to every request; the
  per-request ``body`` omits ``model`` (a mismatching per-request model is
  rejected). ``submit`` therefore requires all items to share one model and
  raises :class:`FatalError` otherwise.
* **Inline results, no download endpoint.** ``GET /api/beta/batches/:id``
  returns ``results`` inline once ``status == "completed"``; for in-progress,
  failed, expired, and cancelled batches ``results`` is ``null`` — an expired
  OpenRouter batch yields NO partial results (unlike OpenAI/Anthropic).
  ``fetch`` on a non-completed terminal batch yields nothing; ``poll`` books
  the loss in the counts.
* **Receipts.** A completed batch carries ``usage`` — ``{prompt_tokens,
  completion_tokens, total_tokens, cost, is_byok}`` — where ``cost`` is the
  actual OpenRouter charge. The frozen IR ``BatchStatus`` can't carry it, so
  it rides the adapter as :attr:`last_usage` (same pattern as
  ``last_count_tokens_source`` in the sibling adapters).
* **Idempotency is local-only.** The documented create body has no
  ``metadata`` field and no server-side lookup surface was verified, so
  :meth:`find_batch` tries ``GET /api/beta/batches`` (list) opportunistically
  and returns ``None`` when the surface doesn't exist. ``submit`` still
  short-circuits on ``known_refs`` (the local half of §B5).
* **Text-only.** Validation rejects image/audio/video/file parts; lazycode's
  M0 ``RenderedCall`` is text-only already, so nothing to strip.

HTTP is injected: ``http`` is any object with ``request(method, url, *,
json=None) -> (status_code: int, body: dict | None)``. ``from_env`` builds one
on ``httpx`` with the ``OPENROUTER_API_KEY`` bearer header.
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


class _NotFound(FatalError):
    """A 404 from the wire — fatal except during post-create visibility lag."""

PROVIDER_NAME = "openrouter-batch"
DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"
BASE_URL = "https://openrouter.ai/api"
BATCH_ENDPOINT = "/v1/chat/completions"
#: The Anthropic-skin endpoint. Selecting it makes every result payload an
#: Anthropic message object — which is exactly what the scheduler's payload
#: parsers (`lazycode.scheduler.payloads`) speak, so the full orchestrator
#: pipeline runs over OpenRouter batches with no scheduler changes.
MESSAGES_ENDPOINT = "/v1/messages"
BATCH_COMPLETION_WINDOW = "24h"  # the only supported window

_OPENROUTER_CAPS = Caps(
    max_items=50_000,  # not documented; stream-parsed "very large" arrays — conservative
    max_bytes=200 * 1024 * 1024,
    enqueued_token_cap=None,
    creation_rate_limit=None,
    disallowed_params=["stream"],
    supports_cache=True,  # provider-dependent; see model page
    supports_webhooks=False,
    result_ttl_days=30,  # GCS artifacts deleted 30 days after creation
    typical_latency_dist=None,
)

_IN_PROGRESS_STATUSES = frozenset({"validating", "in_progress", "finalizing", "cancelling"})
_TERMINAL_STATUSES = frozenset({"completed", "expired", "cancelled", "failed"})


def _system_message(call: RenderedCall) -> dict[str, Any] | None:
    if not call.system:
        return None
    return {"role": "system", "content": "\n\n".join(b.text for b in call.system)}


def build_request_body(call: RenderedCall) -> dict[str, Any]:
    """Chat Completions body for one request. ``model`` is deliberately absent
    — it is batch-level on this wire."""
    messages: list[dict[str, Any]] = []
    system = _system_message(call)
    if system is not None:
        messages.append(system)
    messages.extend({"role": m.role, "content": m.content} for m in call.messages)
    body: dict[str, Any] = {
        "max_tokens": call.max_tokens,
        "temperature": call.temperature,
        "messages": messages,
    }
    if call.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in call.tools
        ]
    return body


def build_messages_body(call: RenderedCall) -> dict[str, Any]:
    """Anthropic ``/v1/messages`` body for one request (batch-level model, so
    the per-request ``model`` is stripped). Reuses the Anthropic adapter's
    param builder so the two wires cannot drift."""
    from .anthropic_batch import build_message_params

    body = build_message_params(call)
    body.pop("model", None)
    return body


def build_batch_payload(
    items: list[RenderedCall], endpoint: str = BATCH_ENDPOINT
) -> dict[str, Any]:
    """The full create body. Key order matters: endpoint, model, then requests."""
    if not items:
        raise FatalError("cannot submit an empty batch")
    models = {c.model for c in items}
    if len(models) > 1:
        raise FatalError(
            f"OpenRouter batches are single-model (batch-level model); got {sorted(models)}"
        )
    builder = build_messages_body if endpoint == MESSAGES_ENDPOINT else build_request_body
    return {
        "endpoint": endpoint,
        "model": items[0].model,
        "requests": [
            {"custom_id": c.custom_id, "body": builder(c)} for c in items
        ],
    }


def _heuristic_token_count(call: RenderedCall) -> int:
    text = "".join(b.text for b in call.system) + "".join(m.content for m in call.messages)
    if call.tools:
        text += json.dumps([t.model_dump(mode="json") for t in call.tools], sort_keys=True)
    return max(1, len(text) // 4)


class OpenRouterBatchAdapter:
    """§10 ``BatchAdapter`` for OpenRouter's beta batch wire."""

    def __init__(
        self,
        http: Any | None = None,
        *,
        http_factory: Callable[[], Any] | None = None,
        provider_name: str = PROVIDER_NAME,
        base_url: str = BASE_URL,
        endpoint: str = BATCH_ENDPOINT,
    ) -> None:
        if http is None and http_factory is None:
            raise ValueError("OpenRouterBatchAdapter requires either http or http_factory")
        if http is not None and http_factory is not None:
            raise ValueError("pass only one of http / http_factory")
        self._http_value = http
        self._http_factory = http_factory
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint
        self.last_count_tokens_source: str | None = None
        #: usage dict from the most recent poll/fetch that saw one —
        #: {"prompt_tokens", "completion_tokens", "total_tokens", "cost",
        #: "is_byok"}. The receipt (E2).
        self.last_usage: dict[str, Any] | None = None
        # last GET body per batch id, so a terminal poll's inline results are
        # not re-fetched over the wire by fetch().
        self._last_get: dict[str, dict[str, Any]] = {}
        # consecutive-404 streak per batch id: a just-created batch is not
        # immediately visible to GET (observed live 2026-08-21), so early
        # 404s are retryable; a *persistent* 404 is a real missing batch.
        self._notfound: dict[str, int] = {}

    @classmethod
    def from_env(
        cls,
        *,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        provider_name: str = PROVIDER_NAME,
        base_url: str = BASE_URL,
        endpoint: str = BATCH_ENDPOINT,
    ) -> OpenRouterBatchAdapter:
        def _make_http() -> Any:
            import os

            import httpx

            key = os.environ.get(api_key_env)
            if not key:
                raise FatalError(f"${api_key_env} is not set")
            client = httpx.Client(
                headers={"Authorization": f"Bearer {key}"}, timeout=120.0
            )

            class _HttpxWire:
                def request(
                    self, method: str, url: str, *, json: Any = None
                ) -> tuple[int, dict[str, Any] | None]:
                    resp = client.request(method, url, json=json)
                    try:
                        body = resp.json()
                    except Exception:
                        body = None
                    return resp.status_code, body

            return _HttpxWire()

        return cls(
            http_factory=_make_http,
            provider_name=provider_name,
            base_url=base_url,
            endpoint=endpoint,
        )

    @property
    def _http(self) -> Any:
        if self._http_value is None:
            self._http_value = self._http_factory()  # type: ignore[misc]
        return self._http_value

    @property
    def caps(self) -> Caps:
        return _OPENROUTER_CAPS

    # --- wire helper ------------------------------------------------------

    def _call(
        self, method: str, path: str, *, json_body: Any = None, ok: tuple[int, ...] = (200, 202)
    ) -> dict[str, Any]:
        try:
            status, body = self._http.request(
                method, f"{self.base_url}{path}", json=json_body
            )
        except AdapterError:
            raise
        except Exception as exc:
            raise RetryableError(str(exc)) from exc
        if status in ok:
            if not isinstance(body, dict):
                raise FatalError(f"{method} {path}: non-object response body")
            return body
        detail = ""
        if isinstance(body, dict):
            detail = f": {json.dumps(body)[:500]}"
        if status == 429:
            raise RateLimited(f"{method} {path} -> 429{detail}")
        if status >= 500:
            raise RetryableError(f"{method} {path} -> {status}{detail}")
        raise _NotFound(f"{method} {path} -> 404{detail}") if status == 404 else FatalError(
            f"{method} {path} -> {status}{detail}"
        )

    # --- protocol ---------------------------------------------------------

    def count_tokens(self, items: list[RenderedCall]) -> TokenEstimate:
        self.last_count_tokens_source = "heuristic"
        if not items:
            return TokenEstimate(input_tokens=0, output_tokens=0, item_count=0)
        return TokenEstimate(
            input_tokens=sum(_heuristic_token_count(c) for c in items),
            output_tokens=0,
            item_count=len(items),
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

        payload = build_batch_payload(items, self.endpoint)
        encoded = json.dumps(payload).encode("utf-8")
        if len(items) > self.caps.max_items:
            raise FatalError(f"batch of {len(items)} items exceeds Caps.max_items")
        if len(encoded) > self.caps.max_bytes:
            raise FatalError(f"batch payload of {len(encoded)} bytes exceeds Caps.max_bytes")

        body = self._call("POST", "/beta/batches", json_body=payload)
        batch_id = body.get("id")
        if not batch_id:
            raise FatalError(f"batch create response without id: {json.dumps(body)[:300]}")
        return BatchRef(
            provider=self.provider_name, batch_id=batch_id, idempotency_key=idempotency_key
        )

    def find_batch(self, idempotency_key: str) -> BatchRef | None:
        """Best-effort: the beta wire documents no metadata and no verified
        list surface, so server-side adoption is not possible — always
        ``None`` unless a list endpoint exists and grows metadata later."""
        return None

    _NOTFOUND_TOLERANCE = 10

    def poll(self, ref: BatchRef) -> BatchStatus:
        try:
            body = self._call("GET", f"/beta/batches/{ref.batch_id}")
        except _NotFound as exc:
            streak = self._notfound.get(ref.batch_id, 0) + 1
            self._notfound[ref.batch_id] = streak
            if streak > self._NOTFOUND_TOLERANCE:
                raise FatalError(str(exc)) from exc
            raise RetryableError(f"{exc} (visibility lag, attempt {streak})") from exc
        self._notfound.pop(ref.batch_id, None)
        self._last_get[ref.batch_id] = body
        usage = body.get("usage")
        if isinstance(usage, dict):
            self.last_usage = usage

        status = body.get("status")
        counts = body.get("request_counts") or {}
        total = counts.get("total", 0) or 0
        completed = counts.get("completed", 0) or 0
        failed = counts.get("failed", 0) or 0
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
        expired = remainder if status == "expired" else 0
        errored = failed if status == "expired" else failed + remainder
        return BatchStatus(
            batch_status=status,
            completed=completed,
            errored=errored,
            expired=expired,
            processing=0,
        )

    def fetch(self, ref: BatchRef) -> Iterator[ItemResult]:
        body = self._last_get.get(ref.batch_id)
        if body is None or body.get("status") not in _TERMINAL_STATUSES:
            body = self._call("GET", f"/beta/batches/{ref.batch_id}")
            self._last_get[ref.batch_id] = body
        usage = body.get("usage")
        if isinstance(usage, dict):
            self.last_usage = usage

        results = body.get("results")
        if not results:
            # completed batches always carry results inline; every other
            # terminal status returns null — nothing to yield (module doc).
            return
        for item in results:
            yield _map_result(item)

    def cancel(self, ref: BatchRef) -> None:
        self._call("POST", f"/beta/batches/{ref.batch_id}/cancel")


def _map_result(item: dict[str, Any]) -> ItemResult:
    custom_id = item.get("custom_id")
    if not custom_id:
        raise FatalError(f"batch result without custom_id: {json.dumps(item)[:300]}")
    error = item.get("error")
    if error:
        payload = error if isinstance(error, dict) else {"message": str(error)}
        return ItemResult(custom_id=custom_id, status=ItemStatus.ERRORED, error=payload)
    response = item.get("response") or {}
    status_code = response.get("status_code", 200)
    body = response.get("body")
    if status_code is not None and status_code >= 400:
        return ItemResult(
            custom_id=custom_id,
            status=ItemStatus.ERRORED,
            error={"status_code": status_code, "body": body},
        )
    return ItemResult(custom_id=custom_id, status=ItemStatus.COMPLETED, payload=body)
