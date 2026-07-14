"""Projection updaters — replay events into ``jobs``/``nodes``/``waves`` rows
(DESIGN.md §7.1, §11).

Scope (deliberately narrow): this module owns exactly the three *projection*
tables named in §7.1/§11 — ``jobs``, ``nodes``, ``waves``. Every other table in
the schema (``leases``, ``llm_calls``, ``call_items``, ``artifacts``,
``applied_diffs``, ``stats``, ``task_groups``) is written directly by its own
owning module (``lease.py``, ``memo.py``, ``ledger.py``, ``cas.py``,
``stats.py``, and — outside ``store/`` — the future ``workspace/`` module for
``task_groups``), not derived here. Those tables are either themselves a
source-of-truth ledger (``applied_diffs``, per §9) or a materialized cache with
data (blob bytes, running averages) that doesn't round-trip through an event
payload, so replaying the event log would not reconstruct them faithfully.
``jobs``/``nodes``/``waves`` are the ones DESIGN.md explicitly calls
"projections rebuilt from [events]" (§7.1) and the ones ``doctor --rebuild``
targets (§11).

Event payload contract
-----------------------
Six B5 events carry a typed payload pinned by ``lazycode.ir.events`` (used
verbatim below): ``WAVE_SUBMITTED``, ``ITEM_RETURNED``, ``ARTIFACT_APPLIED``,
``ARTIFACT_APPLY_INTENT``, ``FANOUT_RESOLVED``, ``NODE_STATE_CHANGED``,
``NODE_RESULT_CHOSEN``, ``LEASE_ACQUIRED``/``LEASE_RENEWED``. The remaining B5
events are *not* given a typed payload by ``ir`` (§7.1 leaves their shape to
the writer), so this module pins a minimal payload contract for them — the
convention scheduler-side writers (future ``scheduler/``) must follow for
projections to update correctly. Keys are read with ``.get`` and sensible
defaults, so a missing key degrades to a no-op field rather than a crash.

| Event                | Expected payload keys                                            |
|-----------------------|-------------------------------------------------------------------|
| ``JOB_CREATED``       | ``goal, repo, base_commit, slider, budget_usd, deadline_utc``      |
| ``PLAN_PROPOSED``     | (none consumed — no plan table in §11; informational only)        |
| ``PLAN_APPROVED``     | (none — marks the job ``RUNNING``)                                 |
| ``NODE_ADDED``        | ``node_id, op, spec, deps, group_id, provider, model, exec_class, template_parent_id, bindings`` |
| ``NODE_READY``        | ``node_id``                                                        |
| ``NODE_HARVESTED``    | ``node_id``                                                        |
| ``WAVE_FORMED``       | ``wave_id, provider, model, node_ids, exec_class``                 |
| ``WAVE_COMPLETED``    | ``wave_id``                                                        |
| ``CONTRACT_RESULT``   | ``node_id, passed``                                                |
| ``VERIFY_RESULT``     | ``node_id, passed``                                                |
| ``NODE_DONE``         | ``node_id``                                                        |
| ``NODE_NEEDS_HUMAN``  | ``node_id``                                                        |
| ``JOB_DONE``          | (none — marks the job ``DONE``)                                    |
| ``JOB_CANCELLED``     | (none — marks the job ``CANCELLED``)                               |

Resolved ambiguities:
  - ``FANOUT_RESOLVED`` mints a ``nodes`` row per ``child_id`` (id, deps, spec,
    op, group_id, provider, model are copied from the parent row when it
    exists), with ``template_parent_id=parent_id`` and ``bindings`` set
    positionally — this is the only event that *creates* rows outside
    ``NODE_ADDED``, since fan-out children are minted at resolution time
    (§3.2), not individually planned.
  - ``ITEM_RETURNED`` has no ``node_id`` in its typed payload (it is
    per-``custom_id``, and one call may vectorize to *k* nodes — R6). The
    handler resolves ``node_id``(s) via a join through ``call_items`` (owned by
    ``memo.py``) on ``(call_id, custom_id)``; a completed item moves those
    nodes to ``RETURNED``, an errored/expired item moves them to ``EXPIRED``
    (§7.4: ``SUBMITTED → EXPIRED → RE_ENQUEUED | HEDGED``). If no matching
    ``call_items`` rows exist yet (e.g. replay ordering), the event is a
    harmless no-op for the nodes table — the log itself is still authoritative.
  - ``CONTRACT_RESULT``/``VERIFY_RESULT`` with ``passed=False`` moves the node
    straight to ``NEEDS_HUMAN`` — M0 has no repair loop (Appendix B11).
    ``CONTRACT_RESULT`` with ``passed=True`` is a no-op (the real transition to
    ``APPLIED`` happens on ``ARTIFACT_APPLIED``); ``VERIFY_RESULT`` with
    ``passed=True`` moves the (local) Verify node straight to ``DONE``.
  - ``ARTIFACT_APPLY_INTENT``, ``NODE_RESULT_CHOSEN``, ``LEASE_ACQUIRED``,
    ``LEASE_RENEWED`` do not touch ``jobs``/``nodes``/``waves`` — their durable
    state lives in ``applied_diffs`` (ledger.py) and ``leases`` (lease.py)
    respectively; the event log is their only trace at the projection layer.
  - ``nodes.group_id`` is nullable and is passed through as given; this module
    never creates ``task_groups`` rows (that's workspace/'s job, out of
    ``store/`` scope), so callers must ensure the referenced ``task_groups``
    row exists first when ``group_id`` is non-null (FK is enforced,
    ``PRAGMA foreign_keys=ON``).
  - Optimizer-owned node columns (``est_in``, ``est_out``, ``act_in``,
    ``act_out``, ``cost_usd``, ``attempt``, ``rounds``) are not driven by any
    B5 event and are left untouched here; a future milestone may add events
    for them.

``apply()`` is the single dispatch entry point (one handler per
:class:`~lazycode.ir.EventType`, 23 total, several intentionally no-ops).
``rebuild()`` wipes a job's projection rows and replays its full event history
through ``apply()`` — used for equivalence testing and ``lazycode doctor
--rebuild``.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from lazycode.ir import Event, EventType, ItemStatus, NodeStatus

from . import eventlog
from .db import Store, transaction

Handler = Callable[[Store, Event], None]


# --- small read helpers -------------------------------------------------


def _node_row(store: Store, job_id: str, node_id: str) -> dict | None:
    row = store.conn.execute(
        "SELECT * FROM nodes WHERE job_id = ? AND id = ?", (job_id, node_id)
    ).fetchone()
    return dict(row) if row is not None else None


def _set_node_status(store: Store, job_id: str, node_id: str, status: NodeStatus) -> None:
    store.conn.execute(
        "UPDATE nodes SET status = ? WHERE job_id = ? AND id = ?",
        (status.value, job_id, node_id),
    )


def _set_nodes_status(store: Store, job_id: str, node_ids: list[str], status: NodeStatus) -> None:
    for node_id in node_ids:
        _set_node_status(store, job_id, node_id, status)


# --- jobs handlers --------------------------------------------------------


def on_job_created(store: Store, event: Event) -> None:
    p = event.payload
    store.conn.execute(
        """
        INSERT INTO jobs(id, goal, repo, base_commit, slider, budget_usd, deadline_utc, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
        ON CONFLICT(id) DO UPDATE SET
            goal=excluded.goal, repo=excluded.repo, base_commit=excluded.base_commit,
            slider=excluded.slider, budget_usd=excluded.budget_usd, deadline_utc=excluded.deadline_utc
        """,
        (
            event.job_id,
            p.get("goal", ""),
            p.get("repo", ""),
            p.get("base_commit"),
            p.get("slider", 70),
            p.get("budget_usd"),
            p.get("deadline_utc"),
            event.ts.isoformat(),
        ),
    )


def on_plan_proposed(store: Store, event: Event) -> None:
    """No projection table for plans (§11 has none) — informational only."""


def on_plan_approved(store: Store, event: Event) -> None:
    store.conn.execute("UPDATE jobs SET status = 'RUNNING' WHERE id = ?", (event.job_id,))


def on_job_done(store: Store, event: Event) -> None:
    store.conn.execute("UPDATE jobs SET status = 'DONE' WHERE id = ?", (event.job_id,))


def on_job_cancelled(store: Store, event: Event) -> None:
    store.conn.execute("UPDATE jobs SET status = 'CANCELLED' WHERE id = ?", (event.job_id,))


# --- nodes handlers --------------------------------------------------------


def on_node_added(store: Store, event: Event) -> None:
    p = event.payload
    node_id = p["node_id"]
    store.conn.execute(
        """
        INSERT INTO nodes(id, job_id, group_id, op, spec, deps, status, exec_class,
                           template_parent_id, bindings, provider, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, id) DO UPDATE SET
            group_id=excluded.group_id, op=excluded.op, spec=excluded.spec, deps=excluded.deps,
            exec_class=excluded.exec_class, template_parent_id=excluded.template_parent_id,
            bindings=excluded.bindings, provider=excluded.provider, model=excluded.model
        """,
        (
            node_id,
            event.job_id,
            p.get("group_id"),
            p.get("op", ""),
            json.dumps(p.get("spec", {}), sort_keys=True),
            json.dumps(p.get("deps", []), sort_keys=True),
            p.get("status", NodeStatus.PENDING.value),
            p.get("exec_class"),
            p.get("template_parent_id"),
            json.dumps(p["bindings"], sort_keys=True) if p.get("bindings") is not None else None,
            p.get("provider"),
            p.get("model"),
        ),
    )


def on_fanout_resolved(store: Store, event: Event) -> None:
    p = event.payload
    parent_id = p["parent_id"]
    child_ids: list[str] = p.get("child_ids", [])
    bindings: list[dict] = p.get("bindings", [])
    parent = _node_row(store, event.job_id, parent_id) or {}
    for i, child_id in enumerate(child_ids):
        binding = bindings[i] if i < len(bindings) else {}
        store.conn.execute(
            """
            INSERT INTO nodes(id, job_id, group_id, op, spec, deps, status, exec_class,
                               template_parent_id, bindings, provider, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, id) DO UPDATE SET
                template_parent_id=excluded.template_parent_id, bindings=excluded.bindings
            """,
            (
                child_id,
                event.job_id,
                parent.get("group_id"),
                parent.get("op", ""),
                parent.get("spec", "{}"),
                parent.get("deps", "[]"),
                NodeStatus.PENDING.value,
                parent.get("exec_class"),
                parent_id,
                json.dumps(binding, sort_keys=True),
                parent.get("provider"),
                parent.get("model"),
            ),
        )


def on_node_ready(store: Store, event: Event) -> None:
    _set_node_status(store, event.job_id, event.payload["node_id"], NodeStatus.READY)


def on_node_harvested(store: Store, event: Event) -> None:
    _set_node_status(store, event.job_id, event.payload["node_id"], NodeStatus.HARVESTED)


def on_contract_result(store: Store, event: Event) -> None:
    p = event.payload
    if p.get("passed") is False:
        _set_node_status(store, event.job_id, p["node_id"], NodeStatus.NEEDS_HUMAN)
    # passed=True: no-op here; ARTIFACT_APPLIED is what advances to APPLIED.


def on_artifact_applied(store: Store, event: Event) -> None:
    _set_node_status(store, event.job_id, event.payload["node_id"], NodeStatus.APPLIED)


def on_artifact_apply_intent(store: Store, event: Event) -> None:
    """No jobs/nodes/waves mutation — the durable ledger is ``applied_diffs``
    (ledger.py); this event is forensic-only at the projection layer."""


def on_verify_result(store: Store, event: Event) -> None:
    p = event.payload
    status = NodeStatus.DONE if p.get("passed") else NodeStatus.NEEDS_HUMAN
    _set_node_status(store, event.job_id, p["node_id"], status)


def on_node_result_chosen(store: Store, event: Event) -> None:
    """No dedicated column for the winning call — the event log is the record
    of hedge/speculation resolution (§7.6); no-op for projections."""


def on_node_done(store: Store, event: Event) -> None:
    _set_node_status(store, event.job_id, event.payload["node_id"], NodeStatus.DONE)


def on_node_needs_human(store: Store, event: Event) -> None:
    _set_node_status(store, event.job_id, event.payload["node_id"], NodeStatus.NEEDS_HUMAN)


def on_node_state_changed(store: Store, event: Event) -> None:
    p = event.payload
    _set_node_status(store, event.job_id, p["node_id"], NodeStatus(p["to_status"]))


def on_item_returned(store: Store, event: Event) -> None:
    p = event.payload
    call_id = p.get("call_id")
    custom_id = p["custom_id"]
    if call_id is None:
        return
    rows = store.conn.execute(
        "SELECT node_id FROM call_items WHERE call_id = ? AND custom_id = ?",
        (call_id, custom_id),
    ).fetchall()
    node_ids = [r["node_id"] for r in rows]
    if not node_ids:
        return
    status = (
        NodeStatus.RETURNED if p["status"] == ItemStatus.COMPLETED.value else NodeStatus.EXPIRED
    )
    _set_nodes_status(store, event.job_id, node_ids, status)


# --- waves handlers --------------------------------------------------------


def on_wave_formed(store: Store, event: Event) -> None:
    p = event.payload
    wave_id = p["wave_id"]
    node_ids: list[str] = p.get("node_ids", [])
    store.conn.execute(
        """
        INSERT INTO waves(id, job_id, provider, model, status)
        VALUES (?, ?, ?, ?, 'FORMED')
        ON CONFLICT(job_id, id) DO UPDATE SET provider=excluded.provider, model=excluded.model
        """,
        (wave_id, event.job_id, p.get("provider", ""), p.get("model", "")),
    )
    for node_id in node_ids:
        store.conn.execute(
            "UPDATE nodes SET wave_id = ?, exec_class = COALESCE(?, exec_class), "
            "provider = COALESCE(?, provider), model = COALESCE(?, model) WHERE job_id = ? AND id = ?",
            (wave_id, p.get("exec_class"), p.get("provider"), p.get("model"), event.job_id, node_id),
        )


def on_wave_submitted(store: Store, event: Event) -> None:
    p = event.payload
    wave_id = p["wave_id"]
    node_ids: list[str] = p.get("node_ids", [])
    store.conn.execute(
        """
        INSERT INTO waves(id, job_id, provider, model, batch_ref, idempotency_key, submitted_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'SUBMITTED')
        ON CONFLICT(job_id, id) DO UPDATE SET
            provider=excluded.provider, model=excluded.model, batch_ref=excluded.batch_ref,
            idempotency_key=excluded.idempotency_key, submitted_at=excluded.submitted_at,
            status='SUBMITTED'
        """,
        (
            wave_id,
            event.job_id,
            p.get("provider", ""),
            p.get("model", ""),
            p.get("batch_ref"),
            p.get("idempotency_key"),
            event.ts.isoformat(),
        ),
    )
    for node_id in node_ids:
        store.conn.execute(
            "UPDATE nodes SET wave_id = ?, status = ?, provider = COALESCE(?, provider), "
            "model = COALESCE(?, model) WHERE job_id = ? AND id = ?",
            (wave_id, NodeStatus.SUBMITTED.value, p.get("provider"), p.get("model"), event.job_id, node_id),
        )


def on_wave_completed(store: Store, event: Event) -> None:
    p = event.payload
    store.conn.execute(
        "UPDATE waves SET completed_at = ?, status = 'COMPLETED' WHERE job_id = ? AND id = ?",
        (event.ts.isoformat(), event.job_id, p["wave_id"]),
    )


def on_lease_acquired(store: Store, event: Event) -> None:
    """No jobs/nodes/waves mutation — ``leases`` is owned by lease.py."""


def on_lease_renewed(store: Store, event: Event) -> None:
    """No jobs/nodes/waves mutation — ``leases`` is owned by lease.py."""


_HANDLERS: dict[EventType, Handler] = {
    EventType.JOB_CREATED: on_job_created,
    EventType.PLAN_PROPOSED: on_plan_proposed,
    EventType.PLAN_APPROVED: on_plan_approved,
    EventType.NODE_ADDED: on_node_added,
    EventType.FANOUT_RESOLVED: on_fanout_resolved,
    EventType.NODE_READY: on_node_ready,
    EventType.NODE_HARVESTED: on_node_harvested,
    EventType.WAVE_FORMED: on_wave_formed,
    EventType.WAVE_SUBMITTED: on_wave_submitted,
    EventType.WAVE_COMPLETED: on_wave_completed,
    EventType.ITEM_RETURNED: on_item_returned,
    EventType.CONTRACT_RESULT: on_contract_result,
    EventType.ARTIFACT_APPLY_INTENT: on_artifact_apply_intent,
    EventType.ARTIFACT_APPLIED: on_artifact_applied,
    EventType.VERIFY_RESULT: on_verify_result,
    EventType.NODE_RESULT_CHOSEN: on_node_result_chosen,
    EventType.NODE_DONE: on_node_done,
    EventType.NODE_NEEDS_HUMAN: on_node_needs_human,
    EventType.NODE_STATE_CHANGED: on_node_state_changed,
    EventType.LEASE_ACQUIRED: on_lease_acquired,
    EventType.LEASE_RENEWED: on_lease_renewed,
    EventType.JOB_DONE: on_job_done,
    EventType.JOB_CANCELLED: on_job_cancelled,
}

assert set(_HANDLERS) == set(EventType), "every EventType must have a projection handler"


def apply(store: Store, event: Event) -> None:
    """Apply one event to the ``jobs``/``nodes``/``waves`` projections.

    Dispatches on ``event.type`` via :data:`_HANDLERS` (one function per
    :class:`~lazycode.ir.EventType`). Runs in its own (possibly nested, via
    SAVEPOINT — see :func:`~lazycode.store.db.transaction`) transaction so a
    single ``apply`` call is always atomic.
    """
    handler = _HANDLERS[event.type]
    with transaction(store.conn):
        handler(store, event)


def rebuild(store: Store, job_id: str) -> None:
    """``lazycode doctor --rebuild``: wipe ``job_id``'s projection rows and
    replay its full event history through :func:`apply`, in one transaction.
    """
    with transaction(store.conn):
        store.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        store.conn.execute("DELETE FROM nodes WHERE job_id = ?", (job_id,))
        store.conn.execute("DELETE FROM waves WHERE job_id = ?", (job_id,))
        for event in list(eventlog.read(store, job_id)):
            apply(store, event)
