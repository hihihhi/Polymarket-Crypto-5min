#!/usr/bin/env python3
"""Compare hold and a declared exit strategy on matched local, timestamp-audited inputs."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

import pandas as pd

from polymarket_crypto_5min.event_replay import compare_replays, load_replay_candles
from polymarket_crypto_5min.exit_backtest import ExitPolicy
from polymarket_crypto_5min.signal_diagnostics import json_safe


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["candles", "decisions", "prices", "outcomes"]:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--evaluated-at", required=True, help="UTC scoring cutoff; outcomes must be available by then")
    parser.add_argument("--stake", type=float, default=10.0)
    parser.add_argument("--fee-rate", type=float, default=0.07, help="explicit modeled fee coefficient")
    parser.add_argument("--take-profit", type=float, default=0.1)
    parser.add_argument("--stop-loss", type=float, default=0.05)
    parser.add_argument("--max-hold-seconds", type=int)
    parser.add_argument("--max-candle-age-seconds", type=float, default=60.0)
    parser.add_argument("--max-price-age-seconds", type=float, default=60.0)
    parser.add_argument("--out", type=Path, help="optional new report file; existing files are refused")
    args = parser.parse_args()
    try:
        paths = {name: getattr(args, name) for name in ["candles", "decisions", "prices", "outcomes"]}
        if args.out and args.out.resolve() in {path.resolve() for path in paths.values()}:
            raise ValueError("output must differ from every input")
        captured = {name: path.read_bytes() for name, path in paths.items()}
        ids = {key: str for key in ["condition_id", "decision_id", "asset_id", "up_asset_id", "down_asset_id"]}
        frames = {name: pd.read_csv(io.BytesIO(raw), dtype=ids) for name, raw in captured.items()}
        frames["candles"] = load_replay_candles(captured["candles"])
        policy = ExitPolicy(
            take_profit=args.take_profit,
            target_price=None,
            stop_loss=args.stop_loss,
            max_hold_seconds=args.max_hold_seconds,
            exit_fee_rate=args.fee_rate,
        )
        report = compare_replays(
            frames["candles"],
            frames["decisions"],
            frames["prices"],
            frames["outcomes"],
            evaluated_at=args.evaluated_at,
            policy=policy,
            stake_usdc=args.stake,
            fee_rate=args.fee_rate,
            max_candle_age_seconds=args.max_candle_age_seconds,
            max_price_age_seconds=args.max_price_age_seconds,
        )
        report["inputs"] = {
            name: {"name": path.name, "sha256": hashlib.sha256(captured[name]).hexdigest()}
            for name, path in paths.items()
        }
        output = json.dumps(json_safe(report), allow_nan=False, indent=2) + "\n"
        if args.out:
            with args.out.open("x", encoding="utf-8") as file:
                file.write(output)
        print(output, end="")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
