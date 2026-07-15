"""Applying returned diffs into a worktree (DESIGN.md §9).

``apply_diff`` is a single apply, using ``git apply --3way`` so that diffs
computed against a stale ``base_commit`` can still land via a real three-way
merge against whatever the worktree's file blob actually is. **Applies are the
caller's to serialize** — this module does not lock or queue; DESIGN.md §9 is
explicit that the scheduler applies returned diffs one at a time per worktree,
in deterministic (topological, then node-id) order, and that side-effect
idempotency lives in the ``applied_diffs`` ledger (§9, §11) — not here. This
module has no store/event-log dependency by design (constraints: pure local
tooling).

``validate_diff_paths`` enforces :class:`~lazycode.ir.DiffContract`'s
``files_within`` allow-list and must be called *before* ``apply_diff`` (the
contract is a pre-condition on the diff, not something to discover mid-apply).

Resolved ambiguity — check-then-apply semantics: a plain ``git apply --check``
(no ``--3way``) rejects patches that only succeed via a genuine three-way merge
(verified empirically — see the ``--3way``-requiring test), so gating on it
first would break exactly the case DESIGN.md asks this module to handle. We
therefore run ``git apply --check --3way`` as a cheap pre-flight (catches
garbage patches without touching the tree) and treat the real
``git apply --3way`` as authoritative: on failure the worktree is rolled back
to its pre-apply state (git leaves conflict markers + an unmerged index entry
behind otherwise) before raising :class:`DiffConflict`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import posixpath
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .manager import Worktree


class DiffConflict(Exception):
    """``git apply --3way`` failed; carries the raw stderr for the report."""

    def __init__(self, stderr: str) -> None:
        self.stderr = stderr
        super().__init__(f"git apply --3way failed:\n{stderr}")


class DiffPathViolation(Exception):
    """A diff touches a path outside its :class:`~lazycode.ir.DiffContract`
    ``files_within`` allow-list (checked before any apply is attempted)."""

    def __init__(self, path: str, files_within: list[str]) -> None:
        self.path = path
        self.files_within = files_within
        super().__init__(
            f"diff touches {path!r}, which is not within the allowed globs {files_within!r}"
        )


@dataclass(frozen=True)
class AppliedDiff:
    """Result of a successful :func:`apply_diff`."""

    diff_hash: str
    files: list[str]


def normalize_diff(diff_text: str) -> str:
    """Normalize line endings and ensure a trailing newline, so the hash is
    stable regardless of how the diff text was transported."""
    text = diff_text.replace("\r\n", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def compute_diff_hash(diff_text: str) -> str:
    """``diff_hash = sha256(normalized diff)`` — the applied-diff ledger key (§9)."""
    return hashlib.sha256(normalize_diff(diff_text).encode("utf-8")).hexdigest()


def extract_diff_paths(diff_text: str) -> list[str]:
    """Repo-relative paths touched by a unified diff, in first-seen order.

    Parses ``+++``/``---`` headers (stripping the standard ``a/``/``b/``
    prefixes ``git diff`` emits) and ignores ``/dev/null`` (pure add/delete).
    """
    paths: list[str] = []
    seen: set[str] = set()
    for line in diff_text.splitlines():
        if not (line.startswith("+++ ") or line.startswith("--- ")):
            continue
        raw = line[4:].strip()
        # Strip an optional git-style timestamp/tab suffix.
        raw = raw.split("\t", 1)[0].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        if raw not in seen:
            seen.add(raw)
            paths.append(raw)
    return paths


def validate_diff_paths(diff_text: str, files_within: list[str]) -> None:
    """Enforce :class:`~lazycode.ir.DiffContract` path allow-listing.

    Must be called before :func:`apply_diff`. Raises :class:`DiffPathViolation`
    on the first offending path (deterministic: paths are checked in
    first-seen diff order).

    Defense-in-depth ahead of git's own backstop (review F8): absolute paths
    and any path whose *normalized* form escapes the worktree (contains a
    ``..`` segment after ``posixpath.normpath``) are rejected BEFORE the
    allow-list globbing — ``fnmatch``'s ``*`` matches ``/`` and ``..``, so
    globbing alone is not a worktree boundary.
    """
    for path in extract_diff_paths(diff_text):
        normalized = posixpath.normpath(path)
        if path.startswith("/") or ".." in normalized.split("/"):
            raise DiffPathViolation(path, files_within)
        if not any(fnmatch.fnmatch(path, pattern) for pattern in files_within):
            raise DiffPathViolation(path, files_within)


def _rollback(worktree_path: Path, paths: list[str]) -> None:
    """Best-effort restore of ``paths`` to their pre-apply (HEAD) state after a
    failed ``git apply --3way`` leaves conflict markers / an unmerged index."""
    subprocess.run(["git", "reset", "-q", "--", *paths], cwd=worktree_path, capture_output=True, text=True)
    for path in paths:
        tracked = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{path}"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        if tracked.returncode == 0:
            subprocess.run(
                ["git", "checkout", "--", path], cwd=worktree_path, capture_output=True, text=True
            )
        else:
            # Newly-added file with no HEAD blob: drop it instead of checkout.
            (worktree_path / path).unlink(missing_ok=True)


def apply_diff(worktree: Worktree, diff_text: str) -> AppliedDiff:
    """Apply ``diff_text`` into ``worktree`` via ``git apply --3way``.

    Runs ``git apply --check --3way`` first as a cheap, tree-untouched sanity
    check, then the real ``git apply --3way``. On any failure the worktree is
    rolled back and :class:`DiffConflict` is raised with the failing stderr —
    the caller (scheduler) decides what to do next (§9: spawn an integration
    Repair node), this module never guesses a resolution.
    """
    normalized = normalize_diff(diff_text)
    worktree_path = Path(worktree.path)

    check = subprocess.run(
        ["git", "apply", "--check", "--3way"],
        cwd=worktree_path,
        input=normalized,
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        raise DiffConflict(check.stderr)

    result = subprocess.run(
        ["git", "apply", "--3way"],
        cwd=worktree_path,
        input=normalized,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        files = extract_diff_paths(normalized)
        _rollback(worktree_path, files)
        raise DiffConflict(result.stdout + result.stderr)

    return AppliedDiff(diff_hash=compute_diff_hash(diff_text), files=extract_diff_paths(normalized))
