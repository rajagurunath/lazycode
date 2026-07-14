"""Output contracts — the typed ``output_contract`` union (DESIGN.md Appendix B4).

A contract is the machine-checkable promise a node's output must satisfy. It is a
discriminated union on ``type`` so the planner emits exactly one shape per node
and the verifier can dispatch on it.

M0 enforcement (Appendix B4): only :class:`DiffContract` *shape* is enforced
(output must parse as a unified diff, apply with ``--3way``, and touch only paths
within ``files_within``). :class:`CommandContract` execution and
:class:`JsonContract` schema validation are M1; the models exist now so the plan
schema is frozen and downstream milestones only add enforcement, not fields.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DiffContract(BaseModel):
    """Output must be a unified diff that applies cleanly and touches only
    paths matching one of ``files_within`` (globs allowed)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["diff"] = "diff"
    files_within: list[str] = Field(default_factory=list)


class CommandContract(BaseModel):
    """Output is validated by running ``cmd`` and asserting its exit code.

    M1+ executes this; M0 verifiers run only the job-level ``verify.command``.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["command"] = "command"
    cmd: str
    timeout_s: int
    expect_exit: int = 0


class JsonContract(BaseModel):
    """Output must be JSON conforming to ``json_schema`` (a JSON-Schema dict).

    The public field name is ``schema`` (aliased) to match Appendix B4; the
    Python attribute is ``json_schema`` to avoid shadowing pydantic's own
    ``BaseModel.schema``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["json"] = "json"
    json_schema: dict[str, Any] = Field(alias="schema")


OutputContract = Annotated[
    DiffContract | CommandContract | JsonContract,
    Field(discriminator="type"),
]
"""The ``output_contract`` typed union, discriminated on ``type``."""
