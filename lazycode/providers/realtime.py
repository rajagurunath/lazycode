"""Anthropic realtime adapter (DESIGN.md §10) -- the M0 planner adapter.

Implements :class:`~lazycode.providers.base.RealtimeAdapter` via plain
``client.messages.create(...)`` (non-streaming). This is the adapter M0's
planner uses for structured-output planning calls, and later the §7.6 hedge
path and slider-0.

**Forced-JSON tool use:** :class:`~lazycode.ir.RenderedCall` carries ``tools``
but has no ``tool_choice`` field (that field doesn't exist in the frozen ``ir``
schema — see B1). The planner forces structured JSON output the standard
Anthropic way: put a tool with an ``input_schema`` describing the desired JSON
shape in ``call.tools``, and pass ``tool_choice={"type": "tool", "name":
"<that tool's name>"}`` as a keyword argument to :meth:`complete` (not on the
call itself). This mirrors §10's "same ``RenderedCall`` shape" note while
keeping the schema frozen: ``tool_choice`` is call-site policy (how *this*
completion should behave), not part of the harvested/memoized request shape.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lazycode.ir import ItemResult, ItemStatus, RenderedCall

from .anthropic_batch import _map_error, build_message_params
from .base import AdapterError


class AnthropicRealtimeAdapter:
    """§10 ``RealtimeAdapter`` implementation using ``client.messages.create``.

    Same client-injection contract as :class:`~lazycode.providers.anthropic_batch.AnthropicBatchAdapter`:
    pass either an already-constructed client, or a zero-arg ``client_factory``.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if client is None and client_factory is None:
            raise ValueError("AnthropicRealtimeAdapter requires either client or client_factory")
        if client is not None and client_factory is not None:
            raise ValueError("pass only one of client / client_factory")
        self._client_value = client
        self._client_factory = client_factory

    @classmethod
    def from_env(cls, *, api_key_env: str = "ANTHROPIC_API_KEY") -> AnthropicRealtimeAdapter:
        def _make_client() -> Any:
            import os

            import anthropic

            api_key = os.environ.get(api_key_env)
            if not api_key:
                raise AdapterError(f"environment variable {api_key_env!r} is not set")
            return anthropic.Anthropic(api_key=api_key)

        return cls(client_factory=_make_client)

    @property
    def _client(self) -> Any:
        if self._client_value is None:
            self._client_value = self._client_factory()  # type: ignore[misc]
        return self._client_value

    def complete(
        self,
        call: RenderedCall,
        *,
        tool_choice: dict[str, Any] | None = None,
        **extra: Any,
    ) -> ItemResult:
        """Run ``call`` synchronously via ``messages.create``.

        ``tool_choice`` forces a specific tool (see module docstring). Any
        other keyword is passed straight through to ``messages.create`` as an
        additional request field (e.g. ``output_config``) -- an escape hatch
        for callers that need it without widening this adapter's own surface.
        """
        params = build_message_params(call)
        if tool_choice is not None:
            params["tool_choice"] = tool_choice
        params.update(extra)

        try:
            message = self._client.messages.create(**params)
        except Exception as exc:
            raise _map_error(exc) from exc

        return ItemResult(
            custom_id=call.custom_id,
            status=ItemStatus.COMPLETED,
            payload=message.model_dump(mode="json"),
        )
