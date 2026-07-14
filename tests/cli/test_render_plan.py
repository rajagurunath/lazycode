from __future__ import annotations

from rich.console import Console

from lazycode.cli.render_plan import (
    NodeSummary,
    PhysicalNodeSummary,
    node_summaries_from_plan,
    physical_summaries_from_assignments,
    render_logical_tree,
    render_physical_tree,
)
from lazycode.ir import ContextSpec, DiffContract, Explore, Generate, Plan, Verify
from lazycode.optimizer import plan_physical
from lazycode.scheduler import SchedulerConfig


def _render(renderable) -> str:
    console = Console(width=100, record=True, force_terminal=False)
    console.print(renderable)
    return console.export_text()


def _sample_plan() -> Plan:
    return Plan(
        goal="raise coverage of src/billing to 90%",
        nodes=[
            Explore(id="n1", question="which functions lack coverage", scope=["src/billing/**"]),
            Generate(
                id="n2",
                spec="write tests for tax.py",
                deps=["n1"],
                context_spec=ContextSpec(files=["src/billing/tax.py"], repo_map=True),
                output_contract=DiffContract(files_within=["tests/**"]),
            ),
            Generate(
                id="n3",
                spec="write tests for invoice.py",
                deps=["n1"],
                context_spec=ContextSpec(files=["src/billing/invoice.py"], repo_map=True),
                output_contract=DiffContract(files_within=["tests/**"]),
            ),
            Verify(id="n4", checks=[], deps=["n2", "n3"]),
        ],
    )


def test_logical_tree_shows_op_id_deps():
    plan = _sample_plan()
    tree = render_logical_tree(plan.goal, node_summaries_from_plan(plan))
    text = _render(tree)

    assert "raise coverage of src/billing to 90%" in text
    assert "Explore(n1)" in text
    assert "deps: —" in text  # n1 has no deps
    assert "Generate(n2)" in text
    assert "deps: n1" in text
    assert "Verify(n4)" in text
    assert "deps: n2, n3" in text


def test_logical_tree_is_stable_across_renders():
    plan = _sample_plan()
    summaries = node_summaries_from_plan(plan)
    first = _render(render_logical_tree(plan.goal, summaries))
    second = _render(render_logical_tree(plan.goal, summaries))
    assert first == second


def test_node_summaries_from_plan_preserve_order_and_fields():
    plan = _sample_plan()
    summaries = node_summaries_from_plan(plan)
    assert [s.id for s in summaries] == ["n1", "n2", "n3", "n4"]
    assert [s.op for s in summaries] == ["Explore", "Generate", "Generate", "Verify"]
    assert summaries[1].deps == ("n1",)


def test_physical_tree_groups_by_wave_and_shows_counts_provider_model_execclass():
    plan = _sample_plan()
    assignments = plan_physical(plan, SchedulerConfig(provider="anthropic", model="claude-haiku-4-5"))
    nodes_by_id = {s.id: s for s in node_summaries_from_plan(plan)}
    summaries = physical_summaries_from_assignments(assignments, nodes_by_id)
    text = _render(render_physical_tree(summaries))

    assert "Physical Plan" in text
    # wave-0: n1 (Explore) is local-pushdown -> local.
    assert "Wave wave-0" in text
    assert "local" in text
    assert "1× Explore" in text
    # wave-1: the two Generates batch together on (provider, model).
    assert "Wave wave-1" in text
    assert "anthropic/claude-haiku-4-5" in text
    assert "2× Generate" in text
    # wave-2: Verify is local too.
    assert "Wave wave-2" in text
    assert "1× Verify" in text


def test_physical_tree_no_cost_estimates_shown():
    """M0 (Appendix B11): structure only, never a cost/token figure."""
    plan = _sample_plan()
    assignments = plan_physical(plan, SchedulerConfig(provider="anthropic", model="claude-haiku-4-5"))
    nodes_by_id = {s.id: s for s in node_summaries_from_plan(plan)}
    text = _render(render_physical_tree(physical_summaries_from_assignments(assignments, nodes_by_id)))
    for token in ("$", "est ", "ETA", "tokens"):
        assert token not in text


def test_render_from_explicit_summaries_matches_plan_derived():
    """The explain-path (DB-row-derived summaries) and the run-path
    (Plan-derived summaries) must render identically for the same DAG shape."""
    from_plan = [
        NodeSummary(id="n1", op="Explore", deps=()),
        NodeSummary(id="n2", op="Generate", deps=("n1",)),
    ]
    from_db_rows = [
        NodeSummary(id="n1", op="Explore", deps=tuple([])),
        NodeSummary(id="n2", op="Generate", deps=tuple(["n1"])),
    ]
    assert _render(render_logical_tree("g", from_plan)) == _render(render_logical_tree("g", from_db_rows))


def test_physical_tree_handles_unassigned_wave_gracefully():
    """explain on a job whose nodes haven't been assigned a wave yet
    (wave_id NULL in the DB) shouldn't crash -- render_plan callers pass a
    placeholder wave id, so this locks that contract in."""
    summaries = [
        PhysicalNodeSummary(node_id="n1", op="Explore", wave_id="unassigned", exec_class="unknown"),
    ]
    text = _render(render_physical_tree(summaries))
    assert "unassigned" in text
    assert "1× Explore" in text
