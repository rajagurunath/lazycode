"""Rendered LLM calls, adapter value types, and key derivation (DESIGN.md §10, B1).

A :class:`RenderedCall` is the provider-agnostic, fully-harvested request the
scheduler hands to a :class:`~lazycode.ir` adapter (Appendix B1). One call maps
to *k* nodes (``node_ids``) when the Vectorize rule (R6) packs several tiny
homogeneous tasks into one request.

Also here:

* The adapter value types from the ``BatchAdapter`` Protocol in §10
  (:class:`TokenEstimate`, :class:`BatchRef`, :class:`BatchStatus`,
  :class:`ItemResult`, :class:`Caps`) — the *data* the protocol exchanges. The
  Protocol itself lives in ``providers/`` (which imports these); nothing here
  imports downstream.
* :func:`compute_memo_key` — the R10 memoization key,
  ``sha256(canonical_json(model, rendered_prompt, mode, sample_idx))``. ``mode``
  and ``sample_idx`` are in the key so a realtime hedge of a batch item and the
  N-best samples of one prompt are distinct cache rows (§5.2 R10).
* :func:`submit_idempotency_key` — the B5 submit key,
  ``sha256(canonical_json(rendered items))[:16] + ":" + flush_ordinal``, which
  makes crash-replay wave submission exactly-once.

Pure schemas + pure functions — no I/O.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .events import ItemStatus

# --- canonical JSON (deterministic serialization for hashing) ----------------


def canonical_json(obj: Any) -> str:
    """Serialize ``obj`` to a canonical (sorted-key, tight) JSON string.

    Pydantic models are dumped via ``model_dump(mode="json")`` so nested models,
    enums and datetimes serialize deterministically. Key order is normalized at
    every level, so two structurally-equal payloads always produce identical
    strings — the precondition for stable content-addressed keys.
    """

    def _default(o: Any) -> Any:
        if isinstance(o, BaseModel):
            return o.model_dump(mode="json")
        raise TypeError(f"not JSON-serializable: {type(o).__name__}")

    return json.dumps(
        obj,
        default=_default,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


# --- prompt building blocks --------------------------------------------------


class PrefixBlock(BaseModel):
    """One ordered block of the system prompt (Appendix B1).

    ``cache_hint`` requests provider prompt caching for this block (best-effort
    on Anthropic batch; §5.2 R4). It is a hint only — never a booked saving.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    cache_hint: bool = False


class Message(BaseModel):
    """A chat message (M0: text content only)."""

    model_config = ConfigDict(extra="forbid")

    role: str
    content: str


class ToolDef(BaseModel):
    """A tool the model may call (escape-hatch tool use; §6).

    ``input_schema`` is a JSON-Schema dict, matching Anthropic's tool shape.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


# --- the rendered call (Appendix B1) -----------------------------------------


class RenderedCall(BaseModel):
    """A fully-harvested, provider-agnostic LLM request (Appendix B1)."""

    model_config = ConfigDict(extra="forbid")

    custom_id: str
    model: str
    system: list[PrefixBlock] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)
    tools: list[ToolDef] | None = None
    max_tokens: int
    temperature: float
    memo_key: str
    node_ids: list[str] = Field(default_factory=list)


# --- adapter value types (§10 BatchAdapter Protocol) -------------------------


class TokenEstimate(BaseModel):
    """Pre-submit sizing returned by ``count_tokens`` (§5.1/§5.3, §10)."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int = 0
    item_count: int = 1


class BatchRef(BaseModel):
    """Opaque handle to a submitted provider batch (§10)."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    batch_id: str
    idempotency_key: str | None = None


class BatchStatus(BaseModel):
    """Poll result: provider-level status + per-item-state counts (§10)."""

    model_config = ConfigDict(extra="forbid")

    batch_status: str
    completed: int = 0
    errored: int = 0
    expired: int = 0
    processing: int = 0

    @property
    def total(self) -> int:
        return self.completed + self.errored + self.expired + self.processing

    @property
    def is_terminal(self) -> bool:
        """True when no items are still processing."""
        return self.processing == 0


class ItemResult(BaseModel):
    """One returned batch item (§10). ``status`` selects payload vs error."""

    model_config = ConfigDict(extra="forbid")

    custom_id: str
    status: ItemStatus
    payload: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class Caps(BaseModel):
    """Provider width/feature constraints the optimizer must respect (§5.3, §10)."""

    model_config = ConfigDict(extra="forbid")

    max_items: int
    max_bytes: int
    enqueued_token_cap: int | None = None
    creation_rate_limit: int | None = None  # batch creations per hour
    disallowed_params: list[str] = Field(default_factory=list)
    supports_cache: bool = False
    supports_webhooks: bool = False
    result_ttl_days: int
    typical_latency_dist: dict[str, float] | None = None  # e.g. {"p50": 0.5, "p90": 6.0} hours


# --- key derivation (R10 memoize + B5 submit idempotency) --------------------


def compute_memo_key(*, model: str, prompt: Any, mode: str, sample_idx: int = 0) -> str:
    """R10 memo key: ``sha256(canonical_json(model, prompt, mode, sample_idx))``.

    ``prompt`` is the rendered prompt content (any canonical-JSON-able value; a
    dict or list of :class:`PrefixBlock`/:class:`Message`). Include ``mode``
    (e.g. ``"batch"`` vs ``"realtime"``) and ``sample_idx`` so hedges and N-best
    samples are distinct rows.
    """
    material = {"model": model, "prompt": prompt, "mode": mode, "sample_idx": sample_idx}
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def memo_key_for_call(call: RenderedCall, *, mode: str, sample_idx: int = 0) -> str:
    """Derive the R10 memo key from a :class:`RenderedCall`.

    The rendered prompt is the call minus bookkeeping fields (``custom_id``,
    ``memo_key``, ``node_ids``), so two calls that differ only in their id or
    node mapping memoize to the same result.
    """
    prompt = call.model_dump(mode="json", exclude={"custom_id", "memo_key", "node_ids", "model"})
    return compute_memo_key(model=call.model, prompt=prompt, mode=mode, sample_idx=sample_idx)


def submit_idempotency_key(items: list[RenderedCall], flush_ordinal: int) -> str:
    """B5 submit idempotency key: ``sha256(canonical_json(items))[:16] + ":" + flush_ordinal``.

    Content-derived, so a replayed wave submission produces the same key and the
    provider (or the store) can dedupe it — no double-submit on crash-replay.
    """
    digest = hashlib.sha256(canonical_json(list(items)).encode("utf-8")).hexdigest()
    return f"{digest[:16]}:{flush_ordinal}"
