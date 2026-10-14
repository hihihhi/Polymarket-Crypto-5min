# Verification scope

This review candidate carries the accepted offline source from
`43be51dd9f8fff49e4a56e84ca9b60f3622b0056`. Publication of a review branch is not
approval to merge or evidence of executable trading results.

On 14 October 2026, the local author checks and an independent verification
passed the original quality commands on Python 3.11: Ruff, formatting, mypy and
112 tests. The unchanged replay evaluation passed 480 independent scalar/Decimal
cases, a control with 48 cases and 10 deliberate source mutations; the audit evaluation
passed a control with 19 cases and 6 mutations. A fresh static source review returned
ADVANCE. Independent verification also ran the documented CLI through valid input, naive-clock
refusal and recovery (exit 0/2/0) with exact captured-input hashes. These are local
algorithm/input-contract checks, not remote-CI or release certification.

Reproduce the original checks without market data, credentials or orders:

```bash
uv sync --extra dev --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy polymarket_crypto_5min scripts tests
uv run pytest -q
uv run python scripts/evaluate_snapshot_audit.py
uv run python scripts/evaluate_event_replay.py
```

[Replay inputs and accounting](EVENT_REPLAY.md) and [snapshot audit contracts](SNAPSHOT_AUDIT.md)
explain the models and runnable synthetic examples. Every mutation must fail its
named witness while the copied control passes; an import failure does not count.
The supplied examples are original synthetic fixtures, not exchange observations.

The historical July results, withdrawn positive result, negative corrected result,
trial exposure and missing original inputs remain unchanged. The later clock/timer
repairs do not revalidate those results. No new market study or strategy sweep was
performed for this review candidate. Slippage, depth, latency, shared capital,
venue rounding/rebates and actual redemption timing remain unmodeled.

The original quality workflow and predecessor test assertions remain intact.
Remote CI, merge and publication status must be assessed on the exact PR head;
local evidence alone does not settle them. Automatic research-workflow policy is
held for a specific decision before opening the PR, because the existing triggers
would start live-data/full-history research. Local coordination records and machine
paths are excluded from this public candidate.
