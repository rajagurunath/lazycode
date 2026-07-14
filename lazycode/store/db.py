"""SQLite connection management, schema, and the :class:`Store` facade
(DESIGN.md §7.1 durability/single-writer, §11 schema).

This module owns the *physical* layer: one WAL-mode SQLite connection per
:class:`Store`, idempotent schema creation, and a reentrant ``transaction``
context manager every other ``store/`` module composes into.

Reentrancy matters here: several higher-level operations (e.g. ``lease.acquire``)
need to write a bookkeeping row *and* append an event as one atomic unit, but
``eventlog.append`` also wants to open its own transaction so it works
standalone. :func:`transaction` handles this with SQLite ``SAVEPOINT``s — the
outermost call does ``BEGIN IMMEDIATE``/``COMMIT``, nested calls do
``SAVEPOINT``/``RELEASE``, and either level rolls back on exception.

Schema fidelity: table shapes follow DESIGN.md §11 verbatim. Additions beyond
the doc's column list (composite primary keys, a couple of indexes, FK
declarations) are noted inline as resolved ambiguities — the doc's schema
sketch doesn't spell out keys/constraints, so *something* reasonable has to be
chosen for a working SQLite DDL.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# --- schema -------------------------------------------------------------
#
# Resolved ambiguities (DESIGN.md §11 gives column lists, not full DDL):
#   * ``nodes`` and ``waves`` ids are only unique *within* a job (e.g. fan-out
#     children are minted as ``{parent_id}.{index}``), so both use a composite
#     primary key ``(job_id, id)`` rather than a bare ``id`` PK.
#   * ``stats`` gets ``PRIMARY KEY (op, model, repo)`` so priors can be
#     upserted; §11 lists the columns but not a key.
#   * FK declarations are limited to single-column PK targets that don't
#     depend on job scoping (``nodes.group_id -> task_groups.id``,
#     ``call_items.call_id -> llm_calls.id``). ``call_items.node_id`` is not
#     declared as an FK to ``nodes`` because ``nodes``'s real key is
#     ``(job_id, id)`` and ``call_items`` (per §11) carries no ``job_id``
#     column to complete that composite reference.
#   * ``PRAGMA foreign_keys = ON`` is set regardless (constraint requirement),
#     even where individual FK declarations were skipped for the reason above.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT NOT NULL,
    ts      TEXT NOT NULL,
    type    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job_seq ON events(job_id, seq);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    goal         TEXT NOT NULL,
    repo         TEXT NOT NULL,
    base_commit  TEXT,
    slider       INTEGER NOT NULL DEFAULT 70,
    budget_usd   REAL,
    deadline_utc TEXT,
    status       TEXT NOT NULL DEFAULT 'PENDING',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    job_id     TEXT PRIMARY KEY,
    holder_id  TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_groups (
    id            TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    branch        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_groups_job ON task_groups(job_id);

CREATE TABLE IF NOT EXISTS nodes (
    id                TEXT NOT NULL,
    job_id            TEXT NOT NULL,
    group_id          TEXT,
    op                TEXT NOT NULL,
    spec              TEXT NOT NULL DEFAULT '{}',
    deps              TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL,
    attempt           INTEGER NOT NULL DEFAULT 0,
    wave_id           TEXT,
    exec_class        TEXT,
    spec_group_id     TEXT,
    branch_label      TEXT,
    template_parent_id TEXT,
    bindings          TEXT,
    provider          TEXT,
    model             TEXT,
    est_in            INTEGER,
    est_out           INTEGER,
    act_in            INTEGER,
    act_out           INTEGER,
    cost_usd          REAL,
    rounds            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (job_id, id),
    FOREIGN KEY (group_id) REFERENCES task_groups(id)
);
CREATE INDEX IF NOT EXISTS idx_nodes_job_wave ON nodes(job_id, wave_id);
CREATE INDEX IF NOT EXISTS idx_nodes_job_status ON nodes(job_id, status);

CREATE TABLE IF NOT EXISTS waves (
    id              TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    batch_ref       TEXT,
    idempotency_key TEXT,
    submitted_at    TEXT,
    completed_at    TEXT,
    status          TEXT NOT NULL,
    PRIMARY KEY (job_id, id)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id           TEXT PRIMARY KEY,
    node_id      TEXT,
    memo_key     TEXT NOT NULL,
    mode         TEXT NOT NULL,
    sample_idx   INTEGER NOT NULL DEFAULT 0,
    provider     TEXT,
    request_ref  TEXT,
    response_ref TEXT,
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    cost_usd     REAL,
    cached       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (memo_key)
);

CREATE TABLE IF NOT EXISTS call_items (
    call_id     TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    custom_id   TEXT NOT NULL,
    item_status TEXT,
    PRIMARY KEY (call_id, node_id),
    FOREIGN KEY (call_id) REFERENCES llm_calls(id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    hash      TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    meta      TEXT NOT NULL DEFAULT '{}',
    blob_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applied_diffs (
    worktree   TEXT NOT NULL,
    diff_hash  TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (worktree, diff_hash)
);

CREATE TABLE IF NOT EXISTS stats (
    op                TEXT NOT NULL,
    model             TEXT NOT NULL,
    repo              TEXT NOT NULL,
    n                 INTEGER NOT NULL DEFAULT 0,
    avg_in            REAL,
    avg_out           REAL,
    avg_rounds        REAL,
    verify_pass_rate  REAL,
    PRIMARY KEY (op, model, repo)
);
"""


def default_db_path(repo: str | Path) -> Path:
    """Default DB path: ``<repo>/.lazycode/lazycode.sqlite3``."""
    return Path(repo) / ".lazycode" / "lazycode.sqlite3"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a WAL-mode connection with busy_timeout and foreign_keys on.

    ``isolation_level=None`` puts the driver in autocommit mode so callers get
    explicit control over transaction boundaries via :func:`transaction`
    (``BEGIN IMMEDIATE`` acquires the write lock up front rather than at the
    first write statement, avoiding late ``SQLITE_BUSY`` surprises).
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create every §11 table (``CREATE TABLE IF NOT EXISTS``)."""
    conn.executescript(_SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Reentrant transaction context manager.

    The outermost call opens a real transaction (``BEGIN IMMEDIATE`` — grabs
    the write lock immediately, matching the single-writer discipline of
    §7.1); a call nested inside an already-open transaction uses a
    ``SAVEPOINT`` instead, so composed operations (e.g. ``lease.acquire``
    writing a lease row *and* appending an event via ``eventlog.append``) are
    atomic as one unit while each piece also works standalone.
    """
    top = not conn.in_transaction
    if top:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("SAVEPOINT lc_nested")
    try:
        yield conn
    except BaseException:
        if top:
            conn.execute("ROLLBACK")
        else:
            conn.execute("ROLLBACK TO lc_nested")
            conn.execute("RELEASE lc_nested")
        raise
    else:
        if top:
            conn.execute("COMMIT")
        else:
            conn.execute("RELEASE lc_nested")


class Store:
    """Facade owning the one connection to a job's SQLite store (§7.1, §11).

    Every other ``store/`` module takes a :class:`Store` (not a raw
    connection) as its first argument, so callers always go through this one
    owning object — mirroring the single-writer rule at the API surface.
    """

    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self.conn = conn
        self.db_path = db_path

    @classmethod
    def open(cls, db_path: str | Path | None = None, *, repo: str | Path | None = None) -> Store:
        """Open (creating if needed) the store at ``db_path``, or the default
        path under ``repo`` (``<repo>/.lazycode/lazycode.sqlite3``) when
        ``db_path`` is omitted. Schema creation is idempotent."""
        if db_path is None:
            if repo is None:
                raise ValueError("Store.open requires either db_path or repo")
            db_path = default_db_path(repo)
        path = Path(db_path)
        conn = connect(path)
        create_schema(conn)
        return cls(conn, path)

    @property
    def objects_root(self) -> Path:
        """Root directory for content-addressed blobs (``cas.py``)."""
        return self.db_path.parent / "objects"

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
