"""Unit tests for ``bench/run_lazycode.py`` against the mock provider seam
(``lazycode/cli/mock_provider.py``) -- zero network, deterministic, safe for
CI. Real-Anthropic runs (``--provider anthropic``) are exercised manually
only (see ``bench/README.md``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import run_lazycode


@pytest.fixture
def add_type_hints_fixture() -> dict:
    path = Path(__file__).parents[2] / "bench" / "tasks" / "add-type-hints" / "mock_fixture.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_run_task_mock_completes_and_reports_actuals(add_type_hints_fixture: dict, tmp_path: Path):
    result = run_lazycode.run_task(
        "add-type-hints",
        provider="mock",
        fixture=add_type_hints_fixture,
        workdir=tmp_path,
        write_results=False,
    )

    assert result["status"] == "DONE"
    assert result["task"] == "add-type-hints"
    assert result["provider"] == "mock"
    assert result["waves"] == 1  # 3 independent Generate nodes -> one batch, one wave
    assert result["tokens_in"] > 0
    assert result["tokens_out"] > 0
    assert result["llm_calls"] == 3
    assert result["cost_usd"] > 0
    assert result["wall_clock_s"] >= 0
    assert result["job_id"]


def test_run_task_mock_requires_a_fixture(tmp_path: Path):
    with pytest.raises(ValueError, match="fixture"):
        run_lazycode.run_task("add-type-hints", provider="mock", workdir=tmp_path, write_results=False)


def test_run_task_mock_accepts_fixture_path(tmp_path: Path):
    fixture_path = Path(__file__).parents[2] / "bench" / "tasks" / "add-type-hints" / "mock_fixture.json"
    result = run_lazycode.run_task(
        "add-type-hints",
        provider="mock",
        fixture_path=fixture_path,
        workdir=tmp_path,
        write_results=False,
    )
    assert result["status"] == "DONE"


def test_run_task_writes_results_file(
    add_type_hints_fixture: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    results_dir = tmp_path / "results"
    monkeypatch.setattr(run_lazycode, "RESULTS_DIR", results_dir)
    workdir = tmp_path / "work"

    run_lazycode.run_task(
        "add-type-hints",
        provider="mock",
        fixture=add_type_hints_fixture,
        workdir=workdir,
        write_results=True,
    )

    out_path = results_dir / "add-type-hints-lazycode.json"
    assert out_path.is_file()
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["status"] == "DONE"
    assert payload["task"] == "add-type-hints"


def test_run_task_unknown_provider_needs_api_key_and_fails_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        run_lazycode.run_task("add-type-hints", provider="anthropic", workdir=tmp_path, write_results=False)
