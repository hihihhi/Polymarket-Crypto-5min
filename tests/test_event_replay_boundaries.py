from __future__ import annotations

import pandas as pd
import pytest

from polymarket_crypto_5min.exit_backtest import ExitPolicy, simulate_exit_policy

START = pd.Timestamp("2026-01-01T12:00:00Z")
END = START + pd.Timedelta(minutes=5)


def run(entry_price: float, points: list[tuple[int, float]], policy: ExitPolicy) -> pd.Series:
    entries = pd.DataFrame(
        [
            {
                "condition_id": "m",
                "side": "UP",
                "snapshot_dt": START,
                "end_dt": END,
                "market_price": entry_price,
                "up_asset_id": "up",
                "down_asset_id": "down",
                "won": False,
            }
        ]
    )
    prices = pd.DataFrame(
        {
            "condition_id": ["m"] * len(points),
            "asset_id": ["up"] * len(points),
            "ts": [START + pd.Timedelta(seconds=second) for second, _ in points],
            "p": [value for _, value in points],
        }
    )
    return simulate_exit_policy(entries, prices, policy).iloc[0]


@pytest.mark.parametrize("seconds", [300, 600])
def test_timer_at_or_after_settlement_is_a_hold(seconds: int) -> None:
    policy = ExitPolicy(take_profit=None, target_price=None, stop_loss=None, max_hold_seconds=seconds)
    trade = run(0.5, [(300, 0.99)], policy)
    assert trade["exit_reason"] == "HOLD_TO_SETTLEMENT"
    assert trade["exit_dt"] == END
    assert trade["exit_price"] == 0.0
    assert trade["realized_settlement"]


@pytest.mark.parametrize(
    ("entry", "price", "policy", "reason"),
    [
        (0.7, 0.65, ExitPolicy(take_profit=None, target_price=None, stop_loss=0.05), "STOP_LOSS"),
        (0.2, 0.3, ExitPolicy(take_profit=0.1, target_price=None, stop_loss=None), "TAKE_PROFIT"),
    ],
)
def test_decimal_threshold_triggers_on_exact_price(entry: float, price: float, policy: ExitPolicy, reason: str) -> None:
    trade = run(entry, [(5, price), (25, price)], policy)
    assert trade["exit_reason"] == reason
    assert trade["exit_dt"] == START + pd.Timedelta(seconds=5)
    assert trade["exit_price"] == price


@pytest.mark.parametrize(
    ("entry", "price", "policy"),
    [
        (0.7, 0.6500000000000001, ExitPolicy(take_profit=None, target_price=None, stop_loss=0.05)),
        (0.2, 0.29999999999999993, ExitPolicy(take_profit=0.1, target_price=None, stop_loss=None)),
    ],
)
def test_decimal_threshold_does_not_round_nearby_price_into_trigger(
    entry: float, price: float, policy: ExitPolicy
) -> None:
    trade = run(entry, [(5, price)], policy)
    assert trade["exit_reason"] == "HOLD_TO_SETTLEMENT"
    assert trade["exit_dt"] == END
