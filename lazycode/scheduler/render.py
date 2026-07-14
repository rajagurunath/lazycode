"""Prompt assembly: one logical node + its harvest → a :class:`RenderedCall`
(DESIGN.md §6, §7.2, Appendix B1).

Separated from the orchestrator so it is deterministic and unit-testable:
:func:`render_node` produces byte-identical output for the same node + harvest +
config, and stamps the R10 memo key via the ``ir`` helper. The prompt is
front-loaded (§6): shared prefix blocks (repo map, house rules) in ``system``,
per-node file blocks + the instruction + the output-contract directive +
the assumption-ledger request in a single ``user`` message.
"""

from __future__ import annotations

from typing import Any

from lazycode.harvest import HarvestResult
from lazycode.ir import (
    DiffContract,
    Edit,
    Generate,
    Message,
    Operator,
    PrefixBlock,
    Reduce,
    RenderedCall,
    memo_key_for_call,
)

from .config import SchedulerConfig

_EXECUTION_ROLE = (
    "You are a lazycode batch executor. You are given a fully harvested, "
    "self-sufficient task: everything you need is in this prompt. Produce the "
    "requested artifact directly. You cannot ask questions — when you must make "
    "a judgment call, make the most reasonable choice and record it under an "
    "'Assumptions:' heading at the end of your response."
)


def _safe_format(text: str, bindings: dict[str, Any] | None) -> str:
    """Format ``{placeholder}`` templates with ``bindings``; leave unknown
    placeholders untouched rather than raising (best-effort per §3.2)."""
    if not bindings or "{" not in text:
        return text
    try:
        return text.format(**bindings)
    except (KeyError, IndexError, ValueError):
        return text


def _instruction_for_node(node: Operator, bindings: dict[str, Any] | None) -> str:
    """The operator-specific instruction body (§3.1)."""
    if isinstance(node, Generate):
        return _safe_format(node.spec, bindings)
    if isinstance(node, Edit):
        files = ", ".join(node.files)
        return f"{_safe_format(node.instruction, bindings)}\n\nFiles to edit: {files}"
    if isinstance(node, Reduce):
        return _safe_format(node.instruction, bindings)
    # Decompose / Judge and any other remote op: use the best available text.
    for attr in ("goal", "rubric", "instruction", "question"):
        value = getattr(node, attr, None)
        if value:
            return _safe_format(str(value), bindings)
    return f"Execute node {node.id} (op={node.op})."


def _contract_directive(node: Operator) -> str | None:
    """The output-contract instruction appended to the prompt (Appendix B4).

    M0 only enforces the diff contract; its directive is load-bearing (the
    scheduler parses the response as a unified diff)."""
    contract = getattr(node, "output_contract", None)
    if isinstance(contract, DiffContract):
        globs = ", ".join(contract.files_within) or "(the files named above)"
        return (
            "Respond with a SINGLE unified diff in git format (--- a/… / +++ b/… / @@ hunks), "
            "and nothing else before it. Only touch files matching: "
            f"{globs}. Do not include prose before the diff; put any 'Assumptions:' notes "
            "AFTER the diff."
        )
    return None


def render_node(
    node: Operator,
    harvest_result: HarvestResult,
    config: SchedulerConfig,
    *,
    model: str | None = None,
    bindings: dict[str, Any] | None = None,
) -> RenderedCall:
    """Assemble the deterministic :class:`RenderedCall` for a remote node.

    ``model`` overrides ``config.model`` (physical planning's per-node model);
    ``bindings`` resolves fan-out ``{placeholder}`` templates in the instruction.
    """
    bindings = bindings if bindings is not None else getattr(node, "bindings", None)

    system: list[PrefixBlock] = [PrefixBlock(text=_EXECUTION_ROLE)]
    system.extend(harvest_result.prefix_blocks)
    if harvest_result.house_rules:
        system.append(
            PrefixBlock(text=f"House rules / project conventions:\n{harvest_result.house_rules}")
        )

    parts: list[str] = [_instruction_for_node(node, bindings)]
    for relpath, content in harvest_result.file_blocks.items():
        parts.append(f"### File: {relpath}\n{content}")
    directive = _contract_directive(node)
    if directive:
        parts.append(directive)
    parts.append(
        "If you made any judgment calls, list them under a final 'Assumptions:' heading."
    )
    messages = [Message(role="user", content="\n\n".join(parts))]

    call = RenderedCall(
        custom_id=node.id,
        model=model or config.model,
        system=system,
        messages=messages,
        tools=None,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        memo_key="pending",
        node_ids=[node.id],
    )
    return call.model_copy(update={"memo_key": memo_key_for_call(call, mode="batch")})
