from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from polymarket_crypto_5min.event_replay import compare_replays


def inputs():
    start = pd.Timestamp("2026-01-01T12:00:00Z")
    end = start + pd.Timedelta(minutes=5)
    frames = {
        "candles": pd.DataFrame({"ts": [start], "close": [100.0], "timestamp_semantics": ["close_available_at"]}),
        "decisions": pd.DataFrame(
            [
                {
                    "decision_id": "d",
                    "condition_id": "m",
                    "side": "UP",
                    "start_dt": start,
                    "end_dt": end,
                    "snapshot_dt": start + pd.Timedelta(seconds=15),
                    "up_asset_id": "up",
                    "down_asset_id": "down",
                }
            ]
        ),
        "prices": pd.DataFrame({"condition_id": ["m"], "asset_id": ["up"], "ts": [start], "p": [0.5]}),
        "outcomes": pd.DataFrame([{"condition_id": "m", "realized_direction": "DOWN", "outcome_available_at": end}]),
    }
    return frames, end


@pytest.mark.parametrize(
    ("frame", "column"),
    [
        ("candles", "ts"),
        ("decisions", "start_dt"),
        ("decisions", "end_dt"),
        ("decisions", "snapshot_dt"),
        ("prices", "ts"),
        ("outcomes", "outcome_available_at"),
    ],
)
def test_library_requires_explicit_timezone(frame: str, column: str) -> None:
    frames, cutoff = inputs()
    frames[frame][column] = frames[frame][column].dt.tz_localize(None)
    with pytest.raises(ValueError, match="timezone|offset"):
        compare_replays(*frames.values(), evaluated_at=cutoff)


def test_cutoff_requires_explicit_timezone() -> None:
    frames, cutoff = inputs()
    with pytest.raises(ValueError, match="timezone|offset"):
        compare_replays(*frames.values(), evaluated_at=cutoff.tz_localize(None))


def test_explicit_local_offsets_preserve_instants() -> None:
    frames, cutoff = inputs()
    for frame in frames.values():
        for column in frame.columns:
            if isinstance(frame[column].dtype, pd.DatetimeTZDtype):
                frame[column] = frame[column].dt.tz_convert("Asia/Taipei")
    report = compare_replays(*frames.values(), evaluated_at=cutoff.tz_convert("Asia/Taipei"))
    assert report["summary"]["admitted_entries"] == 1
    assert report["trades"][0]["entry_dt"] == pd.Timestamp("2026-01-01T12:00:15Z")


@pytest.mark.parametrize(
    ("column", "bad"),
    [
        ("ts", "not-a-timestamp"),
        ("close", "not-a-number"),
        ("ts", "2026-01-01 12:00:00"),
    ],
)
def test_cli_refuses_malformed_candle_without_report(tmp_path: Path, column: str, bad: str) -> None:
    frames, cutoff = inputs()
    candles = frames["candles"].astype(str)
    invalid = candles.iloc[[0]].copy()
    invalid[column] = bad
    frames["candles"] = pd.concat([candles, invalid], ignore_index=True)
    command = [sys.executable, "scripts/replay_events.py"]
    for name, frame in frames.items():
        source = tmp_path / f"{name}.csv"
        frame.to_csv(source, index=False)
        command += [f"--{name}", str(source)]
    output = tmp_path / "report.json"
    command += ["--evaluated-at", cutoff.isoformat(), "--out", str(output)]
    result = subprocess.run(
        command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2
    assert "candle" in result.stderr.lower()
    assert not output.exists()
    frames["candles"] = candles
    frames["candles"].to_csv(tmp_path / "candles.csv", index=False)
    recovered = subprocess.run(
        command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10
    )
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(output.read_text())["summary"]["admitted_entries"] == 1


@pytest.mark.parametrize("column", ["ts", "close"])
def test_cli_requires_candle_columns(tmp_path: Path, column: str) -> None:
    frames, cutoff = inputs()
    frames["candles"] = frames["candles"].drop(columns=column)
    command = [sys.executable, "scripts/replay_events.py"]
    for name, frame in frames.items():
        path = tmp_path / f"{name}.csv"
        frame.to_csv(path, index=False)
        command += [f"--{name}", str(path)]
    output = tmp_path / "report.json"
    command += ["--evaluated-at", cutoff.isoformat(), "--out", str(output)]
    result = subprocess.run(
        command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2
    assert "candles" in result.stderr and column in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    ("column", "bad"),
    [
        ("ts", "not-a-timestamp"),
        ("close", "not-a-number"),
        ("ts", "2026-01-01 12:00:00"),
    ],
)
def test_replay_loader_refuses_ambiguous_or_malformed_raw_candles(column: str, bad: str) -> None:
    module = importlib.import_module("polymarket_crypto_5min.event_replay")
    assert hasattr(module, "load_replay_candles"), "strict replay preparation API is required"
    frames, _ = inputs()
    candles = frames["candles"].astype(str)
    invalid = candles.iloc[[0]].copy()
    invalid[column] = bad
    raw = pd.concat([candles, invalid]).to_csv(index=False).encode()
    with pytest.raises(ValueError, match="candles"):
        module.load_replay_candles(raw)


def test_replay_loader_prepares_valid_explicit_offset_for_library() -> None:
    module = importlib.import_module("polymarket_crypto_5min.event_replay")
    assert hasattr(module, "load_replay_candles"), "strict replay preparation API is required"
    frames, cutoff = inputs()
    frames["candles"]["ts"] = frames["candles"]["ts"].dt.tz_convert("Asia/Taipei")
    frames["candles"] = module.load_replay_candles(frames["candles"].to_csv(index=False).encode())
    report = compare_replays(*frames.values(), evaluated_at=cutoff)
    assert report["summary"]["admitted_entries"] == 1


@pytest.mark.parametrize("close", [0, -1, float("inf")])
def test_replay_loader_requires_finite_positive_close(close: float) -> None:
    module = importlib.import_module("polymarket_crypto_5min.event_replay")
    frames, _ = inputs()
    frames["candles"]["close"] = close
    with pytest.raises(ValueError, match="close"):
        module.load_replay_candles(frames["candles"].to_csv(index=False).encode())
