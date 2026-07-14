from __future__ import annotations

from lazycode.ir import (
    ContextSpec,
    DiffContract,
    Edit,
    ExecClass,
    Explore,
    Generate,
    Plan,
    Verify,
)
from lazycode.optimizer import context_pruning, local_pushdown


def test_r1_local_pushdown_marks_prefer_local_explore():
    plan = Plan(
        goal="g",
        nodes=[
            Explore(id="e1", question="q", scope=["**/*.py"], prefer_local=True),
            Explore(id="e2", question="q", scope=["**/*.py"], prefer_local=False),
            Verify(id="v1", checks=[], deps=["e1"]),
        ],
    )
    overrides = local_pushdown(plan)
    assert overrides == {"e1": ExecClass.LOCAL}
    # Non-local explore and Verify are not R1's concern.
    assert "e2" not in overrides and "v1" not in overrides


def test_r2_context_pruning_drops_map_for_trivial_node():
    plan = Plan(
        goal="g",
        nodes=[
            Generate(
                id="g1",
                spec="tweak",
                context_spec=ContextSpec(files=["a.py"], repo_map=True),
                output_contract=DiffContract(files_within=["a.py"]),
            ),
        ],
    )
    pruned = context_pruning(plan)
    assert pruned.nodes[0].context_spec.repo_map is False
    # Input plan is not mutated.
    assert plan.nodes[0].context_spec.repo_map is True


def test_r2_keeps_map_when_uncertain():
    plan = Plan(
        goal="g",
        nodes=[
            # Many files → not trivially scoped.
            Edit(
                id="wide",
                files=["a.py", "b.py", "c.py"],
                instruction="x",
                context_spec=ContextSpec(files=["a.py", "b.py", "c.py"], repo_map=True),
                output_contract=DiffContract(files_within=["*.py"]),
            ),
            # Templated path → real scope unknown.
            Generate(
                id="tmpl",
                spec="x",
                template=True,
                context_spec=ContextSpec(files=["{module}"], repo_map=True),
                output_contract=DiffContract(files_within=["*.py"]),
            ),
            # No files at all → keep.
            Generate(
                id="broad",
                spec="x",
                context_spec=ContextSpec(files=[], repo_map=True),
                output_contract=DiffContract(files_within=["*.py"]),
            ),
        ],
    )
    pruned = context_pruning(plan)
    for node in pruned.nodes:
        assert node.context_spec.repo_map is True
