"""The append-only event log — the source of truth (DESIGN.md §7.1, §11).

Every side effect in the system is eventually recorded here; ``jobs``/``nodes``/
``waves`` are projections rebuilt from this log (``projections.py``). ``seq`` is
a monotonic ``INTEGER PRIMARY KEY AUTOINCREMENT`` assigned by SQLite, which is
what makes ``read(job_id, after_seq=...)`` a stable resumable cursor across
crash/replay (§7.1).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from lazycode.ir import Event, EventType, canonical_json

from .db import Store, transaction


def append(store: Store, event: Event) -> int:
    """Append ``event`` in a single transaction; returns the assigned ``seq``.

    ``event.seq`` is ignored — the DB assigns the real seq via
    ``AUTOINCREMENT``. Build the event with any placeholder seq (e.g. ``0``),
    or use :func:`record` to build + append + get back the persisted
    :class:`~lazycode.ir.Event` (with its real seq) in one call.
    """
    with transaction(store.conn):
        cur = store.conn.execute(
            "INSERT INTO events(job_id, ts, type, payload) VALUES (?, ?, ?, ?)",
            (event.job_id, event.ts.isoformat(), event.type.value, canonical_json(event.payload)),
        )
        seq = cur.lastrowid
    assert seq is not None
    return seq


def record(
    store: Store,
    *,
    job_id: str,
    type: EventType,
    payload: dict[str, Any] | None = None,
    ts: datetime | None = None,
) -> Event:
    """Build and append an event in one call; returns the persisted
    :class:`~lazycode.ir.Event` (seq filled in, ``ts`` defaulted to now-UTC).

    This is the convenience entry point used by ``lease.py``, ``ledger.py``,
    etc. — anywhere a load-bearing event (with a typed payload from
    ``lazycode.ir.events``) needs to be appended as part of a larger
    operation.
    """
    ts = ts or datetime.now(UTC)
    payload = payload or {}
    draft = Event(seq=0, job_id=job_id, ts=ts, type=type, payload=payload)
    seq = append(store, draft)
    return draft.model_copy(update={"seq": seq})


def read(store: Store, job_id: str, after_seq: int = 0) -> Iterator[Event]:
    """Yield ``job_id``'s events with ``seq > after_seq``, in seq order.

    ``after_seq=0`` (default) reads the full history for the job — a valid
    cursor value since real seqs start at 1.
    """
    cur = store.conn.execute(
        "SELECT seq, job_id, ts, type, payload FROM events WHERE job_id = ? AND seq > ? ORDER BY seq",
        (job_id, after_seq),
    )
    for row in cur:
        yield Event(
            seq=row["seq"],
            job_id=row["job_id"],
            ts=datetime.fromisoformat(row["ts"]),
            type=EventType(row["type"]),
            payload=json.loads(row["payload"]),
        )
