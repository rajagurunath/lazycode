"""Realtime structured-output planning + fan-out resolution (DESIGN.md §3, §3.2).

:func:`propose_plan` runs a single realtime call whose output is constrained to a
valid :class:`~lazycode.ir.Plan` by an ``emit_plan`` tool carrying
``Plan.model_json_schema()`` as its ``input_schema`` (the standard Anthropic
forced-tool way to get structured JSON — the realtime adapter takes
``tool_choice`` as a keyword). Pydantic validation failures are fed back to the
model and retried up to :data:`_MAX_ATTEMPTS` times before :class:`PlanningError`.

:func:`resolve_fanout` mints the concrete children of a ``template`` node
(§3.2): ``{parent_id}.{index}`` in output order, each carrying its ``bindings``.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from lazycode.harvest import build_repo_map
from lazycode.ir import Message, Operator, Plan, PrefixBlock, RenderedCall, memo_key_for_call
from lazycode.providers.base import RealtimeAdapter

from .prompts import planning_system_prompt

_MAX_ATTEMPTS = 3
_PLAN_TOOL = "emit_plan"


class PlanningError(Exception):
    """The planner could not produce a schema-valid :class:`~lazycode.ir.Plan`
    within :data:`_MAX_ATTEMPTS` attempts (or the model never called the plan
    tool). Carries the last validation error / raw output for diagnosis."""


def _extract_tool_input(payload: dict[str, Any] | None, tool_name: str) -> dict[str, Any] | None:
    """Pull the forced-tool ``input`` dict out of an Anthropic message payload.

    Falls back to ``None`` if the model returned no matching ``tool_use`` block
    (the caller then treats it as an invalid attempt).
    """
    if not payload:
        return None
    for block in payload.get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == tool_name:
            got = block.get("input")
            return got if isinstance(got, dict) else None
    return None


def _render_planning_call(
    *, model: str, system: str, user_messages: list[Message], max_tokens: int
) -> RenderedCall:
    from lazycode.ir import ToolDef

    tool = ToolDef(
        name=_PLAN_TOOL,
        description="Emit the complete logical plan as a typed operator DAG.",
        input_schema=Plan.model_json_schema(),
    )
    call = RenderedCall(
        custom_id="plan",
        model=model,
        system=[PrefixBlock(text=system)],
        messages=user_messages,
        tools=[tool],
        max_tokens=max_tokens,
        temperature=0.0,
        memo_key="pending",
        node_ids=[],
    )
    return call.model_copy(update={"memo_key": memo_key_for_call(call, mode="realtime")})


def propose_plan(
    goal: str,
    repo_root: str,
    realtime: RealtimeAdapter,
    model: str,
    *,
    max_tokens: int = 4096,
) -> Plan:
    """Propose a logical :class:`~lazycode.ir.Plan` for ``goal`` (DESIGN.md §3).

    Builds a repo-map prefix + the goal, forces structured output via the
    ``emit_plan`` tool, and validates the response into a :class:`Plan`. On a
    pydantic :class:`ValidationError` the error text is appended to the
    conversation and the call retried (up to :data:`_MAX_ATTEMPTS`); after that
    :class:`PlanningError` is raised.
    """
    repo_map = build_repo_map(repo_root)
    system = planning_system_prompt()
    messages: list[Message] = [
        Message(role="user", content=f"Goal:\n{goal}\n\nRepository map:\n{repo_map}")
    ]

    last_error: Exception | None = None
    last_raw: Any = None
    for _ in range(_MAX_ATTEMPTS):
        call = _render_planning_call(
            model=model, system=system, user_messages=messages, max_tokens=max_tokens
        )
        result = realtime.complete(call, tool_choice={"type": "tool", "name": _PLAN_TOOL})
        raw = _extract_tool_input(result.payload, _PLAN_TOOL)
        last_raw = raw
        if raw is None:
            messages.append(
                Message(
                    role="user",
                    content=(
                        f"You must call the {_PLAN_TOOL} tool with the plan as its input. "
                        "No valid tool call was found. Try again."
                    ),
                )
            )
            last_error = PlanningError("model did not call the emit_plan tool")
            continue
        try:
            return Plan.model_validate(raw)
        except ValidationError as exc:
            last_error = exc
            messages.append(
                Message(
                    role="user",
                    content=(
                        "The plan failed schema validation with these errors:\n"
                        f"{exc}\n\nFix them and call the emit_plan tool again with a corrected plan."
                    ),
                )
            )

    raise PlanningError(
        f"failed to produce a valid plan after {_MAX_ATTEMPTS} attempts; "
        f"last error: {last_error}; last raw output: {last_raw!r}"
    )


def resolve_fanout(
    decompose_or_explore_output: list[dict[str, Any]],
    template_node: Operator,
) -> list[Operator]:
    """Mint the concrete children of a fan-out ``template`` node (§3.2).

    ``decompose_or_explore_output`` is the list of per-child ``bindings`` dicts
    produced by the upstream ``Decompose``/``Explore`` node (in output order).
    Each child is ``{parent_id}.{index}``, with ``template=False``,
    ``template_parent_id=<parent id>``, its ``bindings`` set, and the parent's
    ``deps`` inherited. Raises ``ValueError`` if ``template_node`` isn't a template.
    """
    if not getattr(template_node, "template", False):
        raise ValueError(f"node {template_node.id!r} is not a fan-out template (template=False)")

    children: list[Operator] = []
    for index, bindings in enumerate(decompose_or_explore_output):
        child_id = f"{template_node.id}.{index}"
        children.append(
            template_node.model_copy(
                update={
                    "id": child_id,
                    "template": False,
                    "template_parent_id": template_node.id,
                    "bindings": dict(bindings),
                }
            )
        )
    return children
