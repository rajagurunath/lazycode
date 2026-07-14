"""Fixtures for scheduler tests: real tmp git repos + diff/response builders.

No live API calls — the batch/realtime adapters are the in-memory mocks. Diffs
are generated with real ``git diff`` so they apply cleanly via
``git apply --3way`` in the worktree.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from lazycode.ir import ItemResult, ItemStatus, RenderedCall


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
        """Produce a valid unified diff turning ``relpath`` into ``new_content``,
        without leaving the change in the working tree."""
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
    """Build a completed :class:`ItemResult` with an Anthropic-message payload."""
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


def expired(custom_id: str) -> ItemResult:
    return ItemResult(custom_id=custom_id, status=ItemStatus.EXPIRED)


def diff_response(diff_text: str, assumptions: str | None = None) -> str:
    body = diff_text
    if assumptions:
        body = body + "\n\nAssumptions:\n" + assumptions
    return body


def call_custom_ids(items: list[RenderedCall]) -> list[str]:
    return [c.custom_id for c in items]
