from __future__ import annotations

import pytest

from lazycode.ir import (
    CommandContract,
    ContextSpec,
    DiffContract,
    Explore,
    Generate,
    NodeStatus,
    Plan,
    Verify,
)
from lazycode.providers.mock import MockBatchAdapter
from lazycode.scheduler import Orchestrator, SchedulerConfig, resume_job
from lazycode.store import Store

from .conftest import GitRepo, completed


class _CrashOncePollAdapter(MockBatchAdapter):
    """A mock provider whose first ``poll`` raises (simulating a mid-wave crash
    right after the batch was submitted). The batch itself is retained server-
    side (in ``submitted_batches``), exactly like a real provider."""

    def __init__(self, responses) -> None:
        super().__init__(responses)
        self._crashed = False

    def poll(self, ref):
        if not self._crashed:
            self._crashed = True
            raise RuntimeError("simulated kill -9 mid-wave")
        return super().poll(ref)


def _plan() -> Plan:
    def gen(nid: str, target: str) -> Generate:
        return Generate(
            id=nid,
            spec=f"append to {target}",
            deps=["n1"],
            context_spec=ContextSpec(files=[target], repo_map=True),
            output_contract=DiffContract(files_within=[target]),
        )

    return Plan(
        goal="add constants",
        nodes=[
            Explore(id="n1", question="q", scope=["*.py"], prefer_local=True),
            gen("n2", "mod_a.py"),
            gen("n3", "mod_b.py"),
            Verify(id="n4", checks=[CommandContract(cmd="true", timeout_s=10)], deps=["n2", "n3"]),
        ],
    )


class _CrashMidFetchAdapter(MockBatchAdapter):
    """A mock provider whose first ``fetch`` yields exactly one item result and
    then raises (simulating a crash after the orchestrator processed item 1 of
    a wave but before the rest). The batch itself persists server-side, so a
    resumed orchestrator can re-poll and re-fetch it in full."""

    def __init__(self, responses) -> None:
        super().__init__(responses)
        self._crashed = False

    def fetch(self, ref):
        inner = super().fetch(ref)
        if not self._crashed:
            self._crashed = True
            yield next(inner)
            raise RuntimeError("simulated kill -9 after processing item 1")
        yield from inner


def test_crash_mid_result_processing_resumes_and_reprocesses_wave(git_repo: GitRepo):
    """Review F1: WAVE_COMPLETED must be emitted only after the fetch/process
    loop finishes. A crash after processing 1 of 3 items must leave the wave
    classified in-flight, so resume re-polls it and reprocesses idempotently
    (memo + applied_diffs dedupe) instead of stranding items 2-3 at SUBMITTED."""
    git_repo.write("mod_a.py", "A = 1\n")
    git_repo.write("mod_b.py", "B = 1\n")
    git_repo.write("mod_c.py", "C = 1\n")
    base = git_repo.commit("init")
    patches = {
        "n1": git_repo.make_patch("mod_a.py", "A = 1\nA2 = 2\n"),
        "n2": git_repo.make_patch("mod_b.py", "B = 1\nB2 = 2\n"),
        "n3": git_repo.make_patch("mod_c.py", "C = 1\nC2 = 2\n"),
    }

    def gen(nid: str, target: str) -> Generate:
        return Generate(
            id=nid,
            spec=f"append to {target}",
            context_spec=ContextSpec(files=[target], repo_map=True),
            output_contract=DiffContract(files_within=[target]),
        )

    plan = Plan(
        goal="add constants",
        nodes=[gen("n1", "mod_a.py"), gen("n2", "mod_b.py"), gen("n3", "mod_c.py")],
    )
    adapter = _CrashMidFetchAdapter({nid: completed(nid, patch) for nid, patch in patches.items()})
    cfg = SchedulerConfig()

    # --- first run: crashes after item 1 of the 3-item wave was processed ----
    store1 = Store.open(repo=git_repo.root)
    orch1 = Orchestrator(store1, {"anthropic": adapter}, git_repo.root, cfg, holder_id="runner-1")
    job_id = orch1.create_job("add constants", plan, base)
    with pytest.raises(RuntimeError, match="kill -9"):
        orch1.run_job(job_id)
    store1.close()
    assert len(adapter.submitted_batches) == 1

    # --- resume: the wave must still be classified in-flight ------------------
    store2 = Store.open(repo=git_repo.root)
    state = resume_job(store2, job_id)
    assert len(state.in_flight_waves) == 1, (
        "wave with unprocessed items was classified completed — items 2-3 are stranded"
    )

    orch2 = Orchestrator(store2, {"anthropic": adapter}, git_repo.root, cfg, holder_id="runner-1")
    result = orch2.run_job(job_id)

    assert result.status == "DONE"
    # No re-submit: resume re-polled the existing batch.
    assert len(adapter.submitted_batches) == 1

    statuses = {
        r["id"]: r["status"]
        for r in store2.conn.execute("SELECT id, status FROM nodes WHERE job_id = ?", (job_id,))
    }
    assert all(statuses[n] == NodeStatus.DONE.value for n in ("n1", "n2", "n3"))

    # No double-apply: exactly one ledger row per diff, file content sane.
    from pathlib import Path

    wt = Path(
        store2.conn.execute(
            "SELECT worktree_path FROM task_groups WHERE job_id = ?", (job_id,)
        ).fetchone()["worktree_path"]
    )
    for relpath, marker in (("mod_a.py", "A2 = 2"), ("mod_b.py", "B2 = 2"), ("mod_c.py", "C2 = 2")):
        content = (wt / relpath).read_text()
        assert content.count(marker) == 1
    ledger_rows = store2.conn.execute(
        "SELECT COUNT(*) c FROM applied_diffs WHERE worktree = ?", (str(wt),)
    ).fetchone()["c"]
    assert ledger_rows == 3
    store2.close()


def test_crash_after_submit_resumes_without_double_submit(git_repo: GitRepo):
    git_repo.write("mod_a.py", "A = 1\n")
    git_repo.write("mod_b.py", "B = 1\n")
    base = git_repo.commit("init")
    patch_a = git_repo.make_patch("mod_a.py", "A = 1\nA2 = 2\n")
    patch_b = git_repo.make_patch("mod_b.py", "B = 1\nB2 = 2\n")

    # ONE adapter instance survives the "restart" (the provider keeps the batch).
    adapter = _CrashOncePollAdapter(
        {"n2": completed("n2", patch_a), "n3": completed("n3", patch_b)}
    )
    cfg = SchedulerConfig()

    # --- first run: crashes right after wave-1 is submitted -------------------
    store1 = Store.open(repo=git_repo.root)
    orch1 = Orchestrator(store1, {"anthropic": adapter}, git_repo.root, cfg, holder_id="runner-1")
    job_id = orch1.create_job("add constants", _plan(), base)
    with pytest.raises(RuntimeError, match="kill -9"):
        orch1.run_job(job_id)
    store1.close()

    # The wave WAS submitted (durable) before the crash.
    assert len(adapter.submitted_batches) == 1

    # Resume reconstruction sees the in-flight wave and the known ref.
    store2 = Store.open(repo=git_repo.root)
    state = resume_job(store2, job_id)
    assert len(state.in_flight_waves) == 1
    assert len(state.known_refs) == 1

    # --- restart: fresh Orchestrator + fresh Store on the same DB ------------
    orch2 = Orchestrator(store2, {"anthropic": adapter}, git_repo.root, cfg, holder_id="runner-1")
    result = orch2.run_job(job_id)

    assert result.status == "DONE"
    # CRITICAL: the wave was NOT re-submitted on resume — still exactly one batch.
    assert len(adapter.submitted_batches) == 1

    statuses = {
        r["id"]: r["status"]
        for r in store2.conn.execute("SELECT id, status FROM nodes WHERE job_id = ?", (job_id,))
    }
    assert all(statuses[n] == NodeStatus.DONE.value for n in ("n1", "n2", "n3", "n4"))

    from pathlib import Path

    wt = Path(
        store2.conn.execute(
            "SELECT worktree_path FROM task_groups WHERE job_id = ?", (job_id,)
        ).fetchone()["worktree_path"]
    )
    assert "A2 = 2" in (wt / "mod_a.py").read_text()
    assert "B2 = 2" in (wt / "mod_b.py").read_text()
    store2.close()
