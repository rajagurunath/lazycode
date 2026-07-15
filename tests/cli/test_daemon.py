"""Daemon tests (module brief item 6): ephemeral-port startup in a thread,
health + POST /jobs against mocks, keep-awake inhibitor invoked/released via
an injected fake, and pidfile-based single-instance enforcement."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from lazycode.cli.client import DaemonClient
from lazycode.cli.config import LazycodeConfig
from lazycode.cli.daemon import (
    DaemonAlreadyRunningError,
    Inhibitor,
    check_existing,
    portfile_path,
    run_daemon,
)
from lazycode.ir import ContextSpec, DiffContract, Generate, Plan
from lazycode.providers.mock import MockBatchAdapter
from lazycode.store import Store

from .conftest import GitRepo, completed, diff_response


def _plan(target: str = "mod_a.py") -> Plan:
    return Plan(
        goal="add a constant",
        nodes=[
            Generate(
                id="n1",
                spec="append a constant",
                context_spec=ContextSpec(files=[target]),
                output_contract=DiffContract(files_within=[target]),
            )
        ],
    )


def _start_daemon_thread(repo_root, config, **kwargs) -> tuple[threading.Thread, threading.Event, int]:
    ready = threading.Event()
    stop = threading.Event()
    thread = threading.Thread(
        target=run_daemon,
        kwargs={
            "repo_root": repo_root,
            "config": config,
            "port": 0,
            "ready": ready,
            "stop_event": stop,
            **kwargs,
        },
        daemon=True,
    )
    thread.start()
    assert ready.wait(timeout=5.0), "daemon did not become ready in time"
    port = int(portfile_path(repo_root).read_text())
    return thread, stop, port


def _wait_for_job_done(client: DaemonClient, job_id: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        jobs = client.list_jobs()
        row = next((j for j in jobs if j["id"] == job_id), None)
        if row is not None:
            last = row
            if row["status"] in ("DONE", "NEEDS_HUMAN", "BLOCKED"):
                return row
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not reach a terminal state; last seen: {last}")


@pytest.fixture
def base_repo(git_repo: GitRepo) -> tuple[GitRepo, str, str]:
    git_repo.write("mod_a.py", "A = 1\n")
    base = git_repo.commit("init")
    patch = git_repo.make_patch("mod_a.py", "A = 1\nA2 = 2\n")
    return git_repo, base, patch


def test_health_and_post_job_with_mocks(base_repo: tuple[GitRepo, str, str]):
    git_repo, base, patch = base_repo
    adapter = MockBatchAdapter({"n1": completed("n1", diff_response(patch))})
    store = Store.open(repo=git_repo.root)
    config = LazycodeConfig(keep_awake=False)

    thread, stop, port = _start_daemon_thread(
        git_repo.root, config, adapters={"anthropic": adapter}, store=store
    )
    try:
        client = DaemonClient("127.0.0.1", port)

        health = client.health()
        assert health["status"] == "ok"
        assert health["active_jobs"] == 0
        assert health["keep_awake_active"] is False

        job_id = client.submit_job(goal="add a constant", plan=_plan().model_dump(mode="json"), base_commit=base)
        assert job_id

        row = _wait_for_job_done(client, job_id)
        assert row["status"] == "DONE"
        assert row["waves"] == 1

        # The daemon really did write to the store (single-writer, §7.1).
        node_status = store.conn.execute(
            "SELECT status FROM nodes WHERE job_id = ? AND id = 'n1'", (job_id,)
        ).fetchone()
        assert node_status["status"] == "DONE"
    finally:
        stop.set()
        thread.join(timeout=5.0)
        store.close()


def test_get_jobs_lists_status(base_repo: tuple[GitRepo, str, str]):
    git_repo, base, patch = base_repo
    adapter = MockBatchAdapter({"n1": completed("n1", diff_response(patch))})
    store = Store.open(repo=git_repo.root)
    config = LazycodeConfig(keep_awake=False)

    thread, stop, port = _start_daemon_thread(
        git_repo.root, config, adapters={"anthropic": adapter}, store=store
    )
    try:
        client = DaemonClient("127.0.0.1", port)
        assert client.list_jobs() == []
        job_id = client.submit_job(goal="add a constant", plan=_plan().model_dump(mode="json"), base_commit=base)
        _wait_for_job_done(client, job_id)
        jobs = client.list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["id"] == job_id
        assert jobs[0]["goal"] == "add a constant"
    finally:
        stop.set()
        thread.join(timeout=5.0)
        store.close()


class _CrashOncePollAdapter(MockBatchAdapter):
    """First ``poll`` raises (simulating a crash / daemon death mid-wave);
    the provider-side batch persists in ``submitted_batches``."""

    def __init__(self, responses) -> None:
        super().__init__(responses)
        self._crashed = False

    def poll(self, ref):
        if not self._crashed:
            self._crashed = True
            raise RuntimeError("simulated daemon death mid-wave")
        return super().poll(ref)


def test_daemon_startup_resumes_interrupted_job(base_repo: tuple[GitRepo, str, str]):
    """Review F4: on startup the daemon must scan for jobs with a non-terminal
    status and no live lease and re-enqueue them with resume semantics —
    otherwise a job interrupted by a daemon crash is stranded forever."""
    from lazycode.scheduler import Orchestrator, SchedulerConfig

    git_repo, base, patch = base_repo
    adapter = _CrashOncePollAdapter({"n1": completed("n1", diff_response(patch))})

    # An earlier orchestrator run died mid-wave (batch already submitted).
    store1 = Store.open(repo=git_repo.root)
    orch = Orchestrator(
        store1, {"anthropic": adapter}, git_repo.root, SchedulerConfig(), holder_id="dead-daemon"
    )
    job_id = orch.create_job("add a constant", _plan(), base)
    with pytest.raises(RuntimeError, match="mid-wave"):
        orch.run_job(job_id)
    store1.close()
    assert len(adapter.submitted_batches) == 1

    # A fresh daemon starts: it must pick the half-done job up by itself.
    store = Store.open(repo=git_repo.root)
    config = LazycodeConfig(keep_awake=False)
    thread, stop, port = _start_daemon_thread(
        git_repo.root, config, adapters={"anthropic": adapter}, store=store
    )
    try:
        client = DaemonClient("127.0.0.1", port)
        row = _wait_for_job_done(client, job_id)
        assert row["status"] == "DONE"
        # Resume semantics: the in-flight batch was re-polled, never re-submitted.
        assert len(adapter.submitted_batches) == 1
    finally:
        stop.set()
        thread.join(timeout=5.0)
        store.close()


def test_resume_via_daemon_endpoint_returns_202_and_completes(base_repo: tuple[GitRepo, str, str]):
    """Review F4: ``POST /jobs/{id}/resume`` re-enqueues an interrupted job
    through the worker (202), instead of the CLI refusing while a daemon is up."""
    import json as json_mod
    import urllib.request

    git_repo, base, patch = base_repo
    adapter = _CrashOncePollAdapter({"n1": completed("n1", diff_response(patch))})
    store = Store.open(repo=git_repo.root)
    config = LazycodeConfig(keep_awake=False)

    thread, stop, port = _start_daemon_thread(
        git_repo.root, config, adapters={"anthropic": adapter}, store=store
    )
    try:
        client = DaemonClient("127.0.0.1", port)
        job_id = client.submit_job(
            goal="add a constant", plan=_plan().model_dump(mode="json"), base_commit=base
        )
        # First attempt dies mid-wave inside the worker; wait for it to settle.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            health = client.health()
            jobs = client.list_jobs()
            if (
                health["active_jobs"] == 0
                and health["queue_depth"] == 0
                and any(j["id"] == job_id for j in jobs)
            ):
                break
            time.sleep(0.05)
        assert len(adapter.submitted_batches) == 1

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/jobs/{job_id}/resume", data=b"{}", method="POST"
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            assert resp.status == 202
            body = json_mod.loads(resp.read().decode("utf-8"))
        assert body["job_id"] == job_id

        row = _wait_for_job_done(client, job_id)
        assert row["status"] == "DONE"
        assert len(adapter.submitted_batches) == 1  # adopted, not re-submitted
    finally:
        stop.set()
        thread.join(timeout=5.0)
        store.close()


def test_resume_endpoint_unknown_job_404(base_repo: tuple[GitRepo, str, str]):
    import urllib.error
    import urllib.request

    git_repo, _base, _patch = base_repo
    store = Store.open(repo=git_repo.root)
    config = LazycodeConfig(keep_awake=False)
    thread, stop, port = _start_daemon_thread(
        git_repo.root, config, adapters={"anthropic": MockBatchAdapter()}, store=store
    )
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/jobs/job-doesnotexist/resume", data=b"{}", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(req, timeout=3.0)
        assert excinfo.value.code == 404
    finally:
        stop.set()
        thread.join(timeout=5.0)
        store.close()


class _FakeProc:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


def test_keep_awake_true_policy_starts_and_stops_inhibitor(base_repo: tuple[GitRepo, str, str]):
    git_repo, base, patch = base_repo
    adapter = MockBatchAdapter({"n1": completed("n1", diff_response(patch))})
    store = Store.open(repo=git_repo.root)
    config = LazycodeConfig(keep_awake=True)  # always inhibit while >=1 job active

    spawn_calls: list[list[str]] = []

    def fake_spawn(cmd: list[str]) -> _FakeProc:
        spawn_calls.append(cmd)
        return _FakeProc()

    inhibitor = Inhibitor(command=["fake-caffeinate", "-i"], spawn=fake_spawn)

    thread, stop, port = _start_daemon_thread(
        git_repo.root, config, adapters={"anthropic": adapter}, store=store, inhibitor=inhibitor
    )
    try:
        client = DaemonClient("127.0.0.1", port)
        assert inhibitor.start_count == 0

        job_id = client.submit_job(
            goal="add a constant", plan=_plan().model_dump(mode="json"), base_commit=base, keep_awake=None
        )
        _wait_for_job_done(client, job_id)

        # Give the worker's finally-block a moment to run past job completion.
        deadline = time.monotonic() + 2.0
        while inhibitor.stop_count == 0 and time.monotonic() < deadline:
            time.sleep(0.02)

        assert spawn_calls == [["fake-caffeinate", "-i"]]
        assert inhibitor.start_count == 1
        assert inhibitor.stop_count == 1
        assert inhibitor.active is False
    finally:
        stop.set()
        thread.join(timeout=5.0)
        store.close()


def test_keep_awake_ask_policy_only_inhibits_when_job_requests_it(base_repo: tuple[GitRepo, str, str]):
    git_repo, base, patch = base_repo
    adapter = MockBatchAdapter({"n1": completed("n1", diff_response(patch))})
    store = Store.open(repo=git_repo.root)
    config = LazycodeConfig(keep_awake="ask")

    spawn_calls: list[list[str]] = []

    def fake_spawn(cmd: list[str]) -> _FakeProc:
        spawn_calls.append(cmd)
        return _FakeProc()

    inhibitor = Inhibitor(command=["fake-caffeinate"], spawn=fake_spawn)

    thread, stop, port = _start_daemon_thread(
        git_repo.root, config, adapters={"anthropic": adapter}, store=store, inhibitor=inhibitor
    )
    try:
        client = DaemonClient("127.0.0.1", port)

        # Job 1: CLI asked, user said no -> no inhibitor.
        job1 = client.submit_job(
            goal="job one", plan=_plan().model_dump(mode="json"), base_commit=base, keep_awake=False
        )
        _wait_for_job_done(client, job1)
        assert inhibitor.start_count == 0

        # Job 2: CLI asked, user said yes -> inhibitor starts, then releases.
        job2 = client.submit_job(
            goal="job two", plan=_plan().model_dump(mode="json"), base_commit=base, keep_awake=True
        )
        _wait_for_job_done(client, job2)
        deadline = time.monotonic() + 2.0
        while inhibitor.stop_count == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert inhibitor.start_count == 1
        assert inhibitor.stop_count == 1
    finally:
        stop.set()
        thread.join(timeout=5.0)
        store.close()


def test_pidfile_prevents_second_daemon(base_repo: tuple[GitRepo, str, str]):
    git_repo, base, patch = base_repo
    store = Store.open(repo=git_repo.root)
    config = LazycodeConfig(keep_awake=False)

    thread, stop, port = _start_daemon_thread(
        git_repo.root, config, adapters={"anthropic": MockBatchAdapter()}, store=store
    )
    try:
        assert check_existing(git_repo.root) is not None

        store2 = Store.open(repo=git_repo.root)
        try:
            with pytest.raises(DaemonAlreadyRunningError):
                run_daemon(
                    git_repo.root, config, adapters={"anthropic": MockBatchAdapter()}, store=store2
                )
        finally:
            store2.close()
    finally:
        stop.set()
        thread.join(timeout=5.0)
        store.close()

    # Clean shutdown clears the pidfile -- a new daemon can start again.
    assert check_existing(git_repo.root) is None


def test_sched_config_for_applies_per_job_model_override(base_repo: tuple[GitRepo, str, str]):
    """§8/M2-forward-compat: a job's provider/model override produces a
    distinct SchedulerConfig without mutating the daemon's default one."""
    git_repo, _base, _patch = base_repo
    store = Store.open(repo=git_repo.root)
    config = LazycodeConfig(keep_awake=False)
    from lazycode.cli.daemon import Daemon, JobRequest

    d = Daemon(
        store=store,
        adapters={"anthropic": MockBatchAdapter()},
        repo_root=git_repo.root,
        config=config,
        sched_config=config.to_scheduler_config(),
    )
    try:
        base_cfg = d._base_sched_config
        req = JobRequest(
            job_id="job-x",
            goal="g",
            plan=_plan(),
            base_commit="deadbeef",
            slider=70,
            provider=None,
            model="claude-opus-4",
            keep_awake=None,
        )
        overridden = d._sched_config_for(req)
        assert overridden.model == "claude-opus-4"
        assert overridden.provider == base_cfg.provider
        assert overridden == replace(base_cfg, model="claude-opus-4")
        # No override -> same object back, base config untouched.
        no_override = d._sched_config_for(
            replace(req, provider=None, model=None)
        )
        assert no_override is base_cfg
    finally:
        store.close()
        d._read_store.close()
