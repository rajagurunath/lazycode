"""A tiny stdlib client for the M0 daemon's HTTP API (DESIGN.md §2, §12).

No dependency beyond ``urllib`` — the daemon speaks plain JSON over
localhost HTTP (``daemon.py``), so a full HTTP client library would be
overkill for three endpoints. :func:`daemon_alive`/:func:`get_client` are
what ``lazycode run``/``status`` use to decide "is there a daemon running
for this repo" (§2: when one is, the CLI is a client, never a writer).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .daemon import check_existing

DEFAULT_TIMEOUT_S = 3.0


def daemon_address(repo_root: str | Path) -> tuple[str, int] | None:
    """``(host, port)`` of a live daemon for ``repo_root``, or ``None``.

    Liveness here means "pidfile points at a live process" (`check_existing`);
    it does not itself make an HTTP call -- use :func:`daemon_alive` (or just
    try a request and handle the error) when you need to confirm the HTTP
    server is actually answering.
    """
    existing = check_existing(repo_root)
    if existing is None:
        return None
    _, port = existing
    return "127.0.0.1", port


class DaemonClient:
    """Minimal JSON client for one daemon instance."""

    def __init__(self, host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout

    def _get(self, path: str) -> dict[str, Any]:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def list_jobs(self) -> list[dict[str, Any]]:
        return self._get("/jobs").get("jobs", [])

    def submit_job(
        self,
        *,
        goal: str,
        plan: dict[str, Any],
        base_commit: str,
        slider: int = 70,
        provider: str | None = None,
        model: str | None = None,
        keep_awake: bool | None = None,
    ) -> str:
        payload = {
            "goal": goal,
            "plan": plan,
            "base_commit": base_commit,
            "slider": slider,
            "provider": provider,
            "model": model,
            "keep_awake": keep_awake,
        }
        result = self._post("/jobs", payload)
        return result["job_id"]

    def resume_job(self, job_id: str) -> str:
        """Ask the daemon to resume an interrupted job (POST /jobs/{id}/resume).

        Returns the job id on acceptance (HTTP 202); raises
        ``urllib.error.HTTPError`` (404) when the daemon knows no such job.
        """
        result = self._post(f"/jobs/{job_id}/resume", {})
        return result["job_id"]


def daemon_alive(repo_root: str | Path) -> bool:
    """``True`` iff a daemon's pidfile is live *and* it answers ``/health``."""
    addr = daemon_address(repo_root)
    if addr is None:
        return False
    host, port = addr
    try:
        DaemonClient(host, port).health()
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False
    return True


def get_client(repo_root: str | Path) -> DaemonClient | None:
    """A ready-to-use client if a daemon is alive for ``repo_root``, else
    ``None`` -- callers use this to decide the daemon vs. in-process path."""
    addr = daemon_address(repo_root)
    if addr is None:
        return None
    host, port = addr
    client = DaemonClient(host, port)
    try:
        client.health()
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None
    return client
