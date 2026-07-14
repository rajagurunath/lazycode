from __future__ import annotations

from lazycode.harvest import HarvestResult
from lazycode.ir import ContextSpec, DiffContract, Edit, Generate, PrefixBlock
from lazycode.scheduler import SchedulerConfig, render_node


def _harvest() -> HarvestResult:
    return HarvestResult(
        prefix_blocks=[PrefixBlock(text="REPO MAP", cache_hint=True)],
        file_blocks={"a.py": "x = 1\n"},
        house_rules="use ruff",
    )


def test_render_is_deterministic_and_stamps_memo_key():
    node = Generate(
        id="g1",
        spec="add a docstring",
        context_spec=ContextSpec(files=["a.py"], repo_map=True),
        output_contract=DiffContract(files_within=["a.py"]),
    )
    cfg = SchedulerConfig()
    c1 = render_node(node, _harvest(), cfg)
    c2 = render_node(node, _harvest(), cfg)
    assert c1 == c2
    assert c1.memo_key != "pending" and len(c1.memo_key) == 64
    assert c1.custom_id == "g1"
    assert c1.node_ids == ["g1"]
    # Repo map + house rules land in system; the diff directive + file land in the message.
    system_text = "\n".join(b.text for b in c1.system)
    assert "REPO MAP" in system_text and "use ruff" in system_text
    assert "unified diff" in c1.messages[0].content
    assert "### File: a.py" in c1.messages[0].content


def test_render_substitutes_bindings_in_instruction():
    node = Edit(
        id="e1",
        files=["{module}"],
        instruction="add hints to {module}",
        context_spec=ContextSpec(files=["{module}"]),
        output_contract=DiffContract(files_within=["{module}"]),
    )
    call = render_node(node, HarvestResult(), SchedulerConfig(), bindings={"module": "src/a.py"})
    assert "add hints to src/a.py" in call.messages[0].content
