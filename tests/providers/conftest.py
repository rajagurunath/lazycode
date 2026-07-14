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
