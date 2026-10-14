#!/usr/bin/env python3
"""Audit historical candle/own-token availability for local decision CSVs, offline."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

import pandas as pd

from polymarket_crypto_5min.features import ENTRY_PRICE_RULE, load_candles
from polymarket_crypto_5min.signal_diagnostics import json_safe
from polymarket_crypto_5min.snapshot_audit import audit_snapshots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candles", type=Path, required=True, help="CSV accepted by load_candles")
    parser.add_argument(
        "--decisions", type=Path, required=True, help="condition, window, snapshot and paired asset IDs"
    )
    parser.add_argument("--prices", type=Path, required=True, help="condition_id, asset_id, ts, p CSV")
    parser.add_argument("--max-candle-age-seconds", type=float, default=60.0)
    parser.add_argument("--max-price-age-seconds", type=float, default=60.0)
    parser.add_argument("--out", type=Path, help="optional new JSON file; existing files are never overwritten")
    args = parser.parse_args()
    try:
        paths = {"candles": args.candles, "decisions": args.decisions, "prices": args.prices}
        if args.out and args.out.resolve() in {p.resolve() for p in paths.values()}:
            raise ValueError("output must differ from every input")
        # Hash the bytes actually parsed, even if a source file changes after the read.
        captured = {name: path.read_bytes() for name, path in paths.items()}
        source = {name: pd.read_csv(io.BytesIO(raw)) for name, raw in captured.items()}
        candles = load_candles(io.BytesIO(captured["candles"]))
        rows = audit_snapshots(
            candles,
            source["decisions"],
            source["prices"],
            max_candle_age_seconds=args.max_candle_age_seconds,
            max_price_age_seconds=args.max_price_age_seconds,
        )
        inputs = {
            name: {
                "name": path.name,
                "sha256": hashlib.sha256(captured[name]).hexdigest(),
                "rows": len(source[name]),
            }
            for name, path in paths.items()
        }
        inputs["candles"].update(
            {
                "loaded_rows": len(candles),
                "discarded_rows": len(source["candles"]) - len(candles),
                "clock_evidence": "SUPPLIED_CLOSE_CLOCK_VALIDATED"
                if "close_ts" in source["candles"]
                else "LEGACY_CLOCK_DERIVED"
                if "timestamp_semantics" not in source["candles"]
                else "DECLARED_AVAILABILITY_ONLY",
            }
        )
        report = {
            "schema": "polymarket-snapshot-audit/v1",
            "scope": "offline input-time eligibility; historical prints",
            "inputs": inputs,
            "entry_price_rule": ENTRY_PRICE_RULE,
            "limits": {
                "max_candle_age_seconds": args.max_candle_age_seconds,
                "max_price_age_seconds": args.max_price_age_seconds,
            },
            "summary": {
                "decisions": len(source["decisions"]),
                "side_rows": len(rows),
                "usable": int(rows["audit_status"].eq("USABLE").sum()),
                "blocked": int(rows["audit_status"].eq("BLOCKED").sum()),
            },
            "rows": rows.to_dict("records"),
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
