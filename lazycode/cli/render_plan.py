"""``rich``-based plan-tree rendering for the pre-flight approval gate and
``lazycode explain`` (DESIGN.md §4, §12, Appendix B11).

Two independent renderers, deliberately decoupled from where the data comes
from:

* :func:`render_logical_tree` takes plain :class:`NodeSummary` rows (``op,
  id, deps``) — built either straight from a freshly-proposed
  :class:`~lazycode.ir.Plan` (:func:`node_summaries_from_plan`, the pre-flight
  path) or from the ``nodes`` projection table columns (the ``explain`` path,
  after a job already exists and the in-memory ``Plan`` object is gone).
* :func:`render_physical_tree` takes :class:`WaveSummary` rows (waves × node
  counts × provider/model × exec class) built the same two ways.

**M0 explicitly shows no cost estimates** (Appendix B11 — "no cost
estimates"; the pre-flight mockup in §1 marks the ``$3.80 batch`` line
"M2+"). These renderers therefore never take or display a cost/token figure —
adding one later is a call-site change, not a rewrite of this module.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from rich.tree import Tree

from lazycode.ir import Plan


@dataclass(frozen=True)
class NodeSummary:
    """One logical-plan node, reduced to what the tree needs (op, id, deps)."""

    id: str
    op: str
    deps: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhysicalNodeSummary:
    """One node's physical assignment: which wave, how it runs, on what."""

    node_id: str
    op: str
    wave_id: str
    exec_class: str
    provider: str | None = None
    model: str | None = None


def node_summaries_from_plan(plan: Plan) -> list[NodeSummary]:
    """Build :class:`NodeSummary` rows straight from a proposed logical
    :class:`~lazycode.ir.Plan` (the pre-flight ``run`` path, before any job
    exists)."""
    return [NodeSummary(id=n.id, op=n.op, deps=tuple(n.deps)) for n in plan.nodes]


def render_logical_tree(goal: str, nodes: list[NodeSummary]) -> Tree:
    """Render the logical plan as a ``rich`` :class:`Tree`: one root labeled
    with the goal, one child line per node showing ``Op(id)`` plus its deps.

    A logical plan is a DAG, not a tree (a node may have >1 dependency, and
    fan-out templates give one node many children) — rich's ``Tree`` widget
    has no native multi-parent layout, so this renders a flat list of nodes
    (stable id order) under one root rather than attempting a lossy tree
    projection. Each line spells out its own deps, which is sufficient to
    reconstruct the DAG by eye and matches what B3/§3.1 actually needs shown:
    op, id, deps — not a specific graph layout.
    """
    tree = Tree(f"[bold]Plan[/bold]: {goal}")
    for node in nodes:
        dep_txt = ", ".join(node.deps) if node.deps else "—"
        tree.add(f"[cyan]{node.op}[/cyan]([bold]{node.id}[/bold])  deps: {dep_txt}")
    return tree


def physical_summaries_from_assignments(
    assignments: list, nodes_by_id: dict[str, NodeSummary]
) -> list[PhysicalNodeSummary]:
    """Build :class:`PhysicalNodeSummary` rows from
    :class:`~lazycode.ir.PhysicalNodeAssignment` objects (the optimizer's
    output) plus the logical nodes they assign, for the pre-flight physical
    preview."""
    out = []
    for a in assignments:
        node = nodes_by_id.get(a.node_id)
        op = node.op if node is not None else "?"
        out.append(
            PhysicalNodeSummary(
                node_id=a.node_id,
                op=op,
                wave_id=a.wave_id,
                exec_class=a.exec_class.value if hasattr(a.exec_class, "value") else str(a.exec_class),
                provider=a.provider,
                model=a.model,
            )
        )
    return out


def render_physical_tree(assignments: list[PhysicalNodeSummary]) -> Tree:
    """Render the physical plan Postgres-``explain``-style (§4): one root,
    one child per wave (in wave order), each wave grouping its nodes by
    ``(exec_class, provider, model)`` and showing the op mix + count.

    M0 note (Appendix B11): no cost/token estimates — waves show structure
    only (node counts, provider/model, exec class).
    """
    tree = Tree("[bold]Physical Plan[/bold]")

    by_wave: dict[str, list[PhysicalNodeSummary]] = defaultdict(list)
    for a in assignments:
        by_wave[a.wave_id].append(a)

    def _wave_sort_key(wave_id: str) -> tuple[int, str]:
        # "wave-N" sorts numerically; anything else falls back to lexical.
        if wave_id.startswith("wave-"):
            suffix = wave_id[len("wave-") :]
            if suffix.isdigit():
                return (int(suffix), wave_id)
        return (10**9, wave_id)

    for wave_id in sorted(by_wave, key=_wave_sort_key):
        wave_nodes = by_wave[wave_id]
        wave_branch = tree.add(f"[bold yellow]Wave {wave_id}[/bold yellow]  ({len(wave_nodes)} node(s))")

        groups: dict[tuple[str, str | None, str | None], list[PhysicalNodeSummary]] = defaultdict(list)
        for n in wave_nodes:
            groups[(n.exec_class, n.provider, n.model)].append(n)

        for (exec_class, provider, model), group_nodes in sorted(groups.items(), key=lambda kv: kv[0][0]):
            where = "free" if exec_class == "local" else f"{provider}/{model}"
            header = f"[green]{exec_class}[/green] · {where}"
            group_branch = wave_branch.add(header)

            op_counts: dict[str, int] = defaultdict(int)
            for n in group_nodes:
                op_counts[n.op] += 1
            for op, count in sorted(op_counts.items()):
                group_branch.add(f"{count}× {op}")

    return tree
