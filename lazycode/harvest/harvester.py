"""Front-loading harvester (DESIGN.md §6): gather a node's declared context
locally and for free, before the node is ever submitted to a batch API.

``harvest()`` reads a :class:`~lazycode.ir.ContextSpec` and resolves it into a
:class:`HarvestResult`: the repo-map prefix block (§6 item 1, via
``harvest.repo_map``), whole target files (§6 item 2 — M0 has no LSP slicing,
Appendix B11), and house rules (§6 item 3 — lint config / CONTRIBUTING /
CLAUDE.md / AGENTS.md, when present).

M0 scope only: no LSP, no task-specific harvests (coverage XML, failing-test
output) beyond whatever a caller stuffs into ``ContextSpec.extras`` — those are
M1+ recipes and are intentionally not resolved here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lazycode.ir import ContextSpec, PrefixBlock

from .repo_map import build_repo_map

# Candidate house-rule files, in deterministic priority/inclusion order.
# Only the ones that actually exist in the repo are included.
_HOUSE_RULE_FILES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    ".editorconfig",
    "pyproject.toml",
    "setup.cfg",
    ".flake8",
    "ruff.toml",
    ".ruff.toml",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    ".eslintrc.js",
    ".prettierrc",
    ".prettierrc.json",
)

_TRUNCATION_MARKER = "\n\n... [truncated: {shown} of {total} bytes shown]\n"

# Reserved file_blocks key used to record that the tail of the resolved file
# list was omitted entirely because the overall byte budget was exhausted.
OMITTED_FILES_KEY = "__omitted__"


class HarvestError(Exception):
    """Raised when a :class:`~lazycode.ir.ContextSpec` cannot be resolved
    deterministically (e.g. a ``{binding}`` placeholder with no matching key)."""


@dataclass
class HarvestResult:
    """Everything the harvester gathered for one node, ready to render into a
    prompt (DESIGN.md §6)."""

    prefix_blocks: list[PrefixBlock] = field(default_factory=list)
    file_blocks: dict[str, str] = field(default_factory=dict)
    house_rules: str | None = None


def _truncate(text: str, budget: int) -> str:
    """Cap ``text`` at ``budget`` bytes (UTF-8), appending an explicit marker
    when truncation actually happened."""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    marker = _TRUNCATION_MARKER.format(shown=budget, total=len(encoded))
    room = max(budget - len(marker.encode("utf-8")), 0)
    shown = encoded[:room].decode("utf-8", errors="ignore")
    return shown + marker


def _resolve_template(pattern: str, bindings: dict[str, str] | None) -> str:
    """Resolve ``{binding}`` placeholders in a context-spec file pattern.

    Patterns without ``{`` pass through untouched (the common case). A pattern
    that *does* contain a placeholder but has no matching binding is a hard
    error — silently leaving ``{module}`` in a glob would just match nothing,
    which would hide a planner/bindings bug rather than surface it.
    """
    if "{" not in pattern:
        return pattern
    bindings = bindings or {}
    try:
        return pattern.format(**bindings)
    except KeyError as exc:
        raise HarvestError(
            f"context_spec file pattern {pattern!r} references binding {exc.args[0]!r} "
            f"which is not present in bindings {sorted(bindings)}"
        ) from exc


def _resolve_files(
    context_spec: ContextSpec, repo_root: Path, bindings: dict[str, str] | None
) -> list[str]:
    """Resolve ``context_spec.files`` globs (post template substitution) to a
    deterministic, deduplicated, sorted list of repo-relative paths."""
    resolved: set[str] = set()
    for raw_pattern in context_spec.files:
        pattern = _resolve_template(raw_pattern, bindings)
        for match in repo_root.glob(pattern):
            if match.is_file():
                resolved.add(match.relative_to(repo_root).as_posix())
    return sorted(resolved)


def _collect_house_rules(repo_root: Path, budget: int) -> str | None:
    sections: list[str] = []
    for name in _HOUSE_RULE_FILES:
        path = repo_root / name
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sections.append(f"### {name}\n{content}")
    if not sections:
        return None
    return _truncate("\n\n".join(sections), budget)


def harvest(
    context_spec: ContextSpec,
    repo_root: Path | str,
    *,
    bindings: dict[str, str] | None = None,
    repo_map_budget: int = 8_000,
    per_file_budget: int = 60_000,
    total_file_budget: int = 400_000,
    house_rules_budget: int = 20_000,
) -> HarvestResult:
    """Resolve a :class:`~lazycode.ir.ContextSpec` into a :class:`HarvestResult`.

    Deterministic given the same repo state: file resolution is glob + sort,
    the repo map is byte-identical for byte-identical inputs (mtime-cached),
    and house rules are gathered in a fixed priority order.

    ``bindings`` resolves ``{binding}``-templated paths in ``context_spec.files``
    (e.g. a fan-out child's ``{module}`` binding — DESIGN.md §3.2).
    """
    repo_root = Path(repo_root)

    prefix_blocks: list[PrefixBlock] = []
    if context_spec.repo_map:
        repo_map_text = build_repo_map(repo_root, byte_budget=repo_map_budget)
        prefix_blocks.append(PrefixBlock(text=repo_map_text, cache_hint=True))

    file_blocks: dict[str, str] = {}
    relpaths = _resolve_files(context_spec, repo_root, bindings)
    running_total = 0
    for i, relpath in enumerate(relpaths):
        content = (repo_root / relpath).read_text(encoding="utf-8", errors="replace")
        content = _truncate(content, per_file_budget)
        remaining = total_file_budget - running_total
        if remaining <= 0:
            file_blocks[OMITTED_FILES_KEY] = "\n".join(
                f"# [omitted — total file byte budget exhausted]: {p}" for p in relpaths[i:]
            )
            break
        content = _truncate(content, remaining)
        file_blocks[relpath] = content
        running_total += len(content.encode("utf-8"))

    house_rules = _collect_house_rules(repo_root, house_rules_budget) if context_spec.house_rules else None

    return HarvestResult(prefix_blocks=prefix_blocks, file_blocks=file_blocks, house_rules=house_rules)
