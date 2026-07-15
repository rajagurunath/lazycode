"""Crash-resume reconstruction (DESIGN.md §7.1, §7.5).

``kill -9`` mid-wave must resume with no double-submit and no double-apply. This
module rebuilds everything the orchestrator needs to pick up where it left off,
purely from the append-only event log:

* :func:`resume_job` replays events into the ``jobs``/``nodes``/``waves``
  projections (:func:`~lazycode.store.projections.rebuild`), reconstructs the
  ``known_refs`` map (``idempotency_key -> BatchRef``) from every
  ``WAVE_SUBMITTED`` event, classifies **in-flight** waves (SUBMITTED but not
  yet COMPLETED) that must be re-polled rather than re-submitted, and
  classifies **reconcile** waves (FORMED but never SUBMITTED) — a crash in the
  window between ``adapter.submit()`` and the ``WAVE_SUBMITTED`` event may have
  orphaned a paid provider batch, so the orchestrator asks the provider
  (``adapter.find_batch(idempotency_key)``) whether it exists: found → adopt
  the ref and treat as in-flight; not found → re-render and submit fresh.

``known_refs`` is what makes ``adapter.submit`` idempotent on replay: a wave that
was already submitted before the crash resolves to its existing
:class:`~lazycode.ir.BatchRef` instead of creating a second provider batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lazycode.ir import BatchRef, EventType, WaveSubmittedPayload
from lazycode.store import Store, eventlog, projections


@dataclass(frozen=True)
class InFlightWave:
    """A wave submitted before the crash whose results were not yet processed."""

    wave_id: str
    batch_ref: BatchRef
    provider: str
    model: str
    node_ids: list[str]


@dataclass(frozen=True)
class ReconcileWave:
    """A wave FORMED before a crash with no ``WAVE_SUBMITTED`` on record — the
    provider may or may not hold a (paid) batch for it; ``find_batch`` decides."""

    wave_id: str
    provider: str
    model: str
    node_ids: list[str]
    idempotency_key: str


@dataclass
class RunnableState:
    """What the orchestrator needs to resume a job safely (§7.1)."""

    job_id: str
    known_refs: dict[str, BatchRef] = field(default_factory=dict)
    in_flight_waves: list[InFlightWave] = field(default_factory=list)
    reconcile_waves: list[ReconcileWave] = field(default_factory=list)


def reconstruct_known_refs(store: Store, job_id: str) -> dict[str, BatchRef]:
    """Rebuild ``idempotency_key -> BatchRef`` from ``WAVE_SUBMITTED`` events."""
    known: dict[str, BatchRef] = {}
    for event in eventlog.read(store, job_id):
        if event.type != EventType.WAVE_SUBMITTED:
            continue
        p = WaveSubmittedPayload.model_validate(event.payload)
        known[p.idempotency_key] = BatchRef(
            provider=p.provider, batch_id=p.batch_ref, idempotency_key=p.idempotency_key
        )
    return known


def resume_job(store: Store, job_id: str) -> RunnableState:
    """Rebuild projections + known_refs + in-flight/reconcile waves (§7.1)."""
    projections.rebuild(store, job_id)

    formed: dict[str, ReconcileWave] = {}
    submitted: dict[str, InFlightWave] = {}
    completed: set[str] = set()
    known: dict[str, BatchRef] = {}
    for event in eventlog.read(store, job_id):
        if event.type == EventType.WAVE_FORMED:
            p = event.payload
            idem_key = p.get("idempotency_key")
            if idem_key:  # older events without the key cannot be reconciled
                formed[p["wave_id"]] = ReconcileWave(
                    wave_id=p["wave_id"],
                    provider=p.get("provider", ""),
                    model=p.get("model", ""),
                    node_ids=list(p.get("node_ids", [])),
                    idempotency_key=idem_key,
                )
        elif event.type == EventType.WAVE_SUBMITTED:
            p = WaveSubmittedPayload.model_validate(event.payload)
            ref = BatchRef(
                provider=p.provider, batch_id=p.batch_ref, idempotency_key=p.idempotency_key
            )
            known[p.idempotency_key] = ref
            submitted[p.wave_id] = InFlightWave(
                wave_id=p.wave_id,
                batch_ref=ref,
                provider=p.provider,
                model=p.model,
                node_ids=list(p.node_ids),
            )
        elif event.type == EventType.WAVE_COMPLETED:
            completed.add(event.payload.get("wave_id", ""))

    in_flight = [w for wid, w in submitted.items() if wid not in completed]
    reconcile = [
        w for wid, w in formed.items() if wid not in submitted and wid not in completed
    ]
    return RunnableState(
        job_id=job_id, known_refs=known, in_flight_waves=in_flight, reconcile_waves=reconcile
    )
