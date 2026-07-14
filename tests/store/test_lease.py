"""Tests for lease.py: acquire/renew/release, contention, expiry takeover."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from lazycode.ir import EventType
from lazycode.store import Store, eventlog, lease


def test_acquire_when_no_lease_exists(store: Store):
    assert lease.acquire(store, "j1", "holder-a", ttl_s=60) is True
    holder, expires_at = lease.current(store, "j1")
    assert holder == "holder-a"
    assert expires_at > datetime.now(UTC)


def test_acquire_records_event(store: Store):
    lease.acquire(store, "j1", "holder-a", ttl_s=60)
    events = list(eventlog.read(store, "j1"))
    assert len(events) == 1
    assert events[0].type == EventType.LEASE_ACQUIRED
    assert events[0].payload["holder_id"] == "holder-a"


def test_second_holder_cannot_acquire_active_lease(store: Store):
    assert lease.acquire(store, "j1", "holder-a", ttl_s=60) is True
    assert lease.acquire(store, "j1", "holder-b", ttl_s=60) is False
    holder, _ = lease.current(store, "j1")
    assert holder == "holder-a"


def test_same_holder_can_reacquire(store: Store):
    assert lease.acquire(store, "j1", "holder-a", ttl_s=60) is True
    assert lease.acquire(store, "j1", "holder-a", ttl_s=60) is True


def test_takeover_after_expiry(store: Store):
    assert lease.acquire(store, "j1", "holder-a", ttl_s=0.01) is True
    time.sleep(0.05)
    assert lease.acquire(store, "j1", "holder-b", ttl_s=60) is True
    holder, _ = lease.current(store, "j1")
    assert holder == "holder-b"


def test_renew_extends_expiry_for_current_holder(store: Store):
    lease.acquire(store, "j1", "holder-a", ttl_s=1)
    _, first_expiry = lease.current(store, "j1")
    assert lease.renew(store, "j1", "holder-a", ttl_s=3600) is True
    _, second_expiry = lease.current(store, "j1")
    assert second_expiry > first_expiry
    events = [e.type for e in eventlog.read(store, "j1")]
    assert events == [EventType.LEASE_ACQUIRED, EventType.LEASE_RENEWED]


def test_renew_fails_for_non_holder(store: Store):
    lease.acquire(store, "j1", "holder-a", ttl_s=60)
    assert lease.renew(store, "j1", "holder-b", ttl_s=60) is False


def test_renew_fails_when_no_lease_exists(store: Store):
    assert lease.renew(store, "j1", "holder-a", ttl_s=60) is False


def test_renew_fails_after_takeover(store: Store):
    lease.acquire(store, "j1", "holder-a", ttl_s=0.01)
    time.sleep(0.05)
    lease.acquire(store, "j1", "holder-b", ttl_s=60)
    assert lease.renew(store, "j1", "holder-a", ttl_s=60) is False


def test_release_by_holder_succeeds(store: Store):
    lease.acquire(store, "j1", "holder-a", ttl_s=60)
    assert lease.release(store, "j1", "holder-a") is True
    assert lease.current(store, "j1") is None


def test_release_by_non_holder_fails(store: Store):
    lease.acquire(store, "j1", "holder-a", ttl_s=60)
    assert lease.release(store, "j1", "holder-b") is False
    assert lease.current(store, "j1") is not None


def test_release_appends_no_event(store: Store):
    """B5 has no LEASE_RELEASED event type — release() is silent (see module docstring)."""
    lease.acquire(store, "j1", "holder-a", ttl_s=60)
    lease.release(store, "j1", "holder-a")
    events = [e.type for e in eventlog.read(store, "j1")]
    assert events == [EventType.LEASE_ACQUIRED]


def test_release_after_acquire_allows_reacquire_by_other(store: Store):
    lease.acquire(store, "j1", "holder-a", ttl_s=60)
    lease.release(store, "j1", "holder-a")
    assert lease.acquire(store, "j1", "holder-b", ttl_s=60) is True


def test_leases_are_per_job(store: Store):
    assert lease.acquire(store, "job-a", "holder-a", ttl_s=60) is True
    assert lease.acquire(store, "job-b", "holder-b", ttl_s=60) is True
    assert lease.current(store, "job-a")[0] == "holder-a"
    assert lease.current(store, "job-b")[0] == "holder-b"


def test_current_returns_none_when_unset(store: Store):
    assert lease.current(store, "nope") is None
