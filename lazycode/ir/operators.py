"""The logical-plan operator algebra and :class:`Plan` (DESIGN.md §3, Appendix B3).

A logical plan is a typed DAG over a *closed* 8-operator algebra (§3.1). The
planner's structured-output target is generated from these models, which is what
makes the optimizer's rewrite rules possible — you cannot rewrite free-form
prose (§3).

Each operator is a pydantic model carrying a ``Literal`` ``op`` discriminator;
:data:`Operator` is the discriminated union. ``model_config = extra="forbid"``
is load-bearing: an unknown field is a *validation error* so the planner retries
against the schema (Appendix B3).

Field sourcing:

* Per-operator required/optional fields are pinned by Appendix B3.
* The common node envelope (§3.1) — ``id``, ``deps``, ``difficulty_hint``,
  ``budget_hint``, ``template``, ``template_parent_id``, ``bindings`` — lives on
  :class:`NodeBase` and is shared by every operator. ``context_spec`` and
  ``output_contract`` are added only to the operators that carry them
  ("where applicable": required on :class:`Generate` and :class:`Edit`).

Resolved ambiguities (spec silent, minimal choices documented):
  - ``difficulty_hint`` typed as ``str | None`` (free-form, e.g. "easy"/"hard");
    ``budget_hint`` as ``float | None`` (advisory token/dollar hint). B3 lists
    them only as optional on Generate/Edit; we make them optional envelope
    fields on all operators (a harmless superset; §3.1 calls them universal).
  - ``deps`` defaults to ``[]`` so source nodes (Explore) need not declare it.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context_spec import ContextSpec
from .contracts import OutputContract


class NodeBase(BaseModel):
    """Common node-envelope fields shared by every operator (§3.1)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    deps: list[str] = Field(default_factory=list)
    difficulty_hint: str | None = None
    budget_hint: float | None = None
    # Fan-out (§3.2): unresolved template node + resolved-child provenance.
    template: bool = False
    template_parent_id: str | None = None
    bindings: dict[str, Any] | None = None


class Explore(NodeBase):
    """``Explore(question, scope) -> KnowledgeArtifact`` (§3.1).

    Local-first (harvester); LLM only if judgment is needed. Source node — no
    deps required.
    """

    op: Literal["Explore"] = "Explore"
    question: str
    scope: list[str]
    prefer_local: bool = True


class Decompose(NodeBase):
    """``Decompose(goal, context) -> SubPlan`` (§3.1). Grows the DAG at runtime."""

    op: Literal["Decompose"] = "Decompose"
    goal: str
    fanout_hint: str | None = None


class Generate(NodeBase):
    """``Generate(spec, context) -> CodeArtifact`` (§3.1). Batch LLM."""

    op: Literal["Generate"] = "Generate"
    spec: str
    context_spec: ContextSpec
    output_contract: OutputContract


class Edit(NodeBase):
    """``Edit(files, instruction, context) -> Diff`` (§3.1). File-scoped batch LLM."""

    op: Literal["Edit"] = "Edit"
    files: list[str]
    instruction: str
    context_spec: ContextSpec
    output_contract: OutputContract


class Verify(NodeBase):
    """``Verify(artifact, checks) -> Report`` (§3.1). First-class node, local."""

    op: Literal["Verify"] = "Verify"
    checks: list[OutputContract]


class Judge(NodeBase):
    """``Judge(candidates, rubric) -> Selection`` (§3.1). Picks among candidates."""

    op: Literal["Judge"] = "Judge"
    candidates: list[str]  # node ids of the speculative siblings
    rubric: str


class Reduce(NodeBase):
    """``Reduce(artifacts, instruction) -> MergedArtifact`` (§3.1)."""

    op: Literal["Reduce"] = "Reduce"
    instruction: str


class Gate(NodeBase):
    """``Gate(policy) -> Approval`` (§3.1). Executable approval node.

    M0 does not execute Gate as a DAG node (Appendix B11); the pre-flight CLI
    y/N confirm covers approval. The model exists so plans validate.
    """

    op: Literal["Gate"] = "Gate"
    policy: Literal["human-review", "auto"]


Operator = Annotated[
    Explore | Decompose | Generate | Edit | Verify | Judge | Reduce | Gate,
    Field(discriminator="op"),
]
"""Discriminated union of the 8 logical operators, keyed on ``op``."""


def _dep_target(dep: str, ids: set[str]) -> str | None:
    """Resolve a dep string to an existing node id, or ``None`` if unresolvable.

    Accepts an exact id, or a template pattern ``"<base>.*"`` whose ``<base>``
    (or the literal ``"<base>.*"`` node) exists (§3.2 fan-out patterns).
    """
    if dep in ids:
        return dep
    if dep.endswith(".*") and dep[:-2] in ids:
        return dep[:-2]
    return None


class Plan(BaseModel):
    """A logical plan: goal + assumptions + a validated operator DAG (§3.2)."""

    model_config = ConfigDict(extra="forbid")

    goal: str
    assumptions: list[str] = Field(default_factory=list)
    schema_version: Literal[1] = 1
    nodes: list[Operator] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_dag(self) -> Plan:
        ids = [n.id for n in self.nodes]

        # 1. Unique node ids.
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate node ids: {dupes}")
        id_set = set(ids)

        # 2. Every dep resolves to an existing id or template pattern.
        edges: dict[str, list[str]] = {}
        for node in self.nodes:
            targets: list[str] = []
            for dep in node.deps:
                target = _dep_target(dep, id_set)
                if target is None:
                    raise ValueError(
                        f"node {node.id!r} dep {dep!r} references no existing node id "
                        f"(expected an existing id or a '<id>.*' template pattern)"
                    )
                targets.append(target)
            edges[node.id] = targets

        # 3. DAG acyclicity (iterative DFS with a color map).
        WHITE, GREY, BLACK = 0, 1, 2
        color = dict.fromkeys(id_set, WHITE)
        for start in id_set:
            if color[start] != WHITE:
                continue
            stack: list[tuple[str, int]] = [(start, 0)]
            while stack:
                node_id, idx = stack.pop()
                if idx == 0:
                    color[node_id] = GREY
                nbrs = edges[node_id]
                if idx < len(nbrs):
                    stack.append((node_id, idx + 1))
                    nxt = nbrs[idx]
                    if color[nxt] == GREY:
                        raise ValueError(f"dependency cycle detected involving {nxt!r}")
                    if color[nxt] == WHITE:
                        stack.append((nxt, 0))
                else:
                    color[node_id] = BLACK
        return self
