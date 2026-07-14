"""Tests for harvest/repo_map.py: deterministic outline + byte-budget truncation."""

from __future__ import annotations

from pathlib import Path

from lazycode.harvest.repo_map import build_repo_map, clear_cache

_PKG_SOURCE = '''"""Package docstring."""


class Widget:
    """A small widget."""

    def spin(self, speed: int) -> None:
        """Spin the widget."""
        ...

    def _private(self) -> None:
        ...


def make_widget(name: str) -> "Widget":
    """Construct a widget."""
    return Widget()
'''


def _build_sample_repo(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "widget.py").write_text(_PKG_SOURCE, encoding="utf-8")
    (root / "README.md").write_text("# sample\n", encoding="utf-8")
    (root / "notes.txt").write_text("todo\n", encoding="utf-8")


def test_repo_map_golden(tmp_path: Path):
    clear_cache()
    _build_sample_repo(tmp_path)

    out = build_repo_map(tmp_path, byte_budget=8_000)

    expected = "\n\n".join(
        [
            "README.md",
            "notes.txt",
            "pkg/__init__.py",
            '### pkg/widget.py\nclass Widget:\n    # A small widget.\n    def spin(self, speed: int) -> None:  # Spin the widget.\n    def _private(self) -> None:\ndef make_widget(name: str) -> \'Widget\':  # Construct a widget.',
        ]
    )
    assert out == expected


def test_repo_map_deterministic_across_calls(tmp_path: Path):
    clear_cache()
    _build_sample_repo(tmp_path)
    first = build_repo_map(tmp_path, byte_budget=8_000)
    second = build_repo_map(tmp_path, byte_budget=8_000)
    assert first == second


def test_repo_map_excludes_git_and_venv_dirs(tmp_path: Path):
    clear_cache()
    _build_sample_repo(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("junk", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "site.py").write_text("junk", encoding="utf-8")

    out = build_repo_map(tmp_path, byte_budget=8_000)

    assert ".git" not in out
    assert ".venv" not in out


def test_repo_map_syntax_error_falls_back_to_path_only(tmp_path: Path):
    clear_cache()
    (tmp_path / "broken.py").write_text("def f(:\n", encoding="utf-8")

    out = build_repo_map(tmp_path, byte_budget=8_000)

    assert out == "broken.py"


def test_repo_map_byte_budget_demotes_largest_files_first(tmp_path: Path):
    clear_cache()
    # One large module (many functions) and one tiny module.
    big_lines = "\n".join(f"def f{i}():\n    pass\n" for i in range(50))
    (tmp_path / "big.py").write_text(big_lines, encoding="utf-8")
    (tmp_path / "small.py").write_text("def only():\n    pass\n", encoding="utf-8")

    full = build_repo_map(tmp_path, byte_budget=1_000_000)
    assert "def f0" in full  # full outline present when budget is generous

    # A budget too small for both full outlines but big enough for both paths
    # plus the small file's full outline: big.py must be demoted to path-only
    # while small.py keeps its outline.
    tight = build_repo_map(tmp_path, byte_budget=120)

    assert "[repo map truncated to fit byte budget]" in tight
    assert "big.py" in tight
    assert "def f0" not in tight  # big.py demoted to path-only
    assert "def only" in tight  # small.py's outline survives


def test_repo_map_extreme_budget_drops_files_with_marker(tmp_path: Path):
    clear_cache()
    (tmp_path / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def b():\n    pass\n", encoding="utf-8")

    out = build_repo_map(tmp_path, byte_budget=3)

    assert "more file(s) omitted" in out


def test_repo_map_cache_keyed_by_path_and_mtime(tmp_path: Path):
    clear_cache()
    path = tmp_path / "mod.py"
    path.write_text("def a():\n    pass\n", encoding="utf-8")

    first = build_repo_map(tmp_path, byte_budget=8_000)
    assert "def a" in first

    # Mutate without changing mtime resolution granularity concerns: force a
    # distinct mtime, then rebuild — the cache must pick up the new content.
    import os
    import time

    time.sleep(0.01)
    path.write_text("def b():\n    pass\n", encoding="utf-8")
    os.utime(path, (path.stat().st_mtime + 1, path.stat().st_mtime + 1))

    second = build_repo_map(tmp_path, byte_budget=8_000)
    assert "def b" in second
    assert "def a" not in second
