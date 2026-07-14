from __future__ import annotations

from dataclasses import dataclass

from lazycode.ir import (
    ContextSpec,
    DiffContract,
    Explore,
    ExecClass,
    Generate,
    Plan,
    Reduce,
    Verify,
)
from lazycode.optimizer import plan_physical


@dataclass(frozen=True)
class Cfg:
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5"


def _gen(nid: str, deps: list[str]) -> Generate:
    return Generate(
        id=nid,
        spec="s",
        deps=deps,
        context_spec=ContextSpec(files=[f"{nid}.py"], repo_map=False),
        output_contract=DiffContract(files_within=[f"{nid}.py"]),
    )


def test_minimum_depth_layering_diamond():
    # e -> (g1, g2) -> r  : layers 0,1,1,2
    plan = Plan(
        goal="g",
        nodes=[
            Explore(id="e", question="q", scope=["**"], prefer_local=True),
            _gen("g1", ["e"]),
            _gen("g2", ["e"]),
            Reduce(id="r", instruction="merge", deps=["g1", "g2"]),
        ],
    )
    waves = {a.node_id: a.wave_id for a in plan_physical(plan, Cfg())}
    assert waves["e"] == "wave-0"
    assert waves["g1"] == "wave-1"
    assert waves["g2"] == "wave-1"
    assert waves["r"] == "wave-2"


def test_exec_class_assignment():
    plan = Plan(
        goal="g",
        nodes=[
            Explore(id="e", question="q", scope=["**"], prefer_local=True),
            _gen("g1", ["e"]),
            Verify(id="v", checks=[], deps=["g1"]),
        ],
    )
    by_id = {a.node_id: a for a in plan_physical(plan, Cfg())}
    assert by_id["e"].exec_class is ExecClass.LOCAL  # R1
    assert by_id["v"].exec_class is ExecClass.LOCAL  # structurally local
    assert by_id["g1"].exec_class is ExecClass.BATCH
    # M0: provider/model come from config default (no tiering).
    assert by_id["g1"].provider == "anthropic"
    assert by_id["g1"].model == "claude-haiku-4-5"


def test_grouping_key_is_provider_model():
    plan = Plan(goal="g", nodes=[_gen("g1", []), _gen("g2", [])])
    assigns = plan_physical(plan, Cfg())
    groups = {(a.provider, a.model) for a in assigns}
    assert groups == {("anthropic", "claude-haiku-4-5")}
