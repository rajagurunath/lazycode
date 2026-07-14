"""Tests for eventlog.py: append/read round-trip, monotonic seq, record()."""

from __future__ import annotations

from datetime import UTC, datetime

from lazycode.ir import Event, EventType
from lazycode.store import Store, eventlog


def test_append_assigns_monotonic_seq(store: Store):
    e1 = Event(seq=0, job_id="j1", ts=datetime.now(UTC), type=EventType.JOB_CREATED, payload={})
    e2 = Event(seq=0, job_id="j1", ts=datetime.now(UTC), type=EventType.PLAN_PROPOSED, payload={})
    seq1 = eventlog.append(store, e1)
    seq2 = eventlog.append(store, e2)
    assert seq1 == 1
    assert seq2 == 2


def test_record_builds_and_appends(store: Store):
    ev = eventlog.record(store, job_id="j1", type=EventType.JOB_CREATED, payload={"goal": "g"})
    assert ev.seq == 1
    assert ev.job_id == "j1"
    assert ev.payload == {"goal": "g"}


def test_read_round_trip(store: Store):
    original = [
        eventlog.record(store, job_id="j1", type=EventType.JOB_CREATED, payload={"goal": "g"}),
        eventlog.record(store, job_id="j1", type=EventType.PLAN_PROPOSED, payload={"n": 1}),
        eventlog.record(store, job_id="j1", type=EventType.PLAN_APPROVED, payload={}),
    ]
    got = list(eventlog.read(store, "j1"))
    assert got == original


def test_read_scoped_to_job(store: Store):
    eventlog.record(store, job_id="j1", type=EventType.JOB_CREATED, payload={})
    eventlog.record(store, job_id="j2", type=EventType.JOB_CREATED, payload={})
    j1_events = list(eventlog.read(store, "j1"))
    j2_events = list(eventlog.read(store, "j2"))
    assert len(j1_events) == 1
    assert len(j2_events) == 1
    assert j1_events[0].job_id == "j1"
    assert j2_events[0].job_id == "j2"


def test_read_after_seq_cursor(store: Store):
    e1 = eventlog.record(store, job_id="j1", type=EventType.JOB_CREATED, payload={})
    e2 = eventlog.record(store, job_id="j1", type=EventType.PLAN_PROPOSED, payload={})
    e3 = eventlog.record(store, job_id="j1", type=EventType.PLAN_APPROVED, payload={})

    remaining = list(eventlog.read(store, "j1", after_seq=e1.seq))
    assert [e.type for e in remaining] == [e2.type, e3.type]


def test_read_preserves_seq_order(store: Store):
    for i in range(10):
        eventlog.record(store, job_id="j1", type=EventType.NODE_ADDED, payload={"i": i})
    seqs = [e.seq for e in eventlog.read(store, "j1")]
    assert seqs == sorted(seqs)


def test_events_are_append_only_source_of_truth(store: Store):
    """Appending never mutates a previous row; every read reflects full history."""
    eventlog.record(store, job_id="j1", type=EventType.JOB_CREATED, payload={"v": 1})
    eventlog.record(store, job_id="j1", type=EventType.JOB_CREATED, payload={"v": 2})
    rows = store.conn.execute("SELECT COUNT(*) FROM events WHERE job_id='j1'").fetchone()[0]
    assert rows == 2
