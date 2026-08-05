"""Shared helpers for provider adapter tests.

No live API calls anywhere in this package: every test either uses a hand-built
fake client (``types.SimpleNamespace`` / small stub classes standing in for the
``anthropic`` SDK's response objects) or the in-memory mock adapters.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from lazycode.ir import Message, PrefixBlock, RenderedCall, ToolDef


def make_call(
    custom_id: str = "c1",
    *,
    model: str = "claude-haiku-4-5",
    system: list[PrefixBlock] | None = None,
    messages: list[Message] | None = None,
    tools: list[ToolDef] | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    memo_key: str = "unset",
    node_ids: list[str] | None = None,
) -> RenderedCall:
    """Build a minimal, valid :class:`RenderedCall` for tests."""
    return RenderedCall(
        custom_id=custom_id,
        model=model,
        system=system if system is not None else [],
        messages=messages if messages is not None else [Message(role="user", content="hello")],
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
        memo_key=memo_key,
        node_ids=node_ids if node_ids is not None else [],
    )


# --- fake anthropic SDK response objects -------------------------------------


def fake_batch(
    *,
    batch_id: str = "msgbatch_1",
    processing_status: str = "in_progress",
    succeeded: int = 0,
    errored: int = 0,
    expired: int = 0,
    canceled: int = 0,
    processing: int = 0,
) -> SimpleNamespace:
    """Stand-in for ``anthropic.types.messages.MessageBatch``."""
    return SimpleNamespace(
        id=batch_id,
        processing_status=processing_status,
        request_counts=SimpleNamespace(
            succeeded=succeeded,
            errored=errored,
            expired=expired,
            canceled=canceled,
            processing=processing,
        ),
    )


def fake_message(*, content_text: str = "ok") -> SimpleNamespace:
    """Stand-in for ``anthropic.types.Message`` — just needs ``model_dump``."""

    class _FakeMessage(SimpleNamespace):
        def model_dump(self, mode: str = "python") -> dict[str, Any]:  # noqa: ARG002
            return {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": content_text}],
            }

    return _FakeMessage()


def fake_error_response(*, error_type: str = "invalid_request_error") -> SimpleNamespace:
    class _FakeErrorResponse(SimpleNamespace):
        def model_dump(self, mode: str = "python") -> dict[str, Any]:  # noqa: ARG002
            return {"type": "error", "error": {"type": error_type, "message": "bad request"}}

    return _FakeErrorResponse()


def fake_batch_result(custom_id: str, result_type: str, **extra: Any) -> SimpleNamespace:
    """Stand-in for ``MessageBatchIndividualResponse``."""
    if result_type == "succeeded":
        result = SimpleNamespace(type="succeeded", message=extra["message"])
    elif result_type == "errored":
        result = SimpleNamespace(type="errored", error=extra["error"])
    elif result_type == "expired":
        result = SimpleNamespace(type="expired")
    elif result_type == "canceled":
        result = SimpleNamespace(type="canceled")
    else:  # pragma: no cover - test-construction error
        raise ValueError(result_type)
    return SimpleNamespace(custom_id=custom_id, result=result)


# --- fake openai SDK client + response objects --------------------------------


def fake_openai_batch(
    *,
    batch_id: str = "batch_1",
    status: str = "in_progress",
    total: int = 0,
    completed: int = 0,
    failed: int = 0,
    output_file_id: str | None = None,
    error_file_id: str | None = None,
    metadata: dict[str, str] | None = None,
) -> SimpleNamespace:
    """Stand-in for ``openai.types.Batch``.

    Note the counts: the real object's ``request_counts`` is only
    ``{total, completed, failed}`` — there is no ``expired`` counter.
    """
    return SimpleNamespace(
        id=batch_id,
        status=status,
        output_file_id=output_file_id,
        error_file_id=error_file_id,
        metadata=metadata,
        request_counts=SimpleNamespace(total=total, completed=completed, failed=failed),
    )


class _FakeFiles:
    """``client.files`` — records uploads, serves canned file bodies."""

    def __init__(
        self,
        *,
        file_id: str,
        contents: dict[str, Any],
        create_error: Exception | None,
        content_error: Exception | None,
    ) -> None:
        self._file_id = file_id
        self._contents = contents
        self._create_error = create_error
        self._content_error = content_error
        self.created: list[dict[str, Any]] = []
        self.content_calls: list[str] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        if self._create_error is not None:
            raise self._create_error
        self.created.append(kwargs)
        return SimpleNamespace(id=self._file_id)

    def content(self, file_id: str) -> Any:
        if self._content_error is not None:
            raise self._content_error
        self.content_calls.append(file_id)
        return self._contents[file_id]


class _FakeBatches:
    """``client.batches`` — records create/list/cancel, serves a canned batch."""

    def __init__(
        self,
        *,
        batch: SimpleNamespace,
        batch_id: str,
        listed: list[Any],
        create_error: Exception | None,
        retrieve_error: Exception | None,
        list_error: Exception | None,
        cancel_error: Exception | None,
    ) -> None:
        self._batch = batch
        self._batch_id = batch_id
        self._listed = listed
        self._create_error = create_error
        self._retrieve_error = retrieve_error
        self._list_error = list_error
        self._cancel_error = cancel_error
        self.created: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.retrieved: list[str] = []
        self.cancelled: list[str] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        if self._create_error is not None:
            raise self._create_error
        self.created.append(kwargs)
        return SimpleNamespace(id=self._batch_id)

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        if self._retrieve_error is not None:
            raise self._retrieve_error
        self.retrieved.append(batch_id)
        return self._batch

    def list(self, **kwargs: Any) -> list[Any]:
        if self._list_error is not None:
            raise self._list_error
        self.list_calls.append(kwargs)
        return self._listed

    def cancel(self, batch_id: str) -> None:
        if self._cancel_error is not None:
            raise self._cancel_error
        self.cancelled.append(batch_id)


class FakeOpenAIClient:
    """Hand-built stand-in for ``openai.OpenAI``.

    Exposes only the surface ``OpenAIBatchAdapter`` touches — ``files.create``,
    ``files.content``, ``batches.create/retrieve/list/cancel`` — and records
    every call so tests can assert on the exact wire arguments. Any of the
    ``*_error`` kwargs makes that method raise instead, for error-mapping tests.
    """

    def __init__(
        self,
        *,
        file_id: str = "file_1",
        batch_id: str = "batch_1",
        batch: SimpleNamespace | None = None,
        listed: list[Any] | None = None,
        files_content: dict[str, Any] | None = None,
        files_create_error: Exception | None = None,
        files_content_error: Exception | None = None,
        batches_create_error: Exception | None = None,
        retrieve_error: Exception | None = None,
        list_error: Exception | None = None,
        cancel_error: Exception | None = None,
    ) -> None:
        self.files = _FakeFiles(
            file_id=file_id,
            contents=files_content or {},
            create_error=files_create_error,
            content_error=files_content_error,
        )
        self.batches = _FakeBatches(
            batch=batch if batch is not None else fake_openai_batch(batch_id=batch_id),
            batch_id=batch_id,
            listed=listed or [],
            create_error=batches_create_error,
            retrieve_error=retrieve_error,
            list_error=list_error,
            cancel_error=cancel_error,
        )
