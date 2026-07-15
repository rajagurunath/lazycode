"""Fixture generator for the ``coverage-a-module`` benchmark task (see
``add-type-hints/generate.py`` for the pattern this follows)."""

from __future__ import annotations

from pathlib import Path

_NORMALIZE = '''\
import re


def slugify(text):
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def truncate(text, length, suffix="..."):
    if len(text) <= length:
        return text
    return text[: max(0, length - len(suffix))] + suffix
'''

_EXISTING_TEST = '''\
from pkg.strings.normalize import slugify


def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"
'''


def build(repo_root: Path) -> None:
    pkg = repo_root / "pkg" / "strings"
    pkg.mkdir(parents=True, exist_ok=True)
    (repo_root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "normalize.py").write_text(_NORMALIZE, encoding="utf-8")

    tests_dir = repo_root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_strings_existing.py").write_text(_EXISTING_TEST, encoding="utf-8")
