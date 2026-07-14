"""The R10 memoization cache over ``llm_calls``, plus ``call_items`` helpers
(DESIGN.md §5.2 R10, §11).

Every LLM call is keyed by ``memo_key = hash(model, rendered_prompt, mode,
sample_idx)`` (:func:`lazycode.ir.compute_memo_key` /
:func:`~lazycode.ir.memo_key_for_call`). Before making a call, the caller
checks :func:`get`; on a genuinely new call, it calls :func:`put` to record
the result. ``llm_calls.memo_key`` is ``UNIQUE`` (§11), so this module treats
:func:`put` as idempotent — a duplicate ``put`` for the same ``memo_key``
(crash-replay re-issuing a call whose result was already recorded) is a
no-op that returns the *existing* row rather than raising or overwriting.

``call_items`` maps one vectorized ``llm_call`` (R6 ``Vectorize``) back to the
*k* node ids it packed (§11: ``call_items(call_id, node_id, custom_id,
item_status)``, ``PRIMARY KEY(call_id, node_id)``).

Resolved ambiguity: ``llm_calls.id`` and ``call_items`` rows are plain
application-supplied strings (§11 gives no id-generation scheme); the caller
(providers/scheduler, both out of ``store/`` scope) is responsible for minting
a unique ``call_id`` (e.g. the provider's own item id, or a uuid) before
calling :func:`put`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import Store, transaction


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    """One ``llm_calls`` row (§11)."""

    id: str
    node_id: str | None
    memo_key: str
    mode: str
    sample_idx: int
    provider: str | None
    request_ref: str | None
    response_ref: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    cached: bool


@dataclass(frozen=True, slots=True)
class CallItemRecord:
    """One ``call_items`` row (§11)."""

    call_id: str
    node_id: str
    custom_id: str
    item_status: str | None


def _row_to_record(row) -> LlmCallRecord:
    return LlmCallRecord(
        id=row["id"],
        node_id=row["node_id"],
        memo_key=row["memo_key"],
        mode=row["mode"],
        sample_idx=row["sample_idx"],
        provider=row["provider"],
        request_ref=row["request_ref"],
        response_ref=row["response_ref"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        cost_usd=row["cost_usd"],
        cached=bool(row["cached"]),
    )


def get(store: Store, memo_key: str) -> LlmCallRecord | None:
    """Look up a cached result by R10 memo key; ``None`` on a miss."""
    row = store.conn.execute("SELECT * FROM llm_calls WHERE memo_key = ?", (memo_key,)).fetchone()
    return _row_to_record(row) if row is not None else None


def put(
    store: Store,
    *,
    call_id: str,
    memo_key: str,
    mode: str,
    node_id: str | None = None,
    sample_idx: int = 0,
    provider: str | None = None,
    request_ref: str | None = None,
    response_ref: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    cached: bool = False,
) -> LlmCallRecord:
    """Record a completed LLM call, keyed by ``memo_key`` (idempotent).

    If ``memo_key`` already has a row (replay, or a genuine memo hit recorded
    by a concurrent path), the existing row is returned unchanged rather than
    overwritten — ``memo_key`` is the single source of truth for "have we
    already paid for this exact call".
    """
    with transaction(store.conn):
        store.conn.execute(
            """
            INSERT INTO llm_calls(id, node_id, memo_key, mode, sample_idx, provider,
                                   request_ref, response_ref, tokens_in, tokens_out, cost_usd, cached)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memo_key) DO NOTHING
            """,
            (
                call_id,
                node_id,
                memo_key,
                mode,
                sample_idx,
                provider,
                request_ref,
                response_ref,
                tokens_in,
                tokens_out,
                cost_usd,
                int(cached),
            ),
        )
        row = store.conn.execute(
            "SELECT * FROM llm_calls WHERE memo_key = ?", (memo_key,)
        ).fetchone()
    assert row is not None
    return _row_to_record(row)


def add_call_item(
    store: Store, *, call_id: str, node_id: str, custom_id: str, item_status: str | None = None
) -> None:
    """Link one node id into a (possibly vectorized) call's ``call_items`` (R6).

    Upserts on ``(call_id, node_id)`` so re-linking (replay) is idempotent.
    """
    with transaction(store.conn):
        store.conn.execute(
            """
            INSERT INTO call_items(call_id, node_id, custom_id, item_status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(call_id, node_id) DO UPDATE SET
                custom_id = excluded.custom_id, item_status = excluded.item_status
            """,
            (call_id, node_id, custom_id, item_status),
        )


def set_item_status(store: Store, *, call_id: str, node_id: str, item_status: str) -> None:
    """Update the per-item result state of one ``call_items`` row (§10 ``ItemStatus``)."""
    with transaction(store.conn):
        store.conn.execute(
            "UPDATE call_items SET item_status = ? WHERE call_id = ? AND node_id = ?",
            (item_status, call_id, node_id),
        )


def call_items_for(store: Store, call_id: str) -> list[CallItemRecord]:
    """All ``call_items`` rows for one ``llm_call`` (the *k* nodes it packed)."""
    rows = store.conn.execute(
        "SELECT call_id, node_id, custom_id, item_status FROM call_items WHERE call_id = ?",
        (call_id,),
    ).fetchall()
    return [
        CallItemRecord(
            call_id=r["call_id"], node_id=r["node_id"], custom_id=r["custom_id"], item_status=r["item_status"]
        )
        for r in rows
    ]
