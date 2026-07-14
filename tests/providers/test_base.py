"""Tests for the provider Protocols, error hierarchy, and backoff helper
(providers/base.py).
"""

from __future__ import annotations

from itertools import islice

from lazycode.providers.base import (
    AdapterError,
    BatchAdapter,
    FatalError,
    RateLimited,
    RealtimeAdapter,
    RetryableError,
    backoff_delays,
)
from lazycode.providers.mock import MockBatchAdapter, MockRealtimeAdapter

# --- error hierarchy ----------------------------------------------------------


def test_error_hierarchy():
    assert issubclass(RetryableError, AdapterError)
    assert issubclass(FatalError, AdapterError)
    assert issubclass(RateLimited, RetryableError)
    assert issubclass(RateLimited, AdapterError)
    assert not issubclass(FatalError, RetryableError)


def test_rate_limited_carries_retry_after():
    err = RateLimited("nope", retry_after=12.5)
    assert err.retry_after == 12.5
    assert str(err) == "nope"


def test_rate_limited_retry_after_defaults_to_none():
    assert RateLimited().retry_after is None


def test_adapter_error_preserves_cause():
    original = ValueError("boom")
    try:
        raise FatalError("wrapped") from original
    except FatalError as exc:
        assert exc.__cause__ is original


# --- backoff_delays -----------------------------------------------------------


def test_backoff_delays_exponential_without_jitter():
    delays = list(islice(backoff_delays(base=1.0, cap=100.0, jitter=False), 5))
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_backoff_delays_respects_cap():
    delays = list(islice(backoff_delays(base=1.0, cap=5.0, jitter=False), 6))
    assert delays[-1] == 5.0
    assert max(delays) == 5.0


def test_backoff_delays_jitter_stays_in_bounds():
    for delay in islice(backoff_delays(base=2.0, cap=100.0, jitter=True), 20):
        assert 0 < delay <= 100.0 * 1.25 + 1e-9


def test_backoff_delays_is_a_pure_generator_not_a_sleeper():
    """Advancing the iterator must not block -- it's just arithmetic."""
    gen = backoff_delays(base=0.001, jitter=False)
    first_ten = list(islice(gen, 10))
    assert len(first_ten) == 10


# --- protocol conformance ------------------------------------------------------


def test_mock_batch_adapter_satisfies_protocol():
    assert isinstance(MockBatchAdapter(), BatchAdapter)


def test_mock_realtime_adapter_satisfies_protocol():
    assert isinstance(MockRealtimeAdapter(), RealtimeAdapter)
