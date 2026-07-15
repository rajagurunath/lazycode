"""Fixtures for CLI tests: real tmp git repos, response builders, and a
scripted realtime adapter that always emits a fixed plan via the emit_plan
tool (mirrors ``tests/scheduler/conftest.py`` and
``tests/planner/test_planner.py::ScriptedRealtime``, adapted for CLI-level
end-to-end runs where the plan needs to be schema-valid and reference real
files in the tmp repo)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lazycode.ir import ItemResult, ItemStatus, RenderedCall


@pytest.fixture(autouse=True)
def _plain_terminal_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI tests assert on plain strings; a FORCE_COLOR inherited from the
    invoking shell makes rich emit ANSI escapes and breaks them. The env vars
    cover subprocesses; the module-level console was already constructed at
    import time with the shell's env baked in, so replace it too."""
    from rich.console import Console

    from lazycode.cli import app as app_module

    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(app_module, "console", Console(no_color=True, force_terminal=False))


@dataclass
class GitRepo:
    root: Path

    def run(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.root, capture_output=True, text=True
        )
        assert result.returncode == 0, f"git {args} failed:\n{result.stdout}\n{result.stderr}"
        return result

    def write(self, relpath: str, content: str) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def commit(self, message: str) -> str:
        self.run("add", "-A")
        self.run("commit", "-q", "-m", message)
        return self.run("rev-parse", "HEAD").stdout.strip()

    def make_patch(self, relpath: str, new_content: str) -> str:
        path = self.root / relpath
        original = path.read_text(encoding="utf-8")
        path.write_text(new_content, encoding="utf-8")
        diff = self.run("diff", "--", relpath).stdout
        path.write_text(original, encoding="utf-8")
        return diff


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    root = tmp_path / "repo"
    root.mkdir()
    repo = GitRepo(root=root)
    repo.run("init", "-q")
    repo.run("config", "user.email", "test@example.com")
    repo.run("config", "user.name", "Test User")
    repo.run("config", "commit.gpgsign", "false")
    return repo


def completed(custom_id: str, text: str, *, tokens_in: int = 120, tokens_out: int = 60) -> ItemResult:
    return ItemResult(
        custom_id=custom_id,
        status=ItemStatus.COMPLETED,
        payload={
            "id": f"msg_{custom_id}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        },
    )


def diff_response(diff_text: str, assumptions: str | None = None) -> str:
    body = diff_text
    if assumptions:
        body = body + "\n\nAssumptions:\n" + assumptions
    return body


@dataclass
class FixedPlanRealtime:
    """A :class:`~lazycode.providers.base.RealtimeAdapter` that always
    answers the ``emit_plan`` forced-tool call with ``plan_dict`` -- enough
    to drive ``propose_plan`` deterministically in CLI end-to-end tests
    without a live Anthropic call."""

    plan_dict: dict[str, Any]
    calls: list[RenderedCall] = field(default_factory=list)

    def complete(self, call: RenderedCall, **kwargs: Any) -> ItemResult:
        self.calls.append(call)
        content = [{"type": "tool_use", "name": "emit_plan", "id": "tu1", "input": self.plan_dict}]
        return ItemResult(
            custom_id=call.custom_id,
            status=ItemStatus.COMPLETED,
            payload={"content": content, "usage": {"input_tokens": 10, "output_tokens": 5}},
        )


class FromEnvFactory:
    """Stands in for ``AnthropicRealtimeAdapter``/``AnthropicBatchAdapter``
    at the call site (``Cls.from_env(api_key_env=...)``): monkeypatch the
    class reference in ``lazycode.cli.app`` with an instance of this,
    constructed with the mock adapter to hand back."""

    def __init__(self, instance: Any) -> None:
        self._instance = instance
        self.from_env_calls: list[dict[str, Any]] = []

    def from_env(self, *, api_key_env: str = "ANTHROPIC_API_KEY") -> Any:
        self.from_env_calls.append({"api_key_env": api_key_env})
        return self._instance


@pytest.fixture
def global_config_no_ask(tmp_path: Path) -> Path:
    """A user-global config with ``keep_awake = false`` so CLI tests that
    don't care about the keep-awake prompt never block on a confirm()."""
    path = tmp_path / "global-config.toml"
    path.write_text('[daemon]\nkeep_awake = false\n', encoding="utf-8")
    return path
