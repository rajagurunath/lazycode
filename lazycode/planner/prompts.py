"""The planning system prompt (DESIGN.md §3, Appendix B3, B11).

The operator algebra shown to the planner is **generated from the frozen ``ir``
models**, never hand-duplicated — so the prompt can't drift from the schema the
structured-output call actually validates against (:func:`operator_algebra_docs`
walks each operator's pydantic ``model_fields``). :func:`planning_system_prompt`
wraps that with the planner's role, the M0 constraints (Appendix B11), and the
assumption-ledger instruction (§6).
"""

from __future__ import annotations

from lazycode.ir.operators import (
    Decompose,
    Edit,
    Explore,
    Gate,
    Generate,
    Judge,
    NodeBase,
    Reduce,
    Verify,
)

# Operators in the order they appear in the §3.1 algebra table.
_OPERATORS = (Explore, Decompose, Generate, Edit, Verify, Judge, Reduce, Gate)

# Envelope fields documented once (shared by every operator via NodeBase) rather
# than repeated per-operator.
_ENVELOPE_FIELDS = set(NodeBase.model_fields)


def _field_type_name(annotation: object) -> str:
    """A short, human-readable name for a pydantic field annotation."""
    name = getattr(annotation, "__name__", None)
    if name:
        return name
    # e.g. list[str], str | None, ContextSpec — str() is the most faithful.
    return str(annotation).replace("typing.", "")


def _operator_doc(model: type[NodeBase]) -> str:
    """One operator's line-by-line field doc, generated from ``model_fields``."""
    op_name = model.model_fields["op"].default
    required: list[str] = []
    optional: list[str] = []
    for fname, field in model.model_fields.items():
        if fname == "op" or fname in _ENVELOPE_FIELDS:
            continue
        type_name = _field_type_name(field.annotation)
        entry = f"{fname}: {type_name}"
        if field.is_required():
            required.append(entry)
        else:
            optional.append(f"{entry} (optional)")
    summary = (model.__doc__ or "").strip().splitlines()[0] if model.__doc__ else ""
    lines = [f"- {op_name}: {summary}"]
    for entry in required:
        lines.append(f"    required  {entry}")
    for entry in optional:
        lines.append(f"    optional  {entry}")
    return "\n".join(lines)


def operator_algebra_docs() -> str:
    """The full operator algebra, generated from the ``ir`` operator models.

    Every operator carries the shared node envelope: ``id`` (unique), ``deps``
    (list of upstream node ids; ``"<id>.*"`` matches a fan-out parent's
    children), plus optional ``difficulty_hint``/``budget_hint`` and the fan-out
    fields ``template``/``template_parent_id``/``bindings``.
    """
    parts = [
        "Every node shares this envelope: id (unique str), deps (list[str] of "
        "upstream node ids; a dep '<id>.*' depends on all fan-out children of "
        "<id>), and optional difficulty_hint/budget_hint. Fan-out template nodes "
        "set template=true.",
        "",
        "Operators:",
    ]
    parts.extend(_operator_doc(op) for op in _OPERATORS)
    return "\n".join(parts)


_ROLE = """\
You are the lazycode planner. You translate one engineering goal into a logical
plan: a typed DAG over a closed operator algebra (a "relational algebra" for
coding work). You do NOT write code or diffs yourself — you emit a plan that the
lazycode engine executes on batch APIs. Structure the work as a shallow, WIDE
DAG (few dependency layers, much fan-out) because wall-clock is proportional to
DAG depth: split independent work (per-file, per-module) into sibling nodes at
the same layer instead of chaining it."""

_M0_CONSTRAINTS = """\
M0 constraints (follow exactly):
- Do NOT emit Gate nodes. Human approval is handled outside the plan by a
  pre-flight y/N confirm; a Gate node will be rejected.
- Prefer Explore with prefer_local=true for anything answerable by local tooling
  (ripgrep, symbol outline, coverage, git) — it runs free and instantly.
- Every Generate and Edit node MUST carry both a context_spec (the files/repo_map
  the harvester should gather) and an output_contract. For code changes use a
  diff contract ({"type": "diff", "files_within": [<globs the node may touch>]}).
- Express unbounded fan-out as a single template node (template=true) whose
  context_spec/spec use {placeholder} bindings; its cardinality is resolved at
  runtime from the upstream node's output.
- Keep the DAG shallow: maximize sibling fan-out, minimize dependency depth."""

_ASSUMPTION_LEDGER = """\
Record in the plan's top-level `assumptions` every judgment call you make that a
human might want to revisit (test framework, directory conventions, scope
boundaries). Batch nodes cannot ask questions mid-run, so surfacing assumptions
up front is how the work stays autonomous."""


def planning_system_prompt() -> str:
    """Assemble the full planning system prompt (role + algebra + M0 rules +
    assumption ledger)."""
    return "\n\n".join(
        [
            _ROLE,
            operator_algebra_docs(),
            _M0_CONSTRAINTS,
            _ASSUMPTION_LEDGER,
            "Call the emit_plan tool with the complete plan as its argument.",
        ]
    )
