"""Fixture generator for the ``add-type-hints`` benchmark task.

Committed generator (not committed junk, per the module brief): called by
``bench/task_spec.py::build_repo`` to materialize a fresh git repo into a
tmp directory for each benchmark run, so the task fixture is reproducible
without checking generated files into the repo.
"""

from __future__ import annotations

from pathlib import Path

_MODULES = {
    "invoice.py": '''\
def total(items, tax_rate):
    return sum(items) * (1 + tax_rate)


def apply_discount(amount, pct):
    return amount * (1 - pct / 100)
''',
    "ledger.py": '''\
def record(entries, entry):
    entries.append(entry)
    return entries


def balance(entries):
    return sum(e.get("amount", 0) for e in entries)
''',
    "refunds.py": '''\
def eligible(order, days_since_purchase):
    return days_since_purchase <= 30 and not order.get("final_sale")


def refund_amount(order, restocking_fee=0):
    return order.get("total", 0) - restocking_fee
''',
}


def build(repo_root: Path) -> None:
    pkg = repo_root / "pkg" / "billing"
    pkg.mkdir(parents=True, exist_ok=True)
    (repo_root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, content in _MODULES.items():
        (pkg / name).write_text(content, encoding="utf-8")
