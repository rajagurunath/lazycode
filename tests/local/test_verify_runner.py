"""Tests for verify/runner.py: pass/fail/timeout subprocess execution."""

from __future__ import annotations

from pathlib import Path

from lazycode.ir import CommandContract
from lazycode.verify import run_command_contract, run_verify


def test_run_verify_passing_command(tmp_path: Path):
    result = run_verify(tmp_path, "true", timeout_s=5)
    assert result.passed is True
    assert result.exit_code == 0
    assert result.duration_s >= 0


def test_run_verify_failing_command(tmp_path: Path):
    result = run_verify(tmp_path, "false", timeout_s=5)
    assert result.passed is False
    assert result.exit_code == 1


def test_run_verify_captures_tail_of_output(tmp_path: Path):
    script = tmp_path / "many_lines.py"
    script.write_text(
        "for i in range(300):\n    print(f'line{i}')\n",
        encoding="utf-8",
    )
    import sys

    result = run_verify(tmp_path, f"{sys.executable} many_lines.py", timeout_s=10)

    assert result.passed is True
    lines = result.tail.splitlines()
    assert len(lines) <= 100
    assert lines[-1] == "line299"
    assert "line0" not in result.tail  # early lines dropped by the tail cap


def test_run_verify_timeout(tmp_path: Path):
    import sys

    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")

    result = run_verify(tmp_path, f"{sys.executable} sleeper.py", timeout_s=0.2)

    assert result.passed is False
    assert result.exit_code is None
    assert "timed out" in result.tail


def test_run_verify_uses_worktree_cwd(tmp_path: Path):
    (tmp_path / "marker.txt").write_text("here\n", encoding="utf-8")
    result = run_verify(tmp_path, "cat marker.txt", timeout_s=5)
    assert result.passed is True
    assert "here" in result.tail


def test_run_command_contract_respects_expect_exit(tmp_path: Path):
    # `false` exits 1; a contract that *expects* exit 1 should count as passed.
    contract = CommandContract(cmd="false", timeout_s=5, expect_exit=1)
    result = run_command_contract(tmp_path, contract)
    assert result.passed is True
    assert result.exit_code == 1


def test_run_command_contract_default_expect_exit_zero(tmp_path: Path):
    contract = CommandContract(cmd="false", timeout_s=5)
    result = run_command_contract(tmp_path, contract)
    assert result.passed is False
