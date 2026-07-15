"""DESIGN.md §14 M0 accept criterion (a): a real multi-file task (here: a
3-file fan-out, standing in for "add type hints to package X, >=10 files" --
the plan shape is what matters for wave-count, not the literal file count)
completes in <= 4 waves, counted as rows in the ``waves`` table (Appendix
B6). Same real-subprocess harness as ``test_crash_resume.py``, without the
kill -- this is the plain happy path through the CLI end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from . import _harness as h

pytestmark = pytest.mark.e2e

_GOAL = "add a top-level constant to a.py, b.py, and c.py"
_FILES = ["a.py", "b.py", "c.py"]


def _plan() -> dict:
    gens = [h.generate_node(f"n{i + 1}", f, f"append a constant to {f}") for i, f in enumerate(_FILES)]
    verify = h.verify_node("n_verify", deps=[g["id"] for g in gens])
    return {
        "goal": _GOAL,
        "assumptions": ["each file uses simple module-level constants"],
        "schema_version": 1,
        "nodes": [*gens, verify],
    }


@pytest.fixture
def repo(tmp_path: Path) -> h.GitRepo:
    repo = h.init_git_repo(tmp_path / "repo")
    for i, f in enumerate(_FILES):
        repo.write(f, f"X{i} = {i}\n")
    repo.commit("init")
    return repo


def test_multi_file_fanout_completes_end_to_end(repo: h.GitRepo):
    items = {}
    for i, f in enumerate(_FILES):
        const = f"Y{i} = {i + 100}\n"
        diff = repo.make_patch(f, f"X{i} = {i}\n{const}")
        items[f"n{i + 1}"] = {"diff": diff, "assumptions": f"named the constant Y{i}"}

    fixture_rel = h.write_mock_fixture(repo, plan=_plan(), items=items, poll_delays=0)
    h.write_lazycode_toml(repo, fixture_relpath=fixture_rel)
    global_config = h.write_global_config(repo)
    env = h.subprocess_env(repo, global_config)

    result = h.run_cli(repo, "run", _GOAL, "--yes", env=env, timeout=60.0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Done." in result.stdout
    assert "status=DONE" in result.stdout

    job_id = h.extract_job_id(result.stdout)

    # (a) wave count (Appendix B6): all three Generate nodes are independent
    # (layer 0) so they share one (provider, model) batch -- one wave. Well
    # within the <= 4 accept ceiling for a real multi-file fan-out.
    waves = h.wave_count(repo, job_id)
    assert waves <= 4, f"expected <=4 waves, got {waves}"
    assert waves == 1

    assert "branch: lazycode/" in result.stdout
    branch_line = next(line for line in result.stdout.splitlines() if line.strip().startswith("branch:"))
    assert branch_line.strip().startswith(f"branch: lazycode/{job_id}/")

    worktree = repo.root / ".lazycode" / "worktrees" / job_id / "g0"
    for i, f in enumerate(_FILES):
        assert f"Y{i} = {i + 100}" in (worktree / f).read_text()

    # report.md / report.json present (§9 delivery).
    report_dir = repo.root / ".lazycode" / "reports" / job_id
    assert (report_dir / "report.md").exists()
    report_data = json.loads((report_dir / "report.json").read_text())
    assert report_data["job_id"] == job_id
    assert len(report_data["assumptions"]) == len(_FILES)

    # `lazycode status`/`explain`/`review` all work post-run (read-only, §7.1).
    status_result = h.run_cli(repo, "status", env=env, timeout=30.0)
    assert status_result.returncode == 0
    assert job_id in status_result.stdout
    assert "DONE" in status_result.stdout

    explain_result = h.run_cli(repo, "explain", job_id, env=env, timeout=30.0)
    assert explain_result.returncode == 0
    assert "Physical Plan" in explain_result.stdout

    review_result = h.run_cli(repo, "review", job_id, env=env, timeout=30.0)
    assert review_result.returncode == 0
    assert "report.md" in review_result.stdout
