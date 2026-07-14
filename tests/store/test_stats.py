"""Tests for stats.py: the ANALYZE table (running averages, cold-start gating)."""

from __future__ import annotations

import pytest

from lazycode.store import Store, stats


def test_priors_none_when_no_data(store: Store):
    assert stats.priors(store, op="Edit", model="claude-x", repo="/repo") is None


def test_priors_none_below_cold_start_threshold(store: Store):
    for _ in range(stats.COLD_START_N - 1):
        stats.record(
            store, op="Edit", model="claude-x", repo="/repo", tokens_in=100, tokens_out=50, rounds=1, verify_pass=True
        )
    assert stats.priors(store, op="Edit", model="claude-x", repo="/repo") is None


def test_priors_available_at_threshold(store: Store):
    for _ in range(stats.COLD_START_N):
        stats.record(
            store, op="Edit", model="claude-x", repo="/repo", tokens_in=100, tokens_out=50, rounds=1, verify_pass=True
        )
    priors = stats.priors(store, op="Edit", model="claude-x", repo="/repo")
    assert priors is not None
    assert priors.n == stats.COLD_START_N
    assert priors.avg_in == 100
    assert priors.avg_out == 50
    assert priors.avg_rounds == 1
    assert priors.verify_pass_rate == 1.0


def test_record_computes_running_average(store: Store):
    stats.record(store, op="Edit", model="m", repo="r", tokens_in=100, tokens_out=10, rounds=1, verify_pass=True)
    stats.record(store, op="Edit", model="m", repo="r", tokens_in=200, tokens_out=20, rounds=3, verify_pass=False)
    row = store.conn.execute(
        "SELECT n, avg_in, avg_out, avg_rounds, verify_pass_rate FROM stats WHERE op='Edit' AND model='m' AND repo='r'"
    ).fetchone()
    assert row["n"] == 2
    assert row["avg_in"] == 150.0
    assert row["avg_out"] == 15.0
    assert row["avg_rounds"] == 2.0
    assert row["verify_pass_rate"] == 0.5


def test_stats_scoped_per_op_model_repo(store: Store):
    stats.record(store, op="Edit", model="m1", repo="r", tokens_in=100, tokens_out=10, rounds=1, verify_pass=True)
    stats.record(store, op="Generate", model="m1", repo="r", tokens_in=999, tokens_out=999, rounds=5, verify_pass=False)
    stats.record(store, op="Edit", model="m2", repo="r", tokens_in=1, tokens_out=1, rounds=1, verify_pass=True)
    stats.record(store, op="Edit", model="m1", repo="other-repo", tokens_in=1, tokens_out=1, rounds=1, verify_pass=True)

    row = store.conn.execute(
        "SELECT avg_in FROM stats WHERE op='Edit' AND model='m1' AND repo='r'"
    ).fetchone()
    assert row["avg_in"] == 100


def test_record_multiple_observations_matches_true_mean(store: Store):
    values = [(10, 1), (20, 2), (30, 3), (40, 4), (50, 5)]
    for tokens_out, rounds in values:
        stats.record(
            store, op="Generate", model="m", repo="r", tokens_in=1000, tokens_out=tokens_out, rounds=rounds, verify_pass=True
        )
    row = store.conn.execute(
        "SELECT avg_out, avg_rounds FROM stats WHERE op='Generate' AND model='m' AND repo='r'"
    ).fetchone()
    assert row["avg_out"] == pytest.approx(sum(v[0] for v in values) / len(values))
    assert row["avg_rounds"] == pytest.approx(sum(v[1] for v in values) / len(values))
