"""Illustrative list-price table for the benchmark harness (DESIGN.md
Appendix B7: "prices at list").

**Not authoritative.** These are placeholder, benchmark-only figures in
USD per million tokens -- verify against the provider's current pricing
page before treating any number this module produces as a real cost.
They exist so ``bench/compare.py`` can render a dollar column and the
M0 accept criterion (b) ``<50%`` check alongside the raw token comparison
(which *is* the number Appendix B7 actually specifies: "tokens x list
price"). Edit :data:`PRICE_TABLE` (or point ``LAZYCODE_BENCH_PRICING`` at a
JSON file of the same shape) to use current prices.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# USD per 1,000,000 tokens, realtime list price (no cache, no batch discount).
PRICE_TABLE: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-opus-4": {"input": 15.00, "output": 75.00},
}

_BATCH_DISCOUNT = 0.5  # Appendix A: flat 50% off all models on batch, both providers.


def _table() -> dict[str, dict[str, float]]:
    override = os.environ.get("LAZYCODE_BENCH_PRICING")
    if override:
        return json.loads(Path(override).read_text(encoding="utf-8"))
    return PRICE_TABLE


def _rate(model: str, table: dict[str, dict[str, float]]) -> dict[str, float]:
    if model in table:
        return table[model]
    # Fall back to the cheapest tier's rates rather than raising -- a
    # benchmark run against an unlisted model should still produce a
    # (clearly approximate) number, not crash.
    return next(iter(table.values()))


def realtime_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Uncached realtime list price -- what the Appendix B7 baseline (Claude
    Code CLI, single run) pays."""
    table = _table()
    rate = _rate(model, table)
    return (tokens_in / 1_000_000) * rate["input"] + (tokens_out / 1_000_000) * rate["output"]


def batch_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """List price at the flat batch discount -- what lazycode's own actuals
    (from ``llm_calls``, batch-mode calls) pay."""
    return realtime_cost_usd(model, tokens_in, tokens_out) * _BATCH_DISCOUNT
