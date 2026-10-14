"""Explain the existing point-in-time joins without fitting or selecting a strategy."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .features import (
    DEFAULT_MAX_BTC_CANDLE_AGE_SECONDS,
    DEFAULT_MAX_ENTRY_PRICE_AGE_SECONDS,
    asof_candle,
    market_price_asof,
)

AUDIT_COLUMNS = [
    "decision_row",
    "condition_id",
    "start_dt",
    "end_dt",
    "snapshot_dt",
    "side",
    "asset_id",
    "btc_close",
    "candle_available_at",
    "candle_age_seconds",
    "candle_status",
    "print_price",
    "print_time",
    "print_age_seconds",
    "price_status",
    "audit_status",
]


def audit_snapshots(
    candles: pd.DataFrame,
    decisions: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    max_candle_age_seconds: float = DEFAULT_MAX_BTC_CANDLE_AGE_SECONDS,
    max_price_age_seconds: float = DEFAULT_MAX_ENTRY_PRICE_AGE_SECONDS,
) -> pd.DataFrame:
    """Return UP/DOWN time-eligibility rows in input-decision order.

    Candle ``ts`` is availability, not interval open. Use ``load_candles`` to
    validate/migrate CSV clocks before calling. An eligible print is historical
    price evidence, not an executable quote or a profitable trade.
    """
    for name, value in (
        ("max_candle_age_seconds", max_candle_age_seconds),
        ("max_price_age_seconds", max_price_age_seconds),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    required = {"condition_id", "start_dt", "end_dt", "snapshot_dt", "up_asset_id", "down_asset_id"}
    if missing := required - set(decisions.columns):
        raise ValueError(f"decisions missing required columns: {sorted(missing)}")
    rows = decisions.reset_index(drop=True).copy()
    for col in ("condition_id", "up_asset_id", "down_asset_id"):
        if rows[col].isna().any() or rows[col].astype(str).str.strip().eq("").any():
            raise ValueError(f"decisions require nonempty {col}")
        rows[col] = rows[col].astype(str)
    if rows["up_asset_id"].eq(rows["down_asset_id"]).any():
        raise ValueError("UP and DOWN asset IDs must differ")
    for col in ("start_dt", "end_dt", "snapshot_dt"):
        rows[col] = pd.to_datetime(rows[col], utc=True, errors="raise", format="mixed")
    inside = rows["snapshot_dt"].ge(rows["start_dt"]) & rows["snapshot_dt"].lt(rows["end_dt"])
    if not inside.all():
        raise ValueError("every decision must be inside its market window: start_dt <= snapshot_dt < end_dt")
    rows["decision_row"] = np.arange(len(rows))
    if rows.empty:
        return pd.DataFrame(columns=AUDIT_COLUMNS)

    for name, frame, columns in (
        ("candles", candles, {"ts", "close"}),
        ("prices", prices, {"condition_id", "asset_id", "ts", "p"}),
    ):
        if missing := columns - set(frame.columns):
            raise ValueError(f"{name} missing required columns: {sorted(missing)}")
    candles = candles.copy()
    candles["ts"] = pd.to_datetime(candles["ts"], utc=True, errors="raise", format="mixed")
    candles["close"] = pd.to_numeric(candles["close"], errors="raise")
    if candles["ts"].isna().any() or not (np.isfinite(candles["close"]) & candles["close"].gt(0)).all():
        raise ValueError("candles require valid ts and finite positive close")
    prices = prices.copy()
    prices["ts"] = pd.to_datetime(prices["ts"], utc=True, errors="raise", format="mixed")
    prices["p"] = pd.to_numeric(prices["p"], errors="raise")
    if prices[["condition_id", "asset_id", "ts"]].isna().any().any() or not prices["p"].between(0, 1).all():
        raise ValueError("prices require condition_id, asset_id, valid ts and finite p in [0, 1]")

    observed = asof_candle(candles, rows["snapshot_dt"])
    rows["btc_close"] = observed["close"]
    rows["candle_available_at"] = observed["available_at"]
    rows["candle_age_seconds"] = (rows["snapshot_dt"] - rows["candle_available_at"]).dt.total_seconds()
    rows["candle_status"] = np.where(
        rows["btc_close"].isna(),
        "MISSING",
        np.where(rows["candle_age_seconds"].between(0, max_candle_age_seconds), "FRESH", "STALE"),
    )
    parts = []
    for side_order, (side, asset_col) in enumerate((("UP", "up_asset_id"), ("DOWN", "down_asset_id"))):
        part = rows.copy()
        observed_price = market_price_asof(prices, rows, asset_col=asset_col, max_age_seconds=max_price_age_seconds)
        part["side"], part["_side_order"] = side, side_order
        part["asset_id"] = part[asset_col]
        part["print_price"], part["print_time"] = observed_price["p"], observed_price["ts"]
        part["print_age_seconds"] = (part["snapshot_dt"] - part["print_time"]).dt.total_seconds()
        part["price_status"] = np.where(part["print_price"].notna(), "ELIGIBLE", "NO_ELIGIBLE_PRINT")
        part["audit_status"] = np.where(
            part["candle_status"].eq("FRESH") & part["price_status"].eq("ELIGIBLE"), "USABLE", "BLOCKED"
        )
        parts.append(part)
    return pd.concat(parts).sort_values(["decision_row", "_side_order"])[AUDIT_COLUMNS].reset_index(drop=True)
