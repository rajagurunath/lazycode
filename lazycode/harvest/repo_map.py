"""Deterministic repo symbol outline — the shared prefix block (DESIGN.md §6 item 1).

``build_repo_map`` walks the repo and produces a compact text block: for ``.py``
files a stdlib-``ast`` outline (classes, functions, signatures, first docstring
line); for every other file, just its path. Output is deterministic (files are
always visited in sorted-path order) and byte-budgeted: when the full outline
would exceed the budget, the *largest* file entries are demoted to a path-only
line first (breadth — the file list — is preserved as long as possible), and
only once every entry is demoted does the budget algorithm start dropping files
outright, with an explicit truncation marker either way.

An in-memory cache keyed by ``(path, mtime)`` avoids re-parsing unchanged files
across repeated harvests within one process (§6: "cached, incrementally
updated").
"""

from __future__ import annotations

import ast
from pathlib import Path

# Directories never worth including in a repo map.
_EXCLUDED_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".lazycode",
    "dist",
    "build",
    ".tox",
}

_TRUNCATION_HEADER = "# [repo map truncated to fit byte budget]"

# (relpath, mtime) -> the file's full (untrimmed) outline entry text.
_ENTRY_CACHE: dict[tuple[str, float], str] = {}


def _is_excluded(relparts: tuple[str, ...]) -> bool:
    """True if any path component before the filename should be skipped."""
    for part in relparts[:-1]:
        if part in _EXCLUDED_DIR_NAMES or (part.startswith(".") and part != "."):
            return True
    return False


def _iter_repo_files(repo_root: Path) -> list[Path]:
    """All regular files under ``repo_root``, sorted by relative path."""
    files: list[Path] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        relparts = path.relative_to(repo_root).parts
        if _is_excluded(relparts):
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.relative_to(repo_root).as_posix())


def _func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = ast.unparse(node.args)
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    line = f"{prefix} {node.name}({args}){returns}:"
    doc = ast.get_docstring(node)
    if doc:
        first_line = doc.strip().splitlines()[0]
        line += f"  # {first_line}"
    return line


def _class_outline(node: ast.ClassDef) -> list[str]:
    bases = ", ".join(ast.unparse(b) for b in node.bases)
    header = f"class {node.name}({bases}):" if bases else f"class {node.name}:"
    lines = [header]
    doc = ast.get_docstring(node)
    if doc:
        lines.append(f"    # {doc.strip().splitlines()[0]}")
    for sub in node.body:
        if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef):
            lines.append("    " + _func_signature(sub))
    return lines


def _python_outline(source: str) -> str | None:
    """Return the symbol outline for one Python source file, or ``None`` if it
    has no top-level symbols worth listing (or fails to parse)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            lines.append(_func_signature(node))
        elif isinstance(node, ast.ClassDef):
            lines.extend(_class_outline(node))
    return "\n".join(lines) if lines else None


def _entry_for_file(repo_root: Path, path: Path) -> str:
    """The full (untrimmed) repo-map entry for one file, mtime-cached."""
    relpath = path.relative_to(repo_root).as_posix()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0
    key = (relpath, mtime)
    cached = _ENTRY_CACHE.get(key)
    if cached is not None:
        return cached

    if path.suffix == ".py":
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            entry = relpath
        else:
            outline = _python_outline(source)
            entry = f"### {relpath}\n{outline}" if outline else relpath
    else:
        entry = relpath

    _ENTRY_CACHE[key] = entry
    return entry


def clear_cache() -> None:
    """Drop the module-level (path, mtime) entry cache. Mainly for tests."""
    _ENTRY_CACHE.clear()


def build_repo_map(repo_root: Path | str, byte_budget: int = 8_000) -> str:
    """Build the deterministic repo-map text block, capped at ``byte_budget`` bytes.

    Ordering is always sorted-path, so the same repo state produces byte-identical
    output. When the full outline exceeds the budget, entries are demoted from
    "full outline" to "path only" starting with the *largest* entries first
    (breadth-first truncation — keep every file listed as long as possible); if
    that still doesn't fit, trailing files (in sorted-path order) are dropped
    entirely and an explicit truncation marker is appended.
    """
    repo_root = Path(repo_root)
    files = _iter_repo_files(repo_root)

    relpaths = [f.relative_to(repo_root).as_posix() for f in files]
    full_entries = [_entry_for_file(repo_root, f) for f in files]
    summary_entries = list(relpaths)  # path-only fallback per file

    active = list(full_entries)

    def _total(entries: list[str]) -> int:
        # +1 per entry for the blank-line separator joining them.
        return sum(len(e) + 1 for e in entries)

    truncated = False
    if _total(active) > byte_budget:
        truncated = True
        # Demote largest full entries to their path-only summary first.
        order = sorted(
            range(len(active)),
            key=lambda i: (-len(full_entries[i]), relpaths[i]),
        )
        for i in order:
            if _total(active) <= byte_budget:
                break
            active[i] = summary_entries[i]

    dropped = 0
    if _total(active) > byte_budget:
        # Still over budget with every entry at its minimal (path-only) size:
        # drop trailing files outright, in sorted-path order.
        kept: list[str] = []
        running = 0
        for entry in active:
            cost = len(entry) + 1
            if running + cost > byte_budget:
                break
            kept.append(entry)
            running += cost
        dropped = len(active) - len(kept)
        active = kept

    lines: list[str] = []
    if truncated:
        lines.append(_TRUNCATION_HEADER)
    lines.extend(active)
    if dropped:
        lines.append(f"# [... {dropped} more file(s) omitted — byte budget exceeded]")

    return "\n\n".join(lines)
