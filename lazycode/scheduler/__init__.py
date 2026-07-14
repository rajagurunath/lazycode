"""lazycode scheduler — the M0 barrier-wave orchestrator (DESIGN.md §7, §13).

The event-sourced core: acquire the job lease, drive the §7.2 wave loop with hard
per-job barriers, and deliver a branch + report — crash-safe and resumable via
the event log (§7.1). M0 has no hedging, speculation, or repair loop (Appendix
B11); those are M2+.

Public surface:
    Orchestrator, JobResult, LeaseAcquisitionError, LeaseLostError  (orchestrator)
    SchedulerConfig                                                 (config)
    render_node                                                     (render)
    resume_job, RunnableState, InFlightWave                         (resume)
    write_report                                                    (report)
"""

from __future__ import annotations

from .config import SchedulerConfig
from .orchestrator import (
    JobResult,
    LeaseAcquisitionError,
    LeaseLostError,
    Orchestrator,
)
from .render import render_node
from .report import write_report
from .resume import InFlightWave, RunnableState, resume_job

__all__ = [
    "Orchestrator",
    "JobResult",
    "LeaseAcquisitionError",
    "LeaseLostError",
    "SchedulerConfig",
    "render_node",
    "resume_job",
    "RunnableState",
    "InFlightWave",
    "write_report",
]
