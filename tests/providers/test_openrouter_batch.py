"""Tests for the OpenRouter beta-batch adapter — fake HTTP wire, no live calls."""

from __future__ import annotations

from typing import Any

import pytest

from lazycode.ir import BatchRef, ItemStatus, Message
from lazycode.providers.base import FatalError, RateLimited, RetryableError
from lazycode.providers.openrouter_batch import (
    OpenRouterBatchAdapter,
    build_batch_payload,
    build_request_body,
)

from .conftest import make_call


class FakeWire:
    """Records requests; replies from a scripted (method, path-suffix) map."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.replies: list[tuple[int, dict | None]] = []

    def script(self, status: int, body: dict | None) -> None:
        self.replies.append((status, body))

    def request(self, method: str, url: str, *, json: Any = None) -> tuple[int, dict | None]:
        self.calls.append((method, url, json))
        return self.replies.pop(0)


def adapter(wire: FakeWire | None = None) -> tuple[OpenRouterBatchAdapter, FakeWire]:
    wire = wire or FakeWire()
    return OpenRouterBatchAdapter(http=wire), wire


def or_call(custom_id: str = "c1", model: str = "anthropic/claude-haiku-4.5"):
    return make_call(custom_id, model=model)


# --- payload building --------------------------------------------------------


def test_request_body_has_no_model_key():
    body = build_request_body(or_call())
    assert "model" not in body
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_batch_payload_key_order_is_endpoint_model_requests():
    payload = build_batch_payload([or_call("a"), or_call("b")])
    assert list(payload.keys()) == ["endpoint", "model", "requests"]
    assert payload["model"] == "anthropic/claude-haiku-4.5"
    assert [r["custom_id"] for r in payload["requests"]] == ["a", "b"]


def test_batch_payload_rejects_mixed_models():
    with pytest.raises(FatalError, match="single-model"):
        build_batch_payload([or_call("a"), or_call("b", model="openai/gpt-5.4-mini")])


def test_batch_payload_rejects_empty():
    with pytest.raises(FatalError, match="empty"):
        build_batch_payload([])


# --- submit ------------------------------------------------------------------


def test_submit_posts_and_returns_ref():
    a, wire = adapter()
    wire.script(202, {"id": "batch_abc", "status": "validating"})
    ref = a.submit([or_call()], "idem-1")
    assert ref == BatchRef(
        provider="openrouter-batch", batch_id="batch_abc", idempotency_key="idem-1"
    )
    method, url, body = wire.calls[0]
    assert (method, url) == ("POST", "https://openrouter.ai/api/beta/batches")
    assert list(body.keys()) == ["endpoint", "model", "requests"]


def test_submit_known_refs_short_circuits():
    a, wire = adapter()
    known = {"idem-1": BatchRef(provider="openrouter-batch", batch_id="old")}
    assert a.submit([or_call()], "idem-1", known_refs=known).batch_id == "old"
    assert wire.calls == []


def test_submit_maps_4xx_to_fatal_and_429_to_ratelimited_and_5xx_retryable():
    for status, exc in ((400, FatalError), (429, RateLimited), (503, RetryableError)):
        a, wire = adapter()
        wire.script(status, {"error": "x"})
        with pytest.raises(exc):
            a.submit([or_call()], "k")


# --- poll --------------------------------------------------------------------


def _batch_body(status: str, *, total=2, completed=0, failed=0, usage=None, results=None):
    return {
        "id": "batch_abc",
        "status": status,
        "request_counts": {"total": total, "completed": completed, "failed": failed},
        "usage": usage,
        "results": results,
    }


@pytest.mark.parametrize("status", ["validating", "in_progress", "finalizing", "cancelling"])
def test_poll_in_progress(status):
    a, wire = adapter()
    wire.script(200, _batch_body(status, completed=1))
    st = a.poll(BatchRef(provider="openrouter-batch", batch_id="batch_abc"))
    assert not st.is_terminal and st.processing == 1 and st.completed == 1


def test_poll_completed_captures_usage_receipt():
    a, wire = adapter()
    usage = {"prompt_tokens": 20, "completion_tokens": 40, "total_tokens": 60,
             "cost": 0.000225, "is_byok": False}
    wire.script(200, _batch_body("completed", completed=2, usage=usage, results=[]))
    st = a.poll(BatchRef(provider="openrouter-batch", batch_id="batch_abc"))
    assert st.is_terminal and st.completed == 2
    assert a.last_usage == usage


def test_poll_expired_books_remainder_as_expired():
    a, wire = adapter()
    wire.script(200, _batch_body("expired", total=5, completed=2, failed=1))
    st = a.poll(BatchRef(provider="openrouter-batch", batch_id="b"))
    assert (st.completed, st.errored, st.expired, st.processing) == (2, 1, 2, 0)


def test_poll_cancelled_folds_remainder_into_errored():
    a, wire = adapter()
    wire.script(200, _batch_body("cancelled", total=5, completed=2, failed=1))
    st = a.poll(BatchRef(provider="openrouter-batch", batch_id="b"))
    assert (st.errored, st.expired) == (3, 0)


def test_poll_unknown_status_raises_fatal():
    a, wire = adapter()
    wire.script(200, _batch_body("weird"))
    with pytest.raises(FatalError, match="unknown batch status"):
        a.poll(BatchRef(provider="openrouter-batch", batch_id="b"))


# --- fetch -------------------------------------------------------------------


def _result(custom_id, *, content="ok", status_code=200, error=None):
    if error is not None:
        return {"custom_id": custom_id, "response": None, "error": error}
    return {
        "custom_id": custom_id,
        "response": {
            "status_code": status_code,
            "body": {"id": f"gen-batch-{custom_id}",
                     "choices": [{"message": {"role": "assistant", "content": content}}]},
        },
        "error": None,
    }


def test_fetch_uses_cached_terminal_poll_body():
    a, wire = adapter()
    wire.script(200, _batch_body("completed", completed=1,
                                 results=[_result("c1")]))
    ref = BatchRef(provider="openrouter-batch", batch_id="batch_abc")
    a.poll(ref)
    out = list(a.fetch(ref))  # no second GET scripted — cached body must serve
    assert len(wire.calls) == 1
    assert out[0].custom_id == "c1" and out[0].status is ItemStatus.COMPLETED
    assert out[0].payload["choices"][0]["message"]["content"] == "ok"


def test_fetch_maps_error_and_http_error_results():
    a, wire = adapter()
    wire.script(200, _batch_body("completed", completed=1, failed=2, results=[
        _result("good"),
        _result("bad", error={"code": "boom", "message": "x"}),
        _result("httpbad", status_code=500),
    ]))
    ref = BatchRef(provider="openrouter-batch", batch_id="b")
    got = {r.custom_id: r for r in a.fetch(ref)}
    assert got["good"].status is ItemStatus.COMPLETED
    assert got["bad"].status is ItemStatus.ERRORED and got["bad"].error["code"] == "boom"
    assert got["httpbad"].status is ItemStatus.ERRORED
    assert got["httpbad"].error["status_code"] == 500


def test_fetch_non_completed_terminal_yields_nothing():
    a, wire = adapter()
    wire.script(200, _batch_body("expired", total=3))
    assert list(a.fetch(BatchRef(provider="openrouter-batch", batch_id="b"))) == []


# --- find_batch / cancel -----------------------------------------------------


def test_find_batch_is_local_only():
    a, wire = adapter()
    assert a.find_batch("anything") is None
    assert wire.calls == []


def test_cancel_posts_cancel():
    a, wire = adapter()
    wire.script(200, {"id": "b", "status": "cancelling"})
    a.cancel(BatchRef(provider="openrouter-batch", batch_id="b"))
    assert wire.calls[0][:2] == ("POST", "https://openrouter.ai/api/beta/batches/b/cancel")


# --- count_tokens ------------------------------------------------------------


def test_count_tokens_heuristic():
    a, _ = adapter()
    est = a.count_tokens([make_call(messages=[Message(role="user", content="x" * 400)])])
    assert est.input_tokens == 100 and est.item_count == 1
    assert a.last_count_tokens_source == "heuristic"


# --- messages-endpoint mode --------------------------------------------------


def test_messages_endpoint_builds_anthropic_bodies_without_model():
    from lazycode.providers.openrouter_batch import (
        MESSAGES_ENDPOINT,
        build_batch_payload,
    )

    payload = build_batch_payload([or_call("a")], MESSAGES_ENDPOINT)
    assert payload["endpoint"] == "/v1/messages"
    assert list(payload.keys()) == ["endpoint", "model", "requests"]
    body = payload["requests"][0]["body"]
    assert "model" not in body
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["max_tokens"] == 1024


def test_messages_mode_adapter_submits_messages_endpoint():
    from lazycode.providers.openrouter_batch import MESSAGES_ENDPOINT

    wire = FakeWire()
    a = OpenRouterBatchAdapter(http=wire, endpoint=MESSAGES_ENDPOINT)
    wire.script(202, {"id": "batch_m", "status": "validating"})
    a.submit([or_call()], "k")
    assert wire.calls[0][2]["endpoint"] == "/v1/messages"


def test_poll_tolerates_transient_404_then_recovers():
    a, wire = adapter()
    ref = BatchRef(provider="openrouter-batch", batch_id="b")
    wire.script(404, {"error": {"message": "not found", "code": 404}})
    with pytest.raises(RetryableError, match="visibility lag"):
        a.poll(ref)
    wire.script(200, _batch_body("in_progress"))
    assert a.poll(ref).batch_status == "in_progress"


def test_poll_persistent_404_goes_fatal():
    a, wire = adapter()
    ref = BatchRef(provider="openrouter-batch", batch_id="gone")
    for _ in range(10):
        wire.script(404, {"error": {"message": "not found", "code": 404}})
        with pytest.raises(RetryableError):
            a.poll(ref)
    wire.script(404, {"error": {"message": "not found", "code": 404}})
    with pytest.raises(FatalError):
        a.poll(ref)
