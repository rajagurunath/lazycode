#!/usr/bin/env python3
"""Cost metering: *what does $10 buy?* -- the measurement instrument behind
the paper's worked dollar example (``docs/paper/latex/main.tex``,
\\S"The economics condition").

The rest of ``bench/`` compares lazycode against the Claude Code CLI baseline
in **tokens** (Appendix B7's ``<50%`` accept criterion). This module answers
the money question directly: run *one identical workload twice* -- once
online-only, once through lazycode's batch waves -- meter the real token
ledger on both paths, price both from a declarative published price sheet,
and report ``$ online`` vs ``$ lazycode`` vs ``$ saved`` plus the effective
per-item input factor observed against the model's predicted
``min(0.5, 0.05 + 0.95/N)``.

**Both sides run the same plan through the same** :class:`~lazycode.scheduler.Orchestrator`.
The only difference is the execution tier:

* **batch** -- the normal path: one provider batch per ready frontier
  (``FixtureBatchAdapter`` in dry-run, ``OpenAIBatchAdapter`` under ``--live``).
* **online-only** -- :class:`SequentialOnlineAdapter`, a ``BatchAdapter``-shaped
  shim that satisfies the same protocol but executes each item *one at a time*
  through a :class:`~lazycode.providers.base.RealtimeAdapter`. This is DESIGN
  \\S10's "slider-0" (``realtime: same RenderedCall shape ... and slider-0``),
  not a parallel harness: the orchestrator, renderer, harvester, memo cache and
  event log are all untouched.

Because both sides submit byte-identical :class:`~lazycode.ir.RenderedCall`
objects at ``temperature=0`` with the same model, **quality equivalence is
exact by construction under the mock providers** -- the report says so rather
than implying a measurement it did not make. The ``--live`` path compares real
responses and reports a genuine match rate.

Token accounting
----------------
Each metered call is split into

* ``prefix_tokens`` -- the shared system blocks (execution role, repo map,
  house rules): the hoisted, cache-hintable prefix R4 factors and
  \\S"Width changes the condition" reasons about. Identical bytes across the
  items of a wave.
* ``unique_tokens`` -- the per-item user message (instruction + file blocks +
  contract directive). Never shared, never cached across items.
* ``output_tokens`` -- the response.

In dry-run mode (the default) all three are the ``chars/4`` heuristic below,
since the mock fixtures carry no provider ``usage`` block worth trusting. In
``--live`` mode each metered call also carries the provider's own
``reported_input_tokens``/``reported_output_tokens``; :func:`reconcile_ledgers`
rescales the heuristic prefix/unique split to match the reported input total
(preserving the split's *shape*, correcting its *scale*) and replaces the
heuristic output count outright, so live prices are never derived purely from
the pre-submit estimate. The report line says which happened.

Cache pricing then follows each tier's actual structure:

* **online (sequential)**: the first call writes the prefix; every later call
  with the same prefix hash reads it. Unique tokens always pay full input
  price. This is the paper's *honest cached interactive baseline*, and it is
  deliberately generous to the online side (100% hit rate, no eviction). It
  prices the prefix write at the 5-minute rate (``cache_write_5m``) --
  matching both what the adapter actually requests today (\\S"Width changes
  the condition") and how \\S"What ten dollars buys" prices this same path.
* **batch (cold, the default)**: everything pays the flat batch input rate --
  the conservative, fully-published case. Batch's 50% output discount is
  unconditional.
* **batch (``--stack-cache``, OFF by default)**: models the *unverified*
  case where batch and prompt-cache discounts stack, exactly as
  \\S"Width changes the condition" derives it -- one prefix write plus
  ``N-1`` reads per wave, and only when the wave is wide enough to amortize
  the write (``N >= 3``). The paper does not book this saving and neither
  does the default report.

Modes
-----
``--live`` is **OFF by default**. The default run is a dry run against the
committed mock fixtures: zero network, $0 spent, deterministic, CI-safe.
``--live`` requires ``OPENAI_API_KEY`` and **spends real money** on the OpenAI
Batch and Chat Completions APIs; it refuses to start without an explicit
``--yes-spend-real-money`` acknowledgement (enforced both by the CLI and by
:func:`run_cost_report` itself, so library callers cannot bypass the CLI
gate). ``--live`` also refuses to run with the Anthropic defaults
(``DEFAULT_MODEL``/``DEFAULT_PRICE_KEY``) unmodified, since the only live
adapter today speaks OpenAI: pass an explicit ``--model`` and an ``openai/*``
``--price-key`` together.

Usage::

    # dry run (default): $0, no network
    uv run python bench/cost_report.py add-type-hints

    # same, with the unverified stacked-discount sensitivity shown
    uv run python bench/cost_report.py add-type-hints --stack-cache

    # price the same metered ledger against a different published sheet
    uv run python bench/cost_report.py add-type-hints --price-key openai/gpt-4o-mini

    # REAL MONEY -- not run by any test, not run by CI
    uv run python bench/cost_report.py add-type-hints --live --yes-spend-real-money \\
        --model gpt-4o-mini --price-key openai/gpt-4o-mini
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

try:
    from .task_spec import build_repo, list_tasks, load_task
except ImportError:  # running as a plain script (`python bench/cost_report.py`)
    from task_spec import build_repo, list_tasks, load_task  # type: ignore[no-redef]

from lazycode.cli import mock_provider
from lazycode.ir import BatchRef, BatchStatus, ItemResult, ItemStatus, Plan, RenderedCall
from lazycode.providers.mock import MockRealtimeAdapter
from lazycode.scheduler import Orchestrator, SchedulerConfig
from lazycode.store import Store

RESULTS_DIR = Path(__file__).parent / "results"

# --- price sheet -------------------------------------------------------------
#
# USD per 1,000,000 tokens, transcribed from the providers' own published
# pricing pages. Unlike ``bench/pricing.py`` (explicitly illustrative), these
# are real published list prices with their source and access date recorded.
#
#   anthropic/claude-haiku-4-5
#     https://platform.claude.com/docs/en/about-claude/pricing  (accessed 2026-08-12)
#     Base input $1.00 | 5m cache write $1.25 (1.25x) | 1h cache write $2.00 (2x)
#     | cache read $0.10 (0.1x) | output $5.00.
#     Message Batches API: 50% off input AND output -> $0.50 / $2.50.
#     BOTH cache-write TTLs are carried, because two different sections of the
#     paper price two different paths against them: ``cache_write_5m`` (1.25x)
#     is what the adapter actually requests today and what \S"What ten dollars
#     buys" prices the interactive baseline with -- price_online() defaults to
#     it. ``cache_write_1h`` (2x) is the TTL \S"Width changes the condition"
#     assumes for the batch-stacking sensitivity path (batch-scale queuing
#     outlives a 5-minute cache) -- price_batch()'s --stack-cache branch uses
#     it explicitly. This is the "same sheet" the paper's footnote refers to.
#
#   openai/gpt-4o-mini
#     https://developers.openai.com/api/docs/pricing  (accessed 2026-08-12)
#     Input $0.15 | cached input $0.075 | output $0.60.
#     Batch API: 50% off input AND output -> $0.075 / $0.30.
#     Note gpt-4o-mini's cache discount is 50%, not the 90% newer OpenAI models
#     get -- which is why the batch margin is wider here than on Haiku, and why
#     "the honest cached baseline" is model-specific. OpenAI's automatic prefix
#     caching charges no write premium, so both cache-write keys equal input.
#
# Override with $LAZYCODE_BENCH_PRICE_SHEET pointing at a JSON file of the
# same shape (same convention as bench/pricing.py's $LAZYCODE_BENCH_PRICING).
PRICE_SHEET: dict[str, dict[str, float]] = {
    "anthropic/claude-haiku-4-5": {
        "input": 1.00,
        "cached_input": 0.10,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.00,
        "batch_input": 0.50,
        "output": 5.00,
        "batch_output": 2.50,
    },
    "openai/gpt-4o-mini": {
        "input": 0.15,
        "cached_input": 0.075,
        "cache_write_5m": 0.15,
        "cache_write_1h": 0.15,
        "batch_input": 0.075,
        "output": 0.60,
        "batch_output": 0.30,
    },
}

DEFAULT_PRICE_KEY = "anthropic/claude-haiku-4-5"
DEFAULT_MODEL = "claude-haiku-4-5"

# Anthropic and OpenAI both publish "50% off" for the Batch API. Where the
# batch discount is applied ON TOP OF cache rates (--stack-cache) it is applied
# as this same multiplier -- provider "stacking" language implies it; we have
# NOT verified the line items, which is why --stack-cache is off by default.
_BATCH_MULTIPLIER = 0.5

# Below this wave width the 2x prefix cache write costs more than it saves, so
# a rational renderer does not request caching at all (this is the `min(...)`
# branch in the paper's per-item input factor).
_MIN_CACHEABLE_WIDTH = 3


def price_sheet() -> dict[str, dict[str, float]]:
    override = os.environ.get("LAZYCODE_BENCH_PRICE_SHEET")
    if override:
        return json.loads(Path(override).read_text(encoding="utf-8"))
    return PRICE_SHEET


def rates(price_key: str) -> dict[str, float]:
    sheet = price_sheet()
    if price_key not in sheet:
        raise KeyError(f"unknown price key {price_key!r}; known: {sorted(sheet)}")
    return sheet[price_key]


def predicted_input_factor(width: int) -> float:
    """The paper's per-item input factor: ``min(0.5, 0.05 + 0.95/N)``.

    ``0.5`` is the flat batch discount (no caching); ``0.05 + 0.95/N`` is one
    1-hour prefix write at the batch rate (``2 x 0.5 = 1.0``) amortized over
    ``N`` items plus ``N-1`` reads at the batch rate (``0.1 x 0.5 = 0.05``).
    """
    if width < 1:
        raise ValueError("width must be >= 1")
    return min(0.5, 0.05 + 0.95 / width)


# --- token metering ----------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """chars/4 -- the same heuristic every adapter in this repo uses for
    pre-submit sizing (``providers/mock.py``, ``providers/openai_batch.py``).

    Used for the prefix/unique SPLIT, which no provider reports. Totals from a
    real provider's ``usage`` are metered separately as ``reported_*`` and
    are what ``--live`` prices from.
    """
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class CallLedger:
    """The metered token ledger for one model call on one tier."""

    tier: str  # "online" | "batch"
    wave_seq: int  # submission group ordinal (online: one item per group)
    node_id: str
    model: str
    prefix_sha: str
    prefix_tokens: int
    unique_tokens: int
    output_tokens: int
    reported_input_tokens: int | None
    reported_output_tokens: int | None
    response_sha: str
    errored: bool = False  # a billable ERRORED item (usage-bearing), not a COMPLETED one

    @property
    def input_tokens(self) -> int:
        return self.prefix_tokens + self.unique_tokens


def _split_call(call: RenderedCall) -> tuple[str, int, int]:
    """``(prefix_sha, prefix_tokens, unique_tokens)`` for one rendered call."""
    prefix_text = "".join(block.text for block in call.system)
    unique_text = "".join(m.content for m in call.messages)
    prefix_sha = hashlib.sha256(prefix_text.encode("utf-8")).hexdigest()[:16]
    return prefix_sha, estimate_tokens(prefix_text), estimate_tokens(unique_text)


def _response_text(result: ItemResult) -> str:
    payload = result.payload or {}
    parts = [
        block.get("text", "")
        for block in (payload.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(parts)


def _usage_of(result: ItemResult) -> dict[str, Any]:
    """``usage`` from ``payload`` if present, else from ``error`` -- a provider
    can bill a call that ultimately errors (e.g. a content-policy stop after
    tokens were already generated), and some error payloads carry usage."""
    return (result.payload or {}).get("usage") or (result.error or {}).get("usage") or {}


def _is_billable(result: ItemResult) -> bool:
    """COMPLETED items are always billable. ERRORED items are billable only
    when they carry usage -- a call the provider actually charged for, not a
    request that was rejected before it was ever sent (those cost nothing and
    correctly stay out of the ledger)."""
    if result.status is ItemStatus.COMPLETED:
        return True
    return result.status is ItemStatus.ERRORED and bool(_usage_of(result))


class MeteringAdapter:
    """Wraps any :class:`~lazycode.providers.base.BatchAdapter` and records a
    :class:`CallLedger` per returned item, without changing its behaviour.

    Metering lives here rather than in ``lazycode/`` because the orchestrator
    already persists what it needs (``llm_calls``); the prefix/unique split is
    a *benchmark* concern, and instrumenting at the adapter seam keeps the
    shipped execution path byte-identical between the two runs.
    """

    def __init__(self, inner: Any, tier: str) -> None:
        self._inner = inner
        self.tier = tier
        self.ledgers: list[CallLedger] = []
        self._pending: dict[str, RenderedCall] = {}
        self._wave_of_batch: dict[str, int] = {}
        self._wave_seq = 0

    @property
    def caps(self) -> Any:
        return self._inner.caps

    def count_tokens(self, items: list[RenderedCall]) -> Any:
        return self._inner.count_tokens(items)

    def submit(
        self,
        items: list[RenderedCall],
        idempotency_key: str,
        *,
        known_refs: dict[str, BatchRef] | None = None,
    ) -> BatchRef:
        for call in items:
            self._pending[call.custom_id] = call
        ref = self._inner.submit(items, idempotency_key, known_refs=known_refs)
        if ref.batch_id not in self._wave_of_batch:
            self._wave_of_batch[ref.batch_id] = self._wave_seq
            self._wave_seq += 1
        return ref

    def find_batch(self, idempotency_key: str) -> BatchRef | None:
        return self._inner.find_batch(idempotency_key)

    def poll(self, ref: BatchRef) -> BatchStatus:
        return self._inner.poll(ref)

    def fetch(self, ref: BatchRef) -> Iterator[ItemResult]:
        wave_seq = self._wave_of_batch.get(ref.batch_id, 0)
        for result in self._inner.fetch(ref):
            call = self._pending.get(result.custom_id)
            # Record every BILLABLE result, not just COMPLETED ones: a batch
            # item that errors out after the provider already spent tokens on
            # it is still on the invoice, and dropping it here would make the
            # metered ledger under-report a real live-mode spend.
            if call is not None and _is_billable(result):
                self._record(wave_seq, call, result)
            yield result

    def cancel(self, ref: BatchRef) -> None:
        self._inner.cancel(ref)

    def _record(self, wave_seq: int, call: RenderedCall, result: ItemResult) -> None:
        prefix_sha, prefix_tokens, unique_tokens = _split_call(call)
        text = _response_text(result)
        usage = _usage_of(result)
        self.ledgers.append(
            CallLedger(
                tier=self.tier,
                wave_seq=wave_seq,
                node_id=call.custom_id,
                model=call.model,
                prefix_sha=prefix_sha,
                prefix_tokens=prefix_tokens,
                unique_tokens=unique_tokens,
                output_tokens=estimate_tokens(text),
                reported_input_tokens=usage.get("input_tokens"),
                reported_output_tokens=usage.get("output_tokens"),
                response_sha=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                errored=result.status is ItemStatus.ERRORED,
            )
        )


class SequentialOnlineAdapter:
    """The online-only tier, shaped as a ``BatchAdapter`` (DESIGN \\S10 slider-0).

    Structurally satisfies :class:`~lazycode.providers.base.BatchAdapter` so
    :class:`~lazycode.scheduler.Orchestrator` runs the identical plan through
    the identical code path -- but ``submit`` does not pool anything: it
    executes each :class:`~lazycode.ir.RenderedCall` **synchronously and one at
    a time** through a :class:`~lazycode.providers.base.RealtimeAdapter`, in id
    order, exactly as an interactive harness would. There is no provider batch,
    no 24-hour window, and (in pricing) no within-wave prefix sharing: each call
    is its own request, so the prefix is cached across the *sequence* rather
    than across a *wave*.
    """

    def __init__(self, realtime: Any, *, provider_name: str = "online") -> None:
        self._realtime = realtime
        self.provider_name = provider_name
        self._results: dict[str, list[ItemResult]] = {}
        self._refs: dict[str, BatchRef] = {}
        self.completed_calls: list[RenderedCall] = []

    @property
    def caps(self) -> Any:
        return mock_provider._DEFAULT_CAPS

    def count_tokens(self, items: list[RenderedCall]) -> Any:
        from lazycode.ir import TokenEstimate

        total = sum(sum(_split_call(c)[1:]) for c in items)
        return TokenEstimate(input_tokens=total, output_tokens=0, item_count=len(items))

    def submit(
        self,
        items: list[RenderedCall],
        idempotency_key: str,
        *,
        known_refs: dict[str, BatchRef] | None = None,
    ) -> BatchRef:
        if known_refs is not None and idempotency_key in known_refs:
            return known_refs[idempotency_key]
        if idempotency_key in self._refs:
            return self._refs[idempotency_key]

        batch_id = f"online-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:10]}"
        ref = BatchRef(provider=self.provider_name, batch_id=batch_id, idempotency_key=idempotency_key)
        # The point of this adapter: no pooling. One synchronous call per item,
        # sequentially, in deterministic order.
        results = []
        for call in sorted(items, key=lambda c: c.custom_id):
            results.append(self._realtime.complete(call))
            self.completed_calls.append(call)
        self._results[batch_id] = results
        self._refs[idempotency_key] = ref
        return ref

    def find_batch(self, idempotency_key: str) -> BatchRef | None:
        return self._refs.get(idempotency_key)

    def poll(self, ref: BatchRef) -> BatchStatus:
        n = len(self._results.get(ref.batch_id, []))
        return BatchStatus(batch_status="ended", completed=n, errored=0, expired=0, processing=0)

    def fetch(self, ref: BatchRef) -> Iterator[ItemResult]:
        yield from self._results.get(ref.batch_id, [])

    def cancel(self, ref: BatchRef) -> None:  # pragma: no cover - nothing to cancel
        self._results.pop(ref.batch_id, None)


# --- pricing -----------------------------------------------------------------


@dataclass
class Breakdown:
    """Priced totals for one tier."""

    tier: str
    calls: int
    errored_calls: int
    prefix_tokens: int
    unique_tokens: int
    output_tokens: int
    prefix_usd: float = 0.0
    unique_usd: float = 0.0
    output_usd: float = 0.0
    cache_writes: int = 0
    cache_reads: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def input_usd(self) -> float:
        return self.prefix_usd + self.unique_usd

    @property
    def total_usd(self) -> float:
        return self.input_usd + self.output_usd


def _base(ledgers: list[CallLedger], tier: str) -> Breakdown:
    return Breakdown(
        tier=tier,
        calls=len(ledgers),
        errored_calls=sum(1 for x in ledgers if x.errored),
        prefix_tokens=sum(x.prefix_tokens for x in ledgers),
        unique_tokens=sum(x.unique_tokens for x in ledgers),
        output_tokens=sum(x.output_tokens for x in ledgers),
    )


def reconcile_ledgers(ledgers: list[CallLedger], *, live: bool) -> tuple[list[CallLedger], str]:
    """Reconcile the pre-submit heuristic prefix/unique split against
    provider-reported usage before pricing.

    Dry-run mode (``live=False``): the heuristic (``chars/4``) split is used
    unchanged -- there is no provider usage to reconcile against.

    Live mode: for any ledger carrying both ``reported_input_tokens`` and
    ``reported_output_tokens``, the heuristic prefix/unique split is rescaled
    *proportionally* so it sums to the reported input total (the split shape
    -- which tokens are shared prefix vs. unique -- has no provider-reported
    equivalent, so the heuristic ratio is kept; only its scale is corrected),
    and the heuristic output count is replaced outright by the reported one.
    Ledgers with no reported usage (should not happen once ``_is_billable``
    requires it, but kept defensive) fall back to the heuristic unchanged.

    Returns ``(ledgers, label)`` where ``label`` documents which split was
    actually used, for the report's per-tier notes.
    """
    if not live:
        return ledgers, "token split: heuristic estimate"
    reconciled: list[CallLedger] = []
    any_reported = False
    for lg in ledgers:
        if lg.reported_input_tokens is None or lg.reported_output_tokens is None:
            reconciled.append(lg)
            continue
        any_reported = True
        heuristic_input = lg.prefix_tokens + lg.unique_tokens
        prefix_share = (lg.prefix_tokens / heuristic_input) if heuristic_input else 0.0
        new_prefix = round(lg.reported_input_tokens * prefix_share)
        new_unique = max(0, lg.reported_input_tokens - new_prefix)
        reconciled.append(
            replace(
                lg,
                prefix_tokens=new_prefix,
                unique_tokens=new_unique,
                output_tokens=lg.reported_output_tokens,
            )
        )
    label = (
        "token split: scaled to provider-reported usage"
        if any_reported
        else "token split: heuristic estimate (no reported usage on any call)"
    )
    return reconciled, label


def price_online(ledgers: list[CallLedger], rate: dict[str, float]) -> Breakdown:
    """Price the online-only sequence with a prompt-cached prefix.

    Deliberately generous to the online side (the paper's stance): a 100%
    prefix hit rate with no eviction after the first write. Anything less
    makes online *more* expensive, so the saving reported here is a floor.

    Prices the prefix write at ``cache_write_5m`` -- the TTL the adapter
    actually requests today (\\S"Width changes the condition") and the price
    \\S"What ten dollars buys" uses for this same interactive path.
    """
    out = _base(ledgers, "online")
    seen: set[str] = set()
    for lg in sorted(ledgers, key=lambda x: (x.wave_seq, x.node_id)):
        if lg.prefix_sha in seen:
            out.prefix_usd += lg.prefix_tokens * rate["cached_input"] / 1e6
            out.cache_reads += 1
        else:
            out.prefix_usd += lg.prefix_tokens * rate["cache_write_5m"] / 1e6
            out.cache_writes += 1
            seen.add(lg.prefix_sha)
        out.unique_usd += lg.unique_tokens * rate["input"] / 1e6
        out.output_usd += lg.output_tokens * rate["output"] / 1e6
    out.notes.append("prefix prompt-cached across the sequence at a generous 100% hit rate")
    return out


def price_batch(ledgers: list[CallLedger], rate: dict[str, float], *, stack_cache: bool = False) -> Breakdown:
    """Price the batch waves.

    Default (``stack_cache=False``): every input token pays the flat published
    batch rate -- the conservative case, fully supported by both providers'
    published batch pricing.

    ``stack_cache=True``: the UNVERIFIED case of \\S"Width changes the
    condition" -- batch and cache discounts stack, so a wave of ``N >= 3``
    prefix-sharing items pays one 1-hour prefix write (``cache_write_1h`` --
    the TTL that outlives batch-scale queuing delays, per \\S"Width changes
    the condition") plus ``N-1`` reads, both at the batch multiplier. Below
    ``N = 3`` the write does not amortize and the uncached branch is cheaper,
    which is the ``min(...)`` in the model.
    """
    out = _base(ledgers, "batch")
    waves: dict[tuple[int, str], list[CallLedger]] = {}
    for lg in ledgers:
        waves.setdefault((lg.wave_seq, lg.prefix_sha), []).append(lg)

    for group in waves.values():
        width = len(group)
        prefix_tokens = group[0].prefix_tokens
        if stack_cache and width >= _MIN_CACHEABLE_WIDTH:
            out.prefix_usd += prefix_tokens * rate["cache_write_1h"] * _BATCH_MULTIPLIER / 1e6
            out.cache_writes += 1
            out.prefix_usd += (
                (width - 1) * prefix_tokens * rate["cached_input"] * _BATCH_MULTIPLIER / 1e6
            )
            out.cache_reads += width - 1
        else:
            out.prefix_usd += sum(lg.prefix_tokens for lg in group) * rate["batch_input"] / 1e6
    out.unique_usd = sum(lg.unique_tokens for lg in ledgers) * rate["batch_input"] / 1e6
    out.output_usd = sum(lg.output_tokens for lg in ledgers) * rate["batch_output"] / 1e6
    out.notes.append(
        "batch+cache discounts STACKED (provider-implied, unverified by us)"
        if stack_cache
        else "cold batch caches -- flat published batch rate on every input token"
    )
    return out


def observed_input_factor(breakdown: Breakdown, rate: dict[str, float]) -> float | None:
    """Effective per-item factor on the SHARED PREFIX -- the quantity the
    paper's ``min(0.5, 0.05 + 0.95/N)`` predicts.

    Measured as (dollars actually charged for prefix tokens) / (the same tokens
    at full online input price). The per-item unique context is excluded on
    purpose: it is never shared, so it always pays the flat batch rate and
    would dilute the factor the model is about.
    """
    if not breakdown.prefix_tokens:
        return None
    full_price = breakdown.prefix_tokens * rate["input"] / 1e6
    return breakdown.prefix_usd / full_price if full_price else None


def wave_widths(ledgers: list[CallLedger]) -> list[int]:
    waves: dict[tuple[int, str], int] = {}
    for lg in ledgers:
        key = (lg.wave_seq, lg.prefix_sha)
        waves[key] = waves.get(key, 0) + 1
    return [waves[k] for k in sorted(waves)]


# --- execution ---------------------------------------------------------------


def _fixture_realtime(fixture: dict[str, Any]) -> MockRealtimeAdapter:
    """A realtime adapter answering from the *same* fixture entries the batch
    adapter uses, via ``mock_provider._item_response`` -- deliberately the one
    fixture->response mapping in the repo, so the two tiers cannot drift and
    the quality-equivalence claim stays true by construction.
    """
    items = fixture.get("items") or {}

    def _respond(call: RenderedCall) -> ItemResult:
        spec = items.get(call.custom_id, items.get("*"))
        return mock_provider._item_response(call.custom_id, spec)

    return MockRealtimeAdapter(responses=_respond)


def _run_side(
    task_name: str,
    *,
    tier: str,
    adapter_factory,
    model: str,
    plan: Plan,
    provider_key: str,
) -> tuple[MeteringAdapter, dict[str, Any]]:
    """Run ``task_name``'s plan end to end on one tier; return its metering
    adapter plus the job outcome. Each side gets its own freshly materialized
    fixture repo so the two runs cannot contaminate each other."""
    task = load_task(task_name)
    tmp = Path(tempfile.mkdtemp(prefix=f"lazycode-cost-{tier}-"))
    try:
        repo_root = tmp / "repo"
        base_commit = build_repo(task, repo_root)
        store = Store.open(repo=repo_root)
        try:
            metered = MeteringAdapter(adapter_factory(), tier)
            config = SchedulerConfig(
                provider=provider_key,
                model=model,
                verify_command=task.verify_command,
                temperature=0.0,  # the paper's memoization/quality precondition
            )
            orch = Orchestrator(store, {provider_key: metered}, repo_root, config)
            job_id = orch.create_job(task.goal, plan, base_commit, slider=100)
            result = orch.run_job(job_id)
            outcome = {"status": result.status, "waves": result.waves, "job_id": job_id}
        finally:
            store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return metered, outcome


def _live_adapters(model: str):  # pragma: no cover - never exercised by tests
    """Real OpenAI adapters for ``--live``. Costs real money."""
    from lazycode.providers.openai_batch import OpenAIBatchAdapter, build_chat_body

    class _OpenAIRealtime:
        """Synchronous Chat Completions, built from the SAME
        ``build_chat_body`` the batch adapter serializes with -- so the online
        and batch tiers send byte-identical request bodies."""

        def __init__(self, client: Any) -> None:
            self._client = client

        def complete(self, call: RenderedCall, **_: Any) -> ItemResult:
            body = build_chat_body(call)
            resp = self._client.chat.completions.create(**body)
            text = resp.choices[0].message.content or ""
            return ItemResult(
                custom_id=call.custom_id,
                status=ItemStatus.COMPLETED,
                payload={
                    "content": [{"type": "text", "text": text}],
                    "usage": {
                        "input_tokens": resp.usage.prompt_tokens,
                        "output_tokens": resp.usage.completion_tokens,
                    },
                },
            )

    batch = OpenAIBatchAdapter.from_env(api_key_env="OPENAI_API_KEY", provider_name="live")
    return batch, _OpenAIRealtime(batch._client)


# The only live adapter implemented today (see ``_live_adapters`` above) speaks
# OpenAI. Extend this when a live Anthropic adapter lands.
_LIVE_ADAPTER_PROVIDER = "openai"


def _require_live_consistency(model: str, price_key: str) -> None:
    """Guard the two ways ``--live`` can silently run the wrong provider or
    mis-price a real spend: an un-overridden Anthropic default reaching the
    OpenAI wire, or a ``--price-key`` whose provider does not match the live
    adapter's provider at all.
    """
    if model == DEFAULT_MODEL:
        raise ValueError(
            f"--live requires an explicit --model for the live provider "
            f"({_LIVE_ADAPTER_PROVIDER}); got the unmodified default {DEFAULT_MODEL!r}, "
            "which is an Anthropic model name and will not run against the OpenAI-only "
            "live adapter. Pass e.g. --model gpt-4o-mini."
        )
    price_provider = price_key.split("/", 1)[0]
    if price_provider != _LIVE_ADAPTER_PROVIDER:
        raise ValueError(
            f"--live only runs the {_LIVE_ADAPTER_PROVIDER}-compatible adapter today, but "
            f"--price-key {price_key!r} prices the {price_provider!r} sheet -- pass "
            f"--price-key openai/gpt-4o-mini (or another openai/* key) to match, or wait "
            f"for a {price_provider} live adapter."
        )


def _savings_comparison(
    online_node_ids: set[str],
    batch_node_ids: set[str],
    online_total_usd: float,
    batch_total_usd: float,
) -> tuple[float | None, float | None, str | None]:
    """Only compare dollars across tiers when both ran the identical
    (billable, non-errored) node set. A billed error on one side and not the
    other makes a plain ``saved_usd`` delta meaningless -- worse, silently
    wrong in the direction that flatters whichever side happened to fail,
    since a dropped node just looks like a cheaper run. Per-side bills are
    always real and are printed regardless; only the cross-tier "saved"
    figure is gated.
    """
    if not online_node_ids or online_node_ids != batch_node_ids:
        warning = (
            "online and lazycode_batch did NOT complete the identical node set -- "
            f"online: {sorted(online_node_ids)}, batch: {sorted(batch_node_ids)}. "
            "Printing the per-side bills below; refusing the savings comparison."
        )
        return None, None, warning
    saved = online_total_usd - batch_total_usd
    saved_pct = (saved / online_total_usd * 100) if online_total_usd else None
    return saved, saved_pct, None


def run_cost_report(
    task_name: str,
    *,
    fixture: dict[str, Any] | None = None,
    fixture_path: Path | None = None,
    model: str = DEFAULT_MODEL,
    price_key: str = DEFAULT_PRICE_KEY,
    stack_cache: bool = False,
    live: bool = False,
    yes_spend_real_money: bool = False,
    write_results: bool = True,
) -> dict[str, Any]:
    """Run the workload on both tiers, price both, and return the report dict.

    ``live=True`` spends real, billed money and is gated three ways, all
    enforced here (not only by the CLI, so library callers cannot bypass
    them): an explicit ``yes_spend_real_money=True`` acknowledgment,
    ``OPENAI_API_KEY`` in the environment, and ``model``/``price_key``
    consistent with the (OpenAI-only) live adapter (:func:`_require_live_consistency`).
    """
    if live:
        if not yes_spend_real_money:
            raise RuntimeError(
                "live=True spends real, billed money against a real provider; pass "
                "yes_spend_real_money=True to acknowledge (CLI: --yes-spend-real-money)."
            )
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("--live needs OPENAI_API_KEY set (and spends real money)")
        _require_live_consistency(model, price_key)
    if fixture is None:
        if fixture_path is None:
            default = Path(__file__).parent / "tasks" / task_name / "mock_fixture.json"
            if not default.is_file():
                raise ValueError(
                    f"task {task_name!r} has no committed mock_fixture.json; pass fixture_path="
                )
            fixture_path = default
        fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))

    # ONE plan, used by both tiers: the comparison isolates the execution tier,
    # not the planner. (Under --live this also avoids paying to plan twice.)
    plan = Plan.model_validate(fixture["planner_response"])

    if live:  # pragma: no cover - never exercised by tests
        batch_adapter, realtime = _live_adapters(model)
        batch_factory = lambda: batch_adapter  # noqa: E731
        online_factory = lambda: SequentialOnlineAdapter(realtime)  # noqa: E731
    else:
        batch_factory = lambda: mock_provider.FixtureBatchAdapter(fixture)  # noqa: E731
        online_factory = lambda: SequentialOnlineAdapter(_fixture_realtime(fixture))  # noqa: E731

    online_meter, online_outcome = _run_side(
        task_name, tier="online", adapter_factory=online_factory, model=model,
        plan=plan, provider_key="online",
    )
    batch_meter, batch_outcome = _run_side(
        task_name, tier="batch", adapter_factory=batch_factory, model=model,
        plan=plan, provider_key="batch",
    )

    # Reconcile the heuristic prefix/unique split against provider-reported
    # usage (live mode only; dry-run keeps the heuristic unchanged).
    online_meter.ledgers, online_split_label = reconcile_ledgers(online_meter.ledgers, live=live)
    batch_meter.ledgers, batch_split_label = reconcile_ledgers(batch_meter.ledgers, live=live)

    rate = rates(price_key)
    online_cost = price_online(online_meter.ledgers, rate)
    batch_cost = price_batch(batch_meter.ledgers, rate, stack_cache=stack_cache)
    online_cost.notes.append(online_split_label)
    batch_cost.notes.append(batch_split_label)
    if online_cost.errored_calls:
        online_cost.notes.append(f"{online_cost.errored_calls} errored call(s) included in the bill")
    if batch_cost.errored_calls:
        batch_cost.notes.append(f"{batch_cost.errored_calls} errored call(s) included in the bill")

    # Quality equivalence: same model, same params, temperature 0, and the two
    # runs are compared response-hash by response-hash per node. Errored calls
    # are excluded -- there is no "response" to compare, only a bill.
    online_by_node = {lg.node_id: lg.response_sha for lg in online_meter.ledgers if not lg.errored}
    batch_by_node = {lg.node_id: lg.response_sha for lg in batch_meter.ledgers if not lg.errored}
    shared = sorted(set(online_by_node) & set(batch_by_node))
    matches = sum(1 for n in shared if online_by_node[n] == batch_by_node[n])
    match_rate = matches / len(shared) if shared else None

    widths = wave_widths(batch_meter.ledgers)
    saved, saved_pct, comparison_warning = _savings_comparison(
        set(online_by_node), set(batch_by_node), online_cost.total_usd, batch_cost.total_usd
    )
    report: dict[str, Any] = {
        "task": task_name,
        "mode": "live" if live else "dry-run (mock providers, $0 spent)",
        "model": model,
        "price_key": price_key,
        "price_rates_usd_per_mtok": rate,
        "stack_cache": stack_cache,
        "online": {**asdict(online_cost), "total_usd": online_cost.total_usd,
                   "input_usd": online_cost.input_usd, **online_outcome},
        "lazycode_batch": {**asdict(batch_cost), "total_usd": batch_cost.total_usd,
                           "input_usd": batch_cost.input_usd, **batch_outcome},
        "saved_usd": saved,
        "saved_pct": saved_pct,
        "comparison_warning": comparison_warning,
        "quality": {
            "same_model": True,
            "temperature": 0.0,
            "nodes_compared": len(shared),
            "output_match_rate": match_rate,
            "note": (
                "exact by construction: both tiers replay the same committed fixture "
                "responses, so a 1.0 match rate is a plumbing check, not evidence about "
                "real model quality"
            )
            if not live
            else "real provider responses compared verbatim",
        },
        "input_factor": {
            "wave_widths": widths,
            "observed_batch_prefix_factor": observed_input_factor(batch_cost, rate),
            "predicted_min_0p5_0p05_plus_0p95_over_N": [predicted_input_factor(n) for n in widths],
            "observed_online_prefix_factor": observed_input_factor(online_cost, rate),
        },
        "what_ten_dollars_buys": _ten_dollars(online_cost.total_usd, batch_cost.total_usd),
        "disclaimer": (
            "Token counts are METERED from the two real runs; dollars are PRICED from the "
            "published list sheet above. Under the mock providers this is a $0 dry run: the "
            "dollar column is arithmetic over a metered ledger, not a provider invoice."
        ),
    }
    if write_results:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / f"{task_name}-cost-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
    return report


def _ten_dollars(online_usd: float, batch_usd: float, budget: float = 10.0) -> dict[str, Any]:
    if online_usd <= 0 or batch_usd <= 0:
        return {}
    online_units = budget / online_usd
    batch_units = budget / batch_usd
    return {
        "budget_usd": budget,
        "workload_units_online_only": round(online_units, 2),
        "workload_units_lazycode_batch": round(batch_units, 2),
        "multiplier": round(batch_units / online_units, 4),
        "cost_of_the_online_units_via_batch_usd": round(online_units * batch_usd, 4),
    }


# --- rendering ---------------------------------------------------------------


def render_report(report: dict[str, Any]) -> str:
    on = report["online"]
    lc = report["lazycode_batch"]
    rate = report["price_rates_usd_per_mtok"]
    tdb = report["what_ten_dollars_buys"]
    fac = report["input_factor"]

    def money(x: float) -> str:
        return f"${x:,.6f}"

    on_cache = f"{on['cache_writes']} / {on['cache_reads']}"
    lc_cache = f"{lc['cache_writes']} / {lc['cache_reads']}"

    lines = [
        f"lazycode cost report — task={report['task']}  model={report['model']}",
        f"mode: {report['mode']}",
        f"prices: {report['price_key']} @ in ${rate['input']}/Mtok, cached ${rate['cached_input']}, "
        f"write(5m) ${rate['cache_write_5m']}, write(1h) ${rate['cache_write_1h']}, "
        f"batch-in ${rate['batch_input']}, out ${rate['output']}, "
        f"batch-out ${rate['batch_output']} (per Mtok)",
        "",
        f"{'':<26}{'online-only':>16}{'lazycode batch':>16}",
        "-" * 58,
        f"{'model calls':<26}{on['calls']:>16}{lc['calls']:>16}",
        f"{'  of which errored':<26}{on['errored_calls']:>16}{lc['errored_calls']:>16}",
        f"{'waves / submissions':<26}{on['waves']:>16}{lc['waves']:>16}",
        f"{'prefix tokens (shared)':<26}{on['prefix_tokens']:>16,}{lc['prefix_tokens']:>16,}",
        f"{'unique input tokens':<26}{on['unique_tokens']:>16,}{lc['unique_tokens']:>16,}",
        f"{'output tokens':<26}{on['output_tokens']:>16,}{lc['output_tokens']:>16,}",
        f"{'cache writes / reads':<26}{on_cache:>16}{lc_cache:>16}",
        "-" * 58,
        f"{'input $':<26}{money(on['input_usd']):>16}{money(lc['input_usd']):>16}",
        f"{'output $':<26}{money(on['output_usd']):>16}{money(lc['output_usd']):>16}",
        f"{'TOTAL $':<26}{money(on['total_usd']):>16}{money(lc['total_usd']):>16}",
        "-" * 58,
        (
            f"saved: {money(report['saved_usd'])}  ({report['saved_pct']:.1f}%)"
            if report["comparison_warning"] is None
            else f"saved: n/a -- WARNING: {report['comparison_warning']}"
        ),
        "",
        f"online notes: {'; '.join(on['notes'])}",
        f"batch notes:  {'; '.join(lc['notes'])}",
        "",
        "what $10 buys (one 'unit' = one full run of this task):",
        f"  online-only      : {tdb['workload_units_online_only']:>8} units",
        f"  lazycode (batch) : {tdb['workload_units_lazycode_batch']:>8} units   "
        f"({tdb['multiplier']:.2f}x more work per dollar)",
        f"  the {tdb['workload_units_online_only']} online units cost "
        f"{money(tdb['cost_of_the_online_units_via_batch_usd'])} through the batch tier",
        "",
        "per-item input factor on the shared prefix (sec. 'Width changes the condition'):",
        f"  wave widths N            : {fac['wave_widths']}",
        f"  predicted min(0.5, 0.05+0.95/N): "
        f"{[round(x, 4) for x in fac['predicted_min_0p5_0p05_plus_0p95_over_N']]}",
        f"  observed (batch)         : {fac['observed_batch_prefix_factor']:.4f}"
        if fac["observed_batch_prefix_factor"] is not None
        else "  observed (batch)         : n/a",
        f"  observed (online cached) : {fac['observed_online_prefix_factor']:.4f}"
        if fac["observed_online_prefix_factor"] is not None
        else "  observed (online cached) : n/a",
        "",
        f"quality: {report['quality']['nodes_compared']} nodes compared, "
        f"match rate {report['quality']['output_match_rate']}, temperature "
        f"{report['quality']['temperature']}, same model both sides",
        f"         {report['quality']['note']}",
        "",
        report["disclaimer"],
    ]
    return "\n".join(lines)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("task", choices=list_tasks())
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--price-key", default=DEFAULT_PRICE_KEY, choices=sorted(price_sheet()),
        help="which published price sheet to price the metered ledger against",
    )
    parser.add_argument("--fixture", type=Path, default=None, help="mock fixture JSON")
    parser.add_argument(
        "--stack-cache", action="store_true",
        help="also apply prompt-cache rates inside batch waves (UNVERIFIED that the "
             "batch discount stacks with cache line items -- off by default)",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="REAL MONEY: run both tiers against OpenAI (needs OPENAI_API_KEY, an explicit "
             "--model, and an openai/* --price-key). Off by default.",
    )
    parser.add_argument("--yes-spend-real-money", action="store_true", help="required with --live")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the text report")
    args = parser.parse_args()

    if args.live and not args.yes_spend_real_money:
        raise SystemExit(
            "--live runs REAL, BILLED requests against OpenAI (batch + chat completions).\n"
            "Re-run with --yes-spend-real-money if that is what you want."
        )

    # yes_spend_real_money is threaded through to run_cost_report() itself
    # (not just checked here) so the acknowledgment gate holds for library
    # callers too, not only this CLI entry point.
    report = run_cost_report(
        args.task,
        fixture_path=args.fixture,
        model=args.model,
        price_key=args.price_key,
        stack_cache=args.stack_cache,
        live=args.live,
        yes_spend_real_money=args.yes_spend_real_money,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str) if args.json else render_report(report))


if __name__ == "__main__":
    _main()
