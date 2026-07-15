"""Unit tests for ``bench/compare.py`` -- the comparison table + M0 accept
(b) <50% verdict, against synthetic result files (no execution)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import compare


def _write_result(results_dir: Path, task: str, suffix: str, payload: dict) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"{task}-{suffix}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def results_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "results"
    monkeypatch.setattr(compare, "RESULTS_DIR", d)
    return d


def test_missing_lazycode_result_raises(results_dir: Path):
    with pytest.raises(compare.ComparisonError):
        compare.compare_task("nope")


def test_missing_baseline_gives_a_note_not_a_crash(results_dir: Path):
    _write_result(
        results_dir, "t1", "lazycode",
        {"status": "DONE", "waves": 1, "tokens_in": 100, "tokens_out": 50, "cost_usd": 0.001},
    )
    row = compare.compare_task("t1")
    assert row["baseline"] is None
    assert row["verdict"] is None
    assert "run bench/run_baseline.py" in row["note"]


def test_unavailable_baseline_gives_a_note_not_a_crash(results_dir: Path):
    _write_result(
        results_dir, "t1", "lazycode",
        {"status": "DONE", "waves": 1, "tokens_in": 100, "tokens_out": 50, "cost_usd": 0.001},
    )
    _write_result(
        results_dir, "t1", "baseline",
        {"status": "unavailable", "note": "`claude` CLI not found on PATH -- install Claude Code to run this baseline."},
    )
    row = compare.compare_task("t1")
    assert row["baseline"] is None
    assert row["verdict"] is None
    assert "unavailable" in row["note"]


def test_beats_threshold_is_pass(results_dir: Path):
    _write_result(
        results_dir, "t1", "lazycode",
        {"status": "DONE", "waves": 1, "tokens_in": 100, "tokens_out": 50, "cost_usd": 0.001},
    )
    _write_result(
        results_dir, "t1", "baseline",
        {"status": "DONE", "tokens_in": 2000, "tokens_out": 2000, "cost_usd": 0.05},
    )
    row = compare.compare_task("t1")
    assert row["token_ratio"] < 0.5
    assert row["verdict"] == "PASS"
    assert row["cost_ratio"] == pytest.approx(0.001 / 0.05)


def test_misses_threshold_is_fail(results_dir: Path):
    _write_result(
        results_dir, "t1", "lazycode",
        {"status": "DONE", "waves": 1, "tokens_in": 900, "tokens_out": 900, "cost_usd": 0.04},
    )
    _write_result(
        results_dir, "t1", "baseline",
        {"status": "DONE", "tokens_in": 1000, "tokens_out": 1000, "cost_usd": 0.05},
    )
    row = compare.compare_task("t1")
    assert row["token_ratio"] >= 0.5
    assert row["verdict"] == "FAIL"


def test_render_table_includes_task_and_verdict(results_dir: Path):
    _write_result(
        results_dir, "t1", "lazycode",
        {"status": "DONE", "waves": 1, "tokens_in": 100, "tokens_out": 50, "cost_usd": 0.001},
    )
    _write_result(
        results_dir, "t1", "baseline",
        {"status": "DONE", "tokens_in": 2000, "tokens_out": 2000, "cost_usd": 0.05},
    )
    row = compare.compare_task("t1")
    table = compare.render_table([row])
    assert "t1" in table
    assert "PASS" in table


def test_main_exits_nonzero_on_failure(results_dir: Path, capsys: pytest.CaptureFixture[str]):
    _write_result(
        results_dir, "t1", "lazycode",
        {"status": "DONE", "waves": 1, "tokens_in": 900, "tokens_out": 900, "cost_usd": 0.04},
    )
    _write_result(
        results_dir, "t1", "baseline",
        {"status": "DONE", "tokens_in": 1000, "tokens_out": 1000, "cost_usd": 0.05},
    )
    import sys

    old_argv = sys.argv
    sys.argv = ["compare.py", "t1"]
    try:
        with pytest.raises(SystemExit) as exc_info:
            compare._main()
    finally:
        sys.argv = old_argv
    assert "FAIL" in str(exc_info.value.code)
