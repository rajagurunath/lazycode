"""The config-constructible mock provider seam (test/demo only).

Why this exists: the kill -9 crash-resume acceptance test (DESIGN.md §14
accept criterion c) must drive the *real* CLI in a *real* subprocess — the
in-process ``MockBatchAdapter``/``MockRealtimeAdapter`` injection every other
test uses (monkeypatching ``AnthropicRealtimeAdapter``/``AnthropicBatchAdapter``
at the ``app`` module level, see ``tests/cli/conftest.py``) is impossible
across a process boundary. This module gives ``lazycode.toml`` a way to name a
provider whose adapters are constructed from a canned JSON fixture instead of
a live Anthropic client, so a subprocess-driven test can still run with zero
network I/O and fully deterministic responses.

**This is a CLI-layer seam only.** It does not modify ``lazycode/scheduler/``
or ``lazycode/providers/`` — it independently implements the
:class:`~lazycode.providers.base.BatchAdapter` /
:class:`~lazycode.providers.base.RealtimeAdapter` protocols (both are
``Protocol`` classes; structural typing is exactly what lets a CLI-only module
satisfy them without touching ``providers/``).

Activation (Appendix B2 config layering): set
``[defaults] provider = "mock"`` and ``[providers.mock] fixture =
"path/to/fixture.json"`` in ``lazycode.toml`` (path resolved relative to the
repo root when not absolute). ``app.py`` branches to
:func:`build_mock_realtime_adapter`/:class:`FixtureBatchAdapter` instead of
``AnthropicRealtimeAdapter``/``AnthropicBatchAdapter`` whenever
``config.default_provider == "mock"``, and skips the API-key requirement for
that provider.

Fixture file format (JSON)::

    {
      "planner_response": { ... a lazycode.ir.Plan-shaped dict ... },
      "items": {
        "<node_id>": {"diff": "<unified diff text>", "assumptions": "<optional note>"},
        "n2":        {"text": "<raw response text, no diff parsing>"},
        "*":         {"text": "<fallback for any node id not listed above>"}
      },
      "poll_delays": 0
    }

* ``planner_response`` — the plan the mock realtime adapter hands back for
  *every* planning call (the CLI always plans once per ``run``), wrapped as
  the forced ``emit_plan`` tool-use block ``propose_plan`` expects (see
  ``lazycode/planner/planner.py``). Required.
* ``items`` — per-node canned responses, keyed by node id (== ``custom_id``,
  since ``render_node`` sets ``custom_id = node.id``). A ``"diff"`` entry
  becomes the response body (with an optional trailing ``Assumptions:``
  section built from ``"assumptions"``); a ``"text"`` entry is used verbatim.
  A ``"*"`` entry is the fallback for any node id with no explicit entry; if
  neither is present, a generic synthetic completed response is returned
  (mirrors ``providers/mock.py``'s own default). Optional.
* ``poll_delays`` — how many non-terminal ``poll()`` calls
  :class:`FixtureBatchAdapter` returns before reporting a batch terminal
  (§7.2 stall simulation). Lets a test kill the process deterministically
  while a wave is still "in flight". Default ``0`` (instant completion, same
  as ``providers.mock.MockBatchAdapter``).

**Cross-process durability + the no-double-submit proof.** A real provider
batch persists server-side regardless of the client (§7.5); a killed-and-
resumed CLI process re-polls the *same* batch rather than resubmitting,
because ``Orchestrator.run_job`` rebuilds ``known_refs`` from the
``WAVE_SUBMITTED`` events already in the event log (``scheduler/resume.py`` —
unmodified by this module). But the mock has no real server to ask, and a
fresh Python process gets a fresh :class:`FixtureBatchAdapter` instance with
empty in-memory state. To make cross-process state resumable *and* to give a
test an external, provider-dashboard-style artifact to assert against
(accept criterion c: "verified via provider dashboard"),
:class:`FixtureBatchAdapter` persists one JSON line per *newly created* batch
(never per re-submit-that-hits-known-refs) to ``submissions_log_path``, and
hydrates its in-memory batch registry from that file at construction. Two
CLI processes pointed at the same log file therefore share one source of
truth for "what batches exist and what's in them" — a test reads the log
after a resume and asserts each ``idempotency_key`` was logged exactly once.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from lazycode.ir import (
    BatchRef,
    BatchStatus,
    Caps,
    ItemResult,
    ItemStatus,
    RenderedCall,
    TokenEstimate,
)
from lazycode.providers.mock import MockRealtimeAdapter

_DEFAULT_CAPS = Caps(
    max_items=100_000,
    max_bytes=256 * 1024 * 1024,
    result_ttl_days=29,
    supports_cache=False,
    supports_webhooks=False,
)


class MockFixtureError(Exception):
    """The configured mock fixture is missing or malformed."""


def load_fixture(fixture_path: str | Path) -> dict[str, Any]:
    """Load + minimally validate a mock-provider fixture (see module docstring)."""
    path = Path(fixture_path)
    if not path.is_file():
        raise MockFixtureError(f"mock fixture not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MockFixtureError(f"mock fixture {path} is not valid JSON: {exc}") from exc
    if "planner_response" not in data:
        raise MockFixtureError(f"mock fixture {path} is missing required key 'planner_response'")
    return data


# --- realtime (planner) adapter ---------------------------------------------


def build_mock_realtime_adapter(fixture: dict[str, Any]) -> MockRealtimeAdapter:
    """A :class:`~lazycode.providers.mock.MockRealtimeAdapter` that always answers
    the planner's forced ``emit_plan`` tool call with ``fixture["planner_response"]``.

    Reuses ``MockRealtimeAdapter`` unmodified (its ``responses`` parameter accepts
    a callable) rather than reimplementing ``RealtimeAdapter`` — the planner is
    never in the crash/resume path (planning happens once, synchronously, before
    a job exists), so there is no cross-process durability concern here, unlike
    the batch side.
    """
    plan_dict = fixture["planner_response"]

    def _respond(call: RenderedCall) -> ItemResult:
        content = [{"type": "tool_use", "name": "emit_plan", "id": "tu_mock", "input": plan_dict}]
        return ItemResult(
            custom_id=call.custom_id,
            status=ItemStatus.COMPLETED,
            payload={"content": content, "usage": {"input_tokens": 10, "output_tokens": 5}},
        )

    return MockRealtimeAdapter(responses=_respond)


# --- batch adapter -----------------------------------------------------------


def _heuristic_tokens(call: RenderedCall) -> int:
    text = "".join(b.text for b in call.system) + "".join(m.content for m in call.messages)
    return max(1, len(text) // 4)


def _item_response(node_id: str, spec: dict[str, Any] | None) -> ItemResult:
    if spec is None:
        return ItemResult(
            custom_id=node_id,
            status=ItemStatus.COMPLETED,
            payload={
                "content": [{"type": "text", "text": f"mock response for {node_id}"}],
                "usage": {"input_tokens": 40, "output_tokens": 8},
            },
        )
    if "diff" in spec:
        text = spec["diff"]
        if spec.get("assumptions"):
            text = f"{text}\n\nAssumptions:\n{spec['assumptions']}"
    else:
        text = spec.get("text", f"mock response for {node_id}")
    return ItemResult(
        custom_id=node_id,
        status=ItemStatus.COMPLETED,
        payload={
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 40, "output_tokens": max(1, len(text) // 4)},
        },
    )


class FixtureBatchAdapter:
    """A fixture-driven :class:`~lazycode.providers.base.BatchAdapter`
    (structural match — see module docstring). Independent of
    ``providers/mock.py``: it must survive being re-constructed in a fresh OS
    process after a ``kill -9``, which ``MockBatchAdapter``'s pure in-memory
    design does not support.
    """

    def __init__(
        self,
        fixture: dict[str, Any],
        *,
        submissions_log_path: Path | None = None,
    ) -> None:
        items = fixture.get("items") or {}
        self._items: dict[str, dict[str, Any]] = items
        self._poll_delays = int(fixture.get("poll_delays", 0))
        self._log_path = Path(submissions_log_path) if submissions_log_path else None

        # batch_id -> custom_ids ; idempotency_key -> BatchRef. Hydrated from
        # the durable submissions log so a fresh process (post-crash) knows
        # about batches a *previous* process submitted (§7.5: batches persist
        # server-side regardless of the client).
        self._batches: dict[str, list[str]] = {}
        self._idem_to_ref: dict[str, BatchRef] = {}
        self._cancelled: set[str] = set()
        # In-memory only, intentionally not persisted: poll_delays models "this
        # process is still waiting on the provider", which is meaningless to
        # carry into a freshly-resumed process -- a resumed process should see
        # a previously-submitted batch as immediately done.
        self._remaining_delays: dict[str, int] = {}

        self._hydrate()

    # --- BatchAdapter protocol ---------------------------------------------

    @property
    def caps(self) -> Caps:
        return _DEFAULT_CAPS

    def count_tokens(self, items: list[RenderedCall]) -> TokenEstimate:
        total = sum(_heuristic_tokens(c) for c in items)
        return TokenEstimate(input_tokens=total, output_tokens=0, item_count=len(items))

    def submit(
        self,
        items: list[RenderedCall],
        idempotency_key: str,
        *,
        known_refs: dict[str, BatchRef] | None = None,
    ) -> BatchRef:
        if known_refs is not None and idempotency_key in known_refs:
            return known_refs[idempotency_key]
        if idempotency_key in self._idem_to_ref:
            # Known from the durable log (e.g. a resumed process whose
            # in-process known_refs was rebuilt from the same event log this
            # log agrees with) -- still not a new provider-side batch.
            return self._idem_to_ref[idempotency_key]

        batch_id = f"mock-batch-{uuid.uuid4().hex[:10]}"
        ref = BatchRef(provider="mock", batch_id=batch_id, idempotency_key=idempotency_key)
        custom_ids = [c.custom_id for c in items]
        self._batches[batch_id] = custom_ids
        self._idem_to_ref[idempotency_key] = ref
        self._remaining_delays[batch_id] = self._poll_delays
        self._append_log(
            {
                "batch_id": batch_id,
                "idempotency_key": idempotency_key,
                "custom_ids": custom_ids,
                "pid": os.getpid(),
                "ts": time.time(),
            }
        )
        return ref

    def find_batch(self, idempotency_key: str) -> BatchRef | None:
        """Registry lookup, hydrated from the durable submissions log — a
        resumed process finds batches a previous (crashed) process created,
        emulating the real adapter's metadata match (§7.1 reconcile)."""
        return self._idem_to_ref.get(idempotency_key)

    def poll(self, ref: BatchRef) -> BatchStatus:
        custom_ids = self._batches.get(ref.batch_id, [])
        if ref.batch_id in self._cancelled:
            return BatchStatus(
                batch_status="ended", completed=0, errored=len(custom_ids), expired=0, processing=0
            )
        remaining = self._remaining_delays.get(ref.batch_id, 0)
        if remaining > 0:
            self._remaining_delays[ref.batch_id] = remaining - 1
            return BatchStatus(
                batch_status="in_progress",
                completed=0,
                errored=0,
                expired=0,
                processing=len(custom_ids),
            )
        return BatchStatus(
            batch_status="ended", completed=len(custom_ids), errored=0, expired=0, processing=0
        )

    def fetch(self, ref: BatchRef):
        custom_ids = self._batches.get(ref.batch_id, [])
        if ref.batch_id in self._cancelled:
            for cid in custom_ids:
                yield ItemResult(
                    custom_id=cid,
                    status=ItemStatus.ERRORED,
                    error={"type": "canceled", "message": "batch was canceled"},
                )
            return
        for cid in custom_ids:
            spec = self._items.get(cid, self._items.get("*"))
            yield _item_response(cid, spec)

    def cancel(self, ref: BatchRef) -> None:
        self._cancelled.add(ref.batch_id)

    # --- durable submissions log --------------------------------------------

    def _hydrate(self) -> None:
        if self._log_path is None or not self._log_path.is_file():
            return
        for line in self._log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            batch_id = row.get("batch_id")
            idem_key = row.get("idempotency_key")
            custom_ids = row.get("custom_ids") or []
            if not batch_id or not idem_key:
                continue
            self._batches[batch_id] = custom_ids
            self._idem_to_ref[idem_key] = BatchRef(
                provider="mock", batch_id=batch_id, idempotency_key=idem_key
            )

    def _append_log(self, record: dict[str, Any]) -> None:
        if self._log_path is None:
            return
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
