"""Content-addressed artifact store (DESIGN.md §9, §10, §11).

Blobs (rendered prompts, provider responses, diffs, reports — "results,
requests, and artifacts are stored content-addressed", §10) live on disk under
``<db_dir>/objects/<hash[:2]>/<hash>`` (``Store.objects_root``, sharded by hash
prefix to avoid one giant directory), with one indexing row per blob in the
``artifacts`` table (§11: ``artifacts(hash PK, kind, meta JSON, blob_path)``).
The hash is ``sha256`` of the raw bytes — the same primitive family as
``diff_hash`` (§9) and ``memo_key`` (§5.2 R10), so all three "already have
this" checks in the system are point lookups on a hex digest.

:func:`put` is idempotent by construction: identical content always hashes to
the same digest, so a second ``put`` of the same bytes is a cheap no-op (the
blob file and ``artifacts`` row already exist) rather than a duplicate write.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from lazycode.ir import canonical_json

from .db import Store, transaction


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """One ``artifacts`` row (§11)."""

    hash: str
    kind: str
    meta: dict
    blob_path: Path


def _blob_path(store: Store, digest: str) -> Path:
    return store.objects_root / digest[:2] / digest


def put(store: Store, content: bytes | str, *, kind: str, meta: dict | None = None) -> str:
    """Write ``content`` to the CAS, returning its ``sha256`` hex digest.

    A no-op (aside from an idempotent ``artifacts`` upsert) if the content is
    already stored — the digest determines the path, so re-``put``ting
    identical bytes never writes the file twice.
    """
    data = content.encode("utf-8") if isinstance(content, str) else content
    digest = hashlib.sha256(data).hexdigest()
    blob_path = _blob_path(store, digest)
    with transaction(store.conn):
        existing = store.conn.execute("SELECT hash FROM artifacts WHERE hash = ?", (digest,)).fetchone()
        if existing is None:
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            if not blob_path.exists():
                tmp_path = blob_path.with_name(blob_path.name + ".tmp")
                tmp_path.write_bytes(data)
                tmp_path.replace(blob_path)  # atomic within the same filesystem
            store.conn.execute(
                "INSERT INTO artifacts(hash, kind, meta, blob_path) VALUES (?, ?, ?, ?)",
                (digest, kind, canonical_json(meta or {}), str(blob_path)),
            )
    return digest


def get(store: Store, artifact_hash: str) -> bytes:
    """Read back a blob's raw bytes by hash. Raises ``KeyError`` if unknown."""
    row = store.conn.execute(
        "SELECT blob_path FROM artifacts WHERE hash = ?", (artifact_hash,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no artifact with hash {artifact_hash!r}")
    return Path(row["blob_path"]).read_bytes()


def stat(store: Store, artifact_hash: str) -> ArtifactRecord | None:
    """Return the ``artifacts`` row (kind/meta/path) for a hash, or ``None``."""
    row = store.conn.execute(
        "SELECT hash, kind, meta, blob_path FROM artifacts WHERE hash = ?", (artifact_hash,)
    ).fetchone()
    if row is None:
        return None
    return ArtifactRecord(
        hash=row["hash"], kind=row["kind"], meta=json.loads(row["meta"]), blob_path=Path(row["blob_path"])
    )
