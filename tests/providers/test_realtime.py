"""Tests for the Anthropic realtime adapter (providers/realtime.py) -- the M0
planner adapter. No live API calls: the client is a hand-built fake.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lazycode.ir import ItemStatus, ToolDef
from lazycode.providers.base import AdapterError
from lazycode.providers.realtime import AnthropicRealtimeAdapter

from .conftest import fake_message, make_call


def test_complete_happy_path_maps_to_item_result():
    seen_kwargs = []

    def _create(**kwargs):
        seen_kwargs.append(kwargs)
        return fake_message(content_text="planned")

    client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    adapter = AnthropicRealtimeAdapter(client=client)

    call = make_call("planner-1")
    result = adapter.complete(call)

    assert result.custom_id == "planner-1"
    assert result.status == ItemStatus.COMPLETED
    assert result.payload["content"][0]["text"] == "planned"
    # base message params passed straight through, no tool_choice by default.
    assert seen_kwargs[0]["model"] == "claude-haiku-4-5"
    assert "tool_choice" not in seen_kwargs[0]


def test_complete_forces_tool_choice_for_structured_output():
    """§6/module brief: planner forces JSON via a tool + tool_choice kwarg,
    since RenderedCall has no tool_choice field."""
    seen_kwargs = []

    def _create(**kwargs):
        seen_kwargs.append(kwargs)
        return fake_message()

    client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    adapter = AnthropicRealtimeAdapter(client=client)

    call = make_call(
        "planner-1",
        tools=[ToolDef(name="emit_plan", description="", input_schema={"type": "object"})],
    )
    adapter.complete(call, tool_choice={"type": "tool", "name": "emit_plan"})

    assert seen_kwargs[0]["tool_choice"] == {"type": "tool", "name": "emit_plan"}
    assert seen_kwargs[0]["tools"] == [
        {"name": "emit_plan", "description": "", "input_schema": {"type": "object"}}
    ]


def test_complete_passes_through_extra_kwargs():
    seen_kwargs = []

    def _create(**kwargs):
        seen_kwargs.append(kwargs)
        return fake_message()

    client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    adapter = AnthropicRealtimeAdapter(client=client)

    adapter.complete(make_call(), output_config={"effort": "low"})
    assert seen_kwargs[0]["output_config"] == {"effort": "low"}


def test_complete_maps_sdk_errors_to_adapter_error():
    def _create(**kwargs):  # noqa: ARG001
        raise RuntimeError("network blip")

    client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    adapter = AnthropicRealtimeAdapter(client=client)

    with pytest.raises(AdapterError):
        adapter.complete(make_call())


def test_requires_exactly_one_of_client_or_factory():
    with pytest.raises(ValueError):
        AnthropicRealtimeAdapter()
    with pytest.raises(ValueError):
        AnthropicRealtimeAdapter(client=SimpleNamespace(), client_factory=lambda: SimpleNamespace())


def test_from_env_missing_key_raises_lazily(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    adapter = AnthropicRealtimeAdapter.from_env()
    with pytest.raises(AdapterError, match="ANTHROPIC_API_KEY"):
        adapter._client  # noqa: SLF001
