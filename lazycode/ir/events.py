"""Event vocabulary, state enums, and the event envelope (DESIGN.md §7.4, B5).

The SQLite ``events`` table is the single source of truth (§7.1, §11); every
projection (``jobs``/``nodes``/``waves``) is rebuilt from it. This module pins:

* :class:`EventType` — the closed B5 event vocabulary as a ``StrEnum``.
* :class:`Event` — the envelope stored per row (``seq, job_id, ts, type,
  payload``). ``payload`` is an open dict; the typed ``*Payload`` models below
  are the *load-bearing* events' schemas, used by writers/readers that care
  about structure. They are intentionally not enforced by ``Event`` itself so
  the store can append any event without a schema migration.
* :class:`NodeStatus` — the full §7.4 node state machine (local + remote paths,
  plus EXPIRED/NEEDS_HUMAN/SUPERSEDED/WAITING_APPROVAL/… terminal states).
* :class:`ExecClass` — physical execution class (§4).
* :class:`ItemStatus` — per-item batch result state (§10).

Pure schemas — no I/O. ``ts`` is a timezone-aware ``datetime``; serialize with
``model_dump(mode="json")`` for storage.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    """The closed B5 event vocabulary (order follows the job lifecycle)."""

    JOB_CREATED = "JOB_CREATED"
    PLAN_PROPOSED = "PLAN_PROPOSED"
    PLAN_APPROVED = "PLAN_APPROVED"
    NODE_ADDED = "NODE_ADDED"
    FANOUT_RESOLVED = "FANOUT_RESOLVED"
    NODE_READY = "NODE_READY"
    NODE_HARVESTED = "NODE_HARVESTED"
    WAVE_FORMED = "WAVE_FORMED"
    WAVE_SUBMITTED = "WAVE_SUBMITTED"
    WAVE_COMPLETED = "WAVE_COMPLETED"
    ITEM_RETURNED = "ITEM_RETURNED"
    CONTRACT_RESULT = "CONTRACT_RESULT"
    ARTIFACT_APPLY_INTENT = "ARTIFACT_APPLY_INTENT"
    ARTIFACT_APPLIED = "ARTIFACT_APPLIED"
    VERIFY_RESULT = "VERIFY_RESULT"
    NODE_RESULT_CHOSEN = "NODE_RESULT_CHOSEN"
    NODE_DONE = "NODE_DONE"
    NODE_NEEDS_HUMAN = "NODE_NEEDS_HUMAN"
    NODE_STATE_CHANGED = "NODE_STATE_CHANGED"
    LEASE_ACQUIRED = "LEASE_ACQUIRED"
    LEASE_RENEWED = "LEASE_RENEWED"
    JOB_DONE = "JOB_DONE"
    JOB_CANCELLED = "JOB_CANCELLED"


class NodeStatus(StrEnum):
    """The §7.4 node state machine (local path, remote path, and terminals)."""

    # Common entry
    PENDING = "PENDING"
    READY = "READY"
    # Local path (Explore-local, Verify)
    EXECUTING_LOCAL = "EXECUTING_LOCAL"
    COMPLETED_LOCAL = "COMPLETED_LOCAL"
    # Remote path
    HARVESTED = "HARVESTED"
    ENQUEUED = "ENQUEUED"
    SUBMITTED = "SUBMITTED"
    RETURNED = "RETURNED"
    APPLIED = "APPLIED"
    # Expiry / hedge
    EXPIRED = "EXPIRED"
    RE_ENQUEUED = "RE_ENQUEUED"
    HEDGED = "HEDGED"
    # Repair / human
    REPAIR_SPAWNED = "REPAIR_SPAWNED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    # Gate
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    # Terminal
    DONE = "DONE"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"


class ExecClass(StrEnum):
    """Physical execution class assigned by the optimizer (§4)."""

    BATCH = "batch"
    REALTIME = "realtime"
    LOCAL = "local"
    SPECULATIVE = "speculative"


class ItemStatus(StrEnum):
    """Per-item batch result state returned by an adapter (§10)."""

    COMPLETED = "completed"
    ERRORED = "errored"
    EXPIRED = "expired"


class Event(BaseModel):
    """One append-only row of the ``events`` log (§11).

    ``payload`` is an open dict so any event can be recorded; use the typed
    ``*Payload`` models to build/parse the load-bearing events.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int
    job_id: str
    ts: datetime
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


# --- Typed payloads for the load-bearing events -----------------------------
# These are helpers to construct/validate an Event.payload; Event stores the
# plain dict (payload=model.model_dump(mode="json")).


class WaveSubmittedPayload(BaseModel):
    """Payload for :attr:`EventType.WAVE_SUBMITTED` (§7.2)."""

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    provider: str
    model: str
    batch_ref: str
    idempotency_key: str
    node_ids: list[str] = Field(default_factory=list)
    item_count: int


class ItemReturnedPayload(BaseModel):
    """Payload for :attr:`EventType.ITEM_RETURNED` (per-item result, §7.2)."""

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    custom_id: str
    status: ItemStatus
    call_id: str | None = None


class ArtifactApplyIntentPayload(BaseModel):
    """Payload for :attr:`EventType.ARTIFACT_APPLY_INTENT` (§9 apply ledger)."""

    model_config = ConfigDict(extra="forbid")

    worktree: str
    diff_hash: str
    node_id: str


class ArtifactAppliedPayload(BaseModel):
    """Payload for :attr:`EventType.ARTIFACT_APPLIED` (§9 apply ledger)."""

    model_config = ConfigDict(extra="forbid")

    worktree: str
    diff_hash: str
    node_id: str


class FanoutResolvedPayload(BaseModel):
    """Payload for :attr:`EventType.FANOUT_RESOLVED` (§3.2 dynamic DAG).

    ``child_ids`` are minted deterministically as ``{parent_id}.{index}`` in the
    order they appear in the resolving node's output; ``bindings`` is aligned
    positionally with ``child_ids``.
    """

    model_config = ConfigDict(extra="forbid")

    parent_id: str
    child_ids: list[str] = Field(default_factory=list)
    bindings: list[dict[str, Any]] = Field(default_factory=list)


class NodeResultChosenPayload(BaseModel):
    """Payload for :attr:`EventType.NODE_RESULT_CHOSEN` (§7.6 hedge/spec win)."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    call_id: str
    custom_id: str | None = None
    mode: str | None = None


class NodeStateChangedPayload(BaseModel):
    """Payload for :attr:`EventType.NODE_STATE_CHANGED` (§7.4 transition)."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    from_status: NodeStatus
    to_status: NodeStatus


class LeasePayload(BaseModel):
    """Payload for :attr:`EventType.LEASE_ACQUIRED` / ``LEASE_RENEWED`` (§7.1)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    holder_id: str
    expires_at: datetime
