from __future__ import annotations

from pathlib import Path

import pytest

from lazycode.ir import (
    ContextSpec,
    DiffContract,
    Generate,
    ItemResult,
    ItemStatus,
    RenderedCall,
)
from lazycode.planner import PlanningError, propose_plan, resolve_fanout


class ScriptedRealtime:
    """Realtime adapter returning a scripted sequence of emit_plan tool inputs.

    Each entry is the ``input`` dict for the emit_plan tool_use block (or None to
    return no tool call).
    """

    def __init__(self, scripted_inputs: list[dict | None]) -> None:
        self._scripted = list(scripted_inputs)
        self.calls: list[RenderedCall] = []

    def complete(self, call: RenderedCall, **kwargs) -> ItemResult:
        self.calls.append(call)
        payload_input = self._scripted.pop(0)
        content = []
        if payload_input is not None:
            content.append(
                {"type": "tool_use", "name": "emit_plan", "id": "tu1", "input": payload_input}
            )
        return ItemResult(
            custom_id=call.custom_id,
            status=ItemStatus.COMPLETED,
            payload={"content": content, "usage": {"input_tokens": 10, "output_tokens": 5}},
        )


_VALID_PLAN = {
    "goal": "add type hints",
    "assumptions": ["pytest"],
    "schema_version": 1,
    "nodes": [
        {
            "op": "Explore",
            "id": "n1",
            "question": "which modules lack hints",
            "scope": ["src/**"],
        }
    ],
}

# Missing required Generate fields (context_spec/output_contract) → validation error.
_INVALID_PLAN = {
    "goal": "x",
    "nodes": [{"op": "Generate", "id": "bad", "spec": "do it"}],
}


def test_schema_retry_loop_first_invalid_then_valid(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("x = 1\n")
    realtime = ScriptedRealtime([_INVALID_PLAN, _VALID_PLAN])

    plan = propose_plan("add type hints", str(tmp_path), realtime, "claude-opus-4")

    assert plan.goal == "add type hints"
    assert [n.id for n in plan.nodes] == ["n1"]
    # Two attempts: the second call carried the validation-error feedback.
    assert len(realtime.calls) == 2
    assert "failed schema validation" in realtime.calls[1].messages[-1].content


def test_planning_error_after_exhausting_retries(tmp_path: Path):
    realtime = ScriptedRealtime([_INVALID_PLAN, _INVALID_PLAN, _INVALID_PLAN])
    with pytest.raises(PlanningError):
        propose_plan("x", str(tmp_path), realtime, "claude-opus-4")
    assert len(realtime.calls) == 3


def test_planning_error_when_no_tool_call(tmp_path: Path):
    realtime = ScriptedRealtime([None, None, None])
    with pytest.raises(PlanningError):
        propose_plan("x", str(tmp_path), realtime, "claude-opus-4")


def test_resolve_fanout_mints_indexed_children():
    template = Generate(
        id="n3",
        spec="write tests for {module}",
        template=True,
        deps=["n2"],
        context_spec=ContextSpec(files=["{module}"], repo_map=True),
        output_contract=DiffContract(files_within=["tests/**"]),
    )
    children = resolve_fanout(
        [{"module": "src/a.py"}, {"module": "src/b.py"}], template
    )
    assert [c.id for c in children] == ["n3.0", "n3.1"]
    assert all(not c.template for c in children)
    assert children[0].template_parent_id == "n3"
    assert children[0].bindings == {"module": "src/a.py"}
    assert children[1].bindings == {"module": "src/b.py"}
    # Children inherit the template's upstream deps.
    assert children[0].deps == ["n2"]


def test_resolve_fanout_rejects_non_template():
    node = Generate(
        id="n",
        spec="s",
        context_spec=ContextSpec(files=["a.py"]),
        output_contract=DiffContract(files_within=["a.py"]),
    )
    with pytest.raises(ValueError):
        resolve_fanout([{"x": 1}], node)
