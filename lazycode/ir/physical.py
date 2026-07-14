"""The physical plan: wave + per-node assignment (DESIGN.md §4, §11).

The optimizer's cost-based physical planning (§5.3) assigns every logical node
to a :class:`PhysicalNodeAssignment` and groups them into :class:`Wave` objects.
A **wave is a hard per-job barrier** (§4): the scheduler submits the entire ready
frontier as one wave (one provider batch per (provider, model) group), waits for
all of it, then forms the next wave. This makes ``wall_clock ≈ depth ×
wave_latency`` literal and "wave count" unambiguous for the M0 accept test
(rows in ``waves`` with status ≥ SUBMITTED; §B6).

Pure schemas — no I/O.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .events import ExecClass


class WaveStatus(StrEnum):
    """Lifecycle of a wave row (§7.4, §11).

    Ordering (for the §B6 "status ≥ SUBMITTED" accept-test predicate) is the
    declaration order below; use :meth:`rank` to compare.
    """

    FORMED = "FORMED"
    SUBMITTED = "SUBMITTED"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"

    @property
    def rank(self) -> int:
        """Monotonic lifecycle rank; SUBMITTED and beyond count as submitted."""
        return list(WaveStatus).index(self)


class PhysicalNodeAssignment(BaseModel):
    """Where and how a single logical node executes (§4).

    ``prefix_block_id`` names the shared prefix block (R4 CSE); ``spec_group_id``
    + ``branch_label`` link sibling speculations (R7). All three are ``None`` for
    local nodes and non-speculative batch nodes respectively.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str
    wave_id: str
    exec_class: ExecClass
    provider: str
    model: str
    prefix_block_id: str | None = None
    spec_group_id: str | None = None
    branch_label: str | None = None


class Wave(BaseModel):
    """One topological layer submitted as a (provider, model) batch group (§4, §11)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    job_id: str
    provider: str
    model: str
    exec_class: ExecClass = ExecClass.BATCH
    node_ids: list[str] = Field(default_factory=list)
    batch_ref: str | None = None
    idempotency_key: str | None = None
    status: WaveStatus = WaveStatus.FORMED
    submitted_at: datetime | None = None
    completed_at: datetime | None = None
