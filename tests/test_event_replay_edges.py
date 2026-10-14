from __future__ import annotations

import pandas as pd
import pytest

from polymarket_crypto_5min.event_replay import compare_replays
from polymarket_crypto_5min.exit_backtest import ExitPolicy, simulate_exit_policy


def inputs():
    start = pd.Timestamp("2026-01-01T12:00:00Z")
    end = start + pd.Timedelta(minutes=5)
    candles = pd.DataFrame({"ts": [start], "close": [100.0]})
    decisions = pd.DataFrame(
        [
            {
                "decision_id": "d",
                "condition_id": "m",
                "side": "UP",
                "start_dt": start,
                "snapshot_dt": start + pd.Timedelta(seconds=15),
                "end_dt": end,
                "up_asset_id": "up",
                "down_asset_id": "down",
            }
        ]
    )
    prices = pd.DataFrame(
        {
            "condition_id": ["m", "m"],
            "asset_id": ["up", "up"],
            "ts": [start + pd.Timedelta(seconds=10), start + pd.Timedelta(seconds=25)],
            "p": [0.5, 0.65],
        }
    )
    outcomes = pd.DataFrame([{"condition_id": "m", "realized_direction": "DOWN", "outcome_available_at": end}])
    return candles, decisions, prices, outcomes, end


def test_empty_decisions_emit_empty_paired_reports():
    candles, decisions, prices, outcomes, end = inputs()
    report = compare_replays(candles, decisions.iloc[:0], prices, outcomes, evaluated_at=end)
    assert report["summary"] == {"decisions": 0, "admitted_entries": 0, "excluded_entries": 0}
    assert report["trades"] == report["matched_deltas"] == []
    assert [row["trades"] for row in report["strategies"]] == [0, 0]


def test_decision_auxiliary_labels_cannot_shadow_outcome_source():
    candles, decisions, prices, outcomes, end = inputs()
    decisions = decisions.assign(realized_direction="UP", outcome_available_at=decisions["start_dt"])
    report = compare_replays(candles, decisions, prices, outcomes, evaluated_at=end)
    assert report["summary"]["admitted_entries"] == 1
    assert report["trades"][0]["exit_price"] == 0.0


def test_invalid_print_timestamp_is_not_silently_filtered():
    candles, decisions, prices, outcomes, end = inputs()
    prices = pd.concat([prices, prices.iloc[[0]].assign(ts=pd.NaT)], ignore_index=True)
    with pytest.raises(ValueError, match="ts"):
        compare_replays(candles, decisions, prices, outcomes, evaluated_at=end)


def test_conflicting_candles_at_one_availability_timestamp_fail():
    candles, decisions, prices, outcomes, end = inputs()
    candles = pd.concat([candles, candles.assign(close=999.0)], ignore_index=True)
    with pytest.raises(ValueError, match="conflicting"):
        compare_replays(candles, decisions, prices, outcomes, evaluated_at=end)


def test_future_decision_has_no_future_observation_in_ledger():
    candles, decisions, prices, outcomes, end = inputs()
    future = decisions.loc[0, "snapshot_dt"] - pd.Timedelta(seconds=1)
    candles = pd.concat([candles, candles.assign(ts=decisions.loc[0, "snapshot_dt"], close=999)], ignore_index=True)
    report = compare_replays(candles, decisions, prices, outcomes, evaluated_at=future)
    assert report["decisions"][0]["replay_status"] == "DECISION_AFTER_AS_OF"
    assert report["decisions"][0]["candle_available_at"] <= future
    assert report["trades"] == []


@pytest.mark.parametrize("value", [float("nan"), -0.1])
def test_exit_fee_extension_rejects_invalid_entry_fee(value):
    _, decisions, _, _, _ = inputs()
    entries = decisions.assign(market_price=0.5, won=False)
    history = pd.DataFrame(columns=["condition_id", "asset_id", "ts", "p"])
    with pytest.raises(ValueError, match="entry_fee_rate"):
        simulate_exit_policy(entries, history, ExitPolicy(), entry_fee_rate=value)
