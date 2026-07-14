"""lazycode store — the SQLite event-sourced core (DESIGN.md §7.1, §11, §13).

``events`` is the source of truth; every other table is either a projection
rebuilt from it (``jobs``/``nodes``/``waves`` — ``projections.py``) or a
directly-written ledger/cache with its own idempotency rule
(``leases``/``llm_calls``+``call_items``/``artifacts``/``applied_diffs``/
``stats`` — ``lease.py``/``memo.py``/``cas.py``/``ledger.py``/``stats.py``).

Public surface:

Physical layer (``db``):
    Store, connect, create_schema, default_db_path, transaction.
Event log (``eventlog``):
    append, record, read.
Submodules exposed directly (not flattened — several define same-named
``get``/``put`` functions, so callers do ``store.memo.get(...)``,
``store.cas.put(...)``, etc.):
    eventlog, projections, lease, memo, ledger, cas, stats.

``Store.open(db_path=None, repo=...)`` is the one entry point every other
module needs — everything else takes a ``Store`` as its first argument.
"""

from __future__ import annotations

from . import cas, eventlog, ledger, lease, memo, projections, stats
from .db import Store, connect, create_schema, default_db_path, transaction

__all__ = [
    "Store",
    "connect",
    "create_schema",
    "default_db_path",
    "transaction",
    "eventlog",
    "projections",
    "lease",
    "memo",
    "ledger",
    "cas",
    "stats",
]
