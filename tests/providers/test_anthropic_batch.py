"""Tests for the Anthropic Message Batches adapter (providers/anthropic_batch.py).

No live API calls: the SDK client is a hand-built fake exposing only the
methods the adapter touches. Golden mapping tests assert exact request-kwarg
shapes (including ``cache_control`` placement); status/result tests cover the
full succeeded/errored/expired/canceled matrix from DESIGN.md §10; caps tests
assert the adapter validates and raises *before* calling the client.
"""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from lazycode.ir import BatchRef, ItemStatus, Message, PrefixBlock, ToolDef
from lazycode.providers.anthropic_batch import (
    AnthropicBatchAdapter,
    build_batch_request,
    build_message_params,
)
from lazycode.providers.base import FatalError, RateLimited, RetryableError

from .conftest import fake_batch, fake_batch_result, fake_error_response, fake_message, make_call

# --- golden request-mapping tests --------------------------------------------


def test_build_message_params_minimal():
    call = make_call(messages=[Message(role="user", content="hi")], max_tokens=512, temperature=0.2)
    params = build_message_params(call)
    assert params == {
        "model": "claude-haiku-4-5",
        "max_tokens": 512,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": "hi"}],
    }
    # no system/tools keys at all when the call has none -- not None, omitted.
    assert "system" not in params
    assert "tools" not in params


def test_build_message_params_cache_control_placement():
    """cache_hint=True -> cache_control key present; cache_hint=False -> omitted entirely."""
    call = make_call(
        system=[
            PrefixBlock(text="house rules", cache_hint=True),
            PrefixBlock(text="volatile per-call context", cache_hint=False),
        ]
    )
    params = build_message_params(call)
    assert params["system"] == [
        {"type": "text", "text": "house rules", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "volatile per-call context"},
    ]
    # the un-cached block must not carry a cache_control: None either.
    assert "cache_control" not in params["system"][1]


def test_build_message_params_tools():
    call = make_call(
        tools=[ToolDef(name="emit_plan", description="emit the plan", input_schema={"type": "object"})]
    )
    params = build_message_params(call)
    assert params["tools"] == [
        {"name": "emit_plan", "description": "emit the plan", "input_schema": {"type": "object"}}
    ]


def test_build_batch_request_wraps_custom_id_and_params():
    call = make_call(custom_id="node-3.generate")
    request = build_batch_request(call)
    assert request["custom_id"] == "node-3.generate"
    assert request["params"] == build_message_params(call)


# --- count_tokens -------------------------------------------------------------


def test_count_tokens_empty_items():
    adapter = AnthropicBatchAdapter(client=SimpleNamespace())
    estimate = adapter.count_tokens([])
    assert estimate.input_tokens == 0
    assert estimate.item_count == 0
    assert adapter.last_count_tokens_source == "api"


def test_count_tokens_uses_sdk_endpoint_when_available():
    seen_kwargs = []

    def _count_tokens(**kwargs):
        seen_kwargs.append(kwargs)
        return SimpleNamespace(input_tokens=42)

    client = SimpleNamespace(messages=SimpleNamespace(count_tokens=_count_tokens))
    adapter = AnthropicBatchAdapter(client=client)

    items = [make_call("c1"), make_call("c2")]
    estimate = adapter.count_tokens(items)

    assert estimate.input_tokens == 84  # 42 summed twice
    assert estimate.item_count == 2
    assert adapter.last_count_tokens_source == "api"
    assert len(seen_kwargs) == 2
    assert seen_kwargs[0]["model"] == "claude-haiku-4-5"


def test_count_tokens_falls_back_to_heuristic_on_sdk_error():
    def _count_tokens(**kwargs):  # noqa: ARG001
        raise RuntimeError("count_tokens endpoint unavailable")

    client = SimpleNamespace(messages=SimpleNamespace(count_tokens=_count_tokens))
    adapter = AnthropicBatchAdapter(client=client)

    items = [make_call("c1", messages=[Message(role="user", content="x" * 40)])]
    estimate = adapter.count_tokens(items)

    assert estimate.input_tokens == 10  # 40 chars / 4
    assert adapter.last_count_tokens_source == "heuristic"


# --- caps validation ----------------------------------------------------------


def test_submit_rejects_over_max_items_before_calling_client():
    from lazycode.providers.anthropic_batch import _ANTHROPIC_CAPS

    client = SimpleNamespace(messages=SimpleNamespace(batches=_UnreachableBatches()))
    small_caps = _ANTHROPIC_CAPS.model_copy(update={"max_items": 2})

    class _TightCapsAdapter(AnthropicBatchAdapter):
        @property
        def caps(self):
            return small_caps

    items = [make_call(f"c{i}") for i in range(3)]
    adapter = _TightCapsAdapter(client=client)
    with pytest.raises(FatalError, match="max_items"):
        adapter.submit(items, "idem-key-1")


def test_submit_rejects_over_max_bytes_before_calling_client():
    client = SimpleNamespace(messages=SimpleNamespace(batches=_UnreachableBatches()))
    from lazycode.providers.anthropic_batch import _ANTHROPIC_CAPS

    tiny_caps = _ANTHROPIC_CAPS.model_copy(update={"max_bytes": 10})

    class _TinyBytesAdapter(AnthropicBatchAdapter):
        @property
        def caps(self):
            return tiny_caps

    adapter = _TinyBytesAdapter(client=client)
    items = [make_call("c1", messages=[Message(role="user", content="x" * 1000)])]
    with pytest.raises(FatalError, match="max_bytes"):
        adapter.submit(items, "idem-key-2")


class _UnreachableBatches:
    """Fails the test loudly if the adapter reaches the client after a Caps violation."""

    def create(self, **kwargs):  # noqa: ANN001, ARG002
        raise AssertionError("submit() must validate Caps before calling batches.create")


# --- submit ---------------------------------------------------------------


def test_submit_known_refs_short_circuits_without_calling_client():
    client = SimpleNamespace(messages=SimpleNamespace(batches=_UnreachableBatches()))
    adapter = AnthropicBatchAdapter(client=client)
    existing = BatchRef(provider="anthropic", batch_id="msgbatch_existing", idempotency_key="k1")

    ref = adapter.submit([make_call("c1")], "k1", known_refs={"k1": existing})

    assert ref is existing


def test_submit_happy_path_returns_batch_ref():
    created = fake_batch(batch_id="msgbatch_new")
    seen_requests = []

    def _create(**kwargs):
        seen_requests.append(kwargs["requests"])
        return created

    client = SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(create=_create)))
    adapter = AnthropicBatchAdapter(client=client)

    items = [make_call("c1"), make_call("c2")]
    ref = adapter.submit(items, "idem-key-3")

    assert ref == BatchRef(provider="anthropic", batch_id="msgbatch_new", idempotency_key="idem-key-3")
    assert [r["custom_id"] for r in seen_requests[0]] == ["c1", "c2"]


@pytest.mark.parametrize(
    ("status_code", "expected_type"),
    [(429, RateLimited), (500, RetryableError), (400, FatalError)],
)
def test_submit_maps_api_status_errors(status_code, expected_type):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")
    response = httpx.Response(status_code, request=request, headers={"retry-after": "7"})
    error_cls = {429: anthropic.RateLimitError, 500: anthropic.InternalServerError, 400: anthropic.BadRequestError}[
        status_code
    ]
    error = error_cls("boom", response=response, body=None)

    def _create(**kwargs):  # noqa: ARG001
        raise error

    client = SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(create=_create)))
    adapter = AnthropicBatchAdapter(client=client)

    with pytest.raises(expected_type) as excinfo:
        adapter.submit([make_call("c1")], "idem-key-4")
    assert excinfo.value.__cause__ is error
    if status_code == 429:
        assert excinfo.value.retry_after == 7.0


def test_submit_maps_connection_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")
    error = anthropic.APIConnectionError(request=request)

    def _create(**kwargs):  # noqa: ARG001
        raise error

    client = SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(create=_create)))
    adapter = AnthropicBatchAdapter(client=client)

    with pytest.raises(RetryableError):
        adapter.submit([make_call("c1")], "idem-key-5")


# --- poll -----------------------------------------------------------------


def test_poll_maps_processing_status_and_counts():
    batch = fake_batch(
        processing_status="ended", succeeded=5, errored=1, expired=2, canceled=0, processing=0
    )
    client = SimpleNamespace(
        messages=SimpleNamespace(batches=SimpleNamespace(retrieve=lambda batch_id: batch))  # noqa: ARG005
    )
    adapter = AnthropicBatchAdapter(client=client)

    status = adapter.poll(BatchRef(provider="anthropic", batch_id="msgbatch_1"))

    assert status.batch_status == "ended"
    assert status.completed == 5
    assert status.errored == 1
    assert status.expired == 2
    assert status.processing == 0
    assert status.is_terminal is True
    assert status.total == 8


def test_poll_folds_canceled_into_errored():
    """§7.6/§10: whole-batch cancel returns partials; ItemStatus has no CANCELED,
    so canceled requests count as errored at the poll level too."""
    batch = fake_batch(processing_status="ended", succeeded=3, errored=1, canceled=4, processing=0)
    client = SimpleNamespace(
        messages=SimpleNamespace(batches=SimpleNamespace(retrieve=lambda batch_id: batch))  # noqa: ARG005
    )
    adapter = AnthropicBatchAdapter(client=client)

    status = adapter.poll(BatchRef(provider="anthropic", batch_id="msgbatch_1"))

    assert status.errored == 5  # 1 errored + 4 canceled
    assert status.completed == 3


def test_poll_still_processing_is_not_terminal():
    batch = fake_batch(processing_status="in_progress", processing=10)
    client = SimpleNamespace(
        messages=SimpleNamespace(batches=SimpleNamespace(retrieve=lambda batch_id: batch))  # noqa: ARG005
    )
    adapter = AnthropicBatchAdapter(client=client)

    status = adapter.poll(BatchRef(provider="anthropic", batch_id="msgbatch_1"))
    assert status.is_terminal is False


# --- fetch: succeeded/errored/expired/canceled matrix + expired partials ------


def test_fetch_maps_full_result_matrix():
    results = [
        fake_batch_result("c1", "succeeded", message=fake_message(content_text="done")),
        fake_batch_result("c2", "errored", error=fake_error_response()),
        fake_batch_result("c3", "expired"),
        fake_batch_result("c4", "canceled"),
    ]
    client = SimpleNamespace(
        messages=SimpleNamespace(batches=SimpleNamespace(results=lambda batch_id: iter(results)))  # noqa: ARG005
    )
    adapter = AnthropicBatchAdapter(client=client)

    items = list(adapter.fetch(BatchRef(provider="anthropic", batch_id="msgbatch_1")))
    by_id = {r.custom_id: r for r in items}

    assert by_id["c1"].status == ItemStatus.COMPLETED
    assert by_id["c1"].payload["content"][0]["text"] == "done"
    assert by_id["c1"].error is None

    assert by_id["c2"].status == ItemStatus.ERRORED
    assert by_id["c2"].error["error"]["type"] == "invalid_request_error"
    assert by_id["c2"].payload is None

    assert by_id["c3"].status == ItemStatus.EXPIRED
    assert by_id["c3"].payload is None
    assert by_id["c3"].error is None

    # canceled -> errored, per the module's explicit mapping table.
    assert by_id["c4"].status == ItemStatus.ERRORED
    assert by_id["c4"].error is not None


def test_fetch_expired_partial_batch():
    """A batch that hit the 24h window mid-flight (Appendix A: 'requests may
    expire at 24h under load') returns a mix of succeeded and expired items --
    fetch must map each independently, not fail the whole stream."""
    results = [
        fake_batch_result("c1", "succeeded", message=fake_message()),
        fake_batch_result("c2", "expired"),
        fake_batch_result("c3", "expired"),
    ]
    client = SimpleNamespace(
        messages=SimpleNamespace(batches=SimpleNamespace(results=lambda batch_id: iter(results)))  # noqa: ARG005
    )
    adapter = AnthropicBatchAdapter(client=client)

    items = list(adapter.fetch(BatchRef(provider="anthropic", batch_id="msgbatch_1")))
    statuses = [r.status for r in items]
    assert statuses == [ItemStatus.COMPLETED, ItemStatus.EXPIRED, ItemStatus.EXPIRED]


def test_fetch_unknown_result_type_raises_fatal():
    bogus = SimpleNamespace(custom_id="c1", result=SimpleNamespace(type="mystery"))
    client = SimpleNamespace(
        messages=SimpleNamespace(batches=SimpleNamespace(results=lambda batch_id: iter([bogus])))  # noqa: ARG005
    )
    adapter = AnthropicBatchAdapter(client=client)
    with pytest.raises(FatalError):
        list(adapter.fetch(BatchRef(provider="anthropic", batch_id="msgbatch_1")))


# --- cancel -----------------------------------------------------------------


def test_cancel_calls_client_with_batch_id():
    seen = []
    client = SimpleNamespace(
        messages=SimpleNamespace(batches=SimpleNamespace(cancel=lambda batch_id: seen.append(batch_id)))
    )
    adapter = AnthropicBatchAdapter(client=client)

    adapter.cancel(BatchRef(provider="anthropic", batch_id="msgbatch_1"))
    assert seen == ["msgbatch_1"]


# --- construction / from_env --------------------------------------------------


def test_requires_exactly_one_of_client_or_factory():
    with pytest.raises(ValueError):
        AnthropicBatchAdapter()
    with pytest.raises(ValueError):
        AnthropicBatchAdapter(client=SimpleNamespace(), client_factory=lambda: SimpleNamespace())


def test_from_env_missing_key_raises_lazily(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    adapter = AnthropicBatchAdapter.from_env()
    # constructing the adapter must not read the env or raise yet.
    with pytest.raises(FatalError, match="ANTHROPIC_API_KEY"):
        adapter._client  # noqa: SLF001 -- triggers the lazy factory


def test_caps_reflects_appendix_a():
    adapter = AnthropicBatchAdapter(client=SimpleNamespace())
    caps = adapter.caps
    assert caps.max_items == 100_000
    assert caps.max_bytes == 256 * 1024 * 1024
    assert caps.result_ttl_days == 29
    assert caps.supports_cache is True
    assert caps.supports_webhooks is False
