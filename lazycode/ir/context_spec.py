"""Context specification for a logical-plan node (DESIGN.md §3.2 / §6, M0 scope).

The harvester (``harvest/``) reads a node's :class:`ContextSpec` and gathers the
declared context *locally and for free* before the node is ever submitted to a
batch API — this is the "front-loading engine" that keeps ``rounds_per_node``
low (§6).

M0 scope (Appendix B11): repo map + whole target files + house rules only. LSP
symbol slicing and coverage/failing-test harvests arrive in M1, so this model
deliberately stays small. ``extras`` is an open escape hatch for task-specific
harvest directives (e.g. ``{"coverage_xml": "coverage.xml"}``) so we do not have
to widen the schema every milestone.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContextSpec(BaseModel):
    """What the harvester must gather for a node before submission.

    Attributes:
        files: File paths or globs to include in full. May contain fan-out
            template placeholders (e.g. ``"{module}"``) resolved from a child
            node's ``bindings`` at fan-out time.
        repo_map: Include the tree-sitter repo-map symbol outline (the shared
            prefix block; §6 step 1).
        house_rules: Include lint/CI config, CONTRIBUTING and style exemplars
            (§6 step 3).
        extras: Open map for task-specific harvest directives not yet promoted
            to first-class fields (M1+ recipes such as coverage XML or
            failing-test output).
    """

    model_config = ConfigDict(extra="forbid")

    files: list[str] = Field(default_factory=list)
    repo_map: bool = False
    house_rules: bool = False
    extras: dict[str, Any] = Field(default_factory=dict)
