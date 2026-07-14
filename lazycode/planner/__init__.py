"""lazycode planner — realtime structured-output planning (DESIGN.md §3, §13).

The planner is always realtime (§1): a few small calls whose cost is noise next
to execution. M0 ships plan proposal (schema-forced, retry-on-invalid) and
fan-out child resolution; re-planning/Decompose recursion is M2.

Public surface:
    propose_plan, resolve_fanout, PlanningError  (planner)
    planning_system_prompt, operator_algebra_docs (prompts)
"""

from __future__ import annotations

from .planner import PlanningError, propose_plan, resolve_fanout
from .prompts import operator_algebra_docs, planning_system_prompt

__all__ = [
    "propose_plan",
    "resolve_fanout",
    "PlanningError",
    "planning_system_prompt",
    "operator_algebra_docs",
]
