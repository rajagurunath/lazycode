"""Sandbox & delivery model (DESIGN.md §9): worktree lifecycle + serialized diff
applies. Pure local tooling (git + stdlib subprocess) — no store/event-log
imports; the applied-diff ledger and apply-serialization ownership live in
``scheduler``/``store`` (this module documents but doesn't enforce that split).
"""

from __future__ import annotations

from .apply import (
    AppliedDiff,
    DiffConflict,
    DiffPathViolation,
    apply_diff,
    compute_diff_hash,
    extract_diff_paths,
    normalize_diff,
    validate_diff_paths,
)
from .manager import (
    MergeConflict,
    Worktree,
    WorktreeError,
    create_group_worktree,
    create_integration_worktree,
    remove_worktree,
)

__all__ = [
    # manager
    "Worktree",
    "WorktreeError",
    "MergeConflict",
    "create_group_worktree",
    "create_integration_worktree",
    "remove_worktree",
    # apply
    "AppliedDiff",
    "DiffConflict",
    "DiffPathViolation",
    "apply_diff",
    "validate_diff_paths",
    "compute_diff_hash",
    "extract_diff_paths",
    "normalize_diff",
]
