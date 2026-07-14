"""Tests for memo.py: R10 memo hit/miss, put idempotency, call_items mapping."""

from __future__ import annotations

from lazycode.ir import compute_memo_key
from lazycode.store import Store, memo


def _key(sample_idx: int = 0) -> str:
    return compute_memo_key(model="claude-x", prompt={"messages": ["hi"]}, mode="batch", sample_idx=sample_idx)


def test_get_miss_returns_none(store: Store):
    assert memo.get(store, _key()) is None


def test_put_then_get_hit(store: Store):
    key = _key()
    memo.put(
        store,
        call_id="call1",
        memo_key=key,
        mode="batch",
        node_id="n1",
        provider="anthropic",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.01,
        cached=False,
    )
    record = memo.get(store, key)
    assert record is not None
    assert record.id == "call1"
    assert record.node_id == "n1"
    assert record.tokens_in == 100
    assert record.tokens_out == 50
    assert record.cost_usd == 0.01
    assert record.cached is False


def test_mode_and_sample_idx_produce_distinct_keys():
    """§5.2 R10: mode/sample_idx are in the key so a realtime hedge of a batch
    item and N-best samples of one prompt are distinct rows."""
    batch_key = compute_memo_key(model="m", prompt={"x": 1}, mode="batch", sample_idx=0)
    realtime_key = compute_memo_key(model="m", prompt={"x": 1}, mode="realtime", sample_idx=0)
    sample1_key = compute_memo_key(model="m", prompt={"x": 1}, mode="batch", sample_idx=1)
    assert len({batch_key, realtime_key, sample1_key}) == 3


def test_put_is_idempotent_on_duplicate_memo_key(store: Store):
    key = _key()
    first = memo.put(store, call_id="call1", memo_key=key, mode="batch", tokens_in=10, tokens_out=5)
    second = memo.put(store, call_id="call2", memo_key=key, mode="batch", tokens_in=999, tokens_out=999)
    # second put is a no-op: the original row wins, no duplicate/overwrite
    assert second.id == first.id == "call1"
    assert second.tokens_in == 10
    count = store.conn.execute("SELECT COUNT(*) FROM llm_calls WHERE memo_key=?", (key,)).fetchone()[0]
    assert count == 1


def test_add_call_item_and_call_items_for(store: Store):
    key = _key()
    memo.put(store, call_id="call1", memo_key=key, mode="batch")
    memo.add_call_item(store, call_id="call1", node_id="n1", custom_id="c1")
    memo.add_call_item(store, call_id="call1", node_id="n2", custom_id="c1")
    items = memo.call_items_for(store, "call1")
    assert {i.node_id for i in items} == {"n1", "n2"}
    assert all(i.custom_id == "c1" for i in items)


def test_add_call_item_upserts(store: Store):
    memo.put(store, call_id="call1", memo_key=_key(), mode="batch")
    memo.add_call_item(store, call_id="call1", node_id="n1", custom_id="c1", item_status="completed")
    memo.add_call_item(store, call_id="call1", node_id="n1", custom_id="c1-renamed", item_status="errored")
    items = memo.call_items_for(store, "call1")
    assert len(items) == 1
    assert items[0].custom_id == "c1-renamed"
    assert items[0].item_status == "errored"


def test_set_item_status(store: Store):
    memo.put(store, call_id="call1", memo_key=_key(), mode="batch")
    memo.add_call_item(store, call_id="call1", node_id="n1", custom_id="c1")
    memo.set_item_status(store, call_id="call1", node_id="n1", item_status="completed")
    items = memo.call_items_for(store, "call1")
    assert items[0].item_status == "completed"


def test_vectorized_call_maps_to_k_nodes(store: Store):
    """R6: one llm_call packs k tiny homogeneous tasks -> k call_items rows."""
    memo.put(store, call_id="vec-call", memo_key=_key(), mode="batch")
    node_ids = [f"n{i}" for i in range(5)]
    for i, node_id in enumerate(node_ids):
        memo.add_call_item(store, call_id="vec-call", node_id=node_id, custom_id=f"item-{i}")
    items = memo.call_items_for(store, "vec-call")
    assert {i.node_id for i in items} == set(node_ids)
