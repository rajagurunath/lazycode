"""Unit tests for ``bench/pricing.py`` -- the illustrative list-price table
used to render a dollar column (and the M0 accept (b) <50% check) alongside
the raw token comparison."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import pricing


def test_batch_is_half_of_realtime_for_known_model():
    realtime = pricing.realtime_cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000)
    batch = pricing.batch_cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert batch == pytest.approx(realtime * 0.5)


def test_realtime_cost_matches_table_math():
    cost = pricing.realtime_cost_usd("claude-haiku-4-5", 2_000_000, 1_000_000)
    # 2M in @ $1.00/M + 1M out @ $5.00/M
    assert cost == pytest.approx(2.0 + 5.0)


def test_zero_tokens_cost_zero():
    assert pricing.realtime_cost_usd("claude-haiku-4-5", 0, 0) == 0.0
    assert pricing.batch_cost_usd("claude-haiku-4-5", 0, 0) == 0.0


def test_unknown_model_falls_back_to_a_rate_instead_of_raising():
    cost = pricing.realtime_cost_usd("some-unlisted-model", 1_000_000, 0)
    assert cost > 0


def test_env_override_replaces_the_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    custom = tmp_path / "prices.json"
    custom.write_text(json.dumps({"my-model": {"input": 2.0, "output": 8.0}}), encoding="utf-8")
    monkeypatch.setenv("LAZYCODE_BENCH_PRICING", str(custom))

    cost = pricing.realtime_cost_usd("my-model", 1_000_000, 1_000_000)
    assert cost == pytest.approx(2.0 + 8.0)
