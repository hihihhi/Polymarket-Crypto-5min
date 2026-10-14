from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from polymarket_crypto_5min.features import load_candles
from polymarket_crypto_5min.snapshot_audit import audit_snapshots


def inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    candles = pd.DataFrame({"ts": ["2026-01-01T12:04:00Z"], "close": [101.0]})
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
    prices = pd.DataFrame({"condition_id": ["m1"], "asset_id": ["up1"], "ts": ["2026-01-01T12:03:15Z"], "p": [0.6]})
    return candles, decisions, prices


@pytest.mark.parametrize("seconds,status", [(60, "ELIGIBLE"), (61, "NO_ELIGIBLE_PRINT")])
def test_print_age_boundary(seconds: int, status: str) -> None:
    candles, decisions, prices = inputs()
    snapshot = pd.Timestamp(decisions.loc[0, "snapshot_dt"])
    prices["ts"] = snapshot - pd.Timedelta(seconds=seconds)
    assert audit_snapshots(candles, decisions, prices).loc[0, "price_status"] == status


def test_no_candle_history_preserves_blocked_decisions() -> None:
    candles, decisions, prices = inputs()
    report = audit_snapshots(candles.iloc[:0], decisions, prices)
    assert len(report) == 2
    assert report["candle_status"].tolist() == ["MISSING", "MISSING"]
    assert report["audit_status"].eq("BLOCKED").all()


@pytest.mark.parametrize("column", ["asset_id", "condition_id"])
def test_print_from_another_token_or_condition_is_not_reused(column: str) -> None:
    candles, decisions, prices = inputs()
    prices[column] = "other"
    assert audit_snapshots(candles, decisions, prices)["price_status"].eq("NO_ELIGIBLE_PRINT").all()


def test_report_hashes_the_bytes_actually_loaded(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("audit_cli", repo / "scripts/audit_snapshots.py")
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    candles, decisions, prices = inputs()
    candle_path = tmp_path / "candles.csv"
    candles.assign(timestamp_semantics="close_available_at").to_csv(candle_path, index=False)
    decisions.to_csv(tmp_path / "decisions.csv", index=False)
    prices.to_csv(tmp_path / "prices.csv", index=False)
    initial_hash = hashlib.sha256(candle_path.read_bytes()).hexdigest()

    def load_then_change(source):
        loaded = load_candles(source)
        candle_path.write_text("unrelated replacement bytes")
        return loaded

    monkeypatch.setattr(cli, "load_candles", load_then_change)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit_snapshots",
            "--candles",
            str(candle_path),
            "--decisions",
            str(tmp_path / "decisions.csv"),
            "--prices",
            str(tmp_path / "prices.csv"),
        ],
    )
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["inputs"]["candles"]["sha256"] == initial_hash
    assert report["rows"][0]["btc_close"] == 101.0
