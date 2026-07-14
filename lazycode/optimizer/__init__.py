"""lazycode optimizer — M0 slice: R1/R2 rewrite rules + physical planning
(DESIGN.md §5, §13).

M0 ships only the two always-safe rewrite rules (``rules``: R1 LocalPushdown, R2
ContextPruning) and minimum-depth physical planning (``physical``). The cost
model, R3–R10, and adaptive re-optimization (AQE) are M2+.

Public surface:
    local_pushdown, context_pruning  (rules)
    plan_physical                    (physical)
"""

from __future__ import annotations

from .physical import plan_physical
from .rules import context_pruning, local_pushdown

__all__ = ["local_pushdown", "context_pruning", "plan_physical"]
