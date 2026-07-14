"""M0 rewrite rules — R1 LocalPushdown and R2 ContextPruning (DESIGN.md §5.2).

Only the two cheapest, always-safe rules ship in M0 (§13: "optimizer/ — M0:
R1/R2 only"). Both are pure functions over a :class:`~lazycode.ir.Plan`:

* :func:`local_pushdown` (R1, predicate pushdown): any ``Explore`` answerable by
  deterministic local tooling (``prefer_local=True``) is executed as a *local*
  node — free and instant, no LLM round (§5.2 R1). Returns the set of node ids
  to run local; :mod:`lazycode.optimizer.physical` combines it with the
  structurally-local operators (``Verify``).
* :func:`context_pruning` (R2, projection pruning): drop the whole-repo map from
  a node's context when the node is *trivially scoped* — a literal, tiny file
  set it can be self-sufficient from — so it ships only the columns it reads
  (§5.2 R2). **Conservative**: when there is any doubt (templated paths, a large
  or empty file set, no explicit contract), the repo map is kept.

These do not mutate their input: :func:`context_pruning` returns a new
:class:`~lazycode.ir.Plan`.
"""

from __future__ import annotations

from lazycode.ir import Edit, ExecClass, Explore, Generate, Plan

# R2: a node touching at most this many *literal* files is "trivially scoped"
# and does not need the repo map. Deliberately small — over-pruning context is
# the failure mode the rule must avoid (§5.2 R2 "when unsure, keep").
_TRIVIAL_FILE_COUNT = 2


def local_pushdown(plan: Plan) -> dict[str, ExecClass]:
    """R1 ``LocalPushdown``: map each ``Explore(prefer_local=True)`` node to
    :attr:`~lazycode.ir.ExecClass.LOCAL` (§5.2 R1).

    Returns only the nodes this rule *changes* (Explore-local); the physical
    planner is responsible for the structurally-local operators (Verify) and
    for defaulting everything else to batch.
    """
    overrides: dict[str, ExecClass] = {}
    for node in plan.nodes:
        if isinstance(node, Explore) and node.prefer_local:
            overrides[node.id] = ExecClass.LOCAL
    return overrides


def _is_trivially_scoped(node: Generate | Edit) -> bool:
    """True when ``node`` can safely drop the repo map (R2, conservative).

    Requires: an explicit, non-empty, fully-literal file set of at most
    :data:`_TRIVIAL_FILE_COUNT` paths, and the node is not an unresolved
    fan-out template (whose real cardinality/paths aren't known yet).
    """
    if node.template:
        return False
    files = node.context_spec.files
    if not files or len(files) > _TRIVIAL_FILE_COUNT:
        return False
    # Any templated placeholder ("{module}") means the real path set is unknown
    # at rule time — keep the map (conservative).
    if any("{" in f for f in files):
        return False
    return True


def context_pruning(plan: Plan) -> Plan:
    """R2 ``ContextPruning``: return a copy of ``plan`` with ``repo_map`` dropped
    from every trivially-scoped ``Generate``/``Edit`` node (§5.2 R2).

    Every other node — and any node where trivial-scope can't be proven — is
    copied through unchanged, so the rewrite never *widens* context and never
    removes a map a node might actually need.
    """
    new_nodes = []
    for node in plan.nodes:
        if (
            isinstance(node, Generate | Edit)
            and node.context_spec.repo_map
            and _is_trivially_scoped(node)
        ):
            pruned_spec = node.context_spec.model_copy(update={"repo_map": False})
            node = node.model_copy(update={"context_spec": pruned_spec})
        new_nodes.append(node)
    return plan.model_copy(update={"nodes": new_nodes})
