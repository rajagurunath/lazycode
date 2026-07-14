"""The job lease — orchestrator mutual exclusion (DESIGN.md §7.1).

Exactly one orchestrator (daemon, in-process CLI run, GHA runner, hosted
relay) may advance a given job at a time. The ``leases`` table (§11:
``leases(job_id PK, holder_id, expires_at)``) is the mutex; ``acquire``,
``renew`` and ``release`` are the only ways to mutate it, and each does so
transactionally so two racing writers never both believe they hold the lease.

Events: per B5, ``LEASE_ACQUIRED`` and ``LEASE_RENEWED`` are appended (as the
lease row is written, in the same transaction) so the log carries a forensic
trail of who held the job and when. There is **no** ``LEASE_RELEASED`` in the
closed B5 vocabulary (Appendix B5 / ``lazycode.ir.EventType`` is a frozen
23-symbol enum this module does not own) — :func:`release` therefore mutates
only the ``leases`` table and does not append an event. This is a resolved
ambiguity: releasing is a courtesy (e.g. clean shutdown); the *safety*
property (no double-submit) comes entirely from expiry + takeover-on-expiry
in :func:`acquire`, which does not depend on a well-behaved release.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lazycode.ir import Event, EventType, LeasePayload

from . import eventlog
from .db import Store, transaction


def _now() -> datetime:
    return datetime.now(UTC)


def acquire(store: Store, job_id: str, holder_id: str, ttl_s: float) -> bool:
    """Try to acquire the lease for ``job_id`` on behalf of ``holder_id``.

    Succeeds (returns ``True``) if there is no existing lease row, the
    existing lease is already expired (takeover), or ``holder_id`` already
    holds it (idempotent re-acquire). Fails (``False``, no mutation) if a
    *different* holder's lease has not yet expired. Transactional: the
    read-then-write is atomic under SQLite's own locking (§7.1).
    """
    now = _now()
    expires_at = now + timedelta(seconds=ttl_s)
    with transaction(store.conn):
        row = store.conn.execute(
            "SELECT holder_id, expires_at FROM leases WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is not None:
            current_holder = row["holder_id"]
            current_expiry = datetime.fromisoformat(row["expires_at"])
            if current_holder != holder_id and current_expiry > now:
                return False
        store.conn.execute(
            """
            INSERT INTO leases(job_id, holder_id, expires_at) VALUES (?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET holder_id = excluded.holder_id, expires_at = excluded.expires_at
            """,
            (job_id, holder_id, expires_at.isoformat()),
        )
        _record_lease_event(store, job_id, holder_id, expires_at, EventType.LEASE_ACQUIRED, now)
    return True


def renew(store: Store, job_id: str, holder_id: str, ttl_s: float) -> bool:
    """Extend the lease's ``expires_at`` on behalf of the *current* holder.

    Succeeds only if a lease row exists and its ``holder_id`` matches — a
    holder who lost the lease to a takeover (§7.1) cannot renew it back.
    """
    now = _now()
    expires_at = now + timedelta(seconds=ttl_s)
    with transaction(store.conn):
        row = store.conn.execute("SELECT holder_id FROM leases WHERE job_id = ?", (job_id,)).fetchone()
        if row is None or row["holder_id"] != holder_id:
            return False
        store.conn.execute(
            "UPDATE leases SET expires_at = ? WHERE job_id = ?", (expires_at.isoformat(), job_id)
        )
        _record_lease_event(store, job_id, holder_id, expires_at, EventType.LEASE_RENEWED, now)
    return True


def release(store: Store, job_id: str, holder_id: str) -> bool:
    """Release the lease if currently held by ``holder_id``.

    Returns ``True`` if a row was deleted, ``False`` if no lease existed or it
    was held by someone else (no mutation in that case). No event is appended
    (see module docstring — no ``LEASE_RELEASED`` in B5).
    """
    with transaction(store.conn):
        cur = store.conn.execute(
            "DELETE FROM leases WHERE job_id = ? AND holder_id = ?", (job_id, holder_id)
        )
        return cur.rowcount > 0


def current(store: Store, job_id: str) -> tuple[str, datetime] | None:
    """Return ``(holder_id, expires_at)`` for ``job_id``'s lease, or ``None``.

    Read-only introspection helper (used by ``lazycode status`` and tests);
    does not consider expiry — callers compare against ``datetime.now(UTC)``
    themselves if "currently valid" is what they need.
    """
    row = store.conn.execute(
        "SELECT holder_id, expires_at FROM leases WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        return None
    return row["holder_id"], datetime.fromisoformat(row["expires_at"])


def _record_lease_event(
    store: Store,
    job_id: str,
    holder_id: str,
    expires_at: datetime,
    type_: EventType,
    ts: datetime,
) -> Event:
    payload = LeasePayload(job_id=job_id, holder_id=holder_id, expires_at=expires_at)
    return eventlog.record(
        store, job_id=job_id, type=type_, payload=payload.model_dump(mode="json"), ts=ts
    )
