"""lazycode IR — the frozen schemas every other module builds against.

This package is the foundation of the build order (§13): ``ir → store →
providers → …``. It is pure schemas + pure helper functions, with **no I/O** and
**no imports from downstream modules** — downstream imports from here, never the
reverse.

Public surface (see each module for details):

Logical plan (``operators``):
    Plan, Operator, NodeBase, Explore, Decompose, Generate, Edit, Verify,
    Judge, Reduce, Gate.
Contracts (``contracts``):
    OutputContract, DiffContract, CommandContract, JsonContract.
Context (``context_spec``):
    ContextSpec.
Rendered calls + adapter types + key derivation (``calls``):
    RenderedCall, PrefixBlock, Message, ToolDef, TokenEstimate, BatchRef,
    BatchStatus, ItemResult, Caps, canonical_json, compute_memo_key,
    memo_key_for_call, submit_idempotency_key.
Events + state machine (``events``):
    EventType, Event, NodeStatus, ExecClass, ItemStatus, and typed payloads
    (WaveSubmittedPayload, ItemReturnedPayload, ArtifactApplyIntentPayload,
    ArtifactAppliedPayload, FanoutResolvedPayload, NodeResultChosenPayload,
    NodeStateChangedPayload, LeasePayload).
Physical plan (``physical``):
    PhysicalNodeAssignment, Wave, WaveStatus.
"""

from __future__ import annotations

from .calls import (
    BatchRef,
    BatchStatus,
    Caps,
    ItemResult,
    Message,
    PrefixBlock,
    RenderedCall,
    TokenEstimate,
    ToolDef,
    canonical_json,
    compute_memo_key,
    memo_key_for_call,
    submit_idempotency_key,
)
from .context_spec import ContextSpec
from .contracts import CommandContract, DiffContract, JsonContract, OutputContract
from .events import (
    ArtifactAppliedPayload,
    ArtifactApplyIntentPayload,
    Event,
    EventType,
    ExecClass,
    FanoutResolvedPayload,
    ItemReturnedPayload,
    ItemStatus,
    LeasePayload,
    NodeResultChosenPayload,
    NodeStateChangedPayload,
    NodeStatus,
    WaveSubmittedPayload,
)
from .operators import (
    Decompose,
    Edit,
    Explore,
    Gate,
    Generate,
    Judge,
    NodeBase,
    Operator,
    Plan,
    Reduce,
    Verify,
)
from .physical import PhysicalNodeAssignment, Wave, WaveStatus

__all__ = [
    # operators / plan
    "Plan",
    "Operator",
    "NodeBase",
    "Explore",
    "Decompose",
    "Generate",
    "Edit",
    "Verify",
    "Judge",
    "Reduce",
    "Gate",
    # contracts
    "OutputContract",
    "DiffContract",
    "CommandContract",
    "JsonContract",
    # context
    "ContextSpec",
    # calls + adapter types
    "RenderedCall",
    "PrefixBlock",
    "Message",
    "ToolDef",
    "TokenEstimate",
    "BatchRef",
    "BatchStatus",
    "ItemResult",
    "Caps",
    # key derivation
    "canonical_json",
    "compute_memo_key",
    "memo_key_for_call",
    "submit_idempotency_key",
    # events + state
    "EventType",
    "Event",
    "NodeStatus",
    "ExecClass",
    "ItemStatus",
    "WaveSubmittedPayload",
    "ItemReturnedPayload",
    "ArtifactApplyIntentPayload",
    "ArtifactAppliedPayload",
    "FanoutResolvedPayload",
    "NodeResultChosenPayload",
    "NodeStateChangedPayload",
    "LeasePayload",
    # physical
    "PhysicalNodeAssignment",
    "Wave",
    "WaveStatus",
]
