# LazyCode — preprint / venue submission package

**PUBLISHED preprint: DOI [10.5281/zenodo.21905469](https://doi.org/10.5281/zenodo.21905469) (Zenodo, 2026-08-12, CC BY 4.0). Publish gate below: SATISFIED (repo public since 2026-08-12).**

Paste-ready metadata for a preprint server (Zenodo; TechRxiv when it reopens).
Keep in sync with `latex/main.tex`.

**GATE BEFORE PUBLISHING: the lazycode repo/branches are local-only, but the
paper's title-page footnote already links github.com/rajagurunath/lazycode.
The repo MUST be pushed and public before this preprint goes out, or the
footnote must be removed and the PDF rebuilt.**

## Files

| Purpose | File |
|---|---|
| Preprint PDF (upload this) | `docs/paper/lazycode-paper.pdf` (20 pp — current draft with §Measured economics; Zenodo still serves v1 until the next version is published) |
| LaTeX sources | `docs/paper/latex/` |

## Title

LazyCode: Compiling Interactive Intent into Batch Execution — A
Query-Optimizer Architecture, Its Economics, and the Limits of Overnight
Coding Agents

## Author

Gurunath Lunkupali Venugopal — gurunathrajagopal@gmail.com

## Abstract (plain text)

Coding agents call models the way a person would: one synchronous request at a
time, at the full realtime price. But much of what agents do best — backlog
burndown, migrations, test-coverage pushes — has nobody waiting, and every
major provider already sells the product for that shape: a batch API, 24-hour
window, half price. LazyCode compiles deferrable model calls onto that tier. A
realtime model plans; the plan then runs overnight as barrier-synchronized
waves of self-sufficient batch calls, and the barriers, memoization and
three-window crash recovery are what make paying a provider unattended safe.
The design transplants database query processing: operator algebra, rewrite
rules, physical waves, a write-ahead log. We separate what is implemented
(18,415 LOC, 413 default-suite tests) from what is merely designed or
vocabulary. On cost, batch's ~50% discount on output tokens is unconditional;
against a prompt-cached interactive baseline it wins on input only when T
interactive turns compile to R < T/5 batch rounds. Where batch and cache
discounts stack — provider-dependent, unverified here — width relaxes that
condition, reaching R < 2T only in the N → ∞ limit. One small upstream change
— deferred model requests — is what blocks durable-execution frameworks from
treating model calls like deferred tools.

## Keywords

LLM agents; batch API; query optimization; agent frameworks; prompt caching;
cost model; durable execution; crash safety; coding agents

## Subject categories

- Preprint servers: Computer Science → Software Engineering / Databases
  (arXiv: cs.SE; secondary cs.DB, cs.AI)

## License (preprint)

CC BY 4.0

## Related identifiers

- Companion paper: Tidal — Co-Serving Online and Batch LLM Traffic under Deadline
  Serving Engines, https://github.com/rajagurunath/tidal (add Tidal's DOI once
  it has one)
- Code: add repo URL only after the repo is pushed/public (see gate above)

## Notes / disclosures

- No funding; no conflicts of interest.
- Venue fit: weaker match for IC2E/CLOUD than Tidal (design + economics paper;
  measured real-provider benchmark is the named next step). Reasonable
  targets after the live cost benchmark: workshop tracks co-located with
  systems/SE venues, or resubmit alongside measured results.
