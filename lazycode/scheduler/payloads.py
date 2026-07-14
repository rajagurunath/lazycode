"""Parsing helpers over provider response payloads (DESIGN.md §7.2, §6).

An Anthropic message payload (what :class:`~lazycode.ir.ItemResult.payload`
carries for a completed item) is ``{"content": [ {type: text|tool_use, …} ],
"usage": {...}}``. These pure helpers pull out the assistant text, the unified
diff, the assumption-ledger section, and token usage — shared by the
orchestrator (contract validation + apply) and the report writer (B8).
"""

from __future__ import annotations

import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:diff|patch)?\s*\n(.*?)\n```", re.DOTALL)
_ASSUMPTIONS_RE = re.compile(r"^\s*assumptions\s*:?\s*$", re.IGNORECASE)


def extract_text(payload: dict[str, Any] | None) -> str:
    """Concatenate the text blocks of an Anthropic message payload."""
    if not payload:
        return ""
    parts: list[str] = []
    for block in payload.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def payload_usage(payload: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """``(input_tokens, output_tokens)`` from a payload's ``usage``, or ``(None, None)``."""
    if not payload:
        return None, None
    usage = payload.get("usage") or {}
    return usage.get("input_tokens"), usage.get("output_tokens")


def _split_assumptions(text: str) -> tuple[str, str | None]:
    """Split ``text`` at a standalone ``Assumptions:`` heading.

    Returns ``(body_before, assumptions_after)`` where ``assumptions_after`` is
    ``None`` if no heading is present.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _ASSUMPTIONS_RE.match(line):
            body = "\n".join(lines[:i]).rstrip()
            after = "\n".join(lines[i + 1 :]).strip()
            return body, (after or None)
    return text, None


def extract_assumptions(text: str) -> str | None:
    """The assumption-ledger section of a node response (§6), or ``None``."""
    _, assumptions = _split_assumptions(text)
    return assumptions


def extract_diff(text: str) -> str:
    """Extract the unified diff from a node's response text.

    Prefers a ```diff/```patch fenced block; otherwise drops any trailing
    ``Assumptions:`` section and returns the remaining body. The result is what
    the DiffContract path/parse checks and ``git apply`` receive.
    """
    fenced = _FENCE_RE.search(text)
    if fenced:
        return fenced.group(1).strip() + "\n"
    body, _ = _split_assumptions(text)
    return body.strip()
