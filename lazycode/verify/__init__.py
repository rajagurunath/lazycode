"""Local verifier runners (DESIGN.md §3.1 Verify). M0: pass/fail only, via
subprocess against ``verify.command``. Contract-typed enforcement is M1+."""

from __future__ import annotations

from .runner import VerifyResult, run_command_contract, run_verify

__all__ = ["run_verify", "run_command_contract", "VerifyResult"]
