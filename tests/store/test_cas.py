"""Tests for cas.py: content-addressed blob round-trip."""

from __future__ import annotations

import hashlib

import pytest

from lazycode.store import Store, cas


def test_put_returns_sha256_hash(store: Store):
    h = cas.put(store, b"hello world", kind="blob")
    assert h == hashlib.sha256(b"hello world").hexdigest()


def test_put_get_round_trip_bytes(store: Store):
    h = cas.put(store, b"\x00\x01binary", kind="request")
    assert cas.get(store, h) == b"\x00\x01binary"


def test_put_get_round_trip_str(store: Store):
    h = cas.put(store, "unicode: café 🎉", kind="response")
    assert cas.get(store, h) == "unicode: café 🎉".encode()


def test_str_and_equivalent_bytes_hash_identically(store: Store):
    h1 = cas.put(store, "same content", kind="a")
    h2 = cas.put(store, b"same content", kind="a")
    assert h1 == h2


def test_get_unknown_hash_raises(store: Store):
    with pytest.raises(KeyError):
        cas.get(store, "0" * 64)


def test_put_writes_sharded_blob_path(store: Store):
    h = cas.put(store, b"payload", kind="blob")
    expected_path = store.objects_root / h[:2] / h
    assert expected_path.exists()
    assert expected_path.read_bytes() == b"payload"


def test_put_is_idempotent_no_duplicate_row(store: Store):
    h1 = cas.put(store, b"same", kind="blob", meta={"a": 1})
    h2 = cas.put(store, b"same", kind="blob", meta={"a": 1})
    assert h1 == h2
    count = store.conn.execute("SELECT COUNT(*) FROM artifacts WHERE hash=?", (h1,)).fetchone()[0]
    assert count == 1


def test_stat_returns_kind_and_meta(store: Store):
    h = cas.put(store, b"payload", kind="diff", meta={"node_id": "n1", "files": ["a.py"]})
    record = cas.stat(store, h)
    assert record is not None
    assert record.kind == "diff"
    assert record.meta == {"node_id": "n1", "files": ["a.py"]}
    assert record.hash == h


def test_stat_unknown_hash_returns_none(store: Store):
    assert cas.stat(store, "0" * 64) is None


def test_different_content_different_hash(store: Store):
    h1 = cas.put(store, b"content A", kind="blob")
    h2 = cas.put(store, b"content B", kind="blob")
    assert h1 != h2
