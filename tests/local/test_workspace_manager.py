"""Tests for workspace/manager.py: worktree create/remove + integration merge."""

from __future__ import annotations

import pytest

from lazycode.workspace import (
    MergeConflict,
    create_group_worktree,
    create_integration_worktree,
    remove_worktree,
)

from .conftest import GitRepo


def test_create_group_worktree_layout(git_repo: GitRepo):
    git_repo.write("README.md", "hello\n")
    base = git_repo.commit("initial")

    wt = create_group_worktree(git_repo.root, base, "job1", "grp-a")

    assert wt.branch == "lazycode/job1/grp-a"
    assert wt.path == git_repo.root / ".lazycode" / "worktrees" / "job1" / "grp-a"
    assert wt.path.is_dir()
    assert (wt.path / "README.md").read_text() == "hello\n"

    # Branch exists and points at base_commit.
    show_ref = git_repo.run("rev-parse", wt.branch)
    assert show_ref.stdout.strip() == base


def test_remove_worktree(git_repo: GitRepo):
    git_repo.write("README.md", "hello\n")
    base = git_repo.commit("initial")
    wt = create_group_worktree(git_repo.root, base, "job1", "grp-a")

    remove_worktree(git_repo.root, wt)

    assert not wt.path.exists()
    listed = git_repo.run("worktree", "list", "--porcelain").stdout
    assert str(wt.path) not in listed


def test_two_group_worktrees_are_independent(git_repo: GitRepo):
    git_repo.write("README.md", "hello\n")
    base = git_repo.commit("initial")

    wt_a = create_group_worktree(git_repo.root, base, "job1", "grp-a")
    wt_b = create_group_worktree(git_repo.root, base, "job1", "grp-b")

    (wt_a.path / "a_only.txt").write_text("a\n", encoding="utf-8")

    assert wt_a.path != wt_b.path
    assert not (wt_b.path / "a_only.txt").exists()


def test_create_integration_worktree_merges_group_branches(git_repo: GitRepo):
    git_repo.write("README.md", "hello\n")
    base = git_repo.commit("initial")

    wt_a = create_group_worktree(git_repo.root, base, "job1", "grp-a")
    (wt_a.path / "a.txt").write_text("from a\n", encoding="utf-8")
    git_repo.run("add", "a.txt", cwd=wt_a.path)
    git_repo.run("commit", "-q", "-m", "add a.txt", cwd=wt_a.path)

    wt_b = create_group_worktree(git_repo.root, base, "job1", "grp-b")
    (wt_b.path / "b.txt").write_text("from b\n", encoding="utf-8")
    git_repo.run("add", "b.txt", cwd=wt_b.path)
    git_repo.run("commit", "-q", "-m", "add b.txt", cwd=wt_b.path)

    integration = create_integration_worktree(
        git_repo.root, base, "job1", [wt_a.branch, wt_b.branch]
    )

    assert (integration.path / "a.txt").read_text() == "from a\n"
    assert (integration.path / "b.txt").read_text() == "from b\n"
    assert integration.branch == "lazycode/job1/integration"


def test_create_integration_worktree_conflict_raises_and_aborts(git_repo: GitRepo):
    git_repo.write("shared.txt", "base\n")
    base = git_repo.commit("initial")

    wt_a = create_group_worktree(git_repo.root, base, "job1", "grp-a")
    (wt_a.path / "shared.txt").write_text("from a\n", encoding="utf-8")
    git_repo.run("add", "shared.txt", cwd=wt_a.path)
    git_repo.run("commit", "-q", "-m", "a changes shared", cwd=wt_a.path)

    wt_b = create_group_worktree(git_repo.root, base, "job1", "grp-b")
    (wt_b.path / "shared.txt").write_text("from b\n", encoding="utf-8")
    git_repo.run("add", "shared.txt", cwd=wt_b.path)
    git_repo.run("commit", "-q", "-m", "b changes shared", cwd=wt_b.path)

    with pytest.raises(MergeConflict) as excinfo:
        create_integration_worktree(git_repo.root, base, "job1", [wt_a.branch, wt_b.branch])

    assert excinfo.value.branch == wt_b.branch
    assert excinfo.value.report

    # The integration worktree must be left in a clean (non-conflicted) state.
    integration_path = git_repo.root / ".lazycode" / "worktrees" / "job1" / "integration"
    status = git_repo.run("status", "--porcelain", cwd=integration_path).stdout
    assert status.strip() == ""
