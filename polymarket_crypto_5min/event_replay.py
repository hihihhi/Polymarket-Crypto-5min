"""Matched, fixed-policy replay using declared observation and outcome clocks."""

from __future__ import annotations

import io
import math
from dataclasses import asdict
from typing import Any

import pandas as pd

from .exit_backtest import ExitPolicy, simulate_exit_policy
from .features import load_candles
from .snapshot_audit import audit_snapshots


def parse_utc_clock(values: pd.Series, name: str) -> pd.Series:
    """Refuse ambiguous or invalid clocks before converting explicit zones to UTC."""
    for row, value in enumerate(values):
        try:
            moment = pd.Timestamp(value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"{name} row {row} requires a valid timestamp with explicit timezone offset") from exc
        if pd.isna(moment) or moment.tzinfo is None:
            raise ValueError(f"{name} row {row} requires a valid timestamp with explicit timezone offset")
    return pd.to_datetime(values, utc=True, errors="raise", format="mixed")


def load_replay_candles(captured: bytes) -> pd.DataFrame:
    """Prepare captured CSV bytes without losing raw clock or malformed-row evidence."""
    raw = pd.read_csv(io.BytesIO(captured))
    if missing := {"ts", "close"} - set(raw.columns):
        raise ValueError(f"candles missing required columns: {sorted(missing)}")
    for column in ["ts", "available_at", "open_ts", "close_ts"]:
        if column in raw:
            parse_utc_clock(raw[column], f"candles.{column}")
    loaded = load_candles(io.BytesIO(captured))
    if len(loaded) != len(raw):
        raise ValueError("candles require valid ts and close on every row; loader discarded invalid input")
    if not loaded["close"].map(lambda value: math.isfinite(value) and value > 0).all():
        raise ValueError("candles require finite positive close on every row")
    return loaded


def compare_replays(
    candles: pd.DataFrame,
    decisions: pd.DataFrame,
    prices: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    evaluated_at: str | pd.Timestamp,
    policy: ExitPolicy | None = None,
    stake_usdc: float = 10.0,
    fee_rate: float = 0.07,
    max_candle_age_seconds: float = 60.0,
    max_price_age_seconds: float = 60.0,
) -> dict[str, Any]:
    """Replay hold and one declared exit policy on the same eligible resolved entries.

    Decision side is supplied, never selected using outcomes. Label availability
    admits a market for scoring only; it is not an entry/exit feature. Each
    position has an independent fixed stake, without a portfolio cash constraint.
    """
    for name, value in (("stake_usdc", stake_usdc), ("fee_rate", fee_rate)):
        if not math.isfinite(value) or value < 0 or (name == "stake_usdc" and value == 0):
            raise ValueError(f"{name} must be finite and {'positive' if name == 'stake_usdc' else 'nonnegative'}")
    cutoff = parse_utc_clock(pd.Series([evaluated_at]), "evaluated_at").iloc[0]
    selected_policy = policy or ExitPolicy(take_profit=0.1, target_price=None, stop_loss=0.05, exit_fee_rate=fee_rate)
    _validate_policy(selected_policy)
    rows = decisions.reset_index(drop=True).copy()
    for name, frame, required in (
        (
            "decisions",
            rows,
            {
                "decision_id",
                "condition_id",
                "side",
                "start_dt",
                "end_dt",
                "snapshot_dt",
                "up_asset_id",
                "down_asset_id",
            },
        ),
        ("prices", prices, {"condition_id", "asset_id", "ts", "p"}),
        ("outcomes", outcomes, {"condition_id", "realized_direction", "outcome_available_at"}),
    ):
        if missing := required - set(frame.columns):
            raise ValueError(f"{name} missing required columns: {sorted(missing)}")
    # Decision metadata cannot shadow the separately supplied scoring labels.
    rows = rows[
        ["decision_id", "condition_id", "side", "start_dt", "end_dt", "snapshot_dt", "up_asset_id", "down_asset_id"]
    ].copy()
    for col in ("decision_id", "condition_id"):
        if rows[col].isna().any() or rows[col].astype(str).str.strip().eq("").any():
            raise ValueError(f"decisions require nonempty {col}")
        rows[col] = rows[col].astype(str)
        if rows[col].duplicated().any():
            raise ValueError(f"decisions require unique {col}; one entry per market")
    rows["side"] = rows["side"].astype(str).str.upper()
    if not rows["side"].isin(["UP", "DOWN"]).all():
        raise ValueError("decision side must be UP or DOWN")
    for col in ("start_dt", "snapshot_dt", "end_dt"):
        rows[col] = parse_utc_clock(rows[col], f"decisions.{col}")
    labels = outcomes.copy()
    if labels["condition_id"].isna().any() or labels["condition_id"].astype(str).str.strip().eq("").any():
        raise ValueError("outcomes require nonempty condition_id")
    labels["condition_id"] = labels["condition_id"].astype(str)
    if labels["condition_id"].duplicated().any():
        raise ValueError("outcomes require one condition_id per market")
    labels["realized_direction"] = labels["realized_direction"].astype(str).str.upper()
    if not labels["realized_direction"].isin(["UP", "DOWN"]).all():
        raise ValueError("outcomes require explicit binary realized_direction UP or DOWN")
    labels["outcome_available_at"] = parse_utc_clock(labels["outcome_available_at"], "outcomes.outcome_available_at")
    joined = rows.merge(
        labels[["condition_id", "realized_direction", "outcome_available_at"]],
        on="condition_id",
        how="left",
        validate="one_to_one",
    )
    if joined["outcome_available_at"].lt(joined["end_dt"]).any():
        raise ValueError("outcome_available_at cannot precede the market end_dt")
    prices = prices.copy()
    prices["ts"] = parse_utc_clock(prices["ts"], "prices.ts")
    prices["p"] = pd.to_numeric(prices["p"], errors="raise")
    if prices["ts"].isna().any() or not prices["p"].between(0, 1).all():
        raise ValueError("prices require valid ts and finite p in [0, 1]")
    for col in ("condition_id", "asset_id"):
        if prices[col].isna().any() or prices[col].astype(str).str.strip().eq("").any():
            raise ValueError(f"prices require nonempty {col}")
        prices[col] = prices[col].astype(str)
    # A timestamp without a sequence cannot order conflicting same-token observations.
    counts = prices.groupby(["condition_id", "asset_id", "ts"], dropna=False)["p"].nunique(dropna=False)
    if counts.gt(1).any():
        raise ValueError("conflicting token prints at the same timestamp require an explicit ordering")
    prices = prices.drop_duplicates(["condition_id", "asset_id", "ts", "p"]).reset_index(drop=True)
    candles = candles.copy()
    if not {"ts", "close"}.issubset(candles.columns):
        raise ValueError("candles require ts and close")
    candles["ts"] = parse_utc_clock(candles["ts"], "candles.ts")
    candles["close"] = pd.to_numeric(candles["close"], errors="raise")
    if candles["ts"].isna().any() or not candles["close"].map(lambda value: math.isfinite(value) and value > 0).all():
        raise ValueError("candles require valid ts and finite positive close")
    if candles.groupby("ts")["close"].nunique().gt(1).any():
        raise ValueError("conflicting candle closes at the same availability timestamp")
    candles = candles.drop_duplicates(["ts", "close"])
    visible_prices = prices[prices["ts"].le(cutoff)].copy()
    visible_prices.attrs = {}
    visible_candles = candles[candles["ts"].le(cutoff)]
    audited = audit_snapshots(
        visible_candles,
        rows,
        visible_prices,
        max_candle_age_seconds=max_candle_age_seconds,
        max_price_age_seconds=max_price_age_seconds,
    )
    ledger = audited[audited["side"].eq(audited["decision_row"].map(rows["side"]))].copy()
    ledger["decision_id"] = ledger["decision_row"].map(rows["decision_id"])
    ledger["replay_status"] = "ADMITTED"
    label_by_row = joined.set_index(joined.index)
    for idx in ledger.index:
        decision_row = int(ledger.loc[idx, "decision_row"])
        row = label_by_row.loc[decision_row]
        status = "ADMITTED"
        if row["snapshot_dt"] > cutoff:
            status = "DECISION_AFTER_AS_OF"
        elif ledger.loc[idx, "audit_status"] != "USABLE":
            status = "INPUT_TIME_BLOCKED"
        elif not 0 < ledger.loc[idx, "print_price"] < 1:
            status = "PRICE_OUTSIDE_ENTRY_MODEL"
        elif pd.isna(row["outcome_available_at"]):
            status = "OUTCOME_MISSING"
        elif row["outcome_available_at"] > cutoff:
            status = "OUTCOME_NOT_AVAILABLE_AS_OF"
        ledger.loc[idx, "replay_status"] = status
    admitted = ledger[ledger["replay_status"].eq("ADMITTED")]
    entries = rows.iloc[admitted["decision_row"].to_numpy(dtype=int)].copy()
    entries["market_price"] = admitted["print_price"].to_numpy()
    entries["_candidate_row_id"] = admitted["decision_row"].to_numpy()
    entries["won"] = (
        joined.iloc[admitted["decision_row"].to_numpy(dtype=int)]["realized_direction"].to_numpy() == entries["side"]
    )
    policies = [
        ("HOLD", ExitPolicy(take_profit=None, target_price=None, stop_loss=None)),
        ("CONFIGURED_EXIT", selected_policy),
    ]
    trade_rows: list[dict[str, Any]] = []
    strategy_rows = []
    ledger_by_row = ledger.set_index("decision_row")
    for strategy, exit_policy in policies:
        trades = simulate_exit_policy(
            entries, visible_prices, exit_policy, stake_usdc=stake_usdc, entry_fee_rate=fee_rate
        )
        for trade in trades.to_dict("records"):
            decision_row = int(trade.pop("_candidate_row_id"))
            trade["decision_id"] = rows.loc[decision_row, "decision_id"]
            trade["decision_row"] = decision_row
            trade["strategy"] = strategy
            trade["entry_observation_at"] = ledger_by_row.loc[decision_row, "print_time"]
            trade["outcome_available_at"] = joined.loc[decision_row, "outcome_available_at"]
            if trade["realized_settlement"]:
                trade["exit_dt"] = trade["outcome_available_at"]
            trade["gross_pnl_usdc"] = trade["shares"] * (trade["exit_price"] - trade["entry_price"])
            trade["modeled_fees_usdc"] = trade["shares"] * (trade["entry_fee_per_share"] + trade["exit_fee_per_share"])
            if not all(
                math.isfinite(float(trade[field])) for field in ["pnl_usdc", "gross_pnl_usdc", "modeled_fees_usdc"]
            ):
                raise ValueError("cost assumptions produce nonfinite trade accounting")
            trade_rows.append(trade)
        group = [trade for trade in trade_rows if trade["strategy"] == strategy]
        strategy_rows.append(
            {
                "strategy": strategy,
                "trades": len(group),
                "gross_pnl_usdc": sum(trade["gross_pnl_usdc"] for trade in group),
                "modeled_fees_usdc": sum(trade["modeled_fees_usdc"] for trade in group),
                "modeled_net_pnl_usdc": sum(trade["pnl_usdc"] for trade in group),
                "early_exits": sum(not trade["realized_settlement"] for trade in group),
                "max_hold_no_price": sum(trade["exit_reason"] == "MAX_HOLD_NO_PRICE" for trade in group),
            }
        )
    baseline = {trade["decision_id"]: trade for trade in trade_rows if trade["strategy"] == "HOLD"}
    deltas = [
        {
            "decision_id": trade["decision_id"],
            "hold_net_usdc": baseline[trade["decision_id"]]["pnl_usdc"],
            "configured_exit_net_usdc": trade["pnl_usdc"],
            "modeled_net_delta_usdc": trade["pnl_usdc"] - baseline[trade["decision_id"]]["pnl_usdc"],
        }
        for trade in trade_rows
        if trade["strategy"] == "CONFIGURED_EXIT"
    ]
    return {
        "schema": "polymarket-event-replay/v1",
        "evaluated_at": cutoff,
        "summary": {
            "decisions": len(rows),
            "admitted_entries": len(admitted),
            "excluded_entries": len(rows) - len(admitted),
        },
        "assumptions": {
            "fill_basis": "HISTORICAL_PRINT_MODEL",
            "strategy_selection": "NONE_FIXED_POLICIES",
            "entry_fee_rate": fee_rate,
            "fee_formula": "rate * p * (1 - p), unrounded",
            "stake_usdc": stake_usdc,
            "configured_exit_policy": asdict(selected_policy),
            "position_budget": "INDEPENDENT_FIXED_STAKE",
            "slippage_depth_latency": "UNMODELED",
            "price_clock": "SUPPLIED_TS_ASSUMED_OBSERVABLE",
            "outcome_clock": "SUPPLIED_LABEL_AVAILABILITY",
        },
        "decisions": ledger.to_dict("records"),
        "strategies": strategy_rows,
        "trades": trade_rows,
        "matched_deltas": deltas,
    }


def _validate_policy(policy: ExitPolicy) -> None:
    if not math.isfinite(policy.exit_fee_rate) or policy.exit_fee_rate < 0:
        raise ValueError("exit_fee_rate must be finite and nonnegative")
    for name in ["take_profit", "stop_loss", "target_price"]:
        value = getattr(policy, name)
        if value is not None and (not math.isfinite(value) or not 0 < value <= 1):
            raise ValueError(f"{name} must be in (0, 1] or None")
    if policy.max_hold_seconds is not None and (
        type(policy.max_hold_seconds) is not int or policy.max_hold_seconds <= 0
    ):
        raise ValueError("max_hold_seconds must be a positive integer or None")
