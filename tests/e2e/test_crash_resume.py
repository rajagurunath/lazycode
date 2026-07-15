"""DESIGN.md §14 M0 accept criterion (c), verbatim:

    kill -9 mid-wave, restart, job resumes with no double-submit (verified
    via provider dashboard) and no double-apply.

This drives the *real* CLI (``python -m lazycode.cli.app``) as a real OS
subprocess, because in-process mock injection (``tests/cli/conftest.py``)
cannot survive a process boundary. See ``tests/e2e/_harness.py`` for the
mock-provider-driven fixture/config authoring and subprocess plumbing.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from . import _harness as h

pytestmark = pytest.mark.e2e

_GOAL = "add constants to a.py and b.py"
# Must be short (so the test doesn't wait long) but the resumer must wait it
# out -- the killed process's lease row survives SIGKILL (release() never
# runs) and only expiry, not a clean release, lets a new orchestrator take
# over (§7.1 lease.acquire: takeover-on-expiry).
_LEASE_TTL_S = 1.5


def _plan() -> dict:
    return {
        "goal": _GOAL,
        "assumptions": [],
        "schema_version": 1,
        "nodes": [
            h.generate_node("n1", "a.py", "append A2 = 2 to a.py"),
            h.generate_node("n2", "b.py", "append B2 = 2 to b.py"),
            h.verify_node("n3", deps=["n1", "n2"]),
        ],
    }


@pytest.fixture
def repo(tmp_path: Path) -> h.GitRepo:
    repo = h.init_git_repo(tmp_path / "repo")
    repo.write("a.py", "A = 1\n")
    repo.write("b.py", "B = 1\n")
    repo.commit("init")
    return repo


def test_kill_9_mid_wave_then_resume_no_double_submit_no_double_apply(repo: h.GitRepo):
    diff_a = repo.make_patch("a.py", "A = 1\nA2 = 2\n")
    diff_b = repo.make_patch("b.py", "B = 1\nB2 = 2\n")

    fixture_rel = h.write_mock_fixture(
        repo,
        plan=_plan(),
        items={
            "n1": {"diff": diff_a, "assumptions": "chose A2 name"},
            "n2": {"diff": diff_b, "assumptions": "chose B2 name"},
        },
        # Several non-terminal polls -> a multi-second window where the wave
        # is durably SUBMITTED but not yet COMPLETED, so the kill below is
        # deterministically "mid-wave" rather than a race against instant
        # completion.
        poll_delays=4,
    )
    h.write_lazycode_toml(
        repo, fixture_relpath=fixture_rel, lease_ttl_s=_LEASE_TTL_S, poll_base_s=0.4, poll_cap_s=3.0
    )
    global_config = h.write_global_config(repo)
    env = h.subprocess_env(repo, global_config)

    # --- first run: kill -9 right after the wave is durably submitted -----
    proc = h.start_cli(repo, "run", _GOAL, "--yes", env=env)
    try:
        job_id = h.wait_for_event(repo, "WAVE_SUBMITTED", timeout=30.0)
        assert proc.poll() is None, "process finished before we could kill it -- flaky poll_delays?"
        os.kill(proc.pid, signal.SIGKILL)
        returncode = proc.wait(timeout=10.0)
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    assert proc.poll() is not None, "process should be dead after SIGKILL"
    assert returncode != 0

    # The wave really was submitted (durable) before the crash: exactly one
    # batch in the mock provider's "dashboard" (the submissions log).
    submissions_after_crash = h.read_submissions_log(repo)
    assert len(submissions_after_crash) == 1, submissions_after_crash
    assert h.job_status(repo, job_id) != "DONE"

    # The killed process's job lease is still held (SIGKILL skips its
    # `finally: lease.release(...)`); wait it out so `resume`'s takeover
    # succeeds instead of racing the still-"valid" lease (§7.1).
    time.sleep(_LEASE_TTL_S + 0.75)

    # --- resume: must not resubmit the wave, must finish the job ----------
    result = h.run_cli(repo, "resume", job_id, env=env, timeout=60.0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Done." in result.stdout
    assert "status=DONE" in result.stdout

    # CRITICAL (accept criterion c): still exactly one submission recorded --
    # the resumed process re-polled the existing batch rather than creating a
    # second one. Both the killed process and the resumed process append to
    # the SAME submissions log, so this is the black-box, cross-process
    # equivalent of "verified via provider dashboard".
    submissions_after_resume = h.read_submissions_log(repo)
    assert len(submissions_after_resume) == 1, submissions_after_resume
    assert submissions_after_resume == submissions_after_crash
    idempotency_keys = {s["idempotency_key"] for s in submissions_after_resume}
    assert len(idempotency_keys) == 1

    # Exactly one wave for the job (both Generate nodes share one (provider,
    # model) group at layer 0 -- one batch).
    assert h.wave_count(repo, job_id) == 1

    # No double-apply: the applied-diff ledger has exactly one row per
    # Generate node, and the worktree shows each change exactly once.
    assert h.applied_diff_count(repo, ["n1", "n2"]) == 2
    worktree = repo.root / ".lazycode" / "worktrees" / job_id / "g0"
    a_text = (worktree / "a.py").read_text()
    b_text = (worktree / "b.py").read_text()
    assert a_text.count("A2 = 2") == 1
    assert b_text.count("B2 = 2") == 1

    # Report written (delivery, §9).
    report_dir = repo.root / ".lazycode" / "reports" / job_id
    assert (report_dir / "report.md").exists()
    assert (report_dir / "report.json").exists()

    assert h.job_status(repo, job_id) == "DONE"
