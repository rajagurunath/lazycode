"""Tests for RenderedCall, adapter types, and key derivation (ir/calls.py)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lazycode.ir import (
    BatchStatus,
    ItemResult,
    ItemStatus,
    Message,
    PrefixBlock,
    RenderedCall,
    canonical_json,
    compute_memo_key,
    memo_key_for_call,
    submit_idempotency_key,
)


def _call(custom_id: str = "c1", model: str = "claude-haiku-4-5", **kw) -> RenderedCall:
    base = dict(
        custom_id=custom_id,
        model=model,
        system=[PrefixBlock(text="repo map", cache_hint=True)],
        messages=[Message(role="user", content="write a test")],
        max_tokens=1024,
        temperature=0.0,
        memo_key="unset",
        node_ids=["n3.0"],
    )
    base.update(kw)
    return RenderedCall(**base)


def test_rendered_call_roundtrip():
    call = _call()
    restored = RenderedCall.model_validate(call.model_dump(mode="json"))
    assert restored == call
    assert restored.tools is None


def test_rendered_call_extra_field_rejected():
    with pytest.raises(ValidationError):
        _call(bogus="x")


# --- canonical_json ----------------------------------------------------------


def test_canonical_json_is_key_order_independent():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_canonical_json_serializes_models():
    block = PrefixBlock(text="x", cache_hint=False)
    assert canonical_json(block) == canonical_json({"text": "x", "cache_hint": False})


# --- memo key (R10) ----------------------------------------------------------


def test_memo_key_deterministic():
    a = compute_memo_key(model="m", prompt={"x": 1}, mode="batch", sample_idx=0)
    b = compute_memo_key(model="m", prompt={"x": 1}, mode="batch", sample_idx=0)
    assert a == b
    assert len(a) == 64  # sha256 hexdigest


def test_memo_key_sensitive_to_every_component():
    base = dict(model="m", prompt={"x": 1}, mode="batch", sample_idx=0)
    ref = compute_memo_key(**base)
    assert compute_memo_key(**{**base, "model": "m2"}) != ref
    assert compute_memo_key(**{**base, "prompt": {"x": 2}}) != ref
    assert compute_memo_key(**{**base, "mode": "realtime"}) != ref
    assert compute_memo_key(**{**base, "sample_idx": 1}) != ref


def test_memo_key_for_call_ignores_bookkeeping_fields():
    """custom_id / node_ids differ but the rendered prompt is identical -> same key."""
    c1 = _call(custom_id="c1", node_ids=["n1"])
    c2 = _call(custom_id="c2", node_ids=["n2", "n3"])
    assert memo_key_for_call(c1, mode="batch") == memo_key_for_call(c2, mode="batch")


def test_memo_key_for_call_sensitive_to_prompt_and_mode():
    c1 = _call()
    c2 = _call(messages=[Message(role="user", content="different")])
    assert memo_key_for_call(c1, mode="batch") != memo_key_for_call(c2, mode="batch")
    assert memo_key_for_call(c1, mode="batch") != memo_key_for_call(c1, mode="realtime")
    assert memo_key_for_call(c1, mode="batch", sample_idx=0) != memo_key_for_call(
        c1, mode="batch", sample_idx=1
    )


# --- submit idempotency key (B5) --------------------------------------------


def test_submit_idempotency_key_shape_and_determinism():
    items = [_call("c1"), _call("c2")]
    k1 = submit_idempotency_key(items, flush_ordinal=0)
    k2 = submit_idempotency_key(items, flush_ordinal=0)
    assert k1 == k2
    digest, ordinal = k1.split(":")
    assert len(digest) == 16
    assert ordinal == "0"


def test_submit_idempotency_key_sensitive_to_items_and_ordinal():
    items = [_call("c1")]
    ref = submit_idempotency_key(items, flush_ordinal=0)
    assert submit_idempotency_key(items, flush_ordinal=1) != ref
    assert submit_idempotency_key([_call("c1", model="other")], flush_ordinal=0) != ref


# --- adapter value types -----------------------------------------------------


def test_batch_status_totals():
    s = BatchStatus(batch_status="in_progress", completed=3, errored=1, processing=6)
    assert s.total == 10
    assert s.is_terminal is False
    assert BatchStatus(batch_status="ended", completed=10).is_terminal is True


def test_item_result_status_enum():
    r = ItemResult(custom_id="c1", status="completed", payload={"text": "ok"})
    assert r.status is ItemStatus.COMPLETED
    with pytest.raises(ValidationError):
        ItemResult(custom_id="c1", status="not-a-status")
