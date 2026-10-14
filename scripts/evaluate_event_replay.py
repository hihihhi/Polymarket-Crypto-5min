#!/usr/bin/env python3
"""Independent decimal/scalar oracle and disposable source mutations for event replay."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

import pandas as pd

from polymarket_crypto_5min.exit_backtest import ExitPolicy, simulate_exit_policy

ROOT = Path(__file__).resolve().parents[1]
SUBJECTS = [
    "polymarket_crypto_5min/exit_backtest.py",
    "polymarket_crypto_5min/event_replay.py",
    "scripts/replay_events.py",
    "tests/test_event_replay.py",
    "tests/test_event_replay_edges.py",
    "tests/test_event_replay_boundaries.py",
    "tests/test_event_replay_clock_contract.py",
    "scripts/evaluate_event_replay.py",
]
MUTANTS = [
    (
        "retrospective_timer",
        "polymarket_crypto_5min/exit_backtest.py",
        'point["ts"] >= deadline',
        'point["ts"] < deadline',
        "test_event_replay.py::test_exit_engine_max_hold_waits_for_first_observation_after_deadline",
    ),
    (
        "settlement_print_exit",
        "polymarket_crypto_5min/exit_backtest.py",
        'ts.searchsorted(end_dt, side="left")',
        'ts.searchsorted(end_dt, side="right")',
        "test_event_replay.py::test_exit_engine_prior_print_does_not_invent_deadline_execution",
    ),
    (
        "future_outcome_score",
        "polymarket_crypto_5min/event_replay.py",
        'row["outcome_available_at"] > cutoff',
        'row["outcome_available_at"] < cutoff',
        "test_event_replay.py::test_outcome_after_evaluation_is_excluded_from_both_strategies",
    ),
    (
        "wrong_entry_fee",
        "polymarket_crypto_5min/exit_backtest.py",
        "taker_fee_per_share(entry_price, fee_rate=entry_fee_rate)",
        "taker_fee_per_share(entry_price)",
        "test_event_replay.py::test_exit_engine_explicit_fee_matches_scalar_cash_accounting",
    ),
    (
        "wrong_settlement_clock",
        "polymarket_crypto_5min/event_replay.py",
        'trade["exit_dt"] = trade["outcome_available_at"]',
        'trade["exit_dt"] = trade["end_dt"]',
        "test_event_replay.py::test_comparison_uses_matched_entries_and_exposes_cost_decomposition",
    ),
    (
        "no_conflict_refusal",
        "polymarket_crypto_5min/event_replay.py",
        "if counts.gt(1).any():",
        "if False:",
        "test_event_replay.py::test_conflicting_prints_at_one_timestamp_are_refused",
    ),
    (
        "decimal_boundary_missed",
        "polymarket_crypto_5min/exit_backtest.py",
        "quoted <= entry - Decimal(str(policy.stop_loss))",
        "price <= entry_price - policy.stop_loss",
        "test_event_replay_boundaries.py::test_decimal_threshold_triggers_on_exact_price",
    ),
    (
        "settlement_timer_misclassified",
        "polymarket_crypto_5min/exit_backtest.py",
        "policy.max_hold_seconds is not None and deadline < end_dt:",
        "policy.max_hold_seconds is not None:",
        "test_event_replay_boundaries.py::test_timer_at_or_after_settlement_is_a_hold",
    ),
    (
        "ambiguous_clock",
        "polymarket_crypto_5min/event_replay.py",
        "if pd.isna(moment) or moment.tzinfo is None:",
        "if pd.isna(moment):",
        "test_event_replay_clock_contract.py::test_library_requires_explicit_timezone",
    ),
    (
        "silently_discarded_candle",
        "polymarket_crypto_5min/event_replay.py",
        "if len(loaded) != len(raw):",
        "if False:",
        "test_event_replay_clock_contract.py::test_cli_refuses_malformed_candle_without_report[close-not-a-number]",
    ),
]


class OracleTrade(TypedDict):
    exit_reason: str
    exit_seconds: int
    exit_price: float
    shares: float
    pnl_usdc: float


def scalar_oracle(
    entry: Decimal, points: list[tuple[int, Decimal]], policy: ExitPolicy, won: bool, fee: Decimal
) -> OracleTrade:
    end, when, value, reason = 300, 300, Decimal(int(won)), "HOLD_TO_SETTLEMENT"
    for second, price in sorted(points):
        if not 0 < second < end:
            continue
        triggered = None
        if policy.max_hold_seconds is not None and second >= policy.max_hold_seconds:
            triggered = "MAX_HOLD_EXIT"
        elif policy.stop_loss is not None and price <= entry - Decimal(str(policy.stop_loss)):
            triggered = "STOP_LOSS"
        elif policy.target_price is not None and price >= Decimal(str(policy.target_price)):
            triggered = "TARGET_PRICE"
        elif policy.take_profit is not None and price >= entry + Decimal(str(policy.take_profit)):
            triggered = "TAKE_PROFIT"
        if triggered:
            when, value, reason = second, price, triggered
            break
    if when == end and policy.max_hold_seconds is not None and policy.max_hold_seconds < end:
        reason = "MAX_HOLD_NO_PRICE"
    entry_fee = fee * entry * (1 - entry)
    exit_fee = Decimal(0) if when == end else Decimal(str(policy.exit_fee_rate)) * value * (1 - value)
    shares = Decimal(10) / (entry + entry_fee)
    return {
        "exit_reason": reason,
        "exit_seconds": when,
        "exit_price": float(value),
        "shares": float(shares),
        "pnl_usdc": float(shares * (value - entry - entry_fee - exit_fee)),
    }


def check_oracle() -> dict[str, object]:
    start = pd.Timestamp("2026-01-01T12:00:00Z")
    count = 0
    maximum_error = 0.0
    for entry_price, side, won, path_number, rate in itertools.product(
        [Decimal("0.2"), Decimal("0.5"), Decimal("0.7")],
        ["UP", "DOWN"],
        [False, True],
        range(4),
        [Decimal(0), Decimal("0.07")],
    ):
        patterns = [
            [],
            [(5, entry_price + Decimal("0.15")), (25, entry_price + Decimal("0.1")), (300, Decimal(1))],
            [(5, entry_price - Decimal("0.05")), (25, entry_price - Decimal("0.1"))],
            [(20, entry_price + Decimal("0.1")), (25, entry_price + Decimal("0.15"))],
        ]
        points = patterns[path_number]
        policies = [
            ExitPolicy(take_profit=None, target_price=None, stop_loss=None, exit_fee_rate=float(rate)),
            ExitPolicy(take_profit=0.1, target_price=None, stop_loss=0.05, exit_fee_rate=float(rate)),
            ExitPolicy(
                take_profit=None, target_price=None, stop_loss=None, max_hold_seconds=20, exit_fee_rate=float(rate)
            ),
            ExitPolicy(
                take_profit=None, target_price=None, stop_loss=None, max_hold_seconds=300, exit_fee_rate=float(rate)
            ),
            ExitPolicy(
                take_profit=None, target_price=None, stop_loss=None, max_hold_seconds=600, exit_fee_rate=float(rate)
            ),
        ]
        for policy in policies:
            expected = scalar_oracle(entry_price, points, policy, won, rate)
            entries = pd.DataFrame(
                [
                    {
                        "condition_id": "m",
                        "side": side,
                        "snapshot_dt": start,
                        "end_dt": start + pd.Timedelta(seconds=300),
                        "market_price": float(entry_price),
                        "up_asset_id": "up",
                        "down_asset_id": "down",
                        "won": won,
                    }
                ]
            )
            prices = pd.DataFrame(
                {
                    "condition_id": ["m"] * len(points),
                    "asset_id": [side.lower()] * len(points),
                    "ts": [start + pd.Timedelta(seconds=second) for second, _ in points],
                    "p": [float(value) for _, value in points],
                }
            )
            actual = simulate_exit_policy(entries, prices, policy, entry_fee_rate=float(rate)).iloc[0]
            context = f"entry={entry_price},side={side},won={won},path={path_number},rate={rate},policy={policy}"
            if actual["exit_reason"] != expected["exit_reason"]:
                raise AssertionError(
                    f"oracle reason mismatch: {context}: {actual['exit_reason']} != {expected['exit_reason']}"
                )
            if actual["exit_dt"] != start + pd.Timedelta(seconds=int(expected["exit_seconds"])):
                raise AssertionError(f"oracle clock mismatch: {context}")
            for field, reference in [
                ("exit_price", expected["exit_price"]),
                ("shares", expected["shares"]),
                ("pnl_usdc", expected["pnl_usdc"]),
            ]:
                error = abs(float(actual[field]) - reference)
                maximum_error = max(maximum_error, error)
                if not math.isclose(float(actual[field]), reference, rel_tol=0, abs_tol=1e-12):
                    raise AssertionError(f"oracle accounting mismatch in{field}: {context}")
            count += 1
    return {
        "cases": count,
        "oracle": "independent scalar loop and Decimal cash/threshold arithmetic",
        "maximum_float_error": maximum_error,
        "absolute_tolerance": 1e-12,
        "result": "PASS",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, help="new directory for logs and receipts")
    parser.add_argument("--oracle-only", action="store_true")
    args = parser.parse_args()
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=False)
    pins = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SUBJECTS}
    oracle = check_oracle()
    if args.out_dir:
        (args.out_dir / "oracle.json").write_text(json.dumps(oracle, indent=2) + "\n")
    cases = []
    if not args.oracle_only:
        for name, filename, old, new, witness in [("control", "", "", "", ""), *MUTANTS]:
            with tempfile.TemporaryDirectory(prefix="event-replay-eval-") as folder:
                candidate = Path(folder)
                shutil.copytree(
                    ROOT / "polymarket_crypto_5min",
                    candidate / "polymarket_crypto_5min",
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                for relative in SUBJECTS[2:]:
                    target = candidate / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(ROOT / relative, target)
                if filename:
                    target = candidate / filename
                    source = target.read_text()
                    if source.count(old) != 1:
                        raise RuntimeError(f"{name}: expected one mutation site")
                    target.write_text(source.replace(old, new))
                command = [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "tests/test_event_replay.py",
                    "tests/test_event_replay_edges.py",
                    "tests/test_event_replay_boundaries.py",
                    "tests/test_event_replay_clock_contract.py",
                ]
                run = subprocess.run(
                    command,
                    cwd=candidate,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env={"PATH": os.defpath, "PYTHONPATH": str(candidate)},
                    check=False,
                )
                output = run.stdout + run.stderr
                if args.out_dir:
                    (args.out_dir / f"{name}.log").write_text(output)
                valid = (
                    run.returncode == 0
                    if name == "control"
                    else (run.returncode == 1 and f"FAILED tests/{witness}" in output)
                )
                cases.append(
                    {
                        "case": name,
                        "exit_code": run.returncode,
                        "command": ["<same interpreter as harness>", *command[1:]],
                        "expected_witness": witness or "all replay checks",
                        "result": "PASS" if valid else "FAIL",
                    }
                )
                if not valid:
                    print(output, file=sys.stderr)
                    raise SystemExit(f"replay evaluation failed: {name}")
    if pins != {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SUBJECTS}:
        raise SystemExit("replay source changed during evaluation")
    report = {"oracle": oracle, "source_sha256": pins, "cases": cases, "mutants_caught": len(MUTANTS) if cases else 0}
    output = json.dumps(report, indent=2) + "\n"
    if args.out_dir:
        (args.out_dir / "receipt.json").write_text(output)
    print(output, end="")


if __name__ == "__main__":
    main()
