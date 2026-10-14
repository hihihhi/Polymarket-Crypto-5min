# Offline input fixture

These small CSVs are hand-authored synthetic inputs for the actual audit command.
They exercise candle completion, decision ordering, missing/stale data and independent token histories.
They are neither exchange observations nor strategy-performance evidence.

Run from the repository root:

```bash
uv run python scripts/audit_snapshots.py \
  --candles examples/snapshot_audit/candles.csv \
  --decisions examples/snapshot_audit/decisions.csv \
  --prices examples/snapshot_audit/prices.csv \
  --max-candle-age-seconds 30
```

Expected summary: four decisions, eight side rows, two usable and six blocked.
At12:04:15 the UP print is0.60, the future0.99 print is excluded, and DOWN has no eligible print.
At12:04:59 both own-token prints are known, but the59-second-old candle exceeds the requested
30-second freshness policy. At12:02:59 no supplied candle is available. At12:04:00 a candle
and UP print are usable exactly at the availability boundary. Input order is retained in decision_row.

Use the [CSV contract](../../docs/SNAPSHOT_AUDIT.md) to audit your own saved observations.
