"""Benchmark task definitions + fixture-repo materialization (DESIGN.md §14
"Benchmark suite", Appendix B7 "build first").

A task lives at ``bench/tasks/<name>/`` as ``task.yaml`` (name, goal,
generator, verify_command) + a committed ``generate.py`` (``def
build(repo_root: Path) -> None``, pure filesystem writes -- no git calls).
:func:`build_repo` turns that into a fresh, committed git repo in a tmp
directory, so every benchmark run starts from the exact same fixture state
without checking generated files into the repo (the module brief's
"committed generators, not committed junk").

No YAML dependency: ``task.yaml`` is written in the trivial ``key: value``
subset YAML shares with a hand-rolled parser (no lists/nesting beyond a
folded scalar for ``goal``), so this module avoids adding PyYAML as a
project dependency for three small files.
"""

from __future__ import annotations

import importlib.util
import subprocess
from dataclasses import dataclass
from pathlib import Path

TASKS_DIR = Path(__file__).parent / "tasks"


class TaskError(Exception):
    """A task definition is missing or malformed."""


@dataclass(frozen=True)
class TaskSpec:
    name: str
    goal: str
    verify_command: str
    generator_path: Path


def list_tasks() -> list[str]:
    return sorted(p.name for p in TASKS_DIR.iterdir() if (p / "task.yaml").is_file())


def _parse_minimal_yaml(text: str) -> dict[str, str]:
    """Parse the ``key: value`` / ``key: >-`` folded-scalar subset used by
    ``task.yaml`` files. Comments (full-line, starting with ``#``) are
    skipped; a folded block (``>-`` or ``>``) collects indented continuation
    lines into one space-joined string, matching YAML folded-scalar
    semantics for our purposes (no lists, no nesting)."""
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    key: str | None = None
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if raw[:1] in (" ", "\t") and key is not None:
            out[key] = (out[key] + " " + stripped).strip()
            i += 1
            continue
        if ":" not in raw:
            raise TaskError(f"malformed task.yaml line: {raw!r}")
        k, _, v = raw.partition(":")
        key = k.strip()
        v = v.strip()
        if v in (">-", ">", "|", "|-"):
            out[key] = ""
        else:
            out[key] = v.strip('"').strip("'")
        i += 1
    return out


def load_task(name: str) -> TaskSpec:
    task_dir = TASKS_DIR / name
    yaml_path = task_dir / "task.yaml"
    if not yaml_path.is_file():
        raise TaskError(f"no such benchmark task {name!r} (looked in {yaml_path})")
    raw = _parse_minimal_yaml(yaml_path.read_text(encoding="utf-8"))
    for required in ("name", "goal", "generator", "verify_command"):
        if required not in raw or not raw[required]:
            raise TaskError(f"{yaml_path} is missing required key {required!r}")
    generator_path = task_dir / raw["generator"]
    if not generator_path.is_file():
        raise TaskError(f"{yaml_path} names generator {raw['generator']!r}, not found at {generator_path}")
    return TaskSpec(
        name=raw["name"], goal=raw["goal"], verify_command=raw["verify_command"], generator_path=generator_path
    )


def _load_build_fn(generator_path: Path):
    spec = importlib.util.spec_from_file_location(f"lazycode_bench_gen_{generator_path.parent.name}", generator_path)
    if spec is None or spec.loader is None:
        raise TaskError(f"could not load generator at {generator_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    build_fn = getattr(module, "build", None)
    if build_fn is None:
        raise TaskError(f"{generator_path} does not define build(repo_root)")
    return build_fn


def build_repo(task: TaskSpec, dest: Path) -> str:
    """Materialize ``task``'s fixture into a fresh git repo at ``dest``
    (created if needed) and commit it. Returns the base commit sha."""
    dest.mkdir(parents=True, exist_ok=True)
    build_fn = _load_build_fn(task.generator_path)
    build_fn(dest)

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["git", *args], cwd=dest, capture_output=True, text=True)
        if result.returncode != 0:
            raise TaskError(f"git {args} failed:\n{result.stdout}\n{result.stderr}")
        return result

    git("init", "-q")
    git("config", "user.email", "bench@example.com")
    git("config", "user.name", "lazycode-bench")
    git("config", "commit.gpgsign", "false")
    git("add", "-A")
    git("commit", "-q", "-m", "fixture: " + task.name)
    return git("rev-parse", "HEAD").stdout.strip()
