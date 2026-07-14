"""Tests for ledger.py: applied-diff ledger idempotency (DESIGN.md §9)."""

from __future__ import annotations

from lazycode.ir import EventType
from lazycode.store import Store, eventlog, ledger


def test_already_applied_false_initially(store: Store):
    assert ledger.already_applied(store, worktree="/wt", diff_hash="d1") is False


def test_record_intent_appends_event(store: Store):
    ledger.record_intent(store, job_id="j1", worktree="/wt", diff_hash="d1", node_id="n1")
    events = list(eventlog.read(store, "j1"))
    assert len(events) == 1
    assert events[0].type == EventType.ARTIFACT_APPLY_INTENT
    assert events[0].payload == {"worktree": "/wt", "diff_hash": "d1", "node_id": "n1"}


def test_record_intent_does_not_mark_applied(store: Store):
    ledger.record_intent(store, job_id="j1", worktree="/wt", diff_hash="d1", node_id="n1")
    assert ledger.already_applied(store, worktree="/wt", diff_hash="d1") is False


def test_record_applied_marks_ledger_and_appends_event(store: Store):
    result = ledger.record_applied(store, job_id="j1", worktree="/wt", diff_hash="d1", node_id="n1")
    assert result is True
    assert ledger.already_applied(store, worktree="/wt", diff_hash="d1") is True
    events = [e.type for e in eventlog.read(store, "j1")]
    assert events == [EventType.ARTIFACT_APPLIED]


def test_record_applied_is_idempotent_on_replay(store: Store):
    """Crash-replay calling record_applied twice for the same diff must not
    double-apply or double-log — this is what makes applies exactly-once."""
    first = ledger.record_applied(store, job_id="j1", worktree="/wt", diff_hash="d1", node_id="n1")
    second = ledger.record_applied(store, job_id="j1", worktree="/wt", diff_hash="d1", node_id="n1")
    assert first is True
    assert second is False
    count = store.conn.execute(
        "SELECT COUNT(*) FROM applied_diffs WHERE worktree='/wt' AND diff_hash='d1'"
    ).fetchone()[0]
    assert count == 1
    events = [e.type for e in eventlog.read(store, "j1")]
    assert events == [EventType.ARTIFACT_APPLIED]  # only one, not two


def test_ledger_scoped_per_worktree(store: Store):
    """Same diff_hash in two different worktrees are independent entries."""
    ledger.record_applied(store, job_id="j1", worktree="/wt-a", diff_hash="d1", node_id="n1")
    assert ledger.already_applied(store, worktree="/wt-a", diff_hash="d1") is True
    assert ledger.already_applied(store, worktree="/wt-b", diff_hash="d1") is False


def test_ledger_scoped_per_diff_hash(store: Store):
    ledger.record_applied(store, job_id="j1", worktree="/wt", diff_hash="d1", node_id="n1")
    assert ledger.already_applied(store, worktree="/wt", diff_hash="d2") is False


def test_full_apply_flow_intent_then_applied(store: Store):
    assert ledger.already_applied(store, worktree="/wt", diff_hash="d1") is False
    ledger.record_intent(store, job_id="j1", worktree="/wt", diff_hash="d1", node_id="n1")
    assert ledger.already_applied(store, worktree="/wt", diff_hash="d1") is False
    applied = ledger.record_applied(store, job_id="j1", worktree="/wt", diff_hash="d1", node_id="n1")
    assert applied is True
    assert ledger.already_applied(store, worktree="/wt", diff_hash="d1") is True
    events = [e.type for e in eventlog.read(store, "j1")]
    assert events == [EventType.ARTIFACT_APPLY_INTENT, EventType.ARTIFACT_APPLIED]
