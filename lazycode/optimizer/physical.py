"""M0 physical planning (DESIGN.md §4, §5.3).

:func:`plan_physical` turns a rewritten logical :class:`~lazycode.ir.Plan` into a
list of :class:`~lazycode.ir.PhysicalNodeAssignment` — one per node — by:

1. applying R1 :func:`~lazycode.optimizer.rules.local_pushdown` and marking the
   structurally-local operator (``Verify``) local too;
2. topologically layering the DAG into **minimum-depth waves** (a node's layer is
   ``1 + max(layer(dep))``, sources at layer 0) — the §4 wave-count-minimizing
   assignment;
3. assigning provider + model from the config default (M0 has **no model
   tiering** — that's R5/M2), so within a wave nodes group by ``(provider,
   model)`` at submit time (§5.3).

Local nodes keep the wave of their layer but carry
:attr:`~lazycode.ir.ExecClass.LOCAL`; remote nodes get
:attr:`~lazycode.ir.ExecClass.BATCH`. ``Gate`` nodes are assigned but the M0
scheduler auto-approves them (Appendix B11 — Gate is not executed as a DAG
node); assigning them keeps downstream readiness/layering intact.

Pure function — no I/O.
"""

from __future__ import annotations

from typing import Protocol

from lazycode.ir import ExecClass, PhysicalNodeAssignment, Plan, Verify

from .rules import local_pushdown


class PhysicalConfig(Protocol):
    """Structural config the planner reads (satisfied by
    :class:`~lazycode.scheduler.config.SchedulerConfig`; declared here to avoid a
    scheduler→optimizer→scheduler import cycle)."""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...


def _dep_bases(dep: str) -> str:
    """Normalize a dep string to the id whose layer it depends on.

    A ``"<base>.*"`` fan-out pattern depends on ``<base>`` (the resolving
    parent); an exact id depends on itself (§3.2).
    """
    return dep[:-2] if dep.endswith(".*") else dep


def _layers(plan: Plan) -> dict[str, int]:
    """Minimum-depth topological layer per node id (sources at 0).

    The :class:`~lazycode.ir.Plan` validator has already proven the DAG is
    acyclic and every dep resolves, so a memoized DFS terminates.
    """
    by_id = {n.id: n for n in plan.nodes}
    layer: dict[str, int] = {}

    def resolve(node_id: str) -> int:
        if node_id in layer:
            return layer[node_id]
        node = by_id[node_id]
        deps = [_dep_bases(d) for d in node.deps]
        # Only depend on deps that are real nodes (defensive; validator guarantees it).
        dep_layers = [resolve(d) for d in deps if d in by_id]
        result = 0 if not dep_layers else 1 + max(dep_layers)
        layer[node_id] = result
        return result

    for node in plan.nodes:
        resolve(node.id)
    return layer


def plan_physical(plan: Plan, config: PhysicalConfig) -> list[PhysicalNodeAssignment]:
    """Assign every node a wave + exec class + (provider, model) (§4, §5.3)."""
    local_ids = set(local_pushdown(plan))
    layers = _layers(plan)

    assignments: list[PhysicalNodeAssignment] = []
    for node in plan.nodes:
        if node.id in local_ids or isinstance(node, Verify):
            exec_class = ExecClass.LOCAL
        else:
            exec_class = ExecClass.BATCH
        assignments.append(
            PhysicalNodeAssignment(
                node_id=node.id,
                wave_id=f"wave-{layers[node.id]}",
                exec_class=exec_class,
                provider=config.provider,
                model=config.model,
            )
        )
    return assignments
