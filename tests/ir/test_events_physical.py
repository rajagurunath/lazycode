"""Tests for events, state enums, and the physical plan (ir/events.py, ir/physical.py)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from lazycode.ir import (
    Event,
    EventType,
    ExecClass,
    FanoutResolvedPayload,
    LeasePayload,
    NodeStateChangedPayload,
    NodeStatus,
    PhysicalNodeAssignment,
    Wave,
    WaveStatus,
    WaveSubmittedPayload,
)


def test_event_type_vocabulary_complete():
    # The 23-symbol B5 vocabulary.
    assert len(list(EventType)) == 23
    assert EventType.WAVE_SUBMITTED == "WAVE_SUBMITTED"
    assert EventType.ARTIFACT_APPLY_INTENT == "ARTIFACT_APPLY_INTENT"


def test_event_roundtrip_serialization():
    ev = Event(
        seq=1,
        job_id="job-1",
        ts=datetime(2026, 7, 14, 18, 0, tzinfo=UTC),
        type=EventType.WAVE_SUBMITTED,
        payload={"wave_id": "w1"},
    )
    dumped = ev.model_dump(mode="json")
    assert dumped["type"] == "WAVE_SUBMITTED"
    restored = Event.model_validate(dumped)
    assert restored == ev


def test_event_extra_field_rejected():
    with pytest.raises(ValidationError):
        Event(
            seq=1,
            job_id="j",
            ts=datetime.now(UTC),
            type=EventType.JOB_CREATED,
            surprise=1,
        )


def test_wave_submitted_payload_as_event_payload():
    payload = WaveSubmittedPayload(
        wave_id="w1",
        provider="anthropic",
        model="claude-haiku-4-5",
        batch_ref="batch_abc",
        idempotency_key="deadbeef:0",
        node_ids=["n3.0", "n3.1"],
        item_count=2,
    )
    ev = Event(
        seq=2,
        job_id="job-1",
        ts=datetime.now(UTC),
        type=EventType.WAVE_SUBMITTED,
        payload=payload.model_dump(mode="json"),
    )
    parsed = WaveSubmittedPayload.model_validate(ev.payload)
    assert parsed == payload


def test_node_state_changed_payload_uses_enum():
    p = NodeStateChangedPayload(
        node_id="n1", from_status=NodeStatus.SUBMITTED, to_status=NodeStatus.RETURNED
    )
    assert p.to_status is NodeStatus.RETURNED


def test_fanout_resolved_payload_aligned():
    p = FanoutResolvedPayload(
        parent_id="n3.*",
        child_ids=["n3.0", "n3.1"],
        bindings=[{"module": "a.py"}, {"module": "b.py"}],
    )
    assert len(p.child_ids) == len(p.bindings)


def test_lease_payload_roundtrip():
    p = LeasePayload(job_id="j", holder_id="daemon-1", expires_at=datetime.now(UTC))
    assert LeasePayload.model_validate(p.model_dump(mode="json")) == p


def test_node_status_covers_local_and_remote_and_terminals():
    for name in [
        "PENDING",
        "READY",
        "EXECUTING_LOCAL",
        "COMPLETED_LOCAL",
        "HARVESTED",
        "ENQUEUED",
        "SUBMITTED",
        "RETURNED",
        "APPLIED",
        "EXPIRED",
        "RE_ENQUEUED",
        "HEDGED",
        "REPAIR_SPAWNED",
        "NEEDS_HUMAN",
        "WAITING_APPROVAL",
        "APPROVED",
        "REJECTED",
        "DONE",
        "SUPERSEDED",
        "CANCELLED",
        "ABANDONED",
    ]:
        assert hasattr(NodeStatus, name)


def test_exec_class_values():
    assert {e.value for e in ExecClass} == {"batch", "realtime", "local", "speculative"}


# --- physical plan -----------------------------------------------------------


def test_physical_assignment_optional_speculation_fields():
    a = PhysicalNodeAssignment(
        node_id="n3.0",
        wave_id="w2",
        exec_class=ExecClass.BATCH,
        provider="anthropic",
        model="claude-haiku-4-5",
        prefix_block_id="P1",
    )
    assert a.spec_group_id is None
    assert a.branch_label is None


def test_wave_defaults_and_status_rank():
    w = Wave(id="w2", job_id="job-1", provider="anthropic", model="claude-haiku-4-5")
    assert w.exec_class is ExecClass.BATCH
    assert w.status is WaveStatus.FORMED
    # B6: "status >= SUBMITTED" counts as a submitted wave.
    assert WaveStatus.SUBMITTED.rank >= WaveStatus.SUBMITTED.rank
    assert WaveStatus.COMPLETED.rank > WaveStatus.SUBMITTED.rank
    assert WaveStatus.FORMED.rank < WaveStatus.SUBMITTED.rank


def test_wave_extra_field_rejected():
    with pytest.raises(ValidationError):
        Wave(id="w", job_id="j", provider="p", model="m", nope=1)
