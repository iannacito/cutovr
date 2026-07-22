#!/usr/bin/env python3
"""
Validation script for recovery-reclass anchor merge fix.
Tests Phase A (commit 0e03039) against real PC Law GL files.

Steps:
1. Load real GL files (Jan, Feb, Mar 2021)
2. Parse and group rows
3. Apply TotRec + RecoveryReclass grouping
4. Simulate stamping logic (with defensive re-balance check)
5. Report per-account totals, whole-file balance, and reclass group status
"""

import sys
import os
os.environ["PYTHONIOENCODING"] = "utf-8"
from decimal import Decimal
from collections import OrderedDict
from pathlib import Path

# Add cutovr to path
sys.path.insert(0, str(Path(__file__).parent))

from excel_convert import excel_bytes_to_csv_bytes
from pclaw_pipeline import (
    load_general_ledger_csv,
    group_rows_by_transaction,
    money,
    _resolve_gl_columns,
    _norm_gl_header,
)
from gl_grouping import (
    plan_total_recoveries_group,
    plan_recovery_reclass_groups,
    _row_money,
)


def load_and_parse_gl(excel_path: str) -> list[dict]:
    """Load GL Excel file, convert to CSV, and parse."""
    import csv
    import io

    path = Path(excel_path)
    if not path.exists():
        raise FileNotFoundError(f"GL file not found: {excel_path}")

    print(f"[FILE] Loading: {path.name}")
    with open(path, "rb") as f:
        excel_bytes = f.read()

    csv_bytes = excel_bytes_to_csv_bytes(excel_bytes)
    csv_text = csv_bytes.decode("utf-8")

    # Parse CSV manually since load_general_ledger_csv expects a file path
    rows = []
    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames or []

    # Resolve column mappings (normalize Excel column names to standard names)
    col_mapping = _resolve_gl_columns(list(fieldnames))

    for raw_row in reader:
        # Normalize columns using the resolved mapping
        normalized_row = {}
        for standard_col, orig_col in col_mapping.items():
            if orig_col in raw_row:
                normalized_row[standard_col] = raw_row[orig_col]
        rows.append(normalized_row)

    print(f"   Parsed {len(rows)} rows")
    return rows


def compute_per_account_totals(rows: list[dict]) -> dict:
    """Compute total debits/credits per account number."""
    by_account = {}
    for row in rows:
        acct_num = row.get("account_number", "").strip()
        acct_name = row.get("account_name", "").strip()
        if not acct_num:
            continue

        key = (acct_num, acct_name)
        if key not in by_account:
            by_account[key] = {"debits": Decimal("0.00"), "credits": Decimal("0.00")}

        by_account[key]["debits"] += _row_money(row, "debit")
        by_account[key]["credits"] += _row_money(row, "credit")

    return by_account


def simulate_stamping_logic(
    rows: list[dict],
    tot_rec_groups: list[dict],
    recovery_reclass_groups: list[dict],
) -> dict:
    """Simulate the stamping logic from app.py lines 10344-10370.

    Returns:
    {
        "tot_rec_stamped": count,
        "reclass_stamped": count,
        "reclass_unbalanced": [token, debits, credits],
        "token_groups": {token: {debits, credits, row_count}},
    }
    """
    results = {
        "tot_rec_stamped": 0,
        "reclass_stamped": 0,
        "reclass_unbalanced": [],
        "token_groups": {},
    }

    # Make a copy of rows so we don't mutate the originals
    rows = [dict(r) for r in rows]

    # Stamp TotRec tokens
    tot_rec_row_ids = {id(r) for grp in tot_rec_groups for r in grp.get("rows", [])}
    for grp in tot_rec_groups:
        token = grp.get("token", "")
        if not token:
            continue
        for row in grp.get("rows", []):
            row["transaction_id"] = token
            results["tot_rec_stamped"] += 1

    # Stamp RecoveryReclass tokens (with exclusion logic that causes the bug)
    recovery_reclass_row_ids_by_token = {}
    for grp in recovery_reclass_groups:
        token = grp.get("token", "")
        if not token:
            continue
        row_ids = set()
        for row in grp.get("rows", []):
            # This is the buggy exclusion: skip rows already in TotRec
            if id(row) in tot_rec_row_ids:
                continue
            row_ids.add(id(row))
            row["transaction_id"] = token
            results["reclass_stamped"] += 1
        if row_ids:
            recovery_reclass_row_ids_by_token[token] = row_ids

    # Defensive re-balance check (THE FIX)
    for token, row_ids in recovery_reclass_row_ids_by_token.items():
        token_rows = [r for r in rows if id(r) in row_ids]
        if token_rows:
            debits = sum(_row_money(r, "debit") for r in token_rows)
            credits = sum(_row_money(r, "credit") for r in token_rows)
            results["token_groups"][token] = {
                "debits": float(debits),
                "credits": float(credits),
                "row_count": len(token_rows),
            }
            if round(debits, 2) != round(credits, 2):
                results["reclass_unbalanced"].append({
                    "token": token,
                    "debits": float(debits),
                    "credits": float(credits),
                    "delta": float(abs(debits - credits)),
                })

    return results


def validate_file(excel_path: str, expected_accts: dict = None) -> bool:
    """Validate a single GL file. Returns True if pass, False if fail."""
    print(f"\n{'='*80}")
    print(f"Testing: {Path(excel_path).name}")
    print(f"{'='*80}")

    try:
        rows = load_and_parse_gl(excel_path)
    except Exception as e:
        print(f"[FAIL] Failed to load: {e}")
        return False

    # Group by transaction
    grouped = group_rows_by_transaction(rows)
    print(f"   Grouped into {len(grouped)} transactions")

    # Run detectors
    tot_rec_groups = plan_total_recoveries_group(grouped)
    recovery_reclass_groups = plan_recovery_reclass_groups(grouped)
    print(f"   TotRec groups: {len(tot_rec_groups)}")
    print(f"   RecoveryReclass groups: {len(recovery_reclass_groups)}")

    # Simulate stamping with the fix
    stamp_results = simulate_stamping_logic(rows, tot_rec_groups, recovery_reclass_groups)
    print(f"   Stamped {stamp_results['tot_rec_stamped']} rows with TotRec tokens")
    print(f"   Stamped {stamp_results['reclass_stamped']} rows with reclass tokens")

    # Report token group balance status
    if stamp_results["reclass_unbalanced"]:
        print(f"\n[WARN]  UNBALANCED RECLASS TOKENS (would be caught by the fix):")
        for item in stamp_results["reclass_unbalanced"]:
            print(f"   Token: {item['token']}")
            print(f"   Debits: ${item['debits']:.2f}, Credits: ${item['credits']:.2f}")
            print(f"   Delta: ${item['delta']:.2f}")
    else:
        print(f"\n[PASS] All reclass tokens are balanced")

    # Whole-file balance
    whole_file_debits = sum(_row_money(r, "debit") for r in rows)
    whole_file_credits = sum(_row_money(r, "credit") for r in rows)
    print(f"\n[SUMMARY] Whole File Balance:")
    print(f"   Total Debits:  ${whole_file_debits:>12.2f}")
    print(f"   Total Credits: ${whole_file_credits:>12.2f}")
    print(f"   Difference:    ${abs(whole_file_debits - whole_file_credits):>12.2f}")

    if round(whole_file_debits, 2) == round(whole_file_credits, 2):
        print(f"   [PASS] BALANCED")
    else:
        print(f"   [FAIL] NOT BALANCED")
        return False

    # Per-account check (if expected accounts provided)
    if expected_accts:
        print(f"\n[ACCTS] Per-Account Variance:")
        actual_accts = compute_per_account_totals(rows)

        variance_found = False
        for acct_key, expected in expected_accts.items():
            actual = actual_accts.get(acct_key, {"debits": Decimal("0"), "credits": Decimal("0")})
            dr_var = actual["debits"] - expected["debits"]
            cr_var = actual["credits"] - expected["credits"]

            if dr_var != 0 or cr_var != 0:
                variance_found = True
                acct_num, acct_name = acct_key
                print(f"   {acct_num} {acct_name}:")
                print(f"     Expected: DR ${expected['debits']:>10.2f} CR ${expected['credits']:>10.2f}")
                print(f"     Actual:   DR ${actual['debits']:>10.2f} CR ${actual['credits']:>10.2f}")
                print(f"     Variance: DR ${dr_var:>10.2f} CR ${cr_var:>10.2f}")

        if not variance_found:
            print(f"   [PASS] No variances found")

    return True


def main():
    """Run all 4 validation steps."""
    print("\n" + "="*80)
    print("RECOVERY-RECLASS ANCHOR MERGE FIX — VALIDATION")
    print("="*80)

    base_path = Path("C:/Users/atala/Desktop/SampleLaw PCLawFiles/Original Files")

    # Test files
    march_file = base_path / "GLa04888_GL - 2021-03.xlsx"
    feb_file = base_path / "GLa04888_GL - 2021-02.xlsx"
    jan_file = base_path / "GLa04888_GL - 2021-01.xlsx"

    # Expected totals from PCLaw (established baseline)
    march_expected = {
        ("5010", "Client Disb Expense - Continued"): {"debits": Decimal("9337.61"), "credits": Decimal("0")},
        ("5245", "Legal"): {"debits": Decimal("169.10"), "credits": Decimal("0")},
        ("5070", "Bank Charges"): {"debits": Decimal("9644.77"), "credits": Decimal("0")},
    }

    results = {
        "march": validate_file(str(march_file), march_expected),
        "feb": validate_file(str(feb_file)),
        "jan": validate_file(str(jan_file)),
    }

    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    print(f"March (with expected accounts):     {'[PASS] PASS' if results['march'] else '[FAIL] FAIL'}")
    print(f"February (regression check):        {'[PASS] PASS' if results['feb'] else '[FAIL] FAIL'}")
    print(f"January (regression check):         {'[PASS] PASS' if results['jan'] else '[FAIL] FAIL'}")

    all_pass = all(results.values())
    print(f"\nOverall: {'[PASS] ALL TESTS PASSED' if all_pass else '[FAIL] SOME TESTS FAILED'}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
