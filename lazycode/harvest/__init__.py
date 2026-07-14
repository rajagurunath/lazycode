"""M0 context harvesting: repo map + whole target files + house rules (DESIGN.md §6).

No LSP slicing, no task-specific harvests beyond ``ContextSpec.extras`` — that's
M1+ (Appendix B11). Pure local tooling: stdlib ``ast``/``pathlib`` + ``lazycode.ir``
only, no network, no LLM calls.
"""

from __future__ import annotations

from .harvester import HarvestError, HarvestResult, harvest
from .repo_map import build_repo_map, clear_cache

__all__ = [
    "harvest",
    "HarvestResult",
    "HarvestError",
    "build_repo_map",
    "clear_cache",
]
