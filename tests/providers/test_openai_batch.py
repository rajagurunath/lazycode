"""Tests for the OpenAI-compatible Batch adapter (providers/openai_batch.py).

No live API calls: the SDK client is a hand-built fake
(:class:`FakeOpenAIClient` in ``conftest.py``) exposing only ``files.create``,
``files.content``, ``batches.create/retrieve/list/cancel`` — the methods the
adapter touches. Golden mapping tests assert the exact JSONL the adapter
uploads; status/result tests cover the full in-progress/completed/expired/
cancelled matrix from DESIGN.md §10; caps tests assert the adapter validates
and raises *before* calling the client.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from lazycode.ir import BatchRef, ItemStatus, Message, PrefixBlock, ToolDef
from lazycode.providers.base import FatalError, RateLimited, RetryableError
from lazycode.providers.openai_batch import (
    OpenAIBatchAdapter,
    build_batch_line,
    build_chat_body,
    build_jsonl,
)

from .conftest import FakeOpenAIClient, fake_openai_batch, make_call

# --- golden request-mapping tests --------------------------------------------


def test_build_chat_body_minimal():
    call = make_call(messages=[Message(role="user", content="hi")], max_tokens=512, temperature=0.2)
    body = build_chat_body(call)
    assert body == {
        "model": "claude-haiku-4-5",
        "max_tokens": 512,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": "hi"}],
    }
    # no tools key at all when the call has none -- not None, omitted.
    assert "tools" not in body


def test_build_chat_body_folds_system_blocks_in_order():
    """The OpenAI chat wire has no per-block cache_control, so ordered
    PrefixBlocks collapse into one system message; order is what buys the
    (automatic, positional) prefix-cache hit."""
    call = make_call(
        system=[
            PrefixBlock(text="house rules", cache_hint=True),
            PrefixBlock(text="volatile per-call context", cache_hint=False),
        ],
        messages=[Message(role="user", content="go")],
    )
    body = build_chat_body(call)
    assert body["messages"] == [
        {"role": "system", "content": "house rules\n\nvolatile per-call context"},
        {"role": "user", "content": "go"},
    ]
    assert "cache_control" not in json.dumps(body)


def test_build_chat_body_tools_use_function_envelope():
    call = make_call(
        tools=[
            ToolDef(name="emit_plan", description="emit the plan", input_schema={"type": "object"})
        ]
    )
    body = build_chat_body(call)
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "emit_plan",
                "description": "emit the plan",
                "parameters": {"type": "object"},
            },
        }
    ]


def test_build_batch_line_wraps_custom_id_method_and_url():
    call = make_call(custom_id="node-3.generate")
    line = build_batch_line(call)
    assert line["custom_id"] == "node-3.generate"
    assert line["method"] == "POST"
    assert line["url"] == "/v1/chat/completions"
    assert line["body"] == build_chat_body(call)


def test_build_jsonl_is_newline_delimited_one_line_per_call():
    payload = build_jsonl([make_call("c1"), make_call("c2")])
    text = payload.decode("utf-8")
    assert text.endswith("\n")
    lines = text.strip().split("\n")
    assert [json.loads(line)["custom_id"] for line in lines] == ["c1", "c2"]


def test_build_jsonl_empty_items_is_empty_payload():
    assert build_jsonl([]) == b""


# --- count_tokens -------------------------------------------------------------


def test_count_tokens_empty_items():
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient())
    estimate = adapter.count_tokens([])
    assert estimate.input_tokens == 0
    assert estimate.item_count == 0
    assert adapter.last_count_tokens_source == "heuristic"


def test_count_tokens_uses_chars_over_four_heuristic():
    """This wire has no count_tokens endpoint -- always the heuristic."""
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient())
    items = [make_call("c1", messages=[Message(role="user", content="x" * 40)])]
    estimate = adapter.count_tokens(items)
    assert estimate.input_tokens == 10  # 40 chars / 4
    assert estimate.item_count == 1
    assert adapter.last_count_tokens_source == "heuristic"


# --- caps ---------------------------------------------------------------------


def test_caps_reflects_design_section_10():
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient())
    caps = adapter.caps
    assert caps.max_items == 50_000
    assert caps.max_bytes == 200 * 1024 * 1024
    assert caps.result_ttl_days == 29
    assert caps.disallowed_params == ["stream"]
    assert caps.supports_cache is True
    assert caps.supports_webhooks is False
    assert caps.typical_latency_dist == {"p50": 0.5}


def test_submit_rejects_over_max_items_before_calling_client():
    from lazycode.providers.openai_batch import _OPENAI_CAPS

    client = FakeOpenAIClient()
    small_caps = _OPENAI_CAPS.model_copy(update={"max_items": 2})

    class _TightCapsAdapter(OpenAIBatchAdapter):
        @property
        def caps(self):
            return small_caps

    adapter = _TightCapsAdapter(client=client)
    with pytest.raises(FatalError, match="max_items"):
        adapter.submit([make_call(f"c{i}") for i in range(3)], "idem-key-1")
    assert client.files.created == []
    assert client.batches.created == []


def test_submit_rejects_over_max_bytes_before_calling_client():
    from lazycode.providers.openai_batch import _OPENAI_CAPS

    client = FakeOpenAIClient()
    tiny_caps = _OPENAI_CAPS.model_copy(update={"max_bytes": 10})

    class _TinyBytesAdapter(OpenAIBatchAdapter):
        @property
        def caps(self):
            return tiny_caps

    adapter = _TinyBytesAdapter(client=client)
    items = [make_call("c1", messages=[Message(role="user", content="x" * 1000)])]
    with pytest.raises(FatalError, match="max_bytes"):
        adapter.submit(items, "idem-key-2")
    assert client.files.created == []
    assert client.batches.created == []


# --- submit -------------------------------------------------------------------


def test_submit_known_refs_short_circuits_without_calling_client():
    client = FakeOpenAIClient()
    adapter = OpenAIBatchAdapter(client=client)
    existing = BatchRef(provider="openai-batch", batch_id="batch_existing", idempotency_key="k1")

    ref = adapter.submit([make_call("c1")], "k1", known_refs={"k1": existing})

    assert ref is existing
    assert client.files.created == []
    assert client.batches.created == []


def test_submit_uploads_in_memory_jsonl_and_creates_batch():
    client = FakeOpenAIClient(file_id="file_abc", batch_id="batch_new")
    adapter = OpenAIBatchAdapter(client=client)

    ref = adapter.submit([make_call("c1"), make_call("c2")], "idem-key-3")

    # one files.create, purpose=batch, in-memory (filename, bytes) tuple.
    (upload,) = client.files.created
    assert upload["purpose"] == "batch"
    filename, payload = upload["file"]
    assert filename.endswith(".jsonl")
    assert isinstance(payload, bytes)
    assert [json.loads(line)["custom_id"] for line in payload.decode().strip().split("\n")] == [
        "c1",
        "c2",
    ]

    # one batches.create wired to the uploaded file.
    (created,) = client.batches.created
    assert created["input_file_id"] == "file_abc"
    assert created["endpoint"] == "/v1/chat/completions"
    assert created["completion_window"] == "24h"

    assert ref == BatchRef(
        provider="openai-batch", batch_id="batch_new", idempotency_key="idem-key-3"
    )


def test_submit_stamps_idempotency_key_into_batch_metadata():
    """Unlike Anthropic Batches, create() takes metadata natively, so the §B5
    key is stored server-side for find_batch to reconcile after a crash."""
    client = FakeOpenAIClient()
    adapter = OpenAIBatchAdapter(client=client)

    adapter.submit([make_call("c1")], "idem-key-meta")

    assert client.batches.created[0]["metadata"] == {"idempotency_key": "idem-key-meta"}


def test_submit_uses_configured_provider_name_in_batch_ref():
    """The adapters map is keyed by the configured provider name (``selfhost``),
    and resume looks adapters up via BatchRef.provider."""
    client = FakeOpenAIClient(batch_id="batch_sh")
    adapter = OpenAIBatchAdapter(client=client, provider_name="selfhost")

    ref = adapter.submit([make_call("c1")], "k")
    assert ref.provider == "selfhost"


# --- find_batch ---------------------------------------------------------------


def test_find_batch_matches_metadata_idempotency_key():
    client = FakeOpenAIClient(
        listed=[
            SimpleNamespace(id="batch_other", metadata={"idempotency_key": "other-key"}),
            SimpleNamespace(id="batch_nometa"),  # metadata absent entirely
            SimpleNamespace(id="batch_target", metadata={"idempotency_key": "wanted-key"}),
        ]
    )
    adapter = OpenAIBatchAdapter(client=client)

    ref = adapter.find_batch("wanted-key")

    assert ref == BatchRef(
        provider="openai-batch", batch_id="batch_target", idempotency_key="wanted-key"
    )
    assert client.batches.list_calls == [{"limit": 100}]


def test_find_batch_returns_none_when_no_match():
    client = FakeOpenAIClient(
        listed=[SimpleNamespace(id="batch_x", metadata={"idempotency_key": "some-key"})]
    )
    adapter = OpenAIBatchAdapter(client=client)
    assert adapter.find_batch("missing-key") is None


def test_find_batch_scan_is_bounded():
    listed = [SimpleNamespace(id=f"batch_{i}", metadata={"idempotency_key": "nope"}) for i in range(500)]
    listed.append(SimpleNamespace(id="batch_deep", metadata={"idempotency_key": "wanted"}))
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient(listed=listed))
    # beyond the 200-item bound -- an orphaned batch is always near the head.
    assert adapter.find_batch("wanted") is None


# --- poll ---------------------------------------------------------------------


@pytest.mark.parametrize("status", ["validating", "in_progress", "finalizing"])
def test_poll_in_progress_statuses_are_not_terminal(status):
    batch = fake_openai_batch(status=status, total=10, completed=4, failed=1)
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient(batch=batch))

    result = adapter.poll(BatchRef(provider="openai-batch", batch_id="batch_1"))

    assert result.batch_status == status
    assert result.completed == 4
    assert result.errored == 1
    assert result.expired == 0
    assert result.processing == 5
    assert result.is_terminal is False


def test_poll_completed_is_terminal_with_counts():
    batch = fake_openai_batch(status="completed", total=8, completed=7, failed=1)
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient(batch=batch))

    result = adapter.poll(BatchRef(provider="openai-batch", batch_id="batch_1"))

    assert result.batch_status == "completed"
    assert result.completed == 7
    assert result.errored == 1
    assert result.processing == 0
    assert result.is_terminal is True
    assert result.total == 8


def test_poll_expired_books_the_remainder_as_expired():
    """OpenAI's request_counts has no ``expired`` field -- an expired batch's
    unfinished remainder is inferred from total - completed - failed."""
    batch = fake_openai_batch(status="expired", total=10, completed=6, failed=1)
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient(batch=batch))

    result = adapter.poll(BatchRef(provider="openai-batch", batch_id="batch_1"))

    assert result.expired == 3
    assert result.errored == 1
    assert result.completed == 6
    assert result.is_terminal is True


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_poll_cancelled_or_failed_folds_the_remainder_into_errored(status):
    """§7.6/§10: ItemStatus has no CANCELED, so the lost remainder counts as
    errored -- same fold as the Anthropic adapter."""
    batch = fake_openai_batch(status=status, total=10, completed=3, failed=1)
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient(batch=batch))

    result = adapter.poll(BatchRef(provider="openai-batch", batch_id="batch_1"))

    assert result.errored == 7  # 1 failed + 6 never finished
    assert result.completed == 3
    assert result.expired == 0
    assert result.is_terminal is True


def test_poll_unknown_status_raises_fatal():
    batch = fake_openai_batch(status="teleporting", total=1)
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient(batch=batch))
    with pytest.raises(FatalError, match="unknown batch status"):
        adapter.poll(BatchRef(provider="openai-batch", batch_id="batch_1"))


# --- fetch: completed / http-error / errored / expired matrix -----------------


def _output_line(custom_id: str, *, status_code: int = 200, body: dict | None = None) -> str:
    return json.dumps(
        {
            "id": f"resp_{custom_id}",
            "custom_id": custom_id,
            "response": {
                "status_code": status_code,
                "request_id": "req_1",
                "body": body if body is not None else {"choices": [{"message": {"content": "done"}}]},
            },
            "error": None,
        }
    )


def _error_line(custom_id: str, *, code: str, message: str = "boom") -> str:
    return json.dumps(
        {
            "id": f"resp_{custom_id}",
            "custom_id": custom_id,
            "response": None,
            "error": {"code": code, "message": message},
        }
    )


def test_fetch_maps_full_result_matrix():
    client = FakeOpenAIClient(
        batch=fake_openai_batch(
            status="expired",
            total=4,
            completed=1,
            failed=2,
            output_file_id="file_out",
            error_file_id="file_err",
        ),
        files_content={
            "file_out": "\n".join(
                [
                    _output_line("c1"),
                    _output_line("c2", status_code=400, body={"error": {"type": "invalid_request"}}),
                ]
            )
            + "\n",
            "file_err": "\n".join(
                [
                    _error_line("c3", code="server_error"),
                    _error_line("c4", code="batch_expired"),
                ]
            )
            + "\n",
        },
    )
    adapter = OpenAIBatchAdapter(client=client)

    items = list(adapter.fetch(BatchRef(provider="openai-batch", batch_id="batch_1")))
    by_id = {r.custom_id: r for r in items}
    assert len(items) == 4

    # 1. output line, 2xx -> COMPLETED with the response body as payload.
    assert by_id["c1"].status == ItemStatus.COMPLETED
    assert by_id["c1"].payload == {"choices": [{"message": {"content": "done"}}]}
    assert by_id["c1"].error is None

    # 2. output line, non-2xx -> ERRORED.
    assert by_id["c2"].status == ItemStatus.ERRORED
    assert by_id["c2"].error["status_code"] == 400
    assert by_id["c2"].payload is None

    # 3. error-file line -> ERRORED, carrying the provider error dict.
    assert by_id["c3"].status == ItemStatus.ERRORED
    assert by_id["c3"].error["code"] == "server_error"

    # 4. error-file line with code batch_expired -> EXPIRED, not ERRORED.
    assert by_id["c4"].status == ItemStatus.EXPIRED
    assert by_id["c4"].payload is None
    assert by_id["c4"].error is None


def test_fetch_without_error_file_yields_only_output_lines():
    client = FakeOpenAIClient(
        batch=fake_openai_batch(status="completed", total=1, completed=1, output_file_id="file_out"),
        files_content={"file_out": _output_line("c1") + "\n"},
    )
    adapter = OpenAIBatchAdapter(client=client)

    items = list(adapter.fetch(BatchRef(provider="openai-batch", batch_id="batch_1")))
    assert [r.status for r in items] == [ItemStatus.COMPLETED]
    assert client.files.content_calls == ["file_out"]


def test_fetch_with_no_files_at_all_yields_nothing():
    client = FakeOpenAIClient(batch=fake_openai_batch(status="failed", total=0))
    adapter = OpenAIBatchAdapter(client=client)
    assert list(adapter.fetch(BatchRef(provider="openai-batch", batch_id="batch_1"))) == []


def test_fetch_expired_partial_batch():
    """Appendix A/§7.6: a batch that hit the 24h window returns the completed
    subset plus one batch_expired line per unfinished request."""
    client = FakeOpenAIClient(
        batch=fake_openai_batch(
            status="expired",
            total=3,
            completed=1,
            output_file_id="file_out",
            error_file_id="file_err",
        ),
        files_content={
            "file_out": _output_line("c1") + "\n",
            "file_err": _error_line("c2", code="batch_expired")
            + "\n"
            + _error_line("c3", code="batch_expired")
            + "\n",
        },
    )
    adapter = OpenAIBatchAdapter(client=client)

    statuses = [r.status for r in adapter.fetch(BatchRef(provider="openai-batch", batch_id="b"))]
    assert statuses == [ItemStatus.COMPLETED, ItemStatus.EXPIRED, ItemStatus.EXPIRED]


def test_fetch_reads_binary_response_content():
    """The real SDK returns HttpxBinaryResponseContent, not a str."""

    class _BinaryContent:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

    client = FakeOpenAIClient(
        batch=fake_openai_batch(status="completed", total=1, completed=1, output_file_id="file_out"),
        files_content={"file_out": _BinaryContent((_output_line("c1") + "\n").encode())},
    )
    adapter = OpenAIBatchAdapter(client=client)

    items = list(adapter.fetch(BatchRef(provider="openai-batch", batch_id="batch_1")))
    assert [r.custom_id for r in items] == ["c1"]


def test_fetch_malformed_jsonl_raises_fatal():
    client = FakeOpenAIClient(
        batch=fake_openai_batch(status="completed", total=1, completed=1, output_file_id="file_out"),
        files_content={"file_out": "{not json\n"},
    )
    adapter = OpenAIBatchAdapter(client=client)
    with pytest.raises(FatalError, match="malformed JSONL"):
        list(adapter.fetch(BatchRef(provider="openai-batch", batch_id="batch_1")))


def test_fetch_line_without_custom_id_raises_fatal():
    client = FakeOpenAIClient(
        batch=fake_openai_batch(status="completed", total=1, completed=1, output_file_id="file_out"),
        files_content={"file_out": json.dumps({"response": {"status_code": 200}}) + "\n"},
    )
    adapter = OpenAIBatchAdapter(client=client)
    with pytest.raises(FatalError, match="custom_id"):
        list(adapter.fetch(BatchRef(provider="openai-batch", batch_id="batch_1")))


# --- cancel -------------------------------------------------------------------


def test_cancel_calls_client_with_batch_id():
    client = FakeOpenAIClient()
    adapter = OpenAIBatchAdapter(client=client)

    adapter.cancel(BatchRef(provider="openai-batch", batch_id="batch_1"))
    assert client.batches.cancelled == ["batch_1"]


# --- error mapping ------------------------------------------------------------


def _openai_status_error(status_code: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://gateway.local/v1/batches")
    response = httpx.Response(status_code, request=request, headers={"retry-after": "7"})
    error_cls = {
        429: openai.RateLimitError,
        500: openai.InternalServerError,
        400: openai.BadRequestError,
    }[status_code]
    return error_cls("boom", response=response, body=None)


@pytest.mark.parametrize(
    ("status_code", "expected_type"),
    [(429, RateLimited), (500, RetryableError), (400, FatalError)],
)
def test_submit_maps_api_status_errors(status_code, expected_type):
    error = _openai_status_error(status_code)
    client = FakeOpenAIClient(files_create_error=error)
    adapter = OpenAIBatchAdapter(client=client)

    with pytest.raises(expected_type) as excinfo:
        adapter.submit([make_call("c1")], "idem-key-4")
    assert excinfo.value.__cause__ is error
    if status_code == 429:
        assert excinfo.value.retry_after == 7.0


def test_submit_maps_connection_error():
    error = openai.APIConnectionError(request=httpx.Request("POST", "https://gateway.local/v1/files"))
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient(files_create_error=error))

    with pytest.raises(RetryableError):
        adapter.submit([make_call("c1")], "idem-key-5")


def test_poll_maps_errors():
    adapter = OpenAIBatchAdapter(
        client=FakeOpenAIClient(retrieve_error=_openai_status_error(500))
    )
    with pytest.raises(RetryableError):
        adapter.poll(BatchRef(provider="openai-batch", batch_id="batch_1"))


def test_find_batch_maps_errors():
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient(list_error=_openai_status_error(400)))
    with pytest.raises(FatalError):
        adapter.find_batch("k")


def test_cancel_maps_errors():
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient(cancel_error=_openai_status_error(429)))
    with pytest.raises(RateLimited):
        adapter.cancel(BatchRef(provider="openai-batch", batch_id="batch_1"))


def test_fetch_maps_file_download_errors():
    client = FakeOpenAIClient(
        batch=fake_openai_batch(status="completed", total=1, completed=1, output_file_id="file_out"),
        files_content_error=_openai_status_error(500),
    )
    adapter = OpenAIBatchAdapter(client=client)
    with pytest.raises(RetryableError):
        list(adapter.fetch(BatchRef(provider="openai-batch", batch_id="batch_1")))


def test_map_error_passes_through_timeout_as_retryable():
    adapter = OpenAIBatchAdapter(client=FakeOpenAIClient(cancel_error=TimeoutError("slow")))
    with pytest.raises(RetryableError):
        adapter.cancel(BatchRef(provider="openai-batch", batch_id="batch_1"))


# --- construction / from_env --------------------------------------------------


def test_requires_exactly_one_of_client_or_factory():
    with pytest.raises(ValueError):
        OpenAIBatchAdapter()
    with pytest.raises(ValueError):
        OpenAIBatchAdapter(client=FakeOpenAIClient(), client_factory=FakeOpenAIClient)


def test_from_env_reads_base_url_and_api_key_lazily(monkeypatch):
    monkeypatch.setenv("OPENAI_BATCH_BASE_URL", "http://tidal.local:8000/v1")
    monkeypatch.setenv("OPENAI_BATCH_API_KEY", "sk-from-env")
    seen = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    adapter = OpenAIBatchAdapter.from_env()
    assert seen == {}  # nothing read at construction time
    adapter._client  # noqa: B018, SLF001 -- triggers the lazy factory
    assert seen == {"api_key": "sk-from-env", "base_url": "http://tidal.local:8000/v1"}


def test_from_env_defaults_api_key_for_a_self_hosted_gateway(monkeypatch):
    """A self-hosted gateway usually ignores auth, so an unset key is not an
    error here (it is for AnthropicBatchAdapter.from_env)."""
    monkeypatch.delenv("OPENAI_BATCH_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BATCH_BASE_URL", raising=False)
    seen = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    OpenAIBatchAdapter.from_env()._client  # noqa: B018, SLF001
    assert seen == {"api_key": "tidal-dev-key"}
    assert "base_url" not in seen  # SDK default applies


def test_from_env_explicit_base_url_beats_env(monkeypatch):
    monkeypatch.setenv("OPENAI_BATCH_BASE_URL", "http://from-env/v1")
    seen = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    OpenAIBatchAdapter.from_env(base_url="http://explicit/v1")._client  # noqa: B018, SLF001
    assert seen["base_url"] == "http://explicit/v1"


def test_satisfies_the_batch_adapter_protocol():
    from lazycode.providers.base import BatchAdapter

    assert isinstance(OpenAIBatchAdapter(client=FakeOpenAIClient()), BatchAdapter)
