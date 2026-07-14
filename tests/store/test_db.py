"""Tests for db.py: connection setup, schema creation, transaction reentrancy."""

from __future__ import annotations

import sqlite3

import pytest

from lazycode.store.db import Store, connect, create_schema, default_db_path, transaction


def test_default_db_path(tmp_path):
    assert default_db_path(tmp_path) == tmp_path / ".lazycode" / "lazycode.sqlite3"


def test_connect_sets_pragmas(tmp_path):
    conn = connect(tmp_path / "x.sqlite3")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        conn.close()


def test_create_schema_idempotent(tmp_path):
    conn = connect(tmp_path / "x.sqlite3")
    try:
        create_schema(conn)
        create_schema(conn)  # must not raise
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        expected = {
            "events",
            "jobs",
            "leases",
            "task_groups",
            "nodes",
            "waves",
            "llm_calls",
            "call_items",
            "artifacts",
            "applied_diffs",
            "stats",
        }
        assert expected <= tables
    finally:
        conn.close()


def test_store_open_creates_db_and_dirs(tmp_path):
    db_path = tmp_path / "nested" / "lazycode.sqlite3"
    with Store.open(db_path) as s:
        assert db_path.exists()
        assert s.objects_root == db_path.parent / "objects"


def test_store_open_default_path_from_repo(tmp_path):
    with Store.open(repo=tmp_path) as s:
        assert s.db_path == tmp_path / ".lazycode" / "lazycode.sqlite3"


def test_store_open_requires_db_path_or_repo():
    with pytest.raises(ValueError):
        Store.open()


def test_transaction_commits(store: Store):
    with transaction(store.conn):
        store.conn.execute(
            "INSERT INTO jobs(id, goal, repo, status, created_at) VALUES ('j', 'g', 'r', 'PENDING', 'now')"
        )
    row = store.conn.execute("SELECT id FROM jobs WHERE id='j'").fetchone()
    assert row is not None


def test_transaction_rolls_back_on_exception(store: Store):
    with pytest.raises(RuntimeError):
        with transaction(store.conn):
            store.conn.execute(
                "INSERT INTO jobs(id, goal, repo, status, created_at) VALUES ('j2', 'g', 'r', 'PENDING', 'now')"
            )
            raise RuntimeError("boom")
    row = store.conn.execute("SELECT id FROM jobs WHERE id='j2'").fetchone()
    assert row is None


def test_transaction_nested_savepoint_rolls_back_inner_only(store: Store):
    with transaction(store.conn):
        store.conn.execute(
            "INSERT INTO jobs(id, goal, repo, status, created_at) VALUES ('outer', 'g', 'r', 'PENDING', 'now')"
        )
        with pytest.raises(RuntimeError):
            with transaction(store.conn):
                store.conn.execute(
                    "INSERT INTO jobs(id, goal, repo, status, created_at) VALUES ('inner', 'g', 'r', 'PENDING', 'now')"
                )
                raise RuntimeError("boom")
        # outer transaction is still open and uncorrupted after the nested rollback
        store.conn.execute(
            "INSERT INTO jobs(id, goal, repo, status, created_at) VALUES ('outer2', 'g', 'r', 'PENDING', 'now')"
        )
    ids = {r[0] for r in store.conn.execute("SELECT id FROM jobs").fetchall()}
    assert ids == {"outer", "outer2"}


def test_foreign_key_enforced(store: Store):
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(store.conn):
            store.conn.execute(
                "INSERT INTO nodes(id, job_id, group_id, op, status) VALUES ('n1', 'j', 'missing-group', 'Edit', 'PENDING')"
            )
