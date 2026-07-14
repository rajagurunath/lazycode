"""Tests for harvest/harvester.py: ContextSpec resolution (globs, bindings,
house rules, byte budgets)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lazycode.harvest import HarvestError, harvest
from lazycode.harvest.harvester import OMITTED_FILES_KEY
from lazycode.harvest.repo_map import clear_cache
from lazycode.ir import ContextSpec


def _write(root: Path, relpath: str, content: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_harvest_resolves_plain_glob(tmp_path: Path):
    clear_cache()
    _write(tmp_path, "src/a.py", "A = 1\n")
    _write(tmp_path, "src/b.py", "B = 2\n")
    _write(tmp_path, "src/sub/c.py", "C = 3\n")

    spec = ContextSpec(files=["src/*.py"])
    result = harvest(spec, tmp_path)

    assert set(result.file_blocks) == {"src/a.py", "src/b.py"}
    assert result.file_blocks["src/a.py"] == "A = 1\n"
    assert result.prefix_blocks == []
    assert result.house_rules is None


def test_harvest_resolves_templated_binding(tmp_path: Path):
    clear_cache()
    _write(tmp_path, "src/billing/tax.py", "def tax(): ...\n")

    spec = ContextSpec(files=["{module}"])
    result = harvest(spec, tmp_path, bindings={"module": "src/billing/tax.py"})

    assert result.file_blocks == {"src/billing/tax.py": "def tax(): ...\n"}


def test_harvest_missing_binding_raises(tmp_path: Path):
    clear_cache()
    spec = ContextSpec(files=["{module}"])
    with pytest.raises(HarvestError):
        harvest(spec, tmp_path, bindings={"other": "x"})


def test_harvest_repo_map_included_when_requested(tmp_path: Path):
    clear_cache()
    _write(tmp_path, "pkg/mod.py", "def f():\n    pass\n")

    spec = ContextSpec(repo_map=True)
    result = harvest(spec, tmp_path)

    assert len(result.prefix_blocks) == 1
    assert result.prefix_blocks[0].cache_hint is True
    assert "pkg/mod.py" in result.prefix_blocks[0].text


def test_harvest_house_rules_collected_in_priority_order(tmp_path: Path):
    clear_cache()
    _write(tmp_path, "AGENTS.md", "agents rules\n")
    _write(tmp_path, "CLAUDE.md", "claude rules\n")
    _write(tmp_path, "CONTRIBUTING.md", "contributing rules\n")

    spec = ContextSpec(house_rules=True)
    result = harvest(spec, tmp_path)

    assert result.house_rules is not None
    claude_idx = result.house_rules.index("CLAUDE.md")
    agents_idx = result.house_rules.index("AGENTS.md")
    contrib_idx = result.house_rules.index("CONTRIBUTING.md")
    assert claude_idx < agents_idx < contrib_idx


def test_harvest_house_rules_none_when_no_files_present(tmp_path: Path):
    clear_cache()
    spec = ContextSpec(house_rules=True)
    result = harvest(spec, tmp_path)
    assert result.house_rules is None


def test_harvest_house_rules_not_collected_when_not_requested(tmp_path: Path):
    clear_cache()
    _write(tmp_path, "CLAUDE.md", "claude rules\n")
    spec = ContextSpec(house_rules=False)
    result = harvest(spec, tmp_path)
    assert result.house_rules is None


def test_harvest_per_file_truncation_marker(tmp_path: Path):
    clear_cache()
    _write(tmp_path, "big.txt", "x" * 1000)
    spec = ContextSpec(files=["big.txt"])

    result = harvest(spec, tmp_path, per_file_budget=100)

    content = result.file_blocks["big.txt"]
    assert len(content.encode("utf-8")) <= 100
    assert "truncated" in content


def test_harvest_total_budget_omits_trailing_files(tmp_path: Path):
    clear_cache()
    _write(tmp_path, "a.txt", "a" * 50)
    _write(tmp_path, "b.txt", "b" * 50)
    _write(tmp_path, "c.txt", "c" * 50)
    spec = ContextSpec(files=["*.txt"])

    result = harvest(spec, tmp_path, per_file_budget=1_000, total_file_budget=50)

    assert "a.txt" in result.file_blocks
    assert OMITTED_FILES_KEY in result.file_blocks
    assert "b.txt" in result.file_blocks[OMITTED_FILES_KEY]
    assert "c.txt" in result.file_blocks[OMITTED_FILES_KEY]


def test_harvest_deterministic_ordering(tmp_path: Path):
    clear_cache()
    _write(tmp_path, "z.py", "Z = 1\n")
    _write(tmp_path, "a.py", "A = 1\n")
    spec = ContextSpec(files=["*.py"])

    result = harvest(spec, tmp_path)

    assert list(result.file_blocks.keys()) == ["a.py", "z.py"]
