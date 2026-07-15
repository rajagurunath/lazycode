"""Tests for MockBatchAdapter / MockRealtimeAdapter (providers/mock.py) --
the scheduler's own tests (later milestones) depend on this module, so its
behavior is pinned here: deterministic instant completion, canned responses
keyed by custom_id or a callable, and full submission records.
"""

from __future__ import annotations

import pytest

from lazycode.ir import Caps, ItemResult, ItemStatus, Message
from lazycode.providers.base import FatalError
from lazycode.providers.mock import MockBatchAdapter, MockRealtimeAdapter

from .conftest import make_call

# --- MockBatchAdapter ---------------------------------------------------------


def test_default_result_is_completed_and_plausible():
    adapter = MockBatchAdapter()
    call = make_call("c1")
    ref = adapter.submit([call], "idem-1")
    status = adapter.poll(ref)

    assert status.batch_status == "ended"
    assert status.is_terminal is True
    assert status.completed == 1

    (result,) = list(adapter.fetch(ref))
    assert result.custom_id == "c1"
    assert result.status == ItemStatus.COMPLETED
    assert result.payload["content"][0]["type"] == "text"


def test_canned_responses_by_custom_id():
    canned = {
        "c1": ItemResult(custom_id="c1", status=ItemStatus.COMPLETED, payload={"content": [{"text": "A"}]}),
        "c2": ItemResult(custom_id="c2", status=ItemStatus.ERRORED, error={"message": "boom"}),
    }
    adapter = MockBatchAdapter(responses=canned)
    ref = adapter.submit([make_call("c1"), make_call("c2")], "idem-2")

    status = adapter.poll(ref)
    assert status.completed == 1
    assert status.errored == 1

    results = {r.custom_id: r for r in adapter.fetch(ref)}
    assert results["c1"].status == ItemStatus.COMPLETED
    assert results["c2"].status == ItemStatus.ERRORED
    assert results["c2"].error == {"message": "boom"}


def test_canned_responses_by_callable():
    def responder(call):
        return ItemResult(custom_id=call.custom_id, status=ItemStatus.EXPIRED)

    adapter = MockBatchAdapter(responses=responder)
    ref = adapter.submit([make_call("c1")], "idem-3")
    (result,) = list(adapter.fetch(ref))
    assert result.status == ItemStatus.EXPIRED


def test_submit_records_items_and_batches():
    adapter = MockBatchAdapter()
    c1, c2, c3 = make_call("c1"), make_call("c2"), make_call("c3")
    ref1 = adapter.submit([c1, c2], "idem-4")
    ref2 = adapter.submit([c3], "idem-5")

    assert adapter.submitted_items == [c1, c2, c3]
    assert adapter.submitted_batches[ref1.batch_id] == [c1, c2]
    assert adapter.submitted_batches[ref2.batch_id] == [c3]
    assert ref1.batch_id != ref2.batch_id


def test_submit_known_refs_dedupes_locally():
    adapter = MockBatchAdapter()
    call = make_call("c1")
    first = adapter.submit([call], "idem-6")

    second = adapter.submit([call], "idem-6", known_refs={"idem-6": first})
    assert second is first
    # only submitted once.
    assert adapter.submitted_items == [call]


def test_find_batch_returns_registry_ref_or_none():
    """Review F2(c): the mock emulates metadata-stamped lookup — a submitted
    batch is findable by its idempotency key even without known_refs."""
    adapter = MockBatchAdapter()
    ref = adapter.submit([make_call("c1")], "idem-fb")

    assert adapter.find_batch("idem-fb") == ref
    assert adapter.find_batch("never-submitted") is None


def test_submit_rejects_over_caps():
    tiny_caps = Caps(max_items=1, max_bytes=10_000_000, result_ttl_days=29)
    adapter = MockBatchAdapter(caps=tiny_caps)
    with pytest.raises(FatalError, match="max_items"):
        adapter.submit([make_call("c1"), make_call("c2")], "idem-7")


def test_cancel_marks_partials_as_errored():
    """§7.6: whole-batch cancel returns partials -- nothing "completed" survives
    a canceled batch in the mock, matching the real adapter's canceled->errored
    mapping."""
    adapter = MockBatchAdapter()
    ref = adapter.submit([make_call("c1"), make_call("c2")], "idem-8")

    adapter.cancel(ref)

    status = adapter.poll(ref)
    assert status.errored == 2
    assert status.completed == 0
    assert status.is_terminal is True

    results = list(adapter.fetch(ref))
    assert all(r.status == ItemStatus.ERRORED for r in results)
    assert {r.custom_id for r in results} == {"c1", "c2"}
    assert ref.batch_id in adapter.cancelled_batch_ids


def test_count_tokens_heuristic():
    adapter = MockBatchAdapter()
    call = make_call("c1", messages=[Message(role="user", content="x" * 40)])
    estimate = adapter.count_tokens([call])
    assert estimate.input_tokens == 10
    assert estimate.item_count == 1


def test_caps_property_default_is_generous():
    adapter = MockBatchAdapter()
    assert adapter.caps.max_items == 100_000
    assert adapter.caps.max_bytes == 256 * 1024 * 1024


# --- MockRealtimeAdapter -------------------------------------------------------


def test_realtime_default_completed():
    adapter = MockRealtimeAdapter()
    call = make_call("planner-1")
    result = adapter.complete(call)
    assert result.status == ItemStatus.COMPLETED
    assert result.custom_id == "planner-1"


def test_realtime_records_calls_in_order():
    adapter = MockRealtimeAdapter()
    c1, c2 = make_call("c1"), make_call("c2")
    adapter.complete(c1)
    adapter.complete(c2, tool_choice={"type": "any"})
    assert adapter.calls == [c1, c2]


def test_realtime_canned_responses_by_custom_id():
    canned = {"c1": ItemResult(custom_id="c1", status=ItemStatus.ERRORED, error={"message": "no"})}
    adapter = MockRealtimeAdapter(responses=canned)
    result = adapter.complete(make_call("c1"))
    assert result.status == ItemStatus.ERRORED
