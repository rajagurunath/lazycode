"""Local verifier execution (DESIGN.md §3.1 Verify, §9, Appendix B4).

M0 runs the job-level ``verify.command`` and reports pass/fail only (shape-only
contract enforcement — Appendix B11). ``run_verify`` is also the execution
primitive :class:`~lazycode.ir.CommandContract` reuses (M1+ wires contract
enforcement on top of it); :func:`run_command_contract` is that thin wrapper,
included now so the runner's contract is exercised end-to-end even though full
contract dispatch is out of M0 scope.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

from lazycode.ir import CommandContract

_TAIL_LINES = 100


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of one local verification run."""

    passed: bool
    exit_code: int | None
    tail: str
    duration_s: float


def _tail(text: str, n: int = _TAIL_LINES) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _resolve_cwd(worktree: object) -> Path:
    # Accept either a workspace.Worktree-like object (has .path) or a bare path,
    # without importing workspace (verify has no other-module coupling).
    path = getattr(worktree, "path", worktree)
    return Path(path)


def run_verify(worktree: object, cmd: str, timeout_s: float) -> VerifyResult:
    """Run ``cmd`` (shell-split, ``shell=False``) with ``cwd=worktree``.

    Captures combined stdout+stderr, reports only the last ~100 lines (``tail``)
    to keep node/report payloads bounded, and never raises on a failing or
    timed-out command — failure is data (``passed=False``), not an exception.
    """
    args = shlex.split(cmd)
    cwd = _resolve_cwd(worktree)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        combined = (exc.stdout or "") + (exc.stderr or "")
        tail = _tail(combined) + f"\n[verify: timed out after {timeout_s}s]"
        return VerifyResult(passed=False, exit_code=None, tail=tail, duration_s=duration)
    except FileNotFoundError as exc:
        duration = time.monotonic() - start
        return VerifyResult(
            passed=False, exit_code=None, tail=f"[verify: command not found: {exc}]", duration_s=duration
        )

    duration = time.monotonic() - start
    combined = proc.stdout + proc.stderr
    return VerifyResult(
        passed=proc.returncode == 0,
        exit_code=proc.returncode,
        tail=_tail(combined),
        duration_s=duration,
    )


def run_command_contract(worktree: object, contract: CommandContract) -> VerifyResult:
    """Run a :class:`~lazycode.ir.CommandContract` via :func:`run_verify`,
    grading pass/fail against ``contract.expect_exit`` rather than a bare 0."""
    result = run_verify(worktree, contract.cmd, contract.timeout_s)
    return replace(result, passed=result.exit_code == contract.expect_exit)
