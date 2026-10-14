# Run a matched replay on original synthetic inputs

From the repository root after `uv sync --extra dev --frozen`:

```bash
uv run python scripts/replay_events.py \
  --candles examples/event_replay/candles.csv \
  --decisions examples/event_replay/decisions.csv \
  --prices examples/event_replay/prices.csv \
  --outcomes examples/event_replay/outcomes.csv \
  --evaluated-at 2026-01-01T12:21:00Z
```

These CSVs are constructed examples, not downloaded trading evidence. The actual
library audits the selected token and candle at each decision, admits three of
four entries, and replays HOLD and CONFIGURED_EXIT on that same set.

| Decision | What you can inspect |
|---|---|
| take-profit | UP entry at0.50, observed exit at0.65; holding would lose at settlement. |
| stop-loss | DOWN entry at0.70, observed exit at0.60; holding would win at settlement. |
| no-path | No post-entry print before market close. The0.99 print stamped at settlement is excluded. |
| no-entry | DOWN has no eligible own-token observation inside its window. UP's print does not substitute for it. |

Try `--max-hold-seconds 20`: the first two positions still hit their price triggers
before the timer; no-path becomes MAX_HOLD_NO_PRICE and settles on its supplied
outcome clock. With `--evaluated-at 2026-01-01T12:10:00Z`, only the first resolved
market can be scored. Inspect `decisions` to see why later rows are excluded.

Change a price or the fee coefficient in a copy of the inputs to see the computed
cash decomposition change. `--out new-report.json` writes a new file; reuse of an
existing path is refused. A malformed price or contradictory candle clock exits2.
Correct the input and rerun to a new output path; source inputs are never rewritten.

Read [the contracts, accounting and limits](../../docs/EVENT_REPLAY.md). No model
is fitted or selected here; these prespecified entries demonstrate the replay engine.
