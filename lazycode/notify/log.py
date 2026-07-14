"""M0 notify: a log line (DESIGN.md §7.2 ``notify(user)``, §12, §13, Appendix B11).

``notify`` is the whole M0 notifier: print a ``rich``-styled line to the
console and append a plain-text record to ``<repo>/.lazycode/notify.log``.
Desktop/Slack notifications are M3 (§13); this module's job is to give M3 a
stable call site (``notify(job_id, message)``) to grow into, not to be
clever now.

The scheduler itself never imports this — DESIGN.md's process model treats
"notify" as something the *caller* of ``run_job`` does once it returns
(§7.2's pseudocode has ``notify(user)`` right after the wave loop, at the
same level as the CLI/daemon driving loop, not inside ``Orchestrator``). CLI
paths (`lazycode run`, the daemon's job worker) call this after
``orchestrator.run_job(...)`` returns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

_console = Console()


def notify_log_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".lazycode" / "notify.log"


def notify(job_id: str, message: str, *, repo_root: str | Path, console: Console | None = None) -> None:
    """Emit one notification: a console line now, and an appended record in
    ``.lazycode/notify.log`` for later (``lazycode status`` or a future
    ``lazycode notify --tail``) to read back.

    Never raises on a log-write failure (e.g. read-only filesystem) — a
    notification that can't be persisted should not crash the caller; it
    still reaches the console.
    """
    out = console or _console
    ts = datetime.now(UTC)
    out.print(f"[bold magenta]\\[notify][/bold magenta] {job_id}: {message}")

    try:
        path = notify_log_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{ts.isoformat()}\t{job_id}\t{message}\n")
    except OSError:
        pass
