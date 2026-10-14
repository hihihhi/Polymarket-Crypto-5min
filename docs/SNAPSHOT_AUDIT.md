# Audit saved observations at your decision times

Before running a backtest, inspect which BTC candle and which UP/DOWN token print can be used at
each decision. `scripts/audit_snapshots.py` reads local CSVs and calls the same `asof_candle` and
`market_price_asof` functions used by feature construction. It performs no download or strategy search.

```bash
uv sync --extra dev --frozen
uv run python scripts/audit_snapshots.py \
  --candles examples/snapshot_audit/candles.csv \
  --decisions examples/snapshot_audit/decisions.csv \
  --prices examples/snapshot_audit/prices.csv \
  --max-candle-age-seconds 30
```

JSON goes to stdout; `--out audit.json` additionally writes a **new** file in an existing directory.
An existing output or a path equal to any input is refused. Invalid input exits2 without writing a
new report. A valid audit exits0 even when some rows are blocked: missing observations are findings
to investigate, not command failures. The example contains original synthetic CSVs; replace paths
with your own saved data to use the tool.

## Input contracts

| CSV | Required columns | Meaning |
|---|---|---|
| candles | ts, close, timestamp_semantics | ts is when the final close becomes available; semantics must be close_available_at or the documented migrated-legacy value on every row. |
| decisions | condition_id, start_dt, end_dt, snapshot_dt, up_asset_id, down_asset_id | Each decision is inside its window: start_dt <= snapshot_dt < end_dt. UP/DOWN IDs must be distinct and nonempty. Rows may be out of order. |
| prices | condition_id, asset_id, ts, p | Historical own-token prints, with numeric p in [0,1]. Conditions and token IDs must match the decision. |

Supply ISO8601 timestamps with an explicit UTC offset; UTC is used internally. The underlying
CSV loaders also accept the existing legacy Binance symbol/interval/OHLCV schema and shift its open
timestamps to completion. See features.load_candles and the existing backtest documentation.

If a candle includes close_ts, the loader now checks availability >= close_ts +1ms, using the
repository's established Binance clock convention. A supplied completion clock cannot be
overridden by a contradictory close_available_at declaration. This checks internal consistency,
not the truth of the vendor's timestamp or delivery latency. A file without close_ts has only a
declared availability clock; the report explicitly records that weaker evidence.

The audit uses one snapshot candle per decision. A complete strategy feature row additionally
requires start/one-minute/three-minute history and settled labels; this report does not establish
their availability or train/test safety. Candle values must be finite and positive. Token prices
must be finite and within [0,1]. Freshness limits must be finite and nonnegative.

## Read the report

Each decision generates independent UP and DOWN rows in input order:

| Field/status | Interpretation |
|---|---|
| candle_available_at / candle_age_seconds | Timestamp and age of the latest candle known at the decision. |
| FRESH / STALE / MISSING | Candle meets the selected age bound, exceeds it, or has no prior observation. |
| print_time / print_price / print_age_seconds | The eligible own-token print; null if none is eligible. |
| ELIGIBLE / NO_ELIGIBLE_PRINT | Own-token print exists inside the market window, at or before the decision, within the selected age bound; otherwise unavailable. |
| USABLE / BLOCKED | Snapshot candle and print satisfy these input-time requirements, or at least one does not. |

Missing DOWN evidence is never filled as1-UP. Later prints and the final future candle do not
enter an earlier decision. Defaults are60seconds for both freshness bounds; the fixture requests
30seconds for candles to show how the declared policy changes eligibility.

For the fixture, expect:

```json
{"decisions": 4, "side_rows": 8, "usable": 2, "blocked": 6}
```

The report captures each CSV once and parses/hashes those exact bytes. It records input filenames,
SHA256 hashes, raw row counts, loaded/discarded candle
counts, clock-evidence class, selected limits and each joined timestamp/value. Keep input files
immutable while reading. It is an in-memory audit intended for small/local investigations, without
uniform throughput or process-memory claims.

## What the audit does not establish

A historical print is not an executable ask, order-book depth, fill or payout. No model, calibration,
strategy choice, order or PnL is generated. USABLE is an input-time status, not a recommendation.
The dated July results in docs/results.md remain unchanged and predate later timing fixes; their
inputs are missing, so this fixture does not reproduce or validate those returns.
See [verification scope](VERIFICATION.md) for the tested source and release limits.

## Evaluate the checks

```bash
uv run python scripts/evaluate_snapshot_audit.py
```

The evaluation first runs the19 focused cases against a copied control. Six independent source
mutants then disable clock refusal, drop per-row semantic validation, join future candles, reuse
UP prints for DOWN, remove print-age limits, and hash changed files after loading. Each must fail
its named witness in a disposable offline copy. Actual source and test files are never mutated.
Use `--out-dir new-directory` to keep the full logs and receipt. This evaluates correctness and
provenance, not alpha, fill realism or model quality.
