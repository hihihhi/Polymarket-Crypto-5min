from __future__ import annotations

import importlib
import json
import math
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from polymarket_crypto_5min.exit_backtest import MAX_HOLD_NO_PRICE, ExitPolicy, simulate_exit_policy

START = pd.Timestamp("2026-01-01T12:00:00Z")
END = START + pd.Timedelta(minutes=5)


def entry() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "condition_id": "m",
                "side": "UP",
                "snapshot_dt": START,
                "end_dt": END,
                "market_price": 0.5,
                "up_asset_id": "up",
                "down_asset_id": "down",
                "won": False,
            }
        ]
    )


def prints(seconds: list[float], values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "condition_id": ["m"] * len(seconds),
            "asset_id": ["up"] * len(seconds),
            "ts": [START + pd.Timedelta(seconds=s) for s in seconds],
            "p": values,
        }
    )


def max_hold() -> ExitPolicy:
    return ExitPolicy(take_profit=None, target_price=None, stop_loss=None, max_hold_seconds=20)


def test_exit_engine_max_hold_waits_for_first_observation_after_deadline() -> None:
    result = simulate_exit_policy(entry(), prints([5, 25, 30], [0.55, 0.6, 0.7]), max_hold())
    assert result.loc[0, "exit_dt"] == START + pd.Timedelta(seconds=25)
    assert result.loc[0, "exit_price"] == 0.6
    assert result.loc[0, "exit_reason"] == "MAX_HOLD_EXIT"


def test_exit_engine_timer_includes_exact_deadline() -> None:
    result = simulate_exit_policy(entry(), prints([5, 20, 25], [0.55, 0.6, 0.7]), max_hold())
    assert result.loc[0, "exit_dt"] == START + pd.Timedelta(seconds=20)
    assert result.loc[0, "exit_price"] == 0.6


def test_exit_engine_prior_print_does_not_invent_deadline_execution() -> None:
    result = simulate_exit_policy(entry(), prints([5, 300], [0.55, 0.9]), max_hold())
    assert result.loc[0, "exit_reason"] == MAX_HOLD_NO_PRICE
    assert result.loc[0, "exit_dt"] == END
    assert result.loc[0, "exit_price"] == 0.0


def test_exit_engine_price_trigger_before_timer_keeps_priority() -> None:
    policy = ExitPolicy(take_profit=0.1, target_price=None, stop_loss=None, max_hold_seconds=20)
    result = simulate_exit_policy(entry(), prints([5, 25], [0.65, 0.7]), policy)
    assert result.loc[0, "exit_dt"] == START + pd.Timedelta(seconds=5)
    assert result.loc[0, "exit_reason"] == "TAKE_PROFIT"


def test_exit_engine_explicit_fee_matches_scalar_cash_accounting() -> None:
    policy = ExitPolicy(take_profit=0.1, target_price=None, stop_loss=None, exit_fee_rate=0.04)
    result = simulate_exit_policy(entry(), prints([25], [0.65]), policy, entry_fee_rate=0.02, stake_usdc=10)
    entry_fee = 0.02 * 0.5 * 0.5
    exit_fee = 0.04 * 0.65 * 0.35
    shares = 10 / (0.5 + entry_fee)
    assert math.isclose(result.loc[0, "shares"], shares, abs_tol=1e-12)
    assert math.isclose(result.loc[0, "pnl_usdc"], shares * (0.65 - 0.5 - entry_fee - exit_fee), abs_tol=1e-12)
    assert result.loc[0, "entry_fee_per_share"] == entry_fee


def replay_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    candles = pd.DataFrame({"ts": [START], "close": [100.0]})
    decisions = pd.DataFrame(
        [
            {
                "decision_id": "d",
                "condition_id": "m",
                "side": "UP",
                "start_dt": START,
                "snapshot_dt": START + pd.Timedelta(seconds=15),
                "end_dt": END,
                "up_asset_id": "up",
                "down_asset_id": "down",
            }
        ]
    )
    prices = prints([10, 25, 30, 300], [0.5, 0.65, 0.7, 0.99])
    outcomes = pd.DataFrame(
        [{"condition_id": "m", "realized_direction": "DOWN", "outcome_available_at": END + pd.Timedelta(seconds=30)}]
    )
    return candles, decisions, prices, outcomes


def compare(*args, **kwargs):
    module = importlib.import_module("polymarket_crypto_5min.event_replay")
    return module.compare_replays(*args, **kwargs)


def test_comparison_uses_matched_entries_and_exposes_cost_decomposition() -> None:
    report = compare(*replay_inputs(), evaluated_at=END + pd.Timedelta(minutes=1))
    assert report["summary"]["admitted_entries"] == 1
    assert report["summary"]["excluded_entries"] == 0
    trades = pd.DataFrame(report["trades"])
    assert trades["strategy"].tolist() == ["HOLD", "CONFIGURED_EXIT"]
    assert trades["decision_id"].tolist() == ["d", "d"]
    assert trades["entry_price"].tolist() == [0.5, 0.5]
    assert trades["exit_reason"].tolist() == ["HOLD_TO_SETTLEMENT", "TAKE_PROFIT"]
    assert trades.loc[0, "exit_dt"] == END + pd.Timedelta(seconds=30)
    assert trades.loc[1, "exit_dt"] == START + pd.Timedelta(seconds=25)
    for trade in report["trades"]:
        assert math.isclose(trade["gross_pnl_usdc"] - trade["modeled_fees_usdc"], trade["pnl_usdc"], abs_tol=1e-12)
    assert report["matched_deltas"][0]["decision_id"] == "d"
    assert report["assumptions"]["strategy_selection"] == "NONE_FIXED_POLICIES"


def test_outcome_after_evaluation_is_excluded_from_both_strategies() -> None:
    report = compare(*replay_inputs(), evaluated_at=END)
    assert report["trades"] == []
    assert report["decisions"][0]["replay_status"] == "OUTCOME_NOT_AVAILABLE_AS_OF"
    assert report["summary"]["admitted_entries"] == 0


def test_early_price_trigger_is_invariant_to_settlement_winner() -> None:
    data = replay_inputs()
    first = compare(*data, evaluated_at=END + pd.Timedelta(minutes=1))
    changed = data[3].assign(realized_direction="UP")
    second = compare(*data[:3], changed, evaluated_at=END + pd.Timedelta(minutes=1))
    for field in ["entry_price", "exit_price", "exit_dt", "exit_reason", "pnl_usdc"]:
        assert first["trades"][1][field] == second["trades"][1][field]


def test_unusable_snapshot_remains_in_exclusion_ledger() -> None:
    candles, decisions, prices, outcomes = replay_inputs()
    report = compare(
        candles, decisions, prices, outcomes, evaluated_at=END + pd.Timedelta(minutes=1), max_price_age_seconds=1
    )
    assert report["trades"] == []
    assert report["decisions"][0]["replay_status"] == "INPUT_TIME_BLOCKED"
    assert report["summary"]["excluded_entries"] == 1


@pytest.mark.parametrize("field,value", [("stake_usdc", 0), ("fee_rate", float("nan")), ("fee_rate", -0.1)])
def test_invalid_cost_assumptions_fail(field, value) -> None:
    with pytest.raises(ValueError, match=field):
        compare(*replay_inputs(), evaluated_at=END + pd.Timedelta(minutes=1), **{field: value})


def test_conflicting_prints_at_one_timestamp_are_refused() -> None:
    candles, decisions, prices, outcomes = replay_inputs()
    prices = pd.concat([prices, prices.iloc[[0]].assign(p=0.9)], ignore_index=True)
    with pytest.raises(ValueError, match="conflicting"):
        compare(candles, decisions, prices, outcomes, evaluated_at=END + pd.Timedelta(minutes=1))


def test_outcome_before_market_close_is_refused() -> None:
    candles, decisions, prices, outcomes = replay_inputs()
    outcomes["outcome_available_at"] = START
    with pytest.raises(ValueError, match="outcome_available_at"):
        compare(candles, decisions, prices, outcomes, evaluated_at=END + pd.Timedelta(minutes=1))


def test_replay_cli_runs_actual_program_and_emits_strict_report(tmp_path: Path) -> None:
    data = replay_inputs()
    paths = []
    for name, frame in zip(["candles", "decisions", "prices", "outcomes"], data, strict=True):
        path = tmp_path / f"{name}.csv"
        if name == "candles":
            frame = frame.assign(timestamp_semantics="close_available_at")
        frame.to_csv(path, index=False)
        paths.extend([f"--{name}", str(path)])
    repo = Path(__file__).resolve().parents[1]
    run = subprocess.run(
        [
            sys.executable,
            "scripts/replay_events.py",
            *paths,
            "--evaluated-at",
            (END + pd.Timedelta(minutes=1)).isoformat(),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout)
    assert report["schema"] == "polymarket-event-replay/v1"
    assert report["summary"]["admitted_entries"] == 1
    assert len(report["inputs"]["prices"]["sha256"]) == 64
    assert report["assumptions"]["fill_basis"] == "HISTORICAL_PRINT_MODEL"
