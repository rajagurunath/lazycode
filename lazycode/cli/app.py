"""The ``lazycode`` typer CLI (DESIGN.md §1, §2, §12, Appendix B9).

M0 ships ``run, status, explain, review, daemon`` (B9) plus a hidden
``doctor --rebuild`` for projection repair (§11). Process model (§2): when a
daemon is running for this repo, ``run`` hands the approved plan to it over
HTTP (``client.py``) rather than touching the store directly -- the daemon is
the sole event-log writer while it runs. When no daemon is running, ``run``
hosts an in-process :class:`~lazycode.scheduler.Orchestrator` itself, guarded
by the same job lease. ``status``/``explain``/``review`` are always read-only
and always safe, whichever mode is active (§7.1).
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from lazycode.ir import Plan
from lazycode.planner import PlanningError, propose_plan
from lazycode.providers.anthropic_batch import AnthropicBatchAdapter
from lazycode.providers.base import BatchAdapter, RealtimeAdapter
from lazycode.providers.realtime import AnthropicRealtimeAdapter
from lazycode.scheduler import LeaseAcquisitionError, LeaseLostError, Orchestrator
from lazycode.store import Store, projections

from . import mock_provider
from .client import get_client
from .config import ConfigError, LazycodeConfig, load_config
from .daemon import DaemonAlreadyRunningError, Inhibitor, run_daemon
from .render_plan import (
    NodeSummary,
    PhysicalNodeSummary,
    node_summaries_from_plan,
    render_logical_tree,
    render_physical_tree,
)

app = typer.Typer(
    name="lazycode",
    help="A batch-API-native coding agent: plan realtime, execute overnight.",
    no_args_is_help=True,
)
console = Console()


# --- shared helpers ---------------------------------------------------------


def _repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    if result.returncode != 0:
        console.print("[red]Not inside a git repository.[/red]")
        raise typer.Exit(code=1)
    return Path(result.stdout.strip())


def _base_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True
    )
    if result.returncode != 0:
        console.print(
            "[red]This repository has no commits yet — lazycode needs a "
            "base commit to branch task groups from.[/red]"
        )
        raise typer.Exit(code=1)
    return result.stdout.strip()


def _require_job(store: Store, job_id: str) -> dict:
    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        console.print(f"[red]No such job: {job_id}[/red]")
        raise typer.Exit(code=1)
    return dict(row)


# --- provider adapter construction (real Anthropic, or the mock seam) -------
#
# ``config.default_provider == "mock"`` is the test/demo seam
# (``mock_provider.py``): the kill -9 acceptance test and the benchmark
# harness's mock runs drive the real CLI in a real subprocess, where
# in-process adapter injection (monkeypatching ``AnthropicRealtimeAdapter``/
# ``AnthropicBatchAdapter`` on this module, as ``tests/cli/conftest.py`` does)
# is impossible. Everything else about ``run``/``resume`` is unaware this
# branch exists.


def _build_realtime_adapter(config: LazycodeConfig, repo_root: Path) -> RealtimeAdapter:
    if config.default_provider == "mock":
        fixture_path = config.mock_fixture_path(repo_root)
        if fixture_path is None:
            console.print(
                "[red]provider 'mock' requires [providers.mock] fixture = \"...\" "
                "in lazycode.toml[/red]"
            )
            raise typer.Exit(code=1)
        fixture = mock_provider.load_fixture(fixture_path)
        return mock_provider.build_mock_realtime_adapter(fixture)
    return AnthropicRealtimeAdapter.from_env(api_key_env=config.api_key_env_name())


def _build_batch_adapter(config: LazycodeConfig, repo_root: Path) -> BatchAdapter:
    if config.default_provider == "mock":
        fixture_path = config.mock_fixture_path(repo_root)
        if fixture_path is None:
            console.print(
                "[red]provider 'mock' requires [providers.mock] fixture = \"...\" "
                "in lazycode.toml[/red]"
            )
            raise typer.Exit(code=1)
        fixture = mock_provider.load_fixture(fixture_path)
        log_path = repo_root / ".lazycode" / "mock_submissions.jsonl"
        return mock_provider.FixtureBatchAdapter(fixture, submissions_log_path=log_path)
    return AnthropicBatchAdapter.from_env(api_key_env=config.api_key_env_name())


# --- run ---------------------------------------------------------------


@app.command()
def run(
    goal: str = typer.Argument(..., help="What lazycode should accomplish."),
    verify: str | None = typer.Option(
        None, "--verify", help="Override the [verify].command from lazycode.toml."
    ),
    model: str | None = typer.Option(None, "--model", help="Override the default model."),
    max_waves: int | None = typer.Option(
        None, "--max-waves", help="Cap the number of waves this job may run."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the plan-approval y/N prompt."),
) -> None:
    """Plan GOAL with the realtime planner, show the plan tree, and (after
    approval) execute it -- via the daemon if one is running, else in-process."""
    repo_root = _repo_root()
    config = load_config(repo_root, cli_verify_command=verify)

    if config.default_provider != "mock":
        try:
            config.require_api_key()
        except ConfigError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

    plan_model = config.resolve_model(model)
    realtime = _build_realtime_adapter(config, repo_root)
    try:
        plan = propose_plan(goal, str(repo_root), realtime, plan_model)
    except PlanningError as exc:
        console.print(f"[red]Planning failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(render_logical_tree(plan.goal, node_summaries_from_plan(plan)))
    if plan.assumptions:
        console.print("[dim]Assumptions:[/dim] " + "; ".join(plan.assumptions))

    if not yes and not typer.confirm("Proceed?", default=False):
        console.print("Aborted — no job created.")
        raise typer.Exit(code=0)

    base_commit = _base_commit(repo_root)

    keep_awake_answer: bool = False
    if config.keep_awake == "ask":
        keep_awake_answer = typer.confirm(
            "Keep this machine awake until the job finishes? [y/N]", default=False
        )
    elif config.keep_awake is True:
        keep_awake_answer = True

    client = get_client(repo_root)
    if client is not None:
        job_id = client.submit_job(
            goal=goal,
            plan=plan.model_dump(mode="json"),
            base_commit=base_commit,
            slider=config.slider,
            provider=config.default_provider,
            model=plan_model,
            keep_awake=keep_awake_answer,
        )
        console.print(
            f"Submitted to daemon as job [bold]{job_id}[/bold]. "
            f"Use `lazycode status {job_id}` to follow it."
        )
        return

    _run_in_process(
        repo_root=repo_root,
        config=config,
        goal=goal,
        plan_model=plan_model,
        plan=plan,
        base_commit=base_commit,
        max_waves=max_waves,
        keep_awake=keep_awake_answer,
    )


def _run_in_process(
    *,
    repo_root: Path,
    config: LazycodeConfig,
    goal: str,
    plan_model: str,
    plan: Plan,
    base_commit: str,
    max_waves: int | None,
    keep_awake: bool,
) -> None:
    from lazycode.notify import notify

    store = Store.open(repo=repo_root)
    sched_config = config.to_scheduler_config(model=plan_model, max_waves=max_waves)
    adapters = {config.default_provider: _build_batch_adapter(config, repo_root)}
    inhibitor = Inhibitor() if keep_awake else None
    job_id: str | None = None
    try:
        if inhibitor is not None:
            inhibitor.start()
        orch = Orchestrator(store, adapters, repo_root, sched_config)
        job_id = orch.create_job(goal, plan, base_commit, slider=config.slider)
        result = _run_foreground(store, orch, job_id, console)
        branches = [
            r["branch"]
            for r in store.conn.execute(
                "SELECT branch FROM task_groups WHERE job_id = ?", (job_id,)
            ).fetchall()
        ]
    except (LeaseAcquisitionError, LeaseLostError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        if inhibitor is not None:
            inhibitor.stop()
        store.close()

    notify(job_id, f"status={result.status}, waves={result.waves}", repo_root=repo_root, console=console)
    console.print(f"[bold green]Done.[/bold green] status={result.status}")
    for b in branches:
        console.print(f"  branch: {b}")
    if result.report_dir is not None:
        console.print(f"  report: {result.report_dir / 'report.md'}")
    if result.needs_human:
        console.print(f"[yellow]Needs human attention:[/yellow] {', '.join(result.needs_human)}")


def _run_foreground(store: Store, orch: Orchestrator, job_id: str, console: Console):
    """Run ``orch.run_job(job_id)`` on a worker thread while the main thread
    shows a spinner, polling the ``waves`` table (via a second, read-only
    connection) to surface wave transitions as they land."""
    result_holder: dict[str, object] = {}
    error_holder: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            result_holder["result"] = orch.run_job(job_id)
        except BaseException as exc:  # re-raised on the main thread below
            error_holder["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    poll_store = Store.open(db_path=store.db_path)
    try:
        with console.status(f"[bold cyan]Running job {job_id}...[/bold cyan]") as status:
            thread.start()
            while thread.is_alive():
                thread.join(timeout=0.5)
                waves = poll_store.conn.execute(
                    "SELECT status, COUNT(*) c FROM waves WHERE job_id = ? GROUP BY status",
                    (job_id,),
                ).fetchall()
                summary = ", ".join(f"{r['status']}={r['c']}" for r in waves) or "forming first wave"
                status.update(f"[bold cyan]Running job {job_id}[/bold cyan] — {summary}")
    finally:
        poll_store.close()
    thread.join()
    if "error" in error_holder:
        raise error_holder["error"]
    return result_holder["result"]


# --- resume --------------------------------------------------------------


@app.command()
def resume(job_id: str = typer.Argument(..., help="Job to resume after a crash/restart.")) -> None:
    """Resume JOB_ID's wave loop after an interruption (kill -9, Ctrl-C, host
    reboot) and drive it to completion (§7.1, §7.5).

    ``run`` always creates a *new* job -- this is the thin wrapper for
    continuing an existing one. All the actual crash-safety lives in
    ``Orchestrator.run_job`` already (it calls ``scheduler.resume_job`` on
    every invocation to rebuild ``known_refs`` and re-poll any in-flight wave
    rather than resubmitting it -- see ``scheduler/resume.py``); this command
    just re-opens the store, reconstructs the same adapters/config `run` would
    have used, and calls ``run_job`` again. When a daemon owns this repo's
    event log (§7.1 single-writer), the resume is routed to it over HTTP
    (POST /jobs/{id}/resume) instead of writing from this process."""
    import urllib.error

    repo_root = _repo_root()
    config = load_config(repo_root)

    client = get_client(repo_root)
    if client is not None:
        try:
            resumed = client.resume_job(job_id)
        except urllib.error.HTTPError as exc:
            detail = "no such job" if exc.code == 404 else str(exc)
            console.print(f"[red]Daemon rejected resume of {job_id}: {detail}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(
            f"Handed [bold]{resumed}[/bold] to the daemon for resume. "
            f"Use `lazycode status {resumed}` to follow it."
        )
        return

    if config.default_provider != "mock":
        try:
            config.require_api_key()
        except ConfigError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

    store = Store.open(repo=repo_root)
    job_id_resolved = _require_job(store, job_id)["id"]

    from lazycode.notify import notify

    sched_config = config.to_scheduler_config()
    adapters = {config.default_provider: _build_batch_adapter(config, repo_root)}
    try:
        orch = Orchestrator(store, adapters, repo_root, sched_config)
        result = _run_foreground(store, orch, job_id_resolved, console)
        branches = [
            r["branch"]
            for r in store.conn.execute(
                "SELECT branch FROM task_groups WHERE job_id = ?", (job_id_resolved,)
            ).fetchall()
        ]
    except (LeaseAcquisitionError, LeaseLostError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        store.close()

    notify(
        job_id_resolved,
        f"resumed: status={result.status}, waves={result.waves}",
        repo_root=repo_root,
        console=console,
    )
    console.print(f"[bold green]Done.[/bold green] status={result.status}")
    for b in branches:
        console.print(f"  branch: {b}")
    if result.report_dir is not None:
        console.print(f"  report: {result.report_dir / 'report.md'}")
    if result.needs_human:
        console.print(f"[yellow]Needs human attention:[/yellow] {', '.join(result.needs_human)}")


# --- status ------------------------------------------------------------


@app.command()
def status(job_id: str | None = typer.Argument(None, help="Show per-node detail for this job.")) -> None:
    """List jobs (or show one job's per-node detail). Read-only, always
    safe -- also shows daemon liveness + keep-awake state."""
    repo_root = _repo_root()

    client = get_client(repo_root)
    if client is not None:
        health = client.health()
        console.print(
            f"[green]daemon: alive[/green] pid={health.get('pid')} "
            f"active_jobs={health.get('active_jobs')} "
            f"keep_awake_active={health.get('keep_awake_active')} "
            f"policy={health.get('keep_awake_policy')}"
        )
    else:
        console.print("[dim]daemon: not running (CLI runs jobs in-process)[/dim]")

    store = Store.open(repo=repo_root)
    try:
        if job_id is None:
            _print_job_table(store)
        else:
            _print_node_detail(store, job_id)
    finally:
        store.close()


def _print_job_table(store: Store) -> None:
    table = Table(title="Jobs")
    for col in ("id", "goal", "status", "waves", "nodes (by state)"):
        table.add_column(col)
    rows = store.conn.execute("SELECT id, goal, status FROM jobs ORDER BY created_at DESC").fetchall()
    for r in rows:
        waves = store.conn.execute(
            "SELECT COUNT(*) c FROM waves WHERE job_id = ? AND status IN ('SUBMITTED', 'COMPLETED')",
            (r["id"],),
        ).fetchone()["c"]
        state_counts = store.conn.execute(
            "SELECT status, COUNT(*) c FROM nodes WHERE job_id = ? GROUP BY status", (r["id"],)
        ).fetchall()
        state_txt = ", ".join(f"{s['status']}={s['c']}" for s in state_counts) or "—"
        goal_txt = r["goal"] if len(r["goal"]) <= 40 else r["goal"][:39] + "…"
        table.add_row(r["id"], goal_txt, r["status"], str(waves), state_txt)
    console.print(table)


def _print_node_detail(store: Store, job_id: str) -> None:
    job = _require_job(store, job_id)
    console.print(f"[bold]{job_id}[/bold]: {job['goal']}  (status={job['status']})")
    table = Table(title=f"Nodes for {job_id}")
    for col in ("id", "op", "status", "wave_id", "exec_class", "provider", "model"):
        table.add_column(col)
    rows = store.conn.execute("SELECT * FROM nodes WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
    for r in rows:
        table.add_row(
            r["id"],
            r["op"],
            r["status"],
            r["wave_id"] or "—",
            r["exec_class"] or "—",
            r["provider"] or "—",
            r["model"] or "—",
        )
    console.print(table)


# --- explain -------------------------------------------------------------


@app.command()
def explain(job_id: str = typer.Argument(..., help="Job to explain.")) -> None:
    """Render the stored logical + physical plan trees for JOB_ID
    (Postgres-``EXPLAIN``-style; §4). M0 shows structure only, no cost
    estimates (Appendix B11)."""
    repo_root = _repo_root()
    store = Store.open(repo=repo_root)
    try:
        job = _require_job(store, job_id)
        rows = store.conn.execute("SELECT * FROM nodes WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()

        logical = [
            NodeSummary(id=r["id"], op=r["op"], deps=tuple(json.loads(r["deps"] or "[]")))
            for r in rows
        ]
        console.print(render_logical_tree(job["goal"], logical))

        physical = [
            PhysicalNodeSummary(
                node_id=r["id"],
                op=r["op"],
                wave_id=r["wave_id"] or "unassigned",
                exec_class=r["exec_class"] or "unknown",
                provider=r["provider"],
                model=r["model"],
            )
            for r in rows
        ]
        console.print(render_physical_tree(physical))
    finally:
        store.close()


# --- review ----------------------------------------------------------------


@app.command()
def review(job_id: str = typer.Argument(..., help="Job to review.")) -> None:
    """Print report.md/report.json paths, branch names, verification
    summary, and the assumption ledger for JOB_ID."""
    repo_root = _repo_root()
    report_dir = repo_root / ".lazycode" / "reports" / job_id
    report_json = report_dir / "report.json"
    if not report_json.exists():
        console.print(
            f"[red]No report found for {job_id} at {report_dir}.[/red] "
            "Has the job finished? Try `lazycode status "
            f"{job_id}`."
        )
        raise typer.Exit(code=1)

    data = json.loads(report_json.read_text(encoding="utf-8"))
    console.print(f"[bold]report.md[/bold]:   {report_dir / 'report.md'}")
    console.print(f"[bold]report.json[/bold]: {report_json}")

    groups = data.get("groups", {})
    if groups:
        table = Table(title="Task groups / branches")
        for col in ("group", "branch", "nodes", "files"):
            table.add_column(col)
        for gid, g in groups.items():
            table.add_row(gid, g.get("branch", "—"), str(len(g.get("nodes", []))), str(len(g.get("files", []))))
        console.print(table)

    verifications = data.get("verifications", [])
    if verifications:
        vtable = Table(title="Verification")
        for col in ("node", "result", "exit_code"):
            vtable.add_column(col)
        for v in verifications:
            vtable.add_row(str(v.get("node")), "PASS" if v.get("passed") else "FAIL", str(v.get("exit_code")))
        console.print(vtable)
    else:
        console.print("[dim]No verify nodes ran.[/dim]")

    assumptions = data.get("assumptions", [])
    if assumptions:
        atable = Table(title="Assumption ledger")
        for col in ("node", "assumption", "risk"):
            atable.add_column(col)
        for a in assumptions:
            atable.add_row(a.get("node", ""), a.get("assumption", ""), a.get("risk", ""))
        console.print(atable)
    else:
        console.print("[dim]No assumptions recorded.[/dim]")

    needs_human = data.get("needs_human", [])
    if needs_human:
        console.print(
            "[yellow]Needs human:[/yellow] " + ", ".join(f"{n['node']} ({n.get('op', '?')})" for n in needs_human)
        )


# --- daemon ------------------------------------------------------------


@app.command()
def daemon(
    foreground: bool = typer.Option(
        True,
        "--foreground/--background",
        help=(
            "M0 only supports foreground operation; wrap this command with a "
            "process supervisor (launchd unit / systemd service) for real "
            "backgrounding -- --background is not implemented yet."
        ),
    ),
) -> None:
    """Run the lazycode daemon: the sole event-log writer while it's up,
    plus a local job-submission API for `lazycode run` (§2, §7.5)."""
    if not foreground:
        console.print(
            "[red]--background is not implemented in M0.[/red] Run in the "
            "foreground (the default) under launchd/systemd/tmux for "
            "equivalent behavior."
        )
        raise typer.Exit(code=1)

    repo_root = _repo_root()
    config = load_config(repo_root)
    try:
        config.require_api_key()
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[bold]lazycode daemon[/bold] starting for {repo_root} (Ctrl-C to stop)")
    try:
        run_daemon(repo_root, config)
    except DaemonAlreadyRunningError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


# --- doctor (hidden) ---------------------------------------------------


@app.command(hidden=True)
def doctor(
    rebuild: str = typer.Option(..., "--rebuild", help="Job id to rebuild projections for."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Rebuild even while a daemon is alive (unsafe: concurrent event-log writer).",
    ),
) -> None:
    """Replay JOB_ID's event log to rebuild jobs/nodes/waves projections.

    Rebuilding *writes* the projection tables, so it refuses while a daemon
    (the sole event-log writer, §7.1) is alive unless --force is given, and it
    runs under the job lease so it can never race a live orchestrator."""
    import os

    from lazycode.store import lease

    repo_root = _repo_root()
    if not force and get_client(repo_root) is not None:
        console.print(
            "[red]A daemon is running for this repo and owns its event log "
            "(§7.1 single-writer) — `doctor --rebuild` writes the projection "
            "tables. Stop the daemon first, or pass --force to override.[/red]"
        )
        raise typer.Exit(code=1)

    store = Store.open(repo=repo_root)
    holder = f"doctor-{os.getpid()}"
    try:
        _require_job(store, rebuild)
        if not lease.acquire(store, rebuild, holder, 60.0):
            current = lease.current(store, rebuild)
            console.print(
                f"[red]Job {rebuild}'s lease is held by "
                f"{current[0] if current else '?'} — refusing to rebuild "
                "projections under a live orchestrator.[/red]"
            )
            raise typer.Exit(code=1)
        try:
            projections.rebuild(store, rebuild)
        finally:
            lease.release(store, rebuild, holder)
        console.print(f"Rebuilt projections for {rebuild}.")
    finally:
        store.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
