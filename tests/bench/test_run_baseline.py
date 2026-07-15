"""Unit tests for ``bench/run_baseline.py``. Never invokes a real ``claude``
process: binary resolution (``_claude_bin``) and the single call site that
shells out (``_invoke_claude``) are monkeypatched throughout, so these are
safe to run with no network, no Claude Code installed, and no cost -- and,
critically, so they can't be defeated by ``claude`` actually being on this
machine's PATH (patching ``subprocess.run`` globally was tried and rejected:
it also intercepts ``task_spec.build_repo``'s ``git`` calls, since both
modules share the one ``subprocess`` module object)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bench import run_baseline


def _fake_claude_stdout(*, input_tokens=1000, output_tokens=500, cache_read=0, cache_creation=0, cost=0.01) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "total_cost_usd": cost,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        }
    )


def test_degrades_gracefully_when_claude_not_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(run_baseline, "_claude_bin", lambda: None)
    result = run_baseline.run_task("add-type-hints", workdir=tmp_path, write_results=False)
    assert result["status"] == "unavailable"
    assert "claude" in result["note"].lower()
    assert result["task"] == "add-type-hints"


def test_unavailable_result_is_written_when_requested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(run_baseline, "_claude_bin", lambda: None)
    results_dir = tmp_path / "results"
    monkeypatch.setattr(run_baseline, "RESULTS_DIR", results_dir)
    run_baseline.run_task("add-type-hints", workdir=tmp_path / "work", write_results=True)
    out_path = results_dir / "add-type-hints-baseline.json"
    assert out_path.is_file()
    assert json.loads(out_path.read_text())["status"] == "unavailable"


def test_parses_usage_and_computes_list_price_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_invoke(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=_fake_claude_stdout(), stderr="")

    monkeypatch.setattr(run_baseline, "_invoke_claude", fake_invoke)

    result = run_baseline.run_task(
        "add-type-hints",
        claude_bin="/fake/bin/claude",
        workdir=tmp_path,
        write_results=False,
    )

    assert result["status"] == "DONE"
    assert result["tokens_in"] == 1000
    assert result["tokens_out"] == 500
    assert result["cost_usd"] > 0
    assert result["reported_cost_usd"] == 0.01


def test_cache_tokens_are_folded_into_tokens_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_invoke(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_fake_claude_stdout(input_tokens=100, cache_read=200, cache_creation=50),
            stderr="",
        )

    monkeypatch.setattr(run_baseline, "_invoke_claude", fake_invoke)
    result = run_baseline.run_task(
        "add-type-hints", claude_bin="/fake/bin/claude", workdir=tmp_path, write_results=False
    )
    assert result["tokens_in"] == 100 + 200 + 50


def test_nonzero_exit_reports_error_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_invoke(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(run_baseline, "_invoke_claude", fake_invoke)
    result = run_baseline.run_task(
        "add-type-hints", claude_bin="/fake/bin/claude", workdir=tmp_path, write_results=False
    )
    assert result["status"] == "error"
    assert "boom" in result["note"]


def test_timeout_reports_timeout_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_invoke(*args: Any, **kwargs: Any):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1.0)

    monkeypatch.setattr(run_baseline, "_invoke_claude", fake_invoke)
    result = run_baseline.run_task(
        "add-type-hints",
        claude_bin="/fake/bin/claude",
        workdir=tmp_path,
        write_results=False,
        timeout_s=1.0,
    )
    assert result["status"] == "timeout"


def test_malformed_json_stdout_yields_zero_tokens_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_invoke(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(run_baseline, "_invoke_claude", fake_invoke)
    result = run_baseline.run_task(
        "add-type-hints", claude_bin="/fake/bin/claude", workdir=tmp_path, write_results=False
    )
    assert result["status"] == "DONE"
    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 0
