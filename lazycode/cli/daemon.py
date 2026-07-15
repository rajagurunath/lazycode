"""The M0 daemon (DESIGN.md §2 process model, §7.1 single-writer, §7.5
keep-awake, §12).

When running, this process is the **sole event-log writer** (§2): it owns one
:class:`~lazycode.store.Store` connection, drives jobs through the
:class:`~lazycode.scheduler.Orchestrator` one at a time (M0 — no concurrent
jobs), and serves a minimal JSON-over-localhost-HTTP API (stdlib
``http.server``, no framework dependency) so ``lazycode run`` can hand off a
job without touching the database itself. ``lazycode status``/``explain``/
``review`` never go through this API — they open their own **read-only**
connection to the same SQLite file (safe under WAL with a single writer,
§7.1) — this module's HTTP surface exists only for the one thing a second
process must *not* do directly: writing to the event log.

Endpoints:

* ``GET /health`` — liveness + active-job count + keep-awake state.
* ``GET /jobs`` — status list (id, goal, status, waves).
* ``POST /jobs`` — ``{goal, plan, base_commit, slider, provider, model,
  keep_awake}`` → enqueues a job (already-approved ``Plan``, from the CLI's
  y/N gate) and returns ``{"job_id": ...}`` immediately; a background worker
  thread drains the queue sequentially through the ``Orchestrator``.

**Keep-awake (§7.5).** While ≥1 job is active, the daemon holds a sleep
inhibitor: ``caffeinate -i`` on macOS, ``systemd-inhibit --what=idle
--mode=block sleep infinity`` on Linux. Policy (``config.keep_awake``):
``True`` → always inhibit while any job is active; ``False`` → never;
``"ask"`` → inhibit only if the *specific* job that's currently running was
submitted with ``keep_awake=True`` (the CLI already asked the user at
submission time — see ``app.py``). The inhibitor's process-spawn is
injectable (:class:`Inhibitor`'s ``spawn`` parameter) so tests can verify
start/stop calls without touching the real OS sleep state.

**Single instance (pidfile).** ``.lazycode/daemon/{daemon.pid,daemon.port}``
under the repo root. :func:`check_existing` treats a pidfile whose PID is no
longer alive as stale (safe to start over); a live PID blocks a second
``lazycode daemon`` in the same repo (:class:`DaemonAlreadyRunningError`).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import queue
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from lazycode.ir import Plan
from lazycode.notify import notify as notify_fn
from lazycode.providers.base import BatchAdapter
from lazycode.scheduler import Orchestrator, SchedulerConfig
from lazycode.store import Store

from .config import LazycodeConfig

log = logging.getLogger("lazycode.cli.daemon")


class DaemonAlreadyRunningError(Exception):
    """A live daemon already holds this repo's pidfile."""


# --- pidfile / port file --------------------------------------------------


def daemon_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".lazycode" / "daemon"


def pidfile_path(repo_root: str | Path) -> Path:
    return daemon_dir(repo_root) / "daemon.pid"


def portfile_path(repo_root: str | Path) -> Path:
    return daemon_dir(repo_root) / "daemon.port"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours
    except OSError:
        return False
    return True


def check_existing(repo_root: str | Path) -> tuple[int, int] | None:
    """Return ``(pid, port)`` of a live daemon for this repo, or ``None`` if
    there is none, or the pidfile is stale (process no longer alive)."""
    pf = pidfile_path(repo_root)
    if not pf.is_file():
        return None
    try:
        pid = int(pf.read_text().strip())
    except ValueError:
        return None
    if not _pid_alive(pid):
        return None
    ptf = portfile_path(repo_root)
    if not ptf.is_file():
        return None
    try:
        port = int(ptf.read_text().strip())
    except ValueError:
        return None
    return pid, port


def _write_pidfiles(repo_root: str | Path, port: int) -> None:
    daemon_dir(repo_root).mkdir(parents=True, exist_ok=True)
    pidfile_path(repo_root).write_text(str(os.getpid()), encoding="utf-8")
    portfile_path(repo_root).write_text(str(port), encoding="utf-8")


def _clear_pidfiles(repo_root: str | Path) -> None:
    for p in (pidfile_path(repo_root), portfile_path(repo_root)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


# --- keep-awake inhibitor (§7.5) ------------------------------------------


def default_inhibitor_command() -> list[str] | None:
    """The platform sleep-inhibiting command, or ``None`` on an unsupported
    platform (inhibitor becomes a harmless no-op there)."""
    system = platform.system()
    if system == "Darwin":
        return ["caffeinate", "-i"]
    if system == "Linux":
        return ["systemd-inhibit", "--what=idle", "--mode=block", "sleep", "infinity"]
    return None


SpawnFn = Callable[[list[str]], Any]


class Inhibitor:
    """Holds a sleep-inhibiting subprocess while the daemon has active jobs.

    ``spawn`` is injected (module brief: "inject the inhibitor-spawn as a
    callable for testability") — defaults to ``subprocess.Popen`` against
    :func:`default_inhibitor_command`; tests pass a fake recording calls
    instead of touching real OS sleep state.
    """

    def __init__(self, *, command: list[str] | None = None, spawn: SpawnFn | None = None) -> None:
        self._command = command if command is not None else default_inhibitor_command()
        self._spawn = spawn or subprocess.Popen
        self._proc: Any = None
        self.start_count = 0
        self.stop_count = 0

    @property
    def active(self) -> bool:
        return self._proc is not None

    def start(self) -> None:
        if self._proc is not None or self._command is None:
            return
        self._proc = self._spawn(self._command)
        self.start_count += 1

    def stop(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        self.stop_count += 1
        terminate = getattr(proc, "terminate", None)
        if terminate is not None:
            try:
                terminate()
            except Exception:  # pragma: no cover - best effort cleanup
                log.debug("inhibitor terminate() raised", exc_info=True)


# --- job queue -------------------------------------------------------------


@dataclass(frozen=True)
class JobRequest:
    """One queued job (§7.2 wave loop, one job at a time in M0).

    ``provider``/``model`` are per-job overrides of the daemon's default
    :class:`~lazycode.scheduler.SchedulerConfig` (repo/global config at
    daemon startup) -- applied by building a fresh, per-job
    :class:`~lazycode.scheduler.SchedulerConfig` (`Daemon._sched_config_for`)
    since M0's ``Orchestrator.create_job`` itself takes no such parameter.

    ``resume=True`` re-drives an **existing** job through the worker
    (``Orchestrator.run_job`` only — no ``create_job``); ``plan`` and
    ``base_commit`` are irrelevant then and may be ``None``.
    """

    job_id: str
    goal: str
    plan: Plan | None = None
    base_commit: str | None = None
    slider: int = 70
    provider: str | None = None
    model: str | None = None
    keep_awake: bool | None = None
    resume: bool = False


class Daemon:
    """Owns the Store, the job queue/worker, the keep-awake inhibitor, and
    the HTTP server. Constructed with everything injected so tests never
    touch a real provider or the real OS sleep state.

    A fresh :class:`~lazycode.scheduler.Orchestrator` is built per job (cheap
    -- it holds only references, no heavy state) rather than one shared
    instance, so a per-job ``provider``/``model`` override (:class:`JobRequest`)
    can take effect via a per-job :class:`~lazycode.scheduler.SchedulerConfig`
    even though M0's ``Orchestrator.create_job`` itself has no such
    parameter. All jobs still share the one ``Store`` connection (single
    writer, §7.1) and run strictly sequentially (one job at a time, M0).
    """

    def __init__(
        self,
        *,
        store: Store,
        adapters: dict[str, BatchAdapter],
        repo_root: str | Path,
        config: LazycodeConfig,
        sched_config: SchedulerConfig,
        inhibitor: Inhibitor | None = None,
        holder_id: str | None = None,
    ) -> None:
        self.store = store
        self._adapters = adapters
        self.repo_root = Path(repo_root)
        self.config = config
        self._base_sched_config = sched_config
        self._holder_id = holder_id
        self.inhibitor = inhibitor or Inhibitor()
        # Separate read-only-usage connection for HTTP GETs -- WAL supports
        # many concurrent readers alongside the one writer connection the
        # Orchestrator uses; sharing that connection across threads instead
        # would serialize HTTP reads behind in-flight writes for no reason.
        self._read_store = Store.open(db_path=store.db_path)

        self._queue: queue.Queue[JobRequest | None] = queue.Queue()
        self._lock = threading.Lock()
        self._active_jobs = 0
        self._worker_thread: threading.Thread | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None

    # --- state --------------------------------------------------------

    @property
    def active_jobs(self) -> int:
        with self._lock:
            return self._active_jobs

    def job_list(self) -> list[dict[str, Any]]:
        rows = self._read_store.conn.execute(
            "SELECT id, goal, status FROM jobs ORDER BY created_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            waves = self._read_store.conn.execute(
                "SELECT COUNT(*) c FROM waves WHERE job_id = ? AND status IN ('SUBMITTED', 'COMPLETED')",
                (r["id"],),
            ).fetchone()["c"]
            out.append({"id": r["id"], "goal": r["goal"], "status": r["status"], "waves": waves})
        return out

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "pid": os.getpid(),
            "active_jobs": self.active_jobs,
            "queue_depth": self._queue.qsize(),
            "keep_awake_active": self.inhibitor.active,
            "keep_awake_policy": self.config.keep_awake,
        }

    # --- job submission + worker ---------------------------------------

    def submit(self, req: JobRequest) -> None:
        self._queue.put(req)

    def resume_unfinished_jobs(self) -> list[str]:
        """Re-enqueue every job with a non-terminal status and no live lease
        (review F4; §7.5 — batches complete server-side while no daemon runs,
        so an interrupted job must be picked up again on startup, with resume
        semantics through the normal worker path)."""
        from datetime import UTC, datetime

        from lazycode.store import lease as lease_mod

        now = datetime.now(UTC)
        resumed: list[str] = []
        rows = self._read_store.conn.execute(
            "SELECT id, goal FROM jobs WHERE status NOT IN ('DONE', 'CANCELLED') "
            "ORDER BY created_at"
        ).fetchall()
        for row in rows:
            held = lease_mod.current(self._read_store, row["id"])
            if held is not None and held[1] > now:
                continue  # a live holder is already advancing this job
            self.submit(JobRequest(job_id=row["id"], goal=row["goal"], resume=True))
            resumed.append(row["id"])
        if resumed:
            log.info("daemon startup: resuming %d unfinished job(s): %s", len(resumed), resumed)
        return resumed

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            try:
                self._run_one(item)
            finally:
                self._queue.task_done()

    def _sched_config_for(self, req: JobRequest) -> SchedulerConfig:
        from dataclasses import replace

        overrides: dict[str, Any] = {}
        if req.provider:
            overrides["provider"] = req.provider
        if req.model:
            overrides["model"] = req.model
        if not overrides:
            return self._base_sched_config
        return replace(self._base_sched_config, **overrides)

    def _run_one(self, req: JobRequest) -> None:
        with self._lock:
            self._active_jobs += 1
            self._maybe_start_inhibitor(req.keep_awake)
        try:
            orch = Orchestrator(
                self.store,
                self._adapters,
                self.repo_root,
                self._sched_config_for(req),
                holder_id=self._holder_id,
            )
            if req.resume:
                # Existing job: resume semantics only (run_job replays the
                # event log, re-polls in-flight waves, reconciles orphans).
                job_id = req.job_id
            else:
                job_id = orch.create_job(
                    req.goal, req.plan, req.base_commit, slider=req.slider, job_id=req.job_id
                )
            result = orch.run_job(job_id)
            notify_fn(job_id, f"job finished: status={result.status}", repo_root=self.repo_root)
        except Exception as exc:  # keep the worker alive across job failures
            log.exception("job %s failed", req.job_id)
            notify_fn(req.job_id, f"job failed: {exc}", repo_root=self.repo_root)
        finally:
            with self._lock:
                self._active_jobs -= 1
                if self._active_jobs == 0:
                    self.inhibitor.stop()

    def _maybe_start_inhibitor(self, job_keep_awake: bool | None) -> None:
        policy = self.config.keep_awake
        should = policy is True or (policy == "ask" and bool(job_keep_awake))
        if should:
            self.inhibitor.start()

    # --- HTTP ------------------------------------------------------------

    def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((host, port), handler)
        actual_port = self._httpd.server_address[1]
        self._server_thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._server_thread.start()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        # Pick up any job interrupted while no daemon was running (review F4).
        self.resume_unfinished_jobs()
        return actual_port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._queue.put(None)
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=10.0)
        self.inhibitor.stop()
        self._read_store.close()


def _make_handler(daemon: Daemon) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
            if self.path == "/health":
                self._send_json(200, daemon.health())
            elif self.path == "/jobs":
                self._send_json(200, {"jobs": daemon.job_list()})
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
            resume_match = re.fullmatch(r"/jobs/([^/]+)/resume", self.path)
            if resume_match is not None:
                job_id = resume_match.group(1)
                row = daemon._read_store.conn.execute(
                    "SELECT id, goal FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    self._send_json(404, {"error": f"no such job: {job_id}"})
                    return
                daemon.submit(JobRequest(job_id=row["id"], goal=row["goal"], resume=True))
                self._send_json(202, {"job_id": job_id, "resumed": True})
                return
            if self.path != "/jobs":
                self._send_json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if "base_commit" not in body:
                self._send_json(400, {"error": "missing required field: base_commit"})
                return
            try:
                plan = Plan.model_validate(body["plan"])
            except Exception as exc:
                self._send_json(400, {"error": f"invalid plan: {exc}"})
                return
            job_id = body.get("job_id") or f"job-{uuid.uuid4().hex[:12]}"
            req = JobRequest(
                job_id=job_id,
                goal=body.get("goal") or plan.goal,
                plan=plan,
                base_commit=body["base_commit"],
                slider=int(body.get("slider") or 70),
                provider=body.get("provider"),
                model=body.get("model"),
                keep_awake=body.get("keep_awake"),
            )
            daemon.submit(req)
            self._send_json(202, {"job_id": job_id})

        def log_message(self, fmt: str, *args: Any) -> None:  # silence stderr access log
            log.debug("daemon http: " + fmt, *args)

    return Handler


# --- top-level entry point --------------------------------------------------


def run_daemon(
    repo_root: str | Path,
    config: LazycodeConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    adapters: dict[str, BatchAdapter] | None = None,
    store: Store | None = None,
    inhibitor: Inhibitor | None = None,
    ready: threading.Event | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Start the daemon and block until ``stop_event`` is set (or forever,
    driven by Ctrl-C, when no ``stop_event`` is given -- the real CLI path).

    ``adapters``/``store``/``inhibitor`` are injectable so tests can run a
    real :class:`Daemon` end-to-end against mocks without a live API key or
    OS sleep state; ``ready`` (if given) is set once the HTTP server and
    pidfile are up, so a test driving this in a background thread knows when
    it's safe to connect.
    """
    repo_root = Path(repo_root)
    existing = check_existing(repo_root)
    if existing is not None:
        pid, existing_port = existing
        raise DaemonAlreadyRunningError(
            f"a lazycode daemon is already running for {repo_root} "
            f"(pid={pid}, port={existing_port})"
        )

    owns_store = store is None
    store = store or Store.open(repo=repo_root)
    if adapters is None:
        provider = config.default_provider
        if provider == "mock":
            # Test/demo seam (cli/mock_provider.py) -- see app.py's
            # ``_build_batch_adapter`` for the ``lazycode run`` equivalent.
            from . import mock_provider

            fixture_path = config.mock_fixture_path(repo_root)
            if fixture_path is None:
                raise ValueError(
                    "provider 'mock' requires [providers.mock] fixture = \"...\" in lazycode.toml"
                )
            fixture = mock_provider.load_fixture(fixture_path)
            log_path = Path(repo_root) / ".lazycode" / "mock_submissions.jsonl"
            adapters = {provider: mock_provider.FixtureBatchAdapter(fixture, submissions_log_path=log_path)}
        else:
            config.require_api_key(provider)
            from lazycode.providers.anthropic_batch import AnthropicBatchAdapter

            adapters = {
                provider: AnthropicBatchAdapter.from_env(api_key_env=config.api_key_env_name(provider))
            }
    sched_config = config.to_scheduler_config()

    daemon = Daemon(
        store=store,
        adapters=adapters,
        repo_root=repo_root,
        config=config,
        sched_config=sched_config,
        inhibitor=inhibitor,
        holder_id=f"daemon-{os.getpid()}",
    )
    actual_port = daemon.start(host=host, port=port)
    _write_pidfiles(repo_root, actual_port)
    log.info("lazycode daemon listening on %s:%d (pid=%d)", host, actual_port, os.getpid())

    if ready is not None:
        ready.set()

    try:
        if stop_event is not None:
            stop_event.wait()
        else:
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
        if owns_store:
            store.close()
        _clear_pidfiles(repo_root)
