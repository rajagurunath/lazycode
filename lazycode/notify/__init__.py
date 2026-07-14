"""lazycode notify — M0: a log line (DESIGN.md §7.2, §13).

M0 ships exactly one notifier: ``notify(job_id, message, repo_root=...)``
prints a ``rich`` console line and appends to ``.lazycode/notify.log``.
Desktop/Slack notifications are M3.

Public surface:
    notify, notify_log_path  (log)
"""

from __future__ import annotations

from .log import notify, notify_log_path

__all__ = ["notify", "notify_log_path"]
