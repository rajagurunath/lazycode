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
from lazycode.scheduler import (
    LeaseAcquisitionError,
    Orchestrator,
    SchedulerConfig,
)
from lazycode.store import Store, lease

from .conftest import GitRepo, completed, diff_response, expired


def _open_store(git_repo: GitRepo) -> Store:
    return Store.open(repo=git_repo.root)


def _gen(nid: str, target: str, deps: list[str]) -> Generate:
    return Generate(
        id=nid,
        spec=f"append a constant to {target}",
        deps=deps,
        context_spec=ContextSpec(files=[target], repo_map=True),
        output_contract=DiffContract(files_within=[target]),
    )


def _two_gen_plan() -> Plan:
    return Plan(
        goal="add constants",
        nodes=[
            Explore(id="n1", question="which files", scope=["*.py"], prefer_local=True),
            _gen("n2", "mod_a.py", ["n1"]),
            _gen("n3", "mod_b.py", ["n1"]),
            Verify(id="n4", checks=[CommandContract(cmd="true", timeout_s=10)], deps=["n2", "n3"]),
        ],
    )


def _worktree_path(store: Store, job_id: str):
    from pathlib import Path

    row = store.conn.execute(
        "SELECT worktree_path FROM task_groups WHERE job_id = ?", (job_id,)
    ).fetchone()
    return Path(row["worktree_path"])


def test_happy_path_two_wave_job(git_repo: GitRepo):
    git_repo.write("mod_a.py", "A = 1\n")
    git_repo.write("mod_b.py", "B = 1\n")
    base = git_repo.commit("init")

    patch_a = git_repo.make_patch("mod_a.py", "A = 1\nA2 = 2\n")
    patch_b = git_repo.make_patch("mod_b.py", "B = 1\nB2 = 2\n")
    adapter = MockBatchAdapter(
        {
            "n2": completed("n2", diff_response(patch_a, assumptions="chose A2 name")),
            "n3": completed("n3", diff_response(patch_b)),
        }
    )

    store = _open_store(git_repo)
    orch = Orchestrator(store, {"anthropic": adapter}, git_repo.root, SchedulerConfig())
    job_id = orch.create_job("add constants", _two_gen_plan(), base)
    result = orch.run_job(job_id)

    assert result.status == "DONE"
    # One remote batch wave (the two Generates); Explore + Verify are local.
    assert result.waves == 1
    # B6: a "wave" for the accept test = a waves row that reached >= SUBMITTED.
    submitted_waves = store.conn.execute(
        "SELECT COUNT(*) c FROM waves WHERE job_id = ? AND status IN ('SUBMITTED', 'COMPLETED')",
        (job_id,),
    ).fetchone()["c"]
    assert submitted_waves == 1

    # Diffs actually landed in the group worktree.
    wt = _worktree_path(store, job_id)
    assert "A2 = 2" in (wt / "mod_a.py").read_text()
    assert "B2 = 2" in (wt / "mod_b.py").read_text()

    # Every node reached a terminal-success state.
    statuses = {
        r["id"]: r["status"]
        for r in store.conn.execute("SELECT id, status FROM nodes WHERE job_id = ?", (job_id,))
    }
    assert statuses["n1"] == NodeStatus.DONE.value
    assert statuses["n2"] == NodeStatus.DONE.value
    assert statuses["n3"] == NodeStatus.DONE.value
    assert statuses["n4"] == NodeStatus.DONE.value

    # Report written with the assumption ledger.
    assert result.report_dir is not None
    md = (result.report_dir / "report.md").read_text()
    assert "chose A2 name" in md
    assert (result.report_dir / "report.json").exists()
    store.close()


def test_contract_fail_goes_needs_human(git_repo: GitRepo):
    git_repo.write("mod_a.py", "A = 1\n")
    base = git_repo.commit("init")
    # Diff touches a file OUTSIDE files_within → path violation.
    bad_patch = git_repo.make_patch("mod_a.py", "A = 1\nX = 9\n").replace("mod_a.py", "other.py")
    adapter = MockBatchAdapter({"n2": completed("n2", bad_patch)})

    store = _open_store(git_repo)
    orch = Orchestrator(store, {"anthropic": adapter}, git_repo.root, SchedulerConfig())
    plan = Plan(goal="g", nodes=[_gen("n2", "mod_a.py", [])])
    job_id = orch.create_job("g", plan, base)
    result = orch.run_job(job_id)

    assert result.needs_human == ["n2"]
    assert result.status == "NEEDS_HUMAN"
    store.close()


def test_verify_fail_goes_needs_human(git_repo: GitRepo):
    git_repo.write("mod_a.py", "A = 1\n")
    base = git_repo.commit("init")
    patch_a = git_repo.make_patch("mod_a.py", "A = 1\nA2 = 2\n")
    adapter = MockBatchAdapter({"n2": completed("n2", patch_a)})

    store = _open_store(git_repo)
    # verify_command 'false' always fails.
    cfg = SchedulerConfig(verify_command="false")
    orch = Orchestrator(store, {"anthropic": adapter}, git_repo.root, cfg)
    plan = Plan(
        goal="g",
        nodes=[
            _gen("n2", "mod_a.py", []),
            Verify(id="v", checks=[], deps=["n2"]),
        ],
    )
    job_id = orch.create_job("g", plan, base)
    result = orch.run_job(job_id)

    assert "v" in result.needs_human
    statuses = {
        r["id"]: r["status"]
        for r in store.conn.execute("SELECT id, status FROM nodes WHERE job_id = ?", (job_id,))
    }
    assert statuses["n2"] == NodeStatus.DONE.value  # the edit applied fine
    assert statuses["v"] == NodeStatus.NEEDS_HUMAN.value  # verify failed
    store.close()


def test_lease_contention_blocks_second_orchestrator(git_repo: GitRepo):
    git_repo.write("mod_a.py", "A = 1\n")
    base = git_repo.commit("init")
    adapter = MockBatchAdapter()
    store = _open_store(git_repo)
    orch = Orchestrator(store, {"anthropic": adapter}, git_repo.root, SchedulerConfig())
    plan = Plan(goal="g", nodes=[_gen("n2", "mod_a.py", [])])
    job_id = orch.create_job("g", plan, base)

    # A different holder already holds a live lease.
    assert lease.acquire(store, job_id, "other-holder", 300.0)
    with pytest.raises(LeaseAcquisitionError):
        orch.run_job(job_id)
    store.close()


class _ExpireOnceAdapter(MockBatchAdapter):
    """Returns EXPIRED for a custom_id's first batch, COMPLETED thereafter."""

    def __init__(self, diffs: dict[str, str]) -> None:
        super().__init__()
        self._diffs = diffs
        self.attempts: dict[str, int] = {}
        self._responses = self._result_for

    def submit(self, items, idempotency_key, *, known_refs=None):
        for call in items:
            self.attempts[call.custom_id] = self.attempts.get(call.custom_id, 0) + 1
        return super().submit(items, idempotency_key, known_refs=known_refs)

    def _result_for(self, call):
        if self.attempts.get(call.custom_id, 0) <= 1:
            return expired(call.custom_id)
        return completed(call.custom_id, self._diffs[call.custom_id])


def test_expired_item_is_resubmitted_and_completes(git_repo: GitRepo):
    git_repo.write("mod_a.py", "A = 1\n")
    base = git_repo.commit("init")
    patch_a = git_repo.make_patch("mod_a.py", "A = 1\nA2 = 2\n")
    adapter = _ExpireOnceAdapter({"n2": patch_a})

    store = _open_store(git_repo)
    orch = Orchestrator(store, {"anthropic": adapter}, git_repo.root, SchedulerConfig())
    plan = Plan(goal="g", nodes=[_gen("n2", "mod_a.py", [])])
    job_id = orch.create_job("g", plan, base)
    result = orch.run_job(job_id)

    assert result.status == "DONE"
    # Submitted twice: the expired first attempt + the memo-checked re-enqueue.
    assert len(adapter.submitted_batches) == 2
    # Two distinct idempotency keys (different flush ordinals).
    keys = {
        r["idempotency_key"]
        for r in store.conn.execute(
            "SELECT idempotency_key FROM waves WHERE job_id = ?", (job_id,)
        )
    }
    assert len(keys) == 2
    wt = _worktree_path(store, job_id)
    assert "A2 = 2" in (wt / "mod_a.py").read_text()
    store.close()
