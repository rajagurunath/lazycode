"""Shared e2e fixtures: subprocess-spawned `lazycode` inherits the invoking
shell's environment, so a FORCE_COLOR there makes rich emit ANSI escapes and
breaks plain-string assertions on captured output."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _plain_terminal_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
