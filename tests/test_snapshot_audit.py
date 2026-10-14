from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from polymarket_crypto_5min.features import load_candles


def candle_csv(tmp_path: Path, *, available: str = "2026-01-01T12:01:00Z") -> Path:
    path = tmp_path / "candles.csv"
    pd.DataFrame(
        [
            {
                "ts": available,
                "available_at": available,
                "open_ts": "2026-01-01T12:00:00Z",
                "close_ts": "2026-01-01T12:00:59.999Z",
                "timestamp_semantics": "close_available_at",
                "close": 123.0,
            }
        ]
    ).to_csv(path, index=False)
    return path


@pytest.mark.parametrize("available", ["2026-01-01T12:00:00Z", "2026-01-01T12:00:59.999Z"])
def test_candle_loader_rejects_unfinished_close_clock(tmp_path: Path, available: str) -> None:
    with pytest.raises(ValueError, match="close_ts"):
        load_candles(candle_csv(tmp_path, available=available))


def test_candle_loader_rejects_missing_semantics_on_one_row(tmp_path: Path) -> None:
    path = candle_csv(tmp_path)
    frame = pd.read_csv(path)
    frame = pd.concat([frame, frame.assign(timestamp_semantics=None)], ignore_index=True)
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="semantics"):
        load_candles(path)


def test_candle_loader_keeps_valid_close_boundary(tmp_path: Path) -> None:
    frame = load_candles(candle_csv(tmp_path))
    assert frame.loc[0, "ts"] == pd.Timestamp("2026-01-01T12:01:00Z")
    assert frame.loc[0, "close"] == 123.0


def audit_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    candles = pd.DataFrame(
        {"ts": pd.to_datetime(["2026-01-01T12:03:00Z", "2026-01-01T12:04:00Z"], utc=True), "close": [100.0, 101.0]}
    )
    decisions = pd.DataFrame(
        [
            {
                "condition_id": "m1",
                "start_dt": "2026-01-01T12:00:00Z",
                "end_dt": "2026-01-01T12:05:00Z",
                "snapshot_dt": "2026-01-01T12:04:15Z",
                "up_asset_id": "up1",
                "down_asset_id": "down1",
            }
        ]
    )
    prices = pd.DataFrame(
        {
            "condition_id": ["m1"] * 3,
            "asset_id": ["up1", "up1", "down1"],
            "ts": pd.to_datetime(["2026-01-01T12:04:00Z", "2026-01-01T12:04:16Z", "2026-01-01T11:59:59Z"], utc=True),
            "p": [0.6, 0.99, 0.4],
        }
    )
    return candles, decisions, prices


def audit(*args, **kwargs) -> pd.DataFrame:
    module = importlib.import_module("polymarket_crypto_5min.snapshot_audit")
    return module.audit_snapshots(*args, **kwargs)


def test_audit_uses_prior_candle_and_own_side_print_only() -> None:
    actual = audit(*audit_inputs())
    assert actual["side"].tolist() == ["UP", "DOWN"]
    assert actual["btc_close"].tolist() == [101.0, 101.0]
    assert actual["candle_age_seconds"].tolist() == [15.0, 15.0]
    assert actual["candle_status"].tolist() == ["FRESH", "FRESH"]
    assert actual["price_status"].tolist() == ["ELIGIBLE", "NO_ELIGIBLE_PRINT"]
    assert actual["audit_status"].tolist() == ["USABLE", "BLOCKED"]
    assert actual.loc[0, "print_price"] == 0.6
    assert pd.isna(actual.loc[1, "print_price"])
    assert actual.loc[0, "print_time"] == pd.Timestamp("2026-01-01T12:04:00Z")


def test_audit_exposes_missing_and_stale_candles_without_dropping_decisions() -> None:
    candles, decisions, prices = audit_inputs()
    earlier = decisions.assign(snapshot_dt="2026-01-01T12:02:59Z")
    later = decisions.assign(snapshot_dt="2026-01-01T12:04:59Z")
    result = audit(candles, pd.concat([earlier, later], ignore_index=True), prices, max_candle_age_seconds=30)
    assert len(result) == 4
    assert result["candle_status"].tolist() == ["MISSING", "MISSING", "STALE", "STALE"]
    assert result["audit_status"].eq("BLOCKED").all()


@pytest.mark.parametrize(
    "field,value",
    [("max_candle_age_seconds", -1), ("max_price_age_seconds", float("nan")), ("max_price_age_seconds", float("inf"))],
)
def test_audit_rejects_invalid_freshness_limits(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=field):
        audit(*audit_inputs(), **{field: value})


def test_audit_refuses_decisions_outside_market_window() -> None:
    candles, decisions, prices = audit_inputs()
    with pytest.raises(ValueError, match="window"):
        audit(candles, decisions.assign(snapshot_dt=decisions["end_dt"]), prices)


def test_audit_empty_inputs_keep_report_schema() -> None:
    candles, decisions, prices = audit_inputs()
    result = audit(candles, decisions.iloc[:0], prices)
    assert result.empty
    assert {"decision_row", "side", "candle_status", "price_status", "audit_status"}.issubset(result.columns)


def test_offline_cli_produces_provenance_and_no_outcome_or_pnl(tmp_path: Path) -> None:
    candles, decisions, prices = audit_inputs()
    candle_path = tmp_path / "candles.csv"
    candles.assign(timestamp_semantics="close_available_at").to_csv(candle_path, index=False)
    decisions.to_csv(tmp_path / "decisions.csv", index=False)
    prices.to_csv(tmp_path / "prices.csv", index=False)
    out = tmp_path / "audit.json"
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "scripts/audit_snapshots.py",
            "--candles",
            str(candle_path),
            "--decisions",
            str(tmp_path / "decisions.csv"),
            "--prices",
            str(tmp_path / "prices.csv"),
            "--out",
            str(out),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(out.read_text())
    assert report["schema"] == "polymarket-snapshot-audit/v1"
    assert report["summary"] == {"decisions": 1, "side_rows": 2, "usable": 1, "blocked": 1}
    assert report["inputs"]["candles"]["clock_evidence"] == "DECLARED_AVAILABILITY_ONLY"
    assert len(report["inputs"]["candles"]["sha256"]) == 64
    assert "pnl" not in report and "realized_direction" not in report
    assert report["rows"][1]["print_price"] is None


def test_offline_cli_invalid_clock_fails_without_report(tmp_path: Path) -> None:
    candles = candle_csv(tmp_path, available="2026-01-01T12:00:00Z")
    _, decisions, prices = audit_inputs()
    decisions.to_csv(tmp_path / "decisions.csv", index=False)
    prices.to_csv(tmp_path / "prices.csv", index=False)
    out = tmp_path / "audit.json"
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "scripts/audit_snapshots.py",
            "--candles",
            str(candles),
            "--decisions",
            str(tmp_path / "decisions.csv"),
            "--prices",
            str(tmp_path / "prices.csv"),
            "--out",
            str(out),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 2
    assert "close_ts" in result.stderr
    assert not out.exists()
