"""The applied-diff ledger — side-effect (apply) idempotency (DESIGN.md §9, §11).

Per §9: "before applying, the scheduler appends an ``ARTIFACT_APPLY_INTENT``
event; after applying, ``ARTIFACT_APPLIED``. On crash-replay, a diff whose
hash is already in the ledger for that worktree is skipped." The *ledger* is
the ``applied_diffs`` table itself (§11: ``PRIMARY KEY(worktree, diff_hash)``)
— its presence, not the event log, is what :func:`already_applied` checks,
because it's a plain indexed point-lookup and (unlike replaying the whole log)
doesn't require the caller to have a ``Store`` opened at a particular point in
the log's history. This is explicitly **not** the R10 memo cache (§5.2 note):
memoization avoids re-paying for an LLM call; this ledger avoids re-applying
its diff to the working tree a second time.

Resolved ambiguity: :func:`record_applied` is idempotent end-to-end — a
second call for the same ``(worktree, diff_hash)`` inserts nothing new (the
``INSERT OR IGNORE`` is a no-op) and, deliberately, does **not** append a
second ``ARTIFACT_APPLIED`` event either, so replaying an already-applied
diff leaves no duplicate trace in the log. It returns ``False`` in that case
so the caller can distinguish "this call actually applied it" from "already
done, skip re-running verify/follow-ups for this diff". :func:`record_intent`
has no such table to dedupe against (intent is logged every attempt, even
repeated ones after a crash) and always appends its event.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lazycode.ir import ArtifactAppliedPayload, ArtifactApplyIntentPayload, Event, EventType

from . import eventlog
from .db import Store, transaction


def already_applied(store: Store, *, worktree: str, diff_hash: str) -> bool:
    """True if ``diff_hash`` has already been applied in ``worktree`` (§9)."""
    row = store.conn.execute(
        "SELECT 1 FROM applied_diffs WHERE worktree = ? AND diff_hash = ?", (worktree, diff_hash)
    ).fetchone()
    return row is not None


def record_intent(
    store: Store,
    *,
    job_id: str,
    worktree: str,
    diff_hash: str,
    node_id: str,
    ts: datetime | None = None,
) -> Event:
    """Append ``ARTIFACT_APPLY_INTENT`` before attempting a ``git apply`` (§9).

    Callers should check :func:`already_applied` first and skip the apply (and
    this call) entirely when it returns ``True``.
    """
    ts = ts or datetime.now(UTC)
    payload = ArtifactApplyIntentPayload(worktree=worktree, diff_hash=diff_hash, node_id=node_id)
    return eventlog.record(
        store, job_id=job_id, type=EventType.ARTIFACT_APPLY_INTENT, payload=payload.model_dump(mode="json"), ts=ts
    )


def record_applied(
    store: Store,
    *,
    job_id: str,
    worktree: str,
    diff_hash: str,
    node_id: str,
    ts: datetime | None = None,
) -> bool:
    """Record a successful apply in the ledger and append ``ARTIFACT_APPLIED``.

    Returns ``True`` if this call newly recorded the apply, ``False`` if
    ``(worktree, diff_hash)`` was already in the ledger (idempotent replay —
    no event appended in that case; see module docstring).
    """
    ts = ts or datetime.now(UTC)
    with transaction(store.conn):
        cur = store.conn.execute(
            "INSERT OR IGNORE INTO applied_diffs(worktree, diff_hash, node_id, applied_at) VALUES (?, ?, ?, ?)",
            (worktree, diff_hash, node_id, ts.isoformat()),
        )
        newly_applied = cur.rowcount > 0
        if newly_applied:
            payload = ArtifactAppliedPayload(worktree=worktree, diff_hash=diff_hash, node_id=node_id)
            eventlog.record(
                store,
                job_id=job_id,
                type=EventType.ARTIFACT_APPLIED,
                payload=payload.model_dump(mode="json"),
                ts=ts,
            )
    return newly_applied
