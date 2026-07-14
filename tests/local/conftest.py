"""Shared fixtures for tests/local: real tmp git repos (no mocking of git)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class GitRepo:
    """A throwaway git repository rooted at ``root``, for exercising
    ``harvest``/``workspace``/``verify`` against real git behavior."""

    root: Path

    def run(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"git {args} failed:\n{result.stdout}\n{result.stderr}"
        return result

    def write(self, relpath: str, content: str) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def commit(self, message: str, *, paths: list[str] | None = None) -> str:
        self.run("add", *(paths or ["-A"]))
        self.run("commit", "-q", "-m", message)
        return self.run("rev-parse", "HEAD").stdout.strip()

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").stdout.strip()


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
