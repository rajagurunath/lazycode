"""Git worktree lifecycle (DESIGN.md §9): one worktree per task group, plus a
fresh integration worktree for cross-group ``Reduce``.

All worktrees live under ``<repo_root>/.lazycode/worktrees/`` and are branched
from a pinned ``base_commit`` — never the user's checkout. This module only
creates/removes/merges worktrees; it does not know about the applied-diff
ledger or the scheduler's event log (those own idempotency — §9, §11).

Resolved ambiguity: DESIGN.md's branch pattern is ``lazycode/<job>/<group>``,
which requires a job id the prompt's ``create_group_worktree`` signature didn't
list. We add an explicit ``job_id`` parameter (and mirror it on
``create_integration_worktree``) rather than smuggle it into ``group_id``,
since the branch/path layout needs it unambiguously.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(Exception):
    """A ``git worktree``/``git merge`` invocation failed; carries stderr."""

    def __init__(self, args: list[str], stderr: str) -> None:
        self.args = args
        self.stderr = stderr
        super().__init__(f"`git {' '.join(args)}` failed:\n{stderr}")


class MergeConflict(Exception):
    """Raised by :func:`create_integration_worktree` on a merge conflict.

    Per DESIGN.md §9 the scheduler must not guess a resolution — it spawns an
    integration Repair node instead. The failed merge is aborted (so the
    integration worktree is left clean and reusable) and the full ``git
    merge`` report is attached for that Repair node's context.
    """

    def __init__(self, branch: str, report: str) -> None:
        self.branch = branch
        self.report = report
        super().__init__(f"merge conflict integrating branch {branch!r}:\n{report}")


@dataclass(frozen=True)
class Worktree:
    """A git worktree the scheduler can apply diffs into and verify (§9)."""

    path: Path
    branch: str


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WorktreeError(args, result.stderr)
    return result


def _worktrees_root(repo_root: Path) -> Path:
    return repo_root / ".lazycode" / "worktrees"


def create_group_worktree(
    repo_root: Path | str, base_commit: str, job_id: str, group_id: str
) -> Worktree:
    """Create the dedicated worktree + branch for one task group (§9, §11
    ``task_groups``). Branch: ``lazycode/<job_id>/<group_id>``."""
    repo_root = Path(repo_root)
    branch = f"lazycode/{job_id}/{group_id}"
    worktree_path = _worktrees_root(repo_root) / job_id / group_id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        ["worktree", "add", "-b", branch, str(worktree_path), base_commit],
        cwd=repo_root,
    )
    return Worktree(path=worktree_path, branch=branch)


def remove_worktree(repo_root: Path | str, worktree: Worktree, *, force: bool = False) -> None:
    """Remove a worktree created by this module. Does not delete its branch."""
    repo_root = Path(repo_root)
    args = ["worktree", "remove", str(worktree.path)]
    if force:
        args.append("--force")
    _run_git(args, cwd=repo_root)


def create_integration_worktree(
    repo_root: Path | str,
    base_commit: str,
    job_id: str,
    group_branches: list[str],
) -> Worktree:
    """Create the integration worktree for cross-group ``Reduce`` (§9): branch
    from ``base_commit``, then merge each group branch into it in order.

    Raises :class:`MergeConflict` (aborting the offending merge first) on the
    first branch that doesn't merge cleanly — the caller decides what to do
    (spawn an integration Repair node), this module never guesses a resolution.
    """
    repo_root = Path(repo_root)
    branch = f"lazycode/{job_id}/integration"
    worktree_path = _worktrees_root(repo_root) / job_id / "integration"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        ["worktree", "add", "-b", branch, str(worktree_path), base_commit],
        cwd=repo_root,
    )
    worktree = Worktree(path=worktree_path, branch=branch)

    for group_branch in group_branches:
        result = subprocess.run(
            ["git", "merge", "--no-edit", group_branch],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            report = result.stdout + result.stderr
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            raise MergeConflict(group_branch, report)

    return worktree
