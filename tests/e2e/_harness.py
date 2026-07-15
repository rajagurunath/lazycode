"""Shared plumbing for the ``tests/e2e`` real-subprocess tests (DESIGN.md
§14 M0 accept criteria a/b/c).

Unlike ``tests/cli`` (in-process ``CliRunner`` + monkeypatched adapter
classes), these tests spawn the actual ``lazycode`` CLI as a child process --
that's the whole point of the kill -9 acceptance test (accept criterion c),
which cannot be exercised with in-process mocking. Determinism/zero-network
comes from the config-constructible mock provider seam
(``lazycode/cli/mock_provider.py``) instead.

Not a test module itself (leading underscore keeps pytest from collecting
it); ``conftest.py``/test files import from it directly.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_JOB_ID_RE = re.compile(r"job-[0-9a-f]{12}")

# The CLI entry point, invoked the same way in every subprocess call. Using
# `-m` against the *current* interpreter (rather than the `lazycode` console
# script) guarantees we exercise the exact editable install `uv sync` set up
# for this test run, with no PATH ambiguity.
_CLI = [sys.executable, "-m", "lazycode.cli.app"]


@dataclass
class GitRepo:
    """A throwaway git repo the mock-driven CLI subprocess operates on
    (mirrors ``tests/cli/conftest.py``'s fixture, duplicated per that
    directory's own convention -- see ``tests/scheduler/conftest.py`` too)."""

    root: Path

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        assert result.returncode == 0, f"git {args} failed:\n{result.stdout}\n{result.stderr}"
        return result

    def write(self, relpath: str, content: str) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def commit(self, message: str) -> str:
        self.run("add", "-A")
        self.run("commit", "-q", "-m", message)
        return self.run("rev-parse", "HEAD").stdout.strip()

    def make_patch(self, relpath: str, new_content: str) -> str:
        """A real unified diff for ``relpath`` (working tree left unchanged)."""
        path = self.root / relpath
        original = path.read_text(encoding="utf-8")
        path.write_text(new_content, encoding="utf-8")
        diff = self.run("diff", "--", relpath).stdout
        path.write_text(original, encoding="utf-8")
        return diff


def init_git_repo(root: Path) -> GitRepo:
    root.mkdir(parents=True, exist_ok=True)
    repo = GitRepo(root=root)
    repo.run("init", "-q")
    repo.run("config", "user.email", "e2e@example.com")
    repo.run("config", "user.name", "E2E Test")
    repo.run("config", "commit.gpgsign", "false")
    return repo


# --- fixture + config authoring ---------------------------------------------


def write_mock_fixture(
    repo: GitRepo,
    *,
    plan: dict[str, Any],
    items: dict[str, dict[str, str]],
    poll_delays: int = 0,
    relpath: str = "lazycode_fixture.json",
) -> str:
    """Write a mock-provider fixture (``lazycode/cli/mock_provider.py``
    format) into the repo; returns the repo-relative path for ``lazycode.toml``."""
    fixture = {"planner_response": plan, "items": items, "poll_delays": poll_delays}
    (repo.root / relpath).write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    return relpath


def write_lazycode_toml(
    repo: GitRepo,
    *,
    fixture_relpath: str,
    verify_command: str = "true",
    lease_ttl_s: float = 2.0,
    poll_base_s: float = 0.4,
    poll_cap_s: float = 5.0,
    max_waves: int = 8,
) -> None:
    """Repo-local config selecting the mock provider (Appendix B2) with short
    lease/poll bounds so the kill -9 test doesn't have to wait out the M0
    defaults (300s lease TTL) before ``resume`` can take over."""
    toml = f"""\
[verify]
command = "{verify_command}"

[defaults]
provider = "mock"
lease_ttl_s = {lease_ttl_s}
poll_base_s = {poll_base_s}
poll_cap_s = {poll_cap_s}
max_waves = {max_waves}

[providers.mock]
fixture = "{fixture_relpath}"
"""
    (repo.root / "lazycode.toml").write_text(toml, encoding="utf-8")


def write_global_config(repo: GitRepo) -> Path:
    """``keep_awake = false`` so no confirm() prompt can block a subprocess
    run with no attached tty."""
    path = repo.root / "global-config.toml"
    path.write_text("[daemon]\nkeep_awake = false\n", encoding="utf-8")
    return path


def subprocess_env(repo: GitRepo, global_config_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["LAZYCODE_GLOBAL_CONFIG"] = str(global_config_path)
    # Never require a real Anthropic key in this env (provider=mock doesn't
    # need one, but stay defensive against a dev shell that has one set for
    # a *different* provider tripping an unrelated code path).
    env.pop("ANTHROPIC_API_KEY", None)
    return env


# --- running the CLI ---------------------------------------------------------


def run_cli(repo: GitRepo, *args: str, env: dict[str, str], timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """Blocking subprocess invocation; returns once the process exits."""
    return subprocess.run(
        [*_CLI, *args], cwd=repo.root, env=env, capture_output=True, text=True, timeout=timeout
    )


def start_cli(repo: GitRepo, *args: str, env: dict[str, str]) -> subprocess.Popen[str]:
    """Non-blocking subprocess invocation (for the kill -9 test)."""
    return subprocess.Popen(
        [*_CLI, *args],
        cwd=repo.root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def extract_job_id(text: str) -> str:
    match = _JOB_ID_RE.search(text)
    assert match, f"no job id found in:\n{text}"
    return match.group(0)


# --- polling the store while a subprocess runs -------------------------------


def db_path(repo: GitRepo) -> Path:
    return repo.root / ".lazycode" / "lazycode.sqlite3"


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def wait_for_event(repo: GitRepo, event_type: str, *, timeout: float = 30.0, poll_interval: float = 0.05) -> str:
    """Block until an ``events`` row of ``event_type`` exists; returns its
    ``job_id``. Polls the SQLite file directly (WAL supports concurrent
    readers alongside the subprocess's single writer, §7.1) rather than
    parsing subprocess output, so it works even when the process is killed
    before it prints anything."""
    deadline = time.monotonic() + timeout
    path = db_path(repo)
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                conn = _connect_readonly(path)
                try:
                    row = conn.execute(
                        "SELECT job_id FROM events WHERE type = ? ORDER BY seq LIMIT 1", (event_type,)
                    ).fetchone()
                finally:
                    conn.close()
                if row is not None:
                    return row["job_id"]
            except sqlite3.OperationalError:
                pass  # DB not yet created / mid-migration; keep polling
        time.sleep(poll_interval)
    raise TimeoutError(f"no {event_type} event within {timeout}s")


def read_submissions_log(repo: GitRepo) -> list[dict[str, Any]]:
    path = repo.root / ".lazycode" / "mock_submissions.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def job_status(repo: GitRepo, job_id: str) -> str | None:
    path = db_path(repo)
    if not path.is_file():
        return None
    conn = _connect_readonly(path)
    try:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    return row["status"] if row else None


def wave_count(repo: GitRepo, job_id: str) -> int:
    """DESIGN.md Appendix B6: "wave" for the accept test = rows in ``waves``
    with status >= SUBMITTED for the job."""
    conn = _connect_readonly(db_path(repo))
    try:
        row = conn.execute(
            "SELECT COUNT(*) c FROM waves WHERE job_id = ? AND status IN ('SUBMITTED', 'COMPLETED')",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["c"]


def applied_diff_count(repo: GitRepo, node_ids: list[str]) -> int:
    conn = _connect_readonly(db_path(repo))
    try:
        placeholders = ",".join("?" for _ in node_ids)
        row = conn.execute(
            f"SELECT COUNT(*) c FROM applied_diffs WHERE node_id IN ({placeholders})", node_ids
        ).fetchone()
    finally:
        conn.close()
    return row["c"]


# --- plan / fixture builders --------------------------------------------------


def generate_node(node_id: str, target_file: str, spec: str) -> dict[str, Any]:
    """A ``Generate`` node touching exactly one file, plus a local ``Verify``
    that depends on it (kept local/free -- never adds a wave, Appendix B6)."""
    return {
        "op": "Generate",
        "id": node_id,
        "spec": spec,
        "deps": [],
        "context_spec": {"files": [target_file], "repo_map": False, "house_rules": False, "extras": {}},
        "output_contract": {"type": "diff", "files_within": [target_file]},
    }


def verify_node(node_id: str, deps: list[str]) -> dict[str, Any]:
    return {
        "op": "Verify",
        "id": node_id,
        "deps": deps,
        "checks": [{"type": "command", "cmd": "true", "timeout_s": 10}],
    }
