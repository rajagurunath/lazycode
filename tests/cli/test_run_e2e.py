from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

import lazycode.cli.app as app_module
from lazycode.providers.mock import MockBatchAdapter

from .conftest import FixedPlanRealtime, FromEnvFactory, GitRepo, completed, diff_response

_JOB_ID_RE = re.compile(r"job-[0-9a-f]{12}")


def _plan_dict() -> dict:
    return {
        "goal": "add a constant to mod_a.py",
        "assumptions": ["mod_a.py uses simple module-level constants"],
        "schema_version": 1,
        "nodes": [
            {
                "op": "Generate",
                "id": "n1",
                "spec": "append a constant A2 = 2 to mod_a.py",
                "deps": [],
                "context_spec": {
                    "files": ["mod_a.py"],
                    "repo_map": False,
                    "house_rules": False,
                    "extras": {},
                },
                "output_contract": {"type": "diff", "files_within": ["mod_a.py"]},
            }
        ],
    }


@pytest.fixture
def runnable_repo(git_repo: GitRepo) -> GitRepo:
    git_repo.write("mod_a.py", "A = 1\n")
    git_repo.commit("init")
    return git_repo


@pytest.fixture(autouse=True)
def _patch_adapters(monkeypatch: pytest.MonkeyPatch, runnable_repo: GitRepo):
    """Monkeypatch adapter construction (module-brief requirement): swap the
    real Anthropic realtime/batch adapters app.py constructs via
    ``Cls.from_env(...)`` for mocks, so the whole CLI path runs with zero
    network I/O."""
    patch_text = runnable_repo.make_patch("mod_a.py", "A = 1\nA2 = 2\n")
    batch_adapter = MockBatchAdapter({"n1": completed("n1", diff_response(patch_text, assumptions="chose A2 name"))})
    realtime_adapter = FixedPlanRealtime(plan_dict=_plan_dict())

    monkeypatch.setattr(app_module, "AnthropicRealtimeAdapter", FromEnvFactory(realtime_adapter))
    monkeypatch.setattr(app_module, "AnthropicBatchAdapter", FromEnvFactory(batch_adapter))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    return batch_adapter, realtime_adapter


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, runnable_repo: GitRepo, global_config_no_ask: Path):
    """Chdir into the repo (CLI resolves repo_root via `git rev-parse
    --show-toplevel` off cwd) and point the global config at
    keep_awake=false so no confirm() prompt blocks the run."""
    monkeypatch.chdir(runnable_repo.root)
    monkeypatch.setenv("LAZYCODE_GLOBAL_CONFIG", str(global_config_no_ask))
    return runnable_repo


def _extract_job_id(output: str) -> str:
    match = _JOB_ID_RE.search(output)
    assert match, f"no job id found in output:\n{output}"
    return match.group(0)


def test_run_yes_end_to_end_produces_report(cli_env: GitRepo):
    runner = CliRunner()
    result = runner.invoke(app_module.app, ["run", "add a constant to mod_a.py", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Plan: add a constant to mod_a.py" in result.output
    assert "Generate(n1)" in result.output
    assert "Done." in result.output
    assert "status=DONE" in result.output
    assert "branch: lazycode/" in result.output
    assert "report: " in result.output

    job_id = _extract_job_id(result.output)
    report_dir = cli_env.root / ".lazycode" / "reports" / job_id
    assert (report_dir / "report.md").exists()
    report_data = json.loads((report_dir / "report.json").read_text())
    assert report_data["job_id"] == job_id
    assert any("chose A2 name" in a["assumption"] for a in report_data["assumptions"])

    # The diff actually landed in the task group's worktree.
    worktree = cli_env.root / ".lazycode" / "worktrees" / job_id / "g0"
    assert (worktree / "mod_a.py").exists()
    assert "A2 = 2" in (worktree / "mod_a.py").read_text()


def test_run_without_yes_declines_creates_no_job(cli_env: GitRepo, monkeypatch: pytest.MonkeyPatch):
    runner = CliRunner()
    # No --yes: the y/N confirm defaults to N when the input is empty.
    result = runner.invoke(app_module.app, ["run", "add a constant to mod_a.py"], input="n\n")

    assert result.exit_code == 0
    assert "Aborted" in result.output
    assert not (cli_env.root / ".lazycode" / "reports").exists()


def test_status_lists_job_after_run(cli_env: GitRepo):
    runner = CliRunner()
    run_result = runner.invoke(app_module.app, ["run", "add a constant to mod_a.py", "--yes"])
    assert run_result.exit_code == 0, run_result.output
    job_id = _extract_job_id(run_result.output)

    table_result = runner.invoke(app_module.app, ["status"])
    assert table_result.exit_code == 0, table_result.output
    assert job_id in table_result.output
    assert "DONE" in table_result.output
    assert "daemon: not running" in table_result.output

    detail_result = runner.invoke(app_module.app, ["status", job_id])
    assert detail_result.exit_code == 0, detail_result.output
    assert "n1" in detail_result.output
    assert "Generate" in detail_result.output


def test_explain_renders_logical_and_physical_trees(cli_env: GitRepo):
    runner = CliRunner()
    run_result = runner.invoke(app_module.app, ["run", "add a constant to mod_a.py", "--yes"])
    assert run_result.exit_code == 0, run_result.output
    job_id = _extract_job_id(run_result.output)

    explain_result = runner.invoke(app_module.app, ["explain", job_id])
    assert explain_result.exit_code == 0, explain_result.output
    assert "Generate(n1)" in explain_result.output
    assert "Physical Plan" in explain_result.output
    assert "anthropic" in explain_result.output


def test_review_shows_report_paths_and_assumption_ledger(cli_env: GitRepo):
    runner = CliRunner()
    run_result = runner.invoke(app_module.app, ["run", "add a constant to mod_a.py", "--yes"])
    assert run_result.exit_code == 0, run_result.output
    job_id = _extract_job_id(run_result.output)

    review_result = runner.invoke(app_module.app, ["review", job_id])
    assert review_result.exit_code == 0, review_result.output
    assert "report.md" in review_result.output
    assert "report.json" in review_result.output
    assert "lazycode/" in review_result.output  # branch name in the groups table
    assert "chose A2 name" in review_result.output


def test_doctor_rebuild_replays_projections(cli_env: GitRepo):
    runner = CliRunner()
    run_result = runner.invoke(app_module.app, ["run", "add a constant to mod_a.py", "--yes"])
    assert run_result.exit_code == 0, run_result.output
    job_id = _extract_job_id(run_result.output)

    doctor_result = runner.invoke(app_module.app, ["doctor", "--rebuild", job_id])
    assert doctor_result.exit_code == 0, doctor_result.output
    assert "Rebuilt projections" in doctor_result.output

    # Projections still reflect the finished job after the replay.
    status_result = runner.invoke(app_module.app, ["status", job_id])
    assert status_result.exit_code == 0, status_result.output
    assert "DONE" in status_result.output


def test_review_missing_job_errors_clearly(cli_env: GitRepo):
    runner = CliRunner()
    result = runner.invoke(app_module.app, ["review", "job-doesnotexist"])
    assert result.exit_code == 1
    assert "No report found" in result.output


def test_status_unknown_job_errors_clearly(cli_env: GitRepo):
    runner = CliRunner()
    result = runner.invoke(app_module.app, ["status", "job-doesnotexist"])
    assert result.exit_code == 1
    assert "No such job" in result.output
