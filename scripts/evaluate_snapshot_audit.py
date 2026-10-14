#!/usr/bin/env python3
"""Falsify snapshot-audit checks with source mutants in disposable offline copies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBJECTS = [
    "polymarket_crypto_5min/features.py",
    "polymarket_crypto_5min/snapshot_audit.py",
    "scripts/audit_snapshots.py",
    "tests/test_snapshot_audit.py",
    "tests/test_snapshot_audit_edges.py",
]
MUTANTS = [
    (
        "early_close",
        "polymarket_crypto_5min/features.py",
        'if invalid.any():\n            raise ValueError("candle available_at must',
        'if False:\n            raise ValueError("candle available_at must',
        "test_snapshot_audit.py::test_candle_loader_rejects_unfinished_close_clock",
    ),
    (
        "missing_row_semantics",
        "polymarket_crypto_5min/features.py",
        " or not semantics.isin(allowed).all()",
        "",
        "test_snapshot_audit.py::test_candle_loader_rejects_missing_semantics_on_one_row",
    ),
    (
        "forward_candle_join",
        "polymarket_crypto_5min/features.py",
        'right_on="available_at",\n        direction="backward"',
        'right_on="available_at",\n        direction="forward"',
        "test_snapshot_audit.py::test_audit_uses_prior_candle_and_own_side_print_only",
    ),
    (
        "down_reuses_up",
        "polymarket_crypto_5min/snapshot_audit.py",
        "asset_col=asset_col",
        'asset_col="up_asset_id"',
        "test_snapshot_audit.py::test_audit_uses_prior_candle_and_own_side_print_only",
    ),
    (
        "unbounded_print_age",
        "polymarket_crypto_5min/features.py",
        "tolerance=tolerance",
        "tolerance=None",
        "test_snapshot_audit_edges.py::test_print_age_boundary[61-NO_ELIGIBLE_PRINT]",
    ),
    (
        "hash_after_load",
        "scripts/audit_snapshots.py",
        "hashlib.sha256(captured[name]).hexdigest()",
        "hashlib.sha256(path.read_bytes()).hexdigest()",
        "test_snapshot_audit_edges.py::test_report_hashes_the_bytes_actually_loaded",
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, help="new directory for complete logs and receipt")
    args = parser.parse_args()
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=False)
    pins = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SUBJECTS}
    cases = []
    for name, filename, old, new, witness in [("control", "", "", "", ""), *MUTANTS]:
        with tempfile.TemporaryDirectory(prefix="snapshot-audit-eval-") as folder:
            candidate = Path(folder)
            shutil.copytree(
                ROOT / "polymarket_crypto_5min",
                candidate / "polymarket_crypto_5min",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            for relative_path in SUBJECTS[2:]:
                target = candidate / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative_path, target)
            if filename:
                path = candidate / filename
                source = path.read_text()
                if source.count(old) != 1:
                    raise RuntimeError(f"{name}: expected one mutation site")
                path.write_text(source.replace(old, new))
            # Current directory/PYTHONPATH select the disposable package ahead of the installed editable copy.
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "tests/test_snapshot_audit.py",
                "tests/test_snapshot_audit_edges.py",
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
            passed = (
                run.returncode == 0
                if name == "control"
                else (run.returncode == 1 and f"FAILED tests/{witness}" in output)
            )
            cases.append(
                {
                    "case": name,
                    "command": ["<same interpreter as harness>", *command[1:]],
                    "exit_code": run.returncode,
                    "expected_witness": witness or "all nineteen cases pass",
                    "check": "PASS" if passed else "FAIL",
                }
            )
            if not passed:
                print(output, file=sys.stderr)
                raise SystemExit(f"snapshot audit evaluation failed: {name}")
    if pins != {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SUBJECTS}:
        raise SystemExit("source changed during snapshot audit evaluation")
    report = {
        "scope": "offline correctness evaluation; unchanged tests, disposable source mutations",
        "source_sha256": pins,
        "control": "PASS",
        "mutants_caught": len(MUTANTS),
        "cases": cases,
    }
    output = json.dumps(report, indent=2) + "\n"
    if args.out_dir:
        (args.out_dir / "receipt.json").write_text(output)
    print(output, end="")


if __name__ == "__main__":
    main()
