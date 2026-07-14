"""Tests for the operator algebra and Plan validation (ir/operators.py)."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from lazycode.ir import (
    ContextSpec,
    DiffContract,
    Edit,
    Explore,
    Generate,
    Operator,
    Plan,
    Verify,
)

_operator_adapter: TypeAdapter[Operator] = TypeAdapter(Operator)


def _generate(node_id: str, deps: list[str] | None = None, **kw) -> Generate:
    return Generate(
        id=node_id,
        deps=deps or [],
        spec="write tests",
        context_spec=ContextSpec(files=["{module}"], repo_map=True),
        output_contract=DiffContract(files_within=["src/**"]),
        **kw,
    )


def test_discriminated_union_dispatches_on_op():
    node = _operator_adapter.validate_python(
        {
            "op": "Explore",
            "id": "n1",
            "question": "which functions lack coverage",
            "scope": ["src/billing/**"],
        }
    )
    assert isinstance(node, Explore)
    assert node.prefer_local is True  # default


def test_plan_happy_path_from_spec_example():
    plan = Plan(
        goal="raise coverage of src/billing to 90%",
        assumptions=["tests use pytest"],
        nodes=[
            Explore(id="n1", question="uncovered fns", scope=["src/billing/**"]),
            _generate("n3", deps=["n1"]),
            Verify(id="n4", deps=["n3"], checks=[DiffContract(files_within=["src/**"])]),
        ],
    )
    assert plan.schema_version == 1
    assert [n.id for n in plan.nodes] == ["n1", "n3", "n4"]


def test_extra_field_is_rejected():
    with pytest.raises(ValidationError):
        Explore(id="n1", question="q", scope=["a"], bogus_field=123)


def test_generate_requires_context_and_contract():
    with pytest.raises(ValidationError):
        Generate(id="n1", spec="x")  # missing context_spec + output_contract


def test_duplicate_node_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate node ids"):
        Plan(
            goal="g",
            nodes=[
                Explore(id="n1", question="q", scope=["a"]),
                Explore(id="n1", question="q2", scope=["b"]),
            ],
        )


def test_dangling_dep_rejected():
    with pytest.raises(ValidationError, match="references no existing node id"):
        Plan(goal="g", nodes=[_generate("n2", deps=["does_not_exist"])])


def test_template_pattern_dep_literal_id():
    """A dep 'n3.*' matching a literal template node id 'n3.*' resolves."""
    plan = Plan(
        goal="g",
        nodes=[
            Explore(id="n2", question="q", scope=["a"]),
            _generate("n3.*", deps=["n2"], template=True),
            Edit(
                id="n4",
                deps=["n3.*"],
                files=["conftest.py"],
                instruction="dedupe",
                context_spec=ContextSpec(),
                output_contract=DiffContract(files_within=["**"]),
            ),
        ],
    )
    assert plan.nodes[1].template is True


def test_template_pattern_dep_base_id():
    """A dep 'n3.*' resolves against a base template node whose id is 'n3'."""
    plan = Plan(
        goal="g",
        nodes=[
            _generate("n3", template=True),
            Verify(id="n4", deps=["n3.*"], checks=[DiffContract(files_within=["**"])]),
        ],
    )
    assert plan.nodes[1].deps == ["n3.*"]


def test_self_cycle_rejected():
    with pytest.raises(ValidationError, match="cycle"):
        Plan(goal="g", nodes=[_generate("n1", deps=["n1"])])


def test_two_node_cycle_rejected():
    with pytest.raises(ValidationError, match="cycle"):
        Plan(
            goal="g",
            nodes=[
                _generate("a", deps=["b"]),
                _generate("b", deps=["a"]),
            ],
        )


def test_longer_cycle_rejected():
    with pytest.raises(ValidationError, match="cycle"):
        Plan(
            goal="g",
            nodes=[
                _generate("a", deps=["c"]),
                _generate("b", deps=["a"]),
                _generate("c", deps=["b"]),
            ],
        )


def test_diamond_dag_is_acyclic():
    plan = Plan(
        goal="g",
        nodes=[
            Explore(id="a", question="q", scope=["x"]),
            _generate("b", deps=["a"]),
            _generate("c", deps=["a"]),
            Verify(id="d", deps=["b", "c"], checks=[DiffContract(files_within=["**"])]),
        ],
    )
    assert len(plan.nodes) == 4


def test_bindings_and_template_provenance_roundtrip():
    child = _generate(
        "n3.0",
        deps=["n2"],
        template_parent_id="n3.*",
        bindings={"module": "src/billing/tax.py"},
    )
    dumped = child.model_dump()
    assert dumped["template_parent_id"] == "n3.*"
    assert dumped["bindings"]["module"] == "src/billing/tax.py"
