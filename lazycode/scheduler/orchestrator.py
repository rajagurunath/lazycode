"""The M0 wave-loop orchestrator (DESIGN.md §7.2, §7.4, §9, Appendix B11).

:class:`Orchestrator` drives a job from an approved logical plan to a delivered
branch + report, entirely through the event log so
:func:`~lazycode.store.projections.rebuild` reproduces state exactly.

Wave-loop invariants enforced (§7.1, §7.2):

* **Single-writer / lease.** ``run_job`` acquires the per-job lease before
  advancing it and renews it every iteration; if renewal fails (takeover) it
  aborts cleanly with :class:`LeaseLostError`. A second orchestrator that cannot
  acquire the lease raises :class:`LeaseAcquisitionError`.
* **Hard per-job barriers.** Each iteration forms the entire ready frontier,
  runs local nodes inline, submits remote nodes as one batch per (provider,
  model) group, and **waits for every group to reach a terminal state before
  forming the next frontier** (§4).
* **No double-submit.** The submit idempotency key is content-derived
  (``sha256(items)[:16] + ":" + flush_ordinal``) with ``flush_ordinal`` counted
  from durable ``WAVE_SUBMITTED`` events, and ``adapter.submit`` is given
  ``known_refs`` rebuilt from those events; on resume, in-flight waves are
  re-polled, never re-submitted (§7.1).
* **No double-apply.** Every apply goes ``record_intent → git apply →
  record_applied`` through the ``applied_diffs`` ledger; a diff already in the
  ledger for that worktree is skipped (§9).
* **Event ordering.** A node's transitions are always logged before the state
  they imply is observed by the next frontier computation (NODE_READY →
  harvest/submit → ITEM_RETURNED → CONTRACT_RESULT → ARTIFACT_APPLIED →
  NODE_DONE), so a mid-flight crash replays to a consistent point.

M0 degenerate paths (Appendix B11): contract/verify failure → ``NEEDS_HUMAN``
(no repair loop); shape-only DiffContract validation; single provider; Gate
auto-approved (not executed as a DAG node); notify = a log line.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from lazycode.harvest import HarvestResult, harvest
from lazycode.ir import (
    BatchRef,
    Edit,
    EventType,
    ExecClass,
    Explore,
    FanoutResolvedPayload,
    Gate,
    Generate,
    ItemResult,
    ItemStatus,
    NodeStateChangedPayload,
    NodeStatus,
    Operator,
    Plan,
    RenderedCall,
    Verify,
    WaveSubmittedPayload,
    canonical_json,
    submit_idempotency_key,
)
from lazycode.optimizer import context_pruning, plan_physical
from lazycode.providers.base import BatchAdapter, RetryableError, backoff_delays
from lazycode.store import Store, cas, eventlog, ledger, lease, memo, stats, transaction
from lazycode.verify import run_verify
from lazycode.workspace import (
    DiffConflict,
    DiffPathViolation,
    Worktree,
    apply_diff,
    compute_diff_hash,
    create_group_worktree,
    validate_diff_paths,
)

from .config import SchedulerConfig
from .payloads import extract_diff, extract_text, payload_usage
from .report import write_report
from .resume import resume_job

log = logging.getLogger("lazycode.scheduler")

_OPERATOR_ADAPTER: TypeAdapter[Operator] = TypeAdapter(Operator)

# Node statuses that count as "successfully complete" for dependency readiness.
_SUCCESS = frozenset(
    {NodeStatus.DONE, NodeStatus.APPLIED, NodeStatus.COMPLETED_LOCAL, NodeStatus.APPROVED}
)
# Statuses a node may (re-)run from.
_RUNNABLE = frozenset({NodeStatus.PENDING, NodeStatus.READY, NodeStatus.RE_ENQUEUED})


class LeaseAcquisitionError(Exception):
    """The orchestrator could not acquire the job lease (another holder has it)."""


class LeaseLostError(Exception):
    """The orchestrator lost its lease mid-run (takeover); it aborts cleanly."""


@dataclass
class JobResult:
    """Outcome of a :meth:`Orchestrator.run_job` call."""

    job_id: str
    status: str
    waves: int
    report_dir: Path | None = None
    needs_human: list[str] = field(default_factory=list)


@dataclass
class _NodeRow:
    """A node's projection row plus its parsed :class:`~lazycode.ir.Operator`."""

    id: str
    op: str
    status: NodeStatus
    deps: list[str]
    exec_class: str | None
    provider: str | None
    model: str | None
    group_id: str | None
    template_parent_id: str | None
    bindings: dict[str, Any] | None
    operator: Operator

    @property
    def is_template(self) -> bool:
        # A template *parent* (unresolved fan-out) has template=True and no
        # parent of its own; resolved children carry template_parent_id.
        return bool(getattr(self.operator, "template", False)) and self.template_parent_id is None

    @property
    def is_local(self) -> bool:
        return self.exec_class == ExecClass.LOCAL.value


class Orchestrator:
    """Advances one job through the §7.2 barrier-wave loop."""

    def __init__(
        self,
        store: Store,
        adapters: dict[str, BatchAdapter],
        repo_root: Path | str,
        config: SchedulerConfig,
        *,
        holder_id: str | None = None,
    ) -> None:
        self.store = store
        self.adapters = adapters
        self.repo_root = Path(repo_root)
        self.config = config
        self.holder_id = holder_id or f"orch-{uuid.uuid4().hex[:8]}"
        # Response text of local nodes produced this run (fan-out resolution).
        self._local_outputs: dict[str, str] = {}

    # --- job creation (seed the event log from a plan) --------------------

    def create_job(
        self,
        goal: str,
        plan: Plan,
        base_commit: str,
        *,
        slider: int = 70,
        budget_usd: float | None = None,
        deadline_utc: str | None = None,
        job_id: str | None = None,
    ) -> str:
        """Seed a job's event log from an approved logical plan (§7.2).

        Applies the M0 optimizer (R2 ContextPruning + R1 via physical planning),
        creates the single task-group worktree, and records JOB_CREATED →
        NODE_ADDED* → PLAN_PROPOSED → PLAN_APPROVED. Returns the job id.
        """
        job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"

        plan = context_pruning(plan)  # R2
        assignments = {a.node_id: a for a in plan_physical(plan, self.config)}  # R1 + layering

        eventlog.record(
            self.store,
            job_id=job_id,
            type=EventType.JOB_CREATED,
            payload={
                "goal": goal,
                "repo": str(self.repo_root),
                "base_commit": base_commit,
                "slider": slider,
                "budget_usd": budget_usd,
                "deadline_utc": deadline_utc,
            },
        )

        group_id = "g0"
        worktree = create_group_worktree(self.repo_root, base_commit, job_id, group_id)
        with transaction(self.store.conn):
            self.store.conn.execute(
                "INSERT OR REPLACE INTO task_groups(id, job_id, worktree_path, branch) VALUES (?, ?, ?, ?)",
                (group_id, job_id, str(worktree.path), worktree.branch),
            )

        for node in plan.nodes:
            a = assignments[node.id]
            eventlog.record(
                self.store,
                job_id=job_id,
                type=EventType.NODE_ADDED,
                payload={
                    "node_id": node.id,
                    "op": node.op,
                    "spec": node.model_dump(mode="json"),
                    "deps": list(node.deps),
                    "group_id": group_id,
                    "provider": a.provider,
                    "model": a.model,
                    "exec_class": a.exec_class.value,
                    "template_parent_id": node.template_parent_id,
                    "bindings": node.bindings,
                },
            )

        eventlog.record(self.store, job_id=job_id, type=EventType.PLAN_PROPOSED)
        eventlog.record(self.store, job_id=job_id, type=EventType.PLAN_APPROVED)
        return job_id

    # --- the wave loop ----------------------------------------------------

    def run_job(self, job_id: str) -> JobResult:
        """Drive ``job_id`` through the barrier-wave loop to completion (§7.2)."""
        if not lease.acquire(self.store, job_id, self.holder_id, self.config.lease_ttl_s):
            holder = lease.current(self.store, job_id)
            raise LeaseAcquisitionError(
                f"job {job_id!r} lease held by {holder[0] if holder else '?'!r}"
            )
        try:
            state = resume_job(self.store, job_id)
            known_refs = state.known_refs

            # 1. Finish any wave submitted before a crash — re-poll, never re-submit.
            for w in state.in_flight_waves:
                rendered = self._render_nodes(job_id, w.node_ids)
                self._await_and_process_wave(job_id, w.provider, w.wave_id, w.batch_ref, rendered)

            # 2. Barrier-wave loop.
            waves_run = 0
            for _ in range(self.config.max_waves):
                if not lease.renew(self.store, job_id, self.holder_id, self.config.lease_ttl_s):
                    raise LeaseLostError(f"lost lease on job {job_id!r} mid-run")

                nodes = self._load_nodes(job_id)
                frontier = self._frontier(nodes)
                if not frontier:
                    break

                remote: list[_NodeRow] = []
                for node in frontier:
                    self._mark_ready(job_id, node)
                    if isinstance(node.operator, Gate):
                        self._auto_approve_gate(job_id, node)
                    elif node.is_template:
                        self._resolve_template(job_id, node, nodes)
                    elif node.is_local:
                        self._run_local(job_id, node)
                    else:
                        remote.append(node)

                if remote:
                    waves_run += self._run_remote_wave(job_id, remote, known_refs)

            status, needs_human = self._finalize(job_id)
            report_dir = write_report(self.store, job_id, self.config, self.repo_root)
            log.info("job %s finished: status=%s waves=%d report=%s", job_id, status, waves_run, report_dir)
            return JobResult(
                job_id=job_id,
                status=status,
                waves=waves_run,
                report_dir=report_dir,
                needs_human=needs_human,
            )
        finally:
            lease.release(self.store, job_id, self.holder_id)

    # --- frontier / readiness --------------------------------------------

    def _load_nodes(self, job_id: str) -> dict[str, _NodeRow]:
        rows = self.store.conn.execute(
            "SELECT * FROM nodes WHERE job_id = ?", (job_id,)
        ).fetchall()
        out: dict[str, _NodeRow] = {}
        for row in rows:
            import json

            spec = json.loads(row["spec"])
            operator = _OPERATOR_ADAPTER.validate_python(spec)
            deps = json.loads(row["deps"])
            bindings = json.loads(row["bindings"]) if row["bindings"] else None
            # Row columns are authoritative over the (possibly parent-copied) spec.
            operator = operator.model_copy(
                update={
                    "id": row["id"],
                    "deps": deps,
                    "bindings": bindings,
                    "template_parent_id": row["template_parent_id"],
                }
            )
            out[row["id"]] = _NodeRow(
                id=row["id"],
                op=row["op"],
                status=NodeStatus(row["status"]),
                deps=deps,
                exec_class=row["exec_class"],
                provider=row["provider"],
                model=row["model"],
                group_id=row["group_id"],
                template_parent_id=row["template_parent_id"],
                bindings=bindings,
                operator=operator,
            )
        return out

    def _deps_satisfied(self, node: _NodeRow, nodes: dict[str, _NodeRow]) -> bool:
        for dep in node.deps:
            if dep.endswith(".*"):
                base = dep[:-2]
                children = [n for n in nodes.values() if n.template_parent_id == base]
                if not children:
                    return False  # parent not resolved yet
                if not all(c.status in _SUCCESS for c in children):
                    return False
            else:
                dep_node = nodes.get(dep)
                if dep_node is None or dep_node.status not in _SUCCESS:
                    return False
        return True

    def _frontier(self, nodes: dict[str, _NodeRow]) -> list[_NodeRow]:
        ready = [
            n
            for n in nodes.values()
            if n.status in _RUNNABLE and self._deps_satisfied(n, nodes)
        ]
        # Deterministic order (node-id) so applies within a wave are serialized
        # in a stable topological-then-id order (§9).
        return sorted(ready, key=lambda n: n.id)

    # --- state-change helpers --------------------------------------------

    def _state_change(self, job_id: str, node_id: str, frm: NodeStatus, to: NodeStatus) -> None:
        payload = NodeStateChangedPayload(node_id=node_id, from_status=frm, to_status=to)
        rec = eventlog.record(
            self.store,
            job_id=job_id,
            type=EventType.NODE_STATE_CHANGED,
            payload=payload.model_dump(mode="json"),
        )
        from lazycode.store import projections

        projections.apply(self.store, rec)

    def _emit(self, job_id: str, type_: EventType, payload: dict[str, Any] | None = None) -> None:
        from lazycode.store import projections

        rec = eventlog.record(self.store, job_id=job_id, type=type_, payload=payload or {})
        projections.apply(self.store, rec)

    def _mark_ready(self, job_id: str, node: _NodeRow) -> None:
        if node.status == NodeStatus.PENDING:
            self._emit(job_id, EventType.NODE_READY, {"node_id": node.id})
            node.status = NodeStatus.READY

    # --- Gate / template / local nodes -----------------------------------

    def _auto_approve_gate(self, job_id: str, node: _NodeRow) -> None:
        # M0: Gate is not executed as a DAG node; pre-flight y/N covers approval.
        self._emit(job_id, EventType.NODE_DONE, {"node_id": node.id})

    def _resolve_template(self, job_id: str, node: _NodeRow, nodes: dict[str, _NodeRow]) -> None:
        """Resolve a fan-out template from its upstream node's output (§3.2).

        M0 supports resolution from a local upstream node whose response text is
        a JSON list of ``bindings`` dicts (produced this run). If unavailable,
        the template goes to NEEDS_HUMAN (no realtime Decompose in M0).
        """
        import json

        from lazycode.planner import resolve_fanout

        source = None
        for dep in node.deps:
            base = dep[:-2] if dep.endswith(".*") else dep
            if base in self._local_outputs:
                source = self._local_outputs[base]
                break
        bindings_list: list[dict[str, Any]] | None = None
        if source:
            try:
                parsed = json.loads(source)
                if isinstance(parsed, list) and all(isinstance(x, dict) for x in parsed):
                    bindings_list = parsed
            except (ValueError, TypeError):
                bindings_list = None

        if not bindings_list:
            self._emit(job_id, EventType.NODE_NEEDS_HUMAN, {"node_id": node.id})
            return

        children = resolve_fanout(bindings_list, node.operator)
        child_ids = [c.id for c in children]
        payload = FanoutResolvedPayload(
            parent_id=node.id,
            child_ids=child_ids,
            bindings=[c.bindings or {} for c in children],
        )
        self._emit(job_id, EventType.FANOUT_RESOLVED, payload.model_dump(mode="json"))
        # The template parent is satisfied once its children exist.
        self._emit(job_id, EventType.NODE_DONE, {"node_id": node.id})

    def _run_local(self, job_id: str, node: _NodeRow) -> None:
        self._state_change(job_id, node.id, node.status, NodeStatus.EXECUTING_LOCAL)
        if isinstance(node.operator, Verify):
            result = run_verify(self._worktree(job_id), self.config.verify_command, self.config.verify_timeout_s)
            self._emit(
                job_id,
                EventType.VERIFY_RESULT,
                {"node_id": node.id, "passed": result.passed, "exit_code": result.exit_code, "tail": result.tail},
            )
        elif isinstance(node.operator, Explore):
            # M0 Explore-local: the harvest/repo-map IS the knowledge artifact.
            hr = harvest(node.operator.context_spec, self.repo_root) if getattr(node.operator, "context_spec", None) else HarvestResult()
            text = self._explore_text(node.operator, hr)
            self._local_outputs[node.id] = text
            digest = cas.put(self.store, text, kind="knowledge", meta={"node_id": node.id})
            self._emit(job_id, EventType.NODE_DONE, {"node_id": node.id, "artifact": digest})
        else:
            # Unsupported local op in M0.
            self._emit(job_id, EventType.NODE_NEEDS_HUMAN, {"node_id": node.id})

    def _explore_text(self, node: Explore, hr: HarvestResult) -> str:
        blocks = [b.text for b in hr.prefix_blocks]
        for relpath, content in hr.file_blocks.items():
            blocks.append(f"### {relpath}\n{content}")
        header = f"Explore: {node.question}\nScope: {', '.join(node.scope)}"
        return header + "\n\n" + "\n\n".join(blocks)

    # --- remote wave ------------------------------------------------------

    def _worktree(self, job_id: str) -> Worktree:
        row = self.store.conn.execute(
            "SELECT worktree_path, branch FROM task_groups WHERE job_id = ? LIMIT 1", (job_id,)
        ).fetchone()
        return Worktree(path=Path(row["worktree_path"]), branch=row["branch"])

    def _harvest_render(self, node: _NodeRow) -> RenderedCall:
        from .render import render_node

        spec = getattr(node.operator, "context_spec", None)
        hr = harvest(spec, self.repo_root, bindings=node.bindings) if spec is not None else HarvestResult()
        return render_node(node.operator, hr, self.config, model=node.model or self.config.model, bindings=node.bindings)

    def _render_nodes(self, job_id: str, node_ids: list[str]) -> dict[str, tuple[_NodeRow, RenderedCall]]:
        nodes = self._load_nodes(job_id)
        out: dict[str, tuple[_NodeRow, RenderedCall]] = {}
        for nid in node_ids:
            node = nodes.get(nid)
            if node is None:
                continue
            out[nid] = (node, self._harvest_render(node))
        return out

    def _count_prior_submits(self, job_id: str, content_prefix: str) -> int:
        count = 0
        for event in eventlog.read(self.store, job_id):
            if event.type != EventType.WAVE_SUBMITTED:
                continue
            key = event.payload.get("idempotency_key", "")
            if key.startswith(content_prefix + ":"):
                count += 1
        return count

    def _maybe_memo_hit(self, job_id: str, node: _NodeRow, call: RenderedCall) -> bool:
        """If ``call``'s prompt already has a completed cached result, process it
        from the memo cache instead of submitting (R10). Returns True on a hit."""
        cached = memo.get(self.store, call.memo_key)
        if cached is None or not cached.response_ref:
            return False
        import json

        payload = json.loads(cas.get(self.store, cached.response_ref).decode("utf-8"))
        result = ItemResult(custom_id=node.id, status=ItemStatus.COMPLETED, payload=payload)
        self._emit(job_id, EventType.NODE_HARVESTED, {"node_id": node.id})
        self._process_item(job_id, f"memo-{node.id}", node, call, result)
        return True

    def _run_remote_wave(
        self, job_id: str, remote: list[_NodeRow], known_refs: dict[str, BatchRef]
    ) -> int:
        """Form/submit/await one barrier wave (one batch per provider+model group)."""
        groups: dict[tuple[str, str], list[_NodeRow]] = {}
        for node in remote:
            key = (node.provider or self.config.provider, node.model or self.config.model)
            groups.setdefault(key, []).append(node)

        waves = 0
        for (provider, model), group_nodes in sorted(groups.items()):
            rendered = {n.id: (n, self._harvest_render(n)) for n in group_nodes}
            # R10 memo check: a node whose exact prompt already has a completed
            # result is served from cache — no (re)submission (§5.2 R10). This is
            # what makes an expired item's re-enqueue "memo-checked".
            group_nodes = [n for n in group_nodes if not self._maybe_memo_hit(job_id, n, rendered[n.id][1])]
            if not group_nodes:
                continue
            for n in group_nodes:
                self._emit(job_id, EventType.NODE_HARVESTED, {"node_id": n.id})

            items = [rendered[n.id][1] for n in group_nodes]
            content_prefix = submit_idempotency_key(items, 0).split(":", 1)[0]
            flush_ordinal = self._count_prior_submits(job_id, content_prefix)
            idem_key = submit_idempotency_key(items, flush_ordinal)
            wave_id = f"{content_prefix[:8]}-{flush_ordinal}"

            self._emit(
                job_id,
                EventType.WAVE_FORMED,
                {
                    "wave_id": wave_id,
                    "provider": provider,
                    "model": model,
                    "node_ids": [n.id for n in group_nodes],
                    "exec_class": ExecClass.BATCH.value,
                },
            )

            adapter = self.adapters[provider]
            batch_ref = adapter.submit(items, idem_key, known_refs=known_refs)
            known_refs[idem_key] = batch_ref

            submitted = WaveSubmittedPayload(
                wave_id=wave_id,
                provider=provider,
                model=model,
                batch_ref=batch_ref.batch_id,
                idempotency_key=idem_key,
                node_ids=[n.id for n in group_nodes],
                item_count=len(items),
            )
            self._emit(job_id, EventType.WAVE_SUBMITTED, submitted.model_dump(mode="json"))

            self._await_and_process_wave(job_id, provider, wave_id, batch_ref, rendered)
            waves += 1
        return waves

    def _await_and_process_wave(
        self,
        job_id: str,
        provider: str,
        wave_id: str,
        batch_ref: BatchRef,
        rendered: dict[str, tuple[_NodeRow, RenderedCall]],
    ) -> None:
        adapter = self.adapters[provider]
        delays = backoff_delays(base=self.config.poll_base_s, cap=self.config.poll_cap_s)
        while True:
            try:
                status = adapter.poll(batch_ref)
            except RetryableError:
                time.sleep(next(delays))
                continue
            if status.is_terminal:
                break
            time.sleep(next(delays))

        self._emit(job_id, EventType.WAVE_COMPLETED, {"wave_id": wave_id})

        for result in adapter.fetch(batch_ref):
            pair = rendered.get(result.custom_id)
            if pair is None:
                continue
            node, call = pair
            self._process_item(job_id, wave_id, node, call, result)

    def _process_item(
        self,
        job_id: str,
        wave_id: str,
        node: _NodeRow,
        call: RenderedCall,
        result: ItemResult,
    ) -> None:
        call_id = f"{wave_id}:{node.id}"

        if result.status is not ItemStatus.COMPLETED:
            memo.put(self.store, call_id=call_id, memo_key=call.memo_key, mode="batch", node_id=node.id, provider=call.model)
            memo.add_call_item(self.store, call_id=call_id, node_id=node.id, custom_id=result.custom_id, item_status=result.status.value)
            self._emit(
                job_id,
                EventType.ITEM_RETURNED,
                {"wave_id": wave_id, "custom_id": result.custom_id, "status": result.status.value, "call_id": call_id},
            )
            # Expired/errored → re-enqueue next wave (memo-checked resubmit).
            self._state_change(job_id, node.id, NodeStatus.EXPIRED, NodeStatus.RE_ENQUEUED)
            return

        text = extract_text(result.payload)
        tokens_in, tokens_out = payload_usage(result.payload)
        response_ref = cas.put(self.store, canonical_json(result.payload), kind="llm_response", meta={"node_id": node.id})
        request_ref = cas.put(self.store, canonical_json(call), kind="llm_request", meta={"node_id": node.id})
        rec = memo.put(
            self.store,
            call_id=call_id,
            memo_key=call.memo_key,
            mode="batch",
            node_id=node.id,
            provider=call.model,
            request_ref=request_ref,
            response_ref=response_ref,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        memo.add_call_item(self.store, call_id=rec.id, node_id=node.id, custom_id=result.custom_id, item_status="completed")
        self._emit(
            job_id,
            EventType.ITEM_RETURNED,
            {"wave_id": wave_id, "custom_id": result.custom_id, "status": result.status.value, "call_id": rec.id},
        )
        self._local_outputs[node.id] = text

        self._validate_and_apply(job_id, node, text, tokens_in, tokens_out)

    def _validate_and_apply(
        self, job_id: str, node: _NodeRow, text: str, tokens_in: int | None, tokens_out: int | None
    ) -> None:
        contract = getattr(node.operator, "output_contract", None)
        # M0 only enforces the diff contract (shape-only); other contracts pass through.
        if not isinstance(node.operator, Generate | Edit):
            self._record_stats(node, tokens_in, tokens_out, verify_pass=True)
            self._emit(job_id, EventType.NODE_DONE, {"node_id": node.id})
            return

        diff_text = extract_diff(text)
        files_within = getattr(contract, "files_within", []) or []
        try:
            validate_diff_paths(diff_text, files_within)
        except DiffPathViolation as exc:
            self._contract_fail(job_id, node, str(exc), tokens_in, tokens_out)
            return
        if not diff_text.strip():
            self._contract_fail(job_id, node, "empty diff", tokens_in, tokens_out)
            return

        self._emit(job_id, EventType.CONTRACT_RESULT, {"node_id": node.id, "passed": True})

        worktree = self._worktree(job_id)
        diff_hash = compute_diff_hash(diff_text)
        if ledger.already_applied(self.store, worktree=str(worktree.path), diff_hash=diff_hash):
            self._record_stats(node, tokens_in, tokens_out, verify_pass=True)
            self._emit(job_id, EventType.NODE_DONE, {"node_id": node.id})
            return

        ledger.record_intent(self.store, job_id=job_id, worktree=str(worktree.path), diff_hash=diff_hash, node_id=node.id)
        try:
            apply_diff(worktree, diff_text)
        except DiffConflict as exc:
            # M0: no integration repair — hand to a human.
            self._record_stats(node, tokens_in, tokens_out, verify_pass=False)
            self._emit(job_id, EventType.NODE_NEEDS_HUMAN, {"node_id": node.id, "reason": exc.stderr})
            return
        newly = ledger.record_applied(self.store, job_id=job_id, worktree=str(worktree.path), diff_hash=diff_hash, node_id=node.id)
        if newly:
            from datetime import UTC, datetime

            from lazycode.ir import Event
            from lazycode.store import projections

            # record_applied already appended ARTIFACT_APPLIED to the log; project
            # it into the nodes table (RETURNED → APPLIED) without re-appending.
            projections.apply(
                self.store,
                Event(
                    seq=0,
                    job_id=job_id,
                    ts=datetime.now(UTC),
                    type=EventType.ARTIFACT_APPLIED,
                    payload={"worktree": str(worktree.path), "diff_hash": diff_hash, "node_id": node.id},
                ),
            )
        self._record_stats(node, tokens_in, tokens_out, verify_pass=True)
        self._emit(job_id, EventType.NODE_DONE, {"node_id": node.id})

    def _contract_fail(
        self, job_id: str, node: _NodeRow, reason: str, tokens_in: int | None, tokens_out: int | None
    ) -> None:
        self._record_stats(node, tokens_in, tokens_out, verify_pass=False)
        self._emit(job_id, EventType.CONTRACT_RESULT, {"node_id": node.id, "passed": False, "reason": reason})

    def _record_stats(self, node: _NodeRow, tokens_in: int | None, tokens_out: int | None, *, verify_pass: bool) -> None:
        stats.record(
            self.store,
            op=node.op,
            model=node.model or self.config.model,
            repo=str(self.repo_root),
            tokens_in=tokens_in or 0,
            tokens_out=tokens_out or 0,
            rounds=1,
            verify_pass=verify_pass,
        )

    # --- finalize ---------------------------------------------------------

    def _finalize(self, job_id: str) -> tuple[str, list[str]]:
        nodes = self._load_nodes(job_id)
        needs_human = [n.id for n in nodes.values() if n.status == NodeStatus.NEEDS_HUMAN]
        stuck = [
            n.id
            for n in nodes.values()
            if n.status not in _SUCCESS and n.status != NodeStatus.NEEDS_HUMAN
        ]
        if not needs_human and not stuck:
            self._emit(job_id, EventType.JOB_DONE)
            return "DONE", []
        status = "NEEDS_HUMAN" if needs_human else "BLOCKED"
        return status, needs_human
