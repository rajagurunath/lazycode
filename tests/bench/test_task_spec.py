"""Unit tests for ``bench/task_spec.py`` -- task loading, the minimal-YAML
parser, and fixture-repo materialization. No network, no lazycode
execution (see ``test_run_lazycode.py`` for that)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bench import task_spec

_EXPECTED_TASKS = {"add-type-hints", "coverage-a-module", "docstring-pass"}


def test_list_tasks_finds_all_three():
    assert _EXPECTED_TASKS <= set(task_spec.list_tasks())


@pytest.mark.parametrize("name", sorted(_EXPECTED_TASKS))
def test_load_task_has_required_fields(name: str):
    task = task_spec.load_task(name)
    assert task.name == name
    assert task.goal.strip()
    assert task.verify_command.strip()
    assert task.generator_path.is_file()


def test_load_task_unknown_raises():
    with pytest.raises(task_spec.TaskError):
        task_spec.load_task("does-not-exist")


def test_load_task_missing_required_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(task_spec, "TASKS_DIR", tmp_path)
    task_dir = tmp_path / "broken"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text("name: broken\ngoal: do a thing\n", encoding="utf-8")
    with pytest.raises(task_spec.TaskError, match="generator"):
        task_spec.load_task("broken")


def test_load_task_generator_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(task_spec, "TASKS_DIR", tmp_path)
    task_dir = tmp_path / "broken"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(
        "name: broken\ngoal: do a thing\ngenerator: nope.py\nverify_command: true\n", encoding="utf-8"
    )
    with pytest.raises(task_spec.TaskError, match="not found"):
        task_spec.load_task("broken")


def test_folded_scalar_goal_is_joined():
    # add-type-hints/task.yaml uses a `>-` folded goal spanning multiple lines.
    task = task_spec.load_task("add-type-hints")
    assert "\n" not in task.goal
    assert "type hints" in task.goal


@pytest.mark.parametrize("name", sorted(_EXPECTED_TASKS))
def test_build_repo_materializes_a_committed_git_repo(name: str, tmp_path: Path):
    task = task_spec.load_task(name)
    dest = tmp_path / "repo"
    sha = task_spec.build_repo(task, dest)

    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=dest, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().splitlines()) == 1  # exactly one fixture commit

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True, check=True
    )
    assert status.stdout.strip() == ""  # nothing left uncommitted

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True, check=True
    )
    assert head.stdout.strip() == sha


def test_build_repo_add_type_hints_writes_expected_files(tmp_path: Path):
    task = task_spec.load_task("add-type-hints")
    dest = tmp_path / "repo"
    task_spec.build_repo(task, dest)
    for name in ("invoice.py", "ledger.py", "refunds.py"):
        assert (dest / "pkg" / "billing" / name).is_file()
