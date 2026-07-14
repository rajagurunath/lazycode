"""Tests for projections.py: per-event handlers, and apply-live vs
replay-from-scratch equivalence (the ``lazycode doctor --rebuild`` contract).
"""

from __future__ import annotations

from datetime import UTC, datetime


from lazycode.ir import EventType
from lazycode.store import Store, eventlog, projections
from lazycode.store.projections import _HANDLERS


def _snapshot(store: Store, job_id: str) -> dict:
    """A comparable, order-independent snapshot of a job's projection rows."""
    jobs = [dict(r) for r in store.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,))]
    nodes = sorted(
        (dict(r) for r in store.conn.execute("SELECT * FROM nodes WHERE job_id=?", (job_id,))),
        key=lambda r: r["id"],
    )
    waves = sorted(
        (dict(r) for r in store.conn.execute("SELECT * FROM waves WHERE job_id=?", (job_id,))),
        key=lambda r: r["id"],
    )
    return {"jobs": jobs, "nodes": nodes, "waves": waves}


def _seed_events(store: Store, job_id: str) -> None:
    """A representative slice of a job lifecycle touching every table."""
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    eventlog.record(
        store,
        job_id=job_id,
        type=EventType.JOB_CREATED,
        payload={"goal": "add type hints", "repo": "/repo", "base_commit": "abc123", "slider": 70},
        ts=ts,
    )
    eventlog.record(store, job_id=job_id, type=EventType.PLAN_PROPOSED, payload={}, ts=ts)
    eventlog.record(store, job_id=job_id, type=EventType.PLAN_APPROVED, payload={}, ts=ts)
    eventlog.record(
        store,
        job_id=job_id,
        type=EventType.NODE_ADDED,
        payload={"node_id": "n1", "op": "Edit", "spec": {"files": ["a.py"]}, "deps": []},
        ts=ts,
    )
    eventlog.record(
        store,
        job_id=job_id,
        type=EventType.NODE_ADDED,
        payload={"node_id": "n2", "op": "Verify", "spec": {}, "deps": ["n1"]},
        ts=ts,
    )
    eventlog.record(store, job_id=job_id, type=EventType.NODE_READY, payload={"node_id": "n1"}, ts=ts)
    eventlog.record(store, job_id=job_id, type=EventType.NODE_HARVESTED, payload={"node_id": "n1"}, ts=ts)
    eventlog.record(
        store,
        job_id=job_id,
        type=EventType.WAVE_FORMED,
        payload={"wave_id": "w1", "provider": "anthropic", "model": "claude-x", "node_ids": ["n1"]},
        ts=ts,
    )
    eventlog.record(
        store,
        job_id=job_id,
        type=EventType.WAVE_SUBMITTED,
        payload={
            "wave_id": "w1",
            "provider": "anthropic",
            "model": "claude-x",
            "batch_ref": "batch_abc",
            "idempotency_key": "key1",
            "node_ids": ["n1"],
            "item_count": 1,
        },
        ts=ts,
    )
    eventlog.record(store, job_id=job_id, type=EventType.WAVE_COMPLETED, payload={"wave_id": "w1"}, ts=ts)
    eventlog.record(
        store, job_id=job_id, type=EventType.CONTRACT_RESULT, payload={"node_id": "n1", "passed": True}, ts=ts
    )
    eventlog.record(
        store,
        job_id=job_id,
        type=EventType.ARTIFACT_APPLY_INTENT,
        payload={"worktree": "/wt", "diff_hash": "d1", "node_id": "n1"},
        ts=ts,
    )
    eventlog.record(
        store,
        job_id=job_id,
        type=EventType.ARTIFACT_APPLIED,
        payload={"worktree": "/wt", "diff_hash": "d1", "node_id": "n1"},
        ts=ts,
    )
    eventlog.record(
        store, job_id=job_id, type=EventType.VERIFY_RESULT, payload={"node_id": "n2", "passed": True}, ts=ts
    )
    eventlog.record(store, job_id=job_id, type=EventType.NODE_DONE, payload={"node_id": "n1"}, ts=ts)
    eventlog.record(
        store,
        job_id=job_id,
        type=EventType.NODE_STATE_CHANGED,
        payload={"node_id": "n2", "from_status": "COMPLETED_LOCAL", "to_status": "DONE"},
        ts=ts,
    )
    eventlog.record(store, job_id=job_id, type=EventType.JOB_DONE, payload={}, ts=ts)


def test_all_event_types_have_a_handler():
    assert set(_HANDLERS) == set(EventType)
    assert len(_HANDLERS) == 23


def test_job_created_projection(store: Store):
    ev = eventlog.record(
        store,
        job_id="j1",
        type=EventType.JOB_CREATED,
        payload={"goal": "g", "repo": "r", "base_commit": "c", "slider": 30, "budget_usd": 5.0},
    )
    projections.apply(store, ev)
    row = dict(store.conn.execute("SELECT * FROM jobs WHERE id='j1'").fetchone())
    assert row["goal"] == "g"
    assert row["repo"] == "r"
    assert row["slider"] == 30
    assert row["budget_usd"] == 5.0
    assert row["status"] == "PENDING"


def test_plan_approved_moves_job_to_running(store: Store):
    projections.apply(
        store, eventlog.record(store, job_id="j1", type=EventType.JOB_CREATED, payload={"goal": "g", "repo": "r"})
    )
    projections.apply(store, eventlog.record(store, job_id="j1", type=EventType.PLAN_APPROVED, payload={}))
    row = store.conn.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()
    assert row["status"] == "RUNNING"


def test_node_added_then_node_ready(store: Store):
    projections.apply(
        store,
        eventlog.record(
            store, job_id="j1", type=EventType.NODE_ADDED, payload={"node_id": "n1", "op": "Edit", "deps": []}
        ),
    )
    row = store.conn.execute("SELECT status, op FROM nodes WHERE job_id='j1' AND id='n1'").fetchone()
    assert row["status"] == "PENDING"
    assert row["op"] == "Edit"

    projections.apply(store, eventlog.record(store, job_id="j1", type=EventType.NODE_READY, payload={"node_id": "n1"}))
    row = store.conn.execute("SELECT status FROM nodes WHERE job_id='j1' AND id='n1'").fetchone()
    assert row["status"] == "READY"


def test_fanout_resolved_mints_child_nodes_from_parent(store: Store):
    projections.apply(
        store,
        eventlog.record(
            store,
            job_id="j1",
            type=EventType.NODE_ADDED,
            payload={"node_id": "parent", "op": "Edit", "spec": {"x": 1}, "deps": [], "provider": "anthropic"},
        ),
    )
    projections.apply(
        store,
        eventlog.record(
            store,
            job_id="j1",
            type=EventType.FANOUT_RESOLVED,
            payload={
                "parent_id": "parent",
                "child_ids": ["parent.0", "parent.1"],
                "bindings": [{"module": "a"}, {"module": "b"}],
            },
        ),
    )
    children = {
        r["id"]: dict(r)
        for r in store.conn.execute(
            "SELECT * FROM nodes WHERE job_id='j1' AND id IN ('parent.0','parent.1')"
        )
    }
    assert set(children) == {"parent.0", "parent.1"}
    assert children["parent.0"]["template_parent_id"] == "parent"
    assert children["parent.0"]["op"] == "Edit"
    assert children["parent.0"]["provider"] == "anthropic"
    assert '"module": "a"' in children["parent.0"]["bindings"]
    assert children["parent.0"]["status"] == "PENDING"


def test_wave_lifecycle_updates_waves_and_nodes(store: Store):
    projections.apply(
        store, eventlog.record(store, job_id="j1", type=EventType.NODE_ADDED, payload={"node_id": "n1", "op": "Edit"})
    )
    projections.apply(
        store,
        eventlog.record(
            store,
            job_id="j1",
            type=EventType.WAVE_FORMED,
            payload={"wave_id": "w1", "provider": "anthropic", "model": "claude-x", "node_ids": ["n1"]},
        ),
    )
    wave = dict(store.conn.execute("SELECT * FROM waves WHERE job_id='j1' AND id='w1'").fetchone())
    assert wave["status"] == "FORMED"
    node = dict(store.conn.execute("SELECT * FROM nodes WHERE job_id='j1' AND id='n1'").fetchone())
    assert node["wave_id"] == "w1"
    assert node["provider"] == "anthropic"

    projections.apply(
        store,
        eventlog.record(
            store,
            job_id="j1",
            type=EventType.WAVE_SUBMITTED,
            payload={
                "wave_id": "w1",
                "provider": "anthropic",
                "model": "claude-x",
                "batch_ref": "b1",
                "idempotency_key": "k1",
                "node_ids": ["n1"],
                "item_count": 1,
            },
        ),
    )
    wave = dict(store.conn.execute("SELECT * FROM waves WHERE job_id='j1' AND id='w1'").fetchone())
    assert wave["status"] == "SUBMITTED"
    assert wave["batch_ref"] == "b1"
    assert wave["submitted_at"] is not None
    node = dict(store.conn.execute("SELECT * FROM nodes WHERE job_id='j1' AND id='n1'").fetchone())
    assert node["status"] == "SUBMITTED"

    projections.apply(
        store, eventlog.record(store, job_id="j1", type=EventType.WAVE_COMPLETED, payload={"wave_id": "w1"})
    )
    wave = dict(store.conn.execute("SELECT * FROM waves WHERE job_id='j1' AND id='w1'").fetchone())
    assert wave["status"] == "COMPLETED"
    assert wave["completed_at"] is not None


def test_contract_result_fail_routes_to_needs_human_m0(store: Store):
    projections.apply(
        store, eventlog.record(store, job_id="j1", type=EventType.NODE_ADDED, payload={"node_id": "n1", "op": "Edit"})
    )
    projections.apply(
        store,
        eventlog.record(
            store, job_id="j1", type=EventType.CONTRACT_RESULT, payload={"node_id": "n1", "passed": False}
        ),
    )
    row = store.conn.execute("SELECT status FROM nodes WHERE job_id='j1' AND id='n1'").fetchone()
    assert row["status"] == "NEEDS_HUMAN"


def test_artifact_applied_sets_node_applied(store: Store):
    projections.apply(
        store, eventlog.record(store, job_id="j1", type=EventType.NODE_ADDED, payload={"node_id": "n1", "op": "Edit"})
    )
    projections.apply(
        store,
        eventlog.record(
            store,
            job_id="j1",
            type=EventType.ARTIFACT_APPLIED,
            payload={"worktree": "/wt", "diff_hash": "d1", "node_id": "n1"},
        ),
    )
    row = store.conn.execute("SELECT status FROM nodes WHERE job_id='j1' AND id='n1'").fetchone()
    assert row["status"] == "APPLIED"


def test_verify_result_pass_and_fail(store: Store):
    for node_id, passed, expected in [("n1", True, "DONE"), ("n2", False, "NEEDS_HUMAN")]:
        projections.apply(
            store,
            eventlog.record(store, job_id="j1", type=EventType.NODE_ADDED, payload={"node_id": node_id, "op": "Verify"}),
        )
        projections.apply(
            store,
            eventlog.record(
                store, job_id="j1", type=EventType.VERIFY_RESULT, payload={"node_id": node_id, "passed": passed}
            ),
        )
        row = store.conn.execute("SELECT status FROM nodes WHERE job_id='j1' AND id=?", (node_id,)).fetchone()
        assert row["status"] == expected


def test_item_returned_updates_nodes_via_call_items_join(store: Store):
    projections.apply(
        store, eventlog.record(store, job_id="j1", type=EventType.NODE_ADDED, payload={"node_id": "n1", "op": "Edit"})
    )
    store.conn.execute(
        "INSERT INTO llm_calls(id, memo_key, mode) VALUES ('call1', 'memo1', 'batch')"
    )
    store.conn.execute(
        "INSERT INTO call_items(call_id, node_id, custom_id) VALUES ('call1', 'n1', 'custom1')"
    )
    projections.apply(
        store,
        eventlog.record(
            store,
            job_id="j1",
            type=EventType.ITEM_RETURNED,
            payload={"wave_id": "w1", "custom_id": "custom1", "status": "completed", "call_id": "call1"},
        ),
    )
    row = store.conn.execute("SELECT status FROM nodes WHERE job_id='j1' AND id='n1'").fetchone()
    assert row["status"] == "RETURNED"

    projections.apply(
        store,
        eventlog.record(
            store,
            job_id="j1",
            type=EventType.ITEM_RETURNED,
            payload={"wave_id": "w1", "custom_id": "custom1", "status": "expired", "call_id": "call1"},
        ),
    )
    row = store.conn.execute("SELECT status FROM nodes WHERE job_id='j1' AND id='n1'").fetchone()
    assert row["status"] == "EXPIRED"


def test_item_returned_without_matching_call_items_is_harmless_noop(store: Store):
    ev = eventlog.record(
        store,
        job_id="j1",
        type=EventType.ITEM_RETURNED,
        payload={"wave_id": "w1", "custom_id": "unknown", "status": "completed", "call_id": "missing-call"},
    )
    projections.apply(store, ev)  # must not raise


def test_lease_and_artifact_intent_events_are_projection_noops(store: Store):
    """These events don't touch jobs/nodes/waves — applying them must not raise
    and must not create any rows."""
    for etype, payload in [
        (EventType.LEASE_ACQUIRED, {"job_id": "j1", "holder_id": "h1", "expires_at": "2026-01-01T00:00:00Z"}),
        (EventType.LEASE_RENEWED, {"job_id": "j1", "holder_id": "h1", "expires_at": "2026-01-01T00:00:00Z"}),
        (EventType.ARTIFACT_APPLY_INTENT, {"worktree": "/wt", "diff_hash": "d1", "node_id": "n1"}),
        (EventType.NODE_RESULT_CHOSEN, {"node_id": "n1", "call_id": "c1"}),
        (EventType.PLAN_PROPOSED, {}),
    ]:
        ev = eventlog.record(store, job_id="j1", type=etype, payload=payload)
        projections.apply(store, ev)  # must not raise
    assert store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_rebuild_replay_matches_live_apply(store: Store):
    """The core equivalence property: applying events live as they're appended
    produces the exact same jobs/nodes/waves rows as wiping and replaying the
    full event log from scratch (the ``doctor --rebuild`` contract, §11)."""
    job_id = "job-equiv"

    class _LiveApplier:
        def __init__(self):
            self.after = 0

        def catch_up(self):
            for ev in eventlog.read(store, job_id, after_seq=self.after):
                projections.apply(store, ev)
                self.after = ev.seq

    applier = _LiveApplier()
    _seed_events(store, job_id)
    applier.catch_up()  # applies everything "live" (as if applied incrementally)

    live_snapshot = _snapshot(store, job_id)

    projections.rebuild(store, job_id)
    rebuilt_snapshot = _snapshot(store, job_id)

    assert live_snapshot == rebuilt_snapshot
    # sanity: the snapshot actually has content, not two empty dicts matching trivially
    assert live_snapshot["jobs"]
    assert live_snapshot["nodes"]
    assert live_snapshot["waves"]


def test_rebuild_is_idempotent(store: Store):
    job_id = "job-idem"
    _seed_events(store, job_id)
    projections.rebuild(store, job_id)
    first = _snapshot(store, job_id)
    projections.rebuild(store, job_id)
    second = _snapshot(store, job_id)
    assert first == second


def test_rebuild_only_touches_target_job(store: Store):
    _seed_events(store, "job-a")
    _seed_events(store, "job-b")
    before_b = _snapshot(store, "job-b")
    projections.rebuild(store, "job-a")
    after_b = _snapshot(store, "job-b")
    assert before_b == after_b
