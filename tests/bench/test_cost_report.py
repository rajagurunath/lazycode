"""Unit tests for ``bench/cost_report.py`` -- the cost-metering instrument
behind the paper's "what does $10 buy?" worked example.

Everything here runs against the committed mock fixture: zero network, $0
spent, CI-safe. The ``--live`` path (real OpenAI money) is never exercised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import cost_report


@pytest.fixture
def add_type_hints_fixture() -> dict:
    path = Path(__file__).parents[2] / "bench" / "tasks" / "add-type-hints" / "mock_fixture.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def report(add_type_hints_fixture: dict) -> dict:
    return cost_report.run_cost_report(
        "add-type-hints", fixture=add_type_hints_fixture, write_results=False
    )


# --- the price sheet is real published pricing --------------------------------


def test_price_sheet_carries_both_published_providers():
    sheet = cost_report.price_sheet()
    assert "anthropic/claude-haiku-4-5" in sheet
    assert "openai/gpt-4o-mini" in sheet


@pytest.mark.parametrize("key", ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini"])
def test_batch_rates_are_half_of_online_rates(key: str):
    """Both providers publish "50% off input AND output" for their batch API."""
    r = cost_report.rates(key)
    assert r["batch_input"] == pytest.approx(r["input"] * 0.5)
    assert r["batch_output"] == pytest.approx(r["output"] * 0.5)


def test_haiku_cache_multipliers_match_the_published_table():
    r = cost_report.rates("anthropic/claude-haiku-4-5")
    assert r["cached_input"] == pytest.approx(r["input"] * 0.1)  # 0.1x read
    assert r["cache_write_5m"] == pytest.approx(r["input"] * 1.25)  # 1.25x 5-minute write
    assert r["cache_write_1h"] == pytest.approx(r["input"] * 2.0)  # 2x 1-hour write


def test_price_sheet_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    custom = tmp_path / "sheet.json"
    custom.write_text(
        json.dumps(
            {
                "my/model": {
                    "input": 2.0, "cached_input": 0.2,
                    "cache_write_5m": 2.5, "cache_write_1h": 4.0,
                    "batch_input": 1.0, "output": 8.0, "batch_output": 4.0,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LAZYCODE_BENCH_PRICE_SHEET", str(custom))
    assert cost_report.rates("my/model")["input"] == 2.0


# --- the model's predicted factor ---------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [(1, 0.5), (2, 0.5), (3, 0.05 + 0.95 / 3), (20, 0.05 + 0.95 / 20)],
)
def test_predicted_input_factor_matches_the_paper(n: int, expected: float):
    assert cost_report.predicted_input_factor(n) == pytest.approx(expected)


def test_caching_starts_paying_at_width_three():
    """The paper: "caching pays for N >= 3"."""
    assert cost_report.predicted_input_factor(2) == 0.5
    assert cost_report.predicted_input_factor(3) < 0.5


def test_predicted_factor_limit_is_0p05():
    """As N -> inf the per-item factor -> 0.05, i.e. R < 2T."""
    assert cost_report.predicted_input_factor(1_000_000) == pytest.approx(0.05, abs=1e-5)


# --- both tiers actually run ---------------------------------------------------


def test_both_tiers_complete_the_same_job(report: dict):
    assert report["online"]["status"] == "DONE"
    assert report["lazycode_batch"]["status"] == "DONE"
    assert report["online"]["calls"] == report["lazycode_batch"]["calls"] == 3


def test_identical_token_ledger_on_both_tiers(report: dict):
    """Same plan, same renderer, same harvest -> the two tiers differ in PRICE,
    not in work done. Any drift here would invalidate the comparison."""
    for field in ("prefix_tokens", "unique_tokens", "output_tokens"):
        assert report["online"][field] == report["lazycode_batch"][field] > 0


def test_quality_equivalence_is_exact_under_mocks(report: dict):
    assert report["quality"]["output_match_rate"] == 1.0
    assert report["quality"]["temperature"] == 0.0
    assert report["quality"]["nodes_compared"] == 3
    assert "by construction" in report["quality"]["note"]


# --- the money ------------------------------------------------------------------


def test_batch_is_cheaper_and_saving_is_reported(report: dict):
    assert report["lazycode_batch"]["total_usd"] < report["online"]["total_usd"]
    assert report["saved_usd"] == pytest.approx(
        report["online"]["total_usd"] - report["lazycode_batch"]["total_usd"]
    )
    assert 0 < report["saved_pct"] < 100


def test_output_tokens_get_the_unconditional_fifty_percent(report: dict):
    """Output is never cacheable on either side, so batch's 50% is unconditional."""
    assert report["lazycode_batch"]["output_usd"] == pytest.approx(
        report["online"]["output_usd"] * 0.5
    )


def test_ten_dollar_framing_is_self_consistent(report: dict):
    tdb = report["what_ten_dollars_buys"]
    assert tdb["workload_units_lazycode_batch"] > tdb["workload_units_online_only"]
    assert tdb["cost_of_the_online_units_via_batch_usd"] < tdb["budget_usd"]


# --- observed vs predicted factor -------------------------------------------------


def test_cold_batch_observes_the_flat_half_factor(report: dict):
    assert report["input_factor"]["observed_batch_prefix_factor"] == pytest.approx(0.5)


def test_stacked_batch_reproduces_the_predicted_factor(add_type_hints_fixture: dict):
    """The instrument's whole point: with discounts stacked, the OBSERVED
    per-item prefix factor equals the model's min(0.5, 0.05 + 0.95/N)."""
    stacked = cost_report.run_cost_report(
        "add-type-hints", fixture=add_type_hints_fixture, stack_cache=True, write_results=False
    )
    widths = stacked["input_factor"]["wave_widths"]
    assert widths == [3]
    observed = stacked["input_factor"]["observed_batch_prefix_factor"]
    assert observed == pytest.approx(cost_report.predicted_input_factor(3))


def test_stacking_never_costs_more_than_cold_batch(report: dict, add_type_hints_fixture: dict):
    stacked = cost_report.run_cost_report(
        "add-type-hints", fixture=add_type_hints_fixture, stack_cache=True, write_results=False
    )
    assert stacked["lazycode_batch"]["total_usd"] <= report["lazycode_batch"]["total_usd"]


# --- dry run is the default; live is gated ------------------------------------------


def test_default_mode_is_a_zero_dollar_dry_run(report: dict):
    assert "dry-run" in report["mode"]
    assert "$0" in report["mode"]


def test_live_requires_an_api_key(add_type_hints_fixture: dict, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        cost_report.run_cost_report(
            "add-type-hints", fixture=add_type_hints_fixture, live=True,
            yes_spend_real_money=True, write_results=False,
        )


def test_live_requires_the_spend_acknowledgment(
    add_type_hints_fixture: dict, monkeypatch: pytest.MonkeyPatch
):
    """The --yes-spend-real-money gate must hold for library callers of
    run_cost_report(), not only for the CLI entry point."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    with pytest.raises(RuntimeError, match="yes_spend_real_money"):
        cost_report.run_cost_report(
            "add-type-hints", fixture=add_type_hints_fixture, live=True, write_results=False
        )


def test_live_requires_an_explicit_model(
    add_type_hints_fixture: dict, monkeypatch: pytest.MonkeyPatch
):
    """--live must not silently run the Anthropic default model against the
    OpenAI-only live adapter."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    with pytest.raises(ValueError, match="explicit --model"):
        cost_report.run_cost_report(
            "add-type-hints", fixture=add_type_hints_fixture, live=True,
            yes_spend_real_money=True, price_key="openai/gpt-4o-mini", write_results=False,
        )


def test_live_requires_a_price_key_matching_the_live_provider(
    add_type_hints_fixture: dict, monkeypatch: pytest.MonkeyPatch
):
    """--live (OpenAI-only today) must refuse an Anthropic --price-key even
    with an explicit --model, so it can never price a real OpenAI spend off
    the wrong sheet."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    with pytest.raises(ValueError, match="price-key"):
        cost_report.run_cost_report(
            "add-type-hints", fixture=add_type_hints_fixture, live=True,
            yes_spend_real_money=True, model="gpt-4o-mini", write_results=False,
        )


def test_report_renders_without_raising(report: dict):
    text = cost_report.render_report(report)
    assert "what $10 buys" in text
    assert "online-only" in text
    assert "predicted min(0.5, 0.05+0.95/N)" in text


def test_writes_a_results_file(add_type_hints_fixture: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cost_report, "RESULTS_DIR", tmp_path)
    cost_report.run_cost_report("add-type-hints", fixture=add_type_hints_fixture, write_results=True)
    payload = json.loads((tmp_path / "add-type-hints-cost-report.json").read_text(encoding="utf-8"))
    assert payload["task"] == "add-type-hints"


# --- live token reconciliation (reported usage vs. the heuristic split) --------


def _ledger(**overrides) -> "cost_report.CallLedger":
    base = dict(
        tier="batch", wave_seq=0, node_id="n1", model="m",
        prefix_sha="abc", prefix_tokens=800, unique_tokens=400, output_tokens=200,
        reported_input_tokens=None, reported_output_tokens=None, response_sha="r1",
    )
    base.update(overrides)
    return cost_report.CallLedger(**base)


def test_reconcile_ledgers_dry_run_keeps_the_heuristic_unchanged():
    ledgers = [_ledger(reported_input_tokens=999, reported_output_tokens=999)]
    out, label = cost_report.reconcile_ledgers(ledgers, live=False)
    assert out[0].prefix_tokens == 800
    assert out[0].unique_tokens == 400
    assert out[0].output_tokens == 200
    assert label == "token split: heuristic estimate"


def test_reconcile_ledgers_live_scales_the_split_to_reported_usage():
    # heuristic split is 800:400 (2:1); reported input total is 1200 -> the
    # scaled split must keep the 2:1 ratio: 800, 400 exactly in this case,
    # but reported total 1500 should scale to 1000:500.
    ledgers = [_ledger(reported_input_tokens=1500, reported_output_tokens=50)]
    out, label = cost_report.reconcile_ledgers(ledgers, live=True)
    assert out[0].prefix_tokens == 1000
    assert out[0].unique_tokens == 500
    assert out[0].prefix_tokens + out[0].unique_tokens == 1500
    assert out[0].output_tokens == 50
    assert label == "token split: scaled to provider-reported usage"


def test_reconcile_ledgers_live_falls_back_when_no_reported_usage():
    ledgers = [_ledger()]  # reported_* left as None
    out, label = cost_report.reconcile_ledgers(ledgers, live=True)
    assert out[0].prefix_tokens == 800
    assert out[0].unique_tokens == 400
    assert "no reported usage" in label


# --- errored-but-billable calls must not vanish from the ledger ----------------


class _StubInnerAdapter:
    """Minimal BatchAdapter-shaped stub for driving MeteringAdapter directly,
    without a full orchestrator run -- the fixture format has no way to force
    an ERRORED-with-usage item, so this exercises MeteringAdapter.fetch()
    against a hand-built ItemResult sequence instead."""

    def __init__(self, results: list) -> None:
        self._results = results

    @property
    def caps(self):
        return None

    def count_tokens(self, items):
        return None

    def submit(self, items, idempotency_key, *, known_refs=None):
        return cost_report.BatchRef(provider="stub", batch_id="b1", idempotency_key=idempotency_key)

    def find_batch(self, idempotency_key):
        return None

    def poll(self, ref):
        return None

    def fetch(self, ref):
        yield from self._results

    def cancel(self, ref):
        pass


def _call(custom_id: str) -> "cost_report.RenderedCall":
    from lazycode.ir import Message, PrefixBlock, RenderedCall

    return RenderedCall(
        custom_id=custom_id,
        model="m",
        system=[PrefixBlock(text="shared prefix", cache_hint=True)],
        messages=[Message(role="user", content=f"unique for {custom_id}")],
        max_tokens=100,
        temperature=0.0,
        memo_key=f"memo-{custom_id}",
    )


def test_errored_item_with_usage_is_recorded_and_flagged():
    from lazycode.ir import ItemResult, ItemStatus

    results = [
        ItemResult(
            custom_id="n1",
            status=ItemStatus.COMPLETED,
            payload={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 50, "output_tokens": 10},
            },
        ),
        ItemResult(
            custom_id="n2",
            status=ItemStatus.ERRORED,
            error={"type": "content_policy", "usage": {"input_tokens": 60, "output_tokens": 3}},
        ),
    ]
    metering = cost_report.MeteringAdapter(_StubInnerAdapter(results), tier="batch")
    ref = metering.submit([_call("n1"), _call("n2")], "idem-1")
    list(metering.fetch(ref))

    by_node = {lg.node_id: lg for lg in metering.ledgers}
    assert set(by_node) == {"n1", "n2"}
    assert by_node["n1"].errored is False
    assert by_node["n2"].errored is True
    assert by_node["n2"].reported_input_tokens == 60


def test_errored_item_without_usage_is_dropped():
    """A request rejected before it was ever sent costs nothing and correctly
    stays out of the billable ledger."""
    from lazycode.ir import ItemResult, ItemStatus

    results = [
        ItemResult(custom_id="n1", status=ItemStatus.ERRORED, error={"type": "invalid_request"}),
    ]
    metering = cost_report.MeteringAdapter(_StubInnerAdapter(results), tier="batch")
    ref = metering.submit([_call("n1")], "idem-2")
    list(metering.fetch(ref))
    assert metering.ledgers == []


# --- the savings comparison refuses to compare mismatched node sets ------------


def test_savings_comparison_normal_case_returns_a_numeric_saving():
    saved, saved_pct, warning = cost_report._savings_comparison({"n1", "n2"}, {"n1", "n2"}, 10.0, 6.0)
    assert saved == pytest.approx(4.0)
    assert saved_pct == pytest.approx(40.0)
    assert warning is None


def test_savings_comparison_refuses_when_node_sets_differ():
    saved, saved_pct, warning = cost_report._savings_comparison({"n1", "n2"}, {"n1"}, 10.0, 6.0)
    assert saved is None
    assert saved_pct is None
    assert warning is not None
    assert "did NOT complete the identical node set" in warning


def test_savings_comparison_refuses_when_either_side_is_empty():
    saved, _saved_pct, warning = cost_report._savings_comparison(set(), set(), 10.0, 6.0)
    assert saved is None
    assert warning is not None


def test_report_comparison_warning_is_none_when_both_tiers_complete_everything(report: dict):
    assert report["comparison_warning"] is None
    assert report["saved_usd"] is not None
