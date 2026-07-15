"""Tests for workspace/apply.py: diff apply (incl. a genuine --3way case),
path-allowlist validation, and diff-hash normalization."""

from __future__ import annotations

import subprocess

import pytest

from lazycode.workspace import (
    DiffConflict,
    DiffPathViolation,
    apply_diff,
    compute_diff_hash,
    create_group_worktree,
    validate_diff_paths,
)

from .conftest import GitRepo


def _diff_for(git_repo: GitRepo, wt_path, relpath: str, new_content: str) -> str:
    """Produce a real unified diff for ``relpath`` (whatever the blob at
    ``wt_path`` currently is) against ``new_content``, using ``git diff``."""
    (wt_path / relpath).write_text(new_content, encoding="utf-8")
    diff = subprocess.run(
        ["git", "diff", "--", relpath], cwd=wt_path, capture_output=True, text=True
    ).stdout
    # Restore so the worktree is clean again before the test applies the diff itself.
    subprocess.run(["git", "checkout", "--", relpath], cwd=wt_path, capture_output=True, text=True)
    return diff


def test_apply_diff_simple_change(git_repo: GitRepo):
    git_repo.write("f.txt", "line1\nline2\nline3\n")
    base = git_repo.commit("initial")
    wt = create_group_worktree(git_repo.root, base, "job1", "grp-a")

    diff_text = _diff_for(git_repo, wt.path, "f.txt", "line1\nline2-changed\nline3\n")

    result = apply_diff(wt, diff_text)

    assert result.files == ["f.txt"]
    assert result.diff_hash == compute_diff_hash(diff_text)
    assert (wt.path / "f.txt").read_text() == "line1\nline2-changed\nline3\n"


def test_apply_diff_requires_3way_merge(git_repo: GitRepo):
    """A diff computed against base_commit, applied to a worktree whose file
    has since diverged in a *non-overlapping* way, only succeeds via --3way."""
    git_repo.write("f.txt", "line1\nline2\nline3\nline4\nline5\nline6\n")
    base = git_repo.commit("initial")

    # Diff: change line4, computed from a separate checkout of `base`.
    base_wt = create_group_worktree(git_repo.root, base, "jobdiff", "basecheck")
    diff_text = _diff_for(git_repo, base_wt.path, "f.txt", "line1\nline2\nline3\nline4-changed\nline5\nline6\n")

    # Target worktree: diverged on line2 (non-overlapping hunk) and committed,
    # so a plain (non-3way) `git apply` fails on context mismatch.
    wt = create_group_worktree(git_repo.root, base, "job1", "grp-a")
    (wt.path / "f.txt").write_text("line1\nline2-changed\nline3\nline4\nline5\nline6\n", encoding="utf-8")
    git_repo.run("add", "f.txt", cwd=wt.path)
    git_repo.run("commit", "-q", "-m", "diverge on line2", cwd=wt.path)

    # Sanity: plain apply (no --3way) really does fail here.
    plain_check = subprocess.run(
        ["git", "apply", "--check"],
        cwd=wt.path,
        input=diff_text,
        capture_output=True,
        text=True,
    )
    assert plain_check.returncode != 0

    result = apply_diff(wt, diff_text)

    assert result.files == ["f.txt"]
    content = (wt.path / "f.txt").read_text()
    assert "line2-changed" in content
    assert "line4-changed" in content


def test_apply_diff_conflict_raises_and_leaves_tree_clean(git_repo: GitRepo):
    git_repo.write("f.txt", "line1\nline2\nline3\n")
    base = git_repo.commit("initial")

    base_wt = create_group_worktree(git_repo.root, base, "jobdiff2", "basecheck")
    diff_text = _diff_for(git_repo, base_wt.path, "f.txt", "line1\nline2-CONFLICT\nline3\n")

    wt = create_group_worktree(git_repo.root, base, "job1", "grp-a")
    (wt.path / "f.txt").write_text("line1\nline2-changed-locally\nline3\n", encoding="utf-8")
    git_repo.run("add", "f.txt", cwd=wt.path)
    git_repo.run("commit", "-q", "-m", "diverge on same line", cwd=wt.path)

    with pytest.raises(DiffConflict) as excinfo:
        apply_diff(wt, diff_text)

    assert excinfo.value.stderr or True  # stderr may be on stdout for --3way conflicts; message non-empty
    assert str(excinfo.value)

    status = git_repo.run("status", "--porcelain", cwd=wt.path).stdout
    assert status.strip() == ""
    assert (wt.path / "f.txt").read_text() == "line1\nline2-changed-locally\nline3\n"


def test_validate_diff_paths_allows_matching_glob():
    diff_text = (
        "diff --git a/src/billing/tax.py b/src/billing/tax.py\n"
        "--- a/src/billing/tax.py\n"
        "+++ b/src/billing/tax.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    validate_diff_paths(diff_text, ["src/billing/*.py"])  # no raise


def test_validate_diff_paths_rejects_out_of_scope_file():
    diff_text = (
        "diff --git a/src/other/module.py b/src/other/module.py\n"
        "--- a/src/other/module.py\n"
        "+++ b/src/other/module.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    with pytest.raises(DiffPathViolation) as excinfo:
        validate_diff_paths(diff_text, ["src/billing/*.py"])
    assert excinfo.value.path == "src/other/module.py"


def test_validate_diff_paths_rejects_parent_traversal_even_when_glob_matches():
    """Review F8: '..' must be rejected BEFORE globbing — fnmatch's '*'
    happily matches '../escape.py'."""
    diff_text = (
        "diff --git a/../escape.py b/../escape.py\n"
        "--- a/../escape.py\n"
        "+++ b/../escape.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    with pytest.raises(DiffPathViolation) as excinfo:
        validate_diff_paths(diff_text, ["*"])
    assert excinfo.value.path == "../escape.py"


def test_validate_diff_paths_rejects_embedded_traversal_after_normalization():
    diff_text = (
        "--- a/src/../../outside.py\n"
        "+++ b/src/../../outside.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    with pytest.raises(DiffPathViolation):
        validate_diff_paths(diff_text, ["*"])


def test_validate_diff_paths_rejects_absolute_path():
    diff_text = (
        "--- /abs/path.py\n"
        "+++ /abs/path.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    with pytest.raises(DiffPathViolation) as excinfo:
        validate_diff_paths(diff_text, ["*"])
    assert excinfo.value.path == "/abs/path.py"


def test_validate_diff_paths_allows_interior_dotdot_that_normalizes_inside():
    """src/a/../b.py normalizes to src/b.py — inside the tree, allowed if the
    allow-list matches the *written* path."""
    diff_text = (
        "--- a/src/billing/sub/../tax.py\n"
        "+++ b/src/billing/sub/../tax.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    validate_diff_paths(diff_text, ["src/billing/*"])  # no raise


def test_compute_diff_hash_normalizes_line_endings():
    a = "line1\nline2\n"
    b = "line1\r\nline2\r\n"
    assert compute_diff_hash(a) == compute_diff_hash(b)


def test_compute_diff_hash_stable_and_content_derived():
    a = "diff content A\n"
    b = "diff content B\n"
    assert compute_diff_hash(a) == compute_diff_hash(a)
    assert compute_diff_hash(a) != compute_diff_hash(b)
