"""The ANALYZE table — per-(op, model, repo) actuals, converging cold-start
priors (DESIGN.md §5.1, §11, Appendix B10).

"A local ``stats`` table records actuals per (operator, model, repo) — tokens,
rounds, verify-pass-rate. Estimates start from hardcoded cold-start priors
(Appendix B10) and converge per-repo" (§5.1). This module owns the
observational side only: :func:`record` folds one more observation into the
running means with the standard streaming-average update (``avg += (x -
avg) / n``), and :func:`priors` reads them back.

Resolved ambiguity — the cold-start threshold: Appendix B10 says priors are
hardcoded "until n ≥ 20 per (op, model)". :func:`priors` enforces exactly that
threshold and returns ``None`` below it, *including* when there is no row at
all yet — so the caller (optimizer, §5.1/B10) always applies the Appendix B10
hardcoded table as the fallback and never has to duplicate the n<20 check
itself. This module does not hardcode the B10 priors table (that belongs to
``optimizer/``, which owns the cost model); ``store/`` only ever returns
observed, non-fabricated numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import Store, transaction

COLD_START_N = 20
"""Appendix B10: priors are hardcoded until n >= 20 per (op, model)."""


@dataclass(frozen=True, slots=True)
class StatsPriors:
    """One ``stats`` row's converged averages (§11)."""

    op: str
    model: str
    repo: str
    n: int
    avg_in: float
    avg_out: float
    avg_rounds: float
    verify_pass_rate: float


def record(
    store: Store,
    *,
    op: str,
    model: str,
    repo: str,
    tokens_in: int,
    tokens_out: int,
    rounds: int,
    verify_pass: bool,
) -> None:
    """Fold one more observation into the (op, model, repo) running averages."""
    with transaction(store.conn):
        row = store.conn.execute(
            "SELECT n, avg_in, avg_out, avg_rounds, verify_pass_rate FROM stats "
            "WHERE op = ? AND model = ? AND repo = ?",
            (op, model, repo),
        ).fetchone()
        pass_value = 1.0 if verify_pass else 0.0
        if row is None:
            store.conn.execute(
                """
                INSERT INTO stats(op, model, repo, n, avg_in, avg_out, avg_rounds, verify_pass_rate)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (op, model, repo, float(tokens_in), float(tokens_out), float(rounds), pass_value),
            )
            return
        n = row["n"] + 1
        avg_in = row["avg_in"] + (tokens_in - row["avg_in"]) / n
        avg_out = row["avg_out"] + (tokens_out - row["avg_out"]) / n
        avg_rounds = row["avg_rounds"] + (rounds - row["avg_rounds"]) / n
        verify_pass_rate = row["verify_pass_rate"] + (pass_value - row["verify_pass_rate"]) / n
        store.conn.execute(
            """
            UPDATE stats SET n = ?, avg_in = ?, avg_out = ?, avg_rounds = ?, verify_pass_rate = ?
            WHERE op = ? AND model = ? AND repo = ?
            """,
            (n, avg_in, avg_out, avg_rounds, verify_pass_rate, op, model, repo),
        )


def priors(store: Store, *, op: str, model: str, repo: str) -> StatsPriors | None:
    """Converged averages for (op, model, repo), or ``None`` below the
    Appendix B10 cold-start threshold (``n < 20``, including "no row yet").
    Caller applies the hardcoded B10 priors table on a ``None``.
    """
    row = store.conn.execute(
        "SELECT n, avg_in, avg_out, avg_rounds, verify_pass_rate FROM stats "
        "WHERE op = ? AND model = ? AND repo = ?",
        (op, model, repo),
    ).fetchone()
    if row is None or row["n"] < COLD_START_N:
        return None
    return StatsPriors(
        op=op,
        model=model,
        repo=repo,
        n=row["n"],
        avg_in=row["avg_in"],
        avg_out=row["avg_out"],
        avg_rounds=row["avg_rounds"],
        verify_pass_rate=row["verify_pass_rate"],
    )
