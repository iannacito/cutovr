#!/usr/bin/env python3
"""
Step 1: Validate using SPECIFIC anchor row debit (not journal-wide sum).
For non-reconciling months, scan all source journals for the closing row.

Step 2: Dump April 2021 in full for manual inspection.
"""

import sys
import os
import csv
import io
from pathlib import Path
from collections import defaultdict
from decimal import Decimal

os.environ["PYTHONIOENCODING"] = "utf-8"

sys.path.insert(0, str(Path(__file__).parent))

from excel_convert import excel_bytes_to_csv_bytes
from pclaw_pipeline import _resolve_gl_columns
from gl_grouping import source_journal_token, _row_money, parse_gl_date

def load_gl_from_excel(excel_path: str) -> list[dict]:
    """Load GL Excel file, return parsed rows."""
    path = Path(excel_path)
    if not path.exists():
        return []

    with open(path, "rb") as f:
        excel_bytes = f.read()

    csv_bytes = excel_bytes_to_csv_bytes(excel_bytes)
    csv_text = csv_bytes.decode("utf-8")

    rows = []
    reader = csv.DictReader(io.StringIO(csv_text))
    col_mapping = _resolve_gl_columns(reader.fieldnames or [])

    for raw_row in reader:
        normalized_row = {}
        for standard_col, orig_col in col_mapping.items():
            if orig_col in raw_row:
                normalized_row[standard_col] = raw_row[orig_col]
        rows.append(normalized_row)

    return rows

def validate_cer_anchor_specific():
    """STEP 1: Validate using specific anchor row debit, scan cross-journal for gaps."""
    print("\n" + "="*80)
    print("STEP 1: CER ANCHOR-SPECIFIC VALIDATION WITH CROSS-JOURNAL GAP SCAN")
    print("="*80)

    base_path = Path("C:/Users/atala/Desktop/SampleLaw PCLawFiles/Original Files")
    gl_files = sorted(base_path.glob("GLa04888_GL - *.xlsx"))

    print(f"\nChecking {len(gl_files)} GL files\n")

    results = []
    reconciling_months = 0
    non_reconciling_months = 0

    for gl_file in gl_files:
        rows = load_gl_from_excel(str(gl_file))
        if not rows:
            continue

        # Group rows by month
        by_month = defaultdict(list)
        for row in rows:
            iso_date = parse_gl_date(row.get("date"))
            if iso_date:
                month_key = iso_date[:7]
                by_month[month_key].append(row)

        # Process each month
        for month_key, month_rows in by_month.items():
            # Find the anchor row (CER, description == "Total of Recoveries")
            anchor_row = None
            for r in month_rows:
                journal = source_journal_token(r) or ""
                if (journal == "CER" and
                    (r.get("description") or "").strip() == "Total of Recoveries"):
                    anchor_row = r
                    break

            if not anchor_row:
                continue

            # Get anchor debit value
            anchor_debit = _row_money(anchor_row, "debit")

            # Sum all CER credits in the month (excluding anchor)
            cer_credits = Decimal("0")
            for r in month_rows:
                journal = source_journal_token(r) or ""
                if journal == "CER" and r is not anchor_row:
                    cer_credits += _row_money(r, "credit")

            # Check if they reconcile
            delta = anchor_debit - cer_credits
            reconciles = round(delta, 2) == 0

            if reconciles:
                reconciling_months += 1
                print(f"{month_key}: [PASS] Anchor ${anchor_debit:.2f} == CER credits ${cer_credits:.2f}")
            else:
                non_reconciling_months += 1
                print(f"{month_key}: [FAIL] Anchor ${anchor_debit:.2f} vs CER credits ${cer_credits:.2f} | Delta: ${delta:.2f}")

                # Scan all other journals for a row that closes the gap
                closing_row = None
                for r in month_rows:
                    journal = source_journal_token(r) or ""
                    if journal == "CER":
                        continue  # Skip CER rows

                    row_debit = _row_money(r, "debit")
                    row_credit = _row_money(r, "credit")

                    # Check if this row closes the gap (either debit or credit direction)
                    if round(row_debit, 2) == round(abs(delta), 2):
                        closing_row = r
                        direction = "debit"
                        break
                    elif round(row_credit, 2) == round(abs(delta), 2):
                        closing_row = r
                        direction = "credit"
                        break

                if closing_row:
                    journal = source_journal_token(closing_row) or "UNKNOWN"
                    txn_id = closing_row.get("transaction_id", "").strip()
                    desc = closing_row.get("description", "").strip()
                    print(f"  -> CLOSES VIA: {journal} txn {txn_id} ({desc})")
                else:
                    print(f"  -> NO CLOSING ROW FOUND IN OTHER JOURNALS")

            results.append({
                "file": gl_file.name,
                "month": month_key,
                "anchor_debit": anchor_debit,
                "cer_credits": cer_credits,
                "delta": delta,
                "reconciles": reconciles,
            })

    # Summary
    print(f"\n" + "="*80)
    print("STEP 1 SUMMARY")
    print("="*80)
    print(f"Months reconciling (anchor = CER credits): {reconciling_months}")
    print(f"Months non-reconciling (need other journals): {non_reconciling_months}")

    return results

def dump_april_2021():
    """STEP 2: Dump April 2021 in full, all journals."""
    print(f"\n" + "="*80)
    print("STEP 2: APRIL 2021 FULL DUMP (ALL JOURNALS)")
    print("="*80)

    april_file = Path("C:/Users/atala/Desktop/SampleLaw PCLawFiles/Original Files/GLa04888_GL - 2021-04.xlsx")
    rows = load_gl_from_excel(str(april_file))

    if not rows:
        print("Could not load April 2021 file")
        return

    # Group by source journal
    by_journal = defaultdict(list)
    for row in rows:
        journal = source_journal_token(row) or "UNKNOWN"
        iso_date = parse_gl_date(row.get("date"))
        if iso_date and iso_date[:7] == "2021-04":
            by_journal[journal].append(row)

    print(f"\nApril 2021 rows by source journal:\n")

    # Report CER first
    if "CER" in by_journal:
        print("CER JOURNAL:")
        print(f"{'Txn ID':<12} {'Description':<35} {'Debit':>12} {'Credit':>12}")
        print("-" * 75)

        cer_debits = Decimal("0")
        cer_credits = Decimal("0")

        for row in by_journal["CER"]:
            txn_id = row.get("transaction_id", "").strip() or "(no ID)"
            desc = row.get("description", "").strip()[:32]
            debit = _row_money(row, "debit")
            credit = _row_money(row, "credit")

            if debit > 0:
                cer_debits += debit
            if credit > 0:
                cer_credits += credit

            print(f"{txn_id:<12} {desc:<35} ${debit:>11.2f} ${credit:>11.2f}")

        print(f"\n  CER Debits Total: ${cer_debits:.2f}")
        print(f"  CER Credits Total: ${cer_credits:.2f}")
        print(f"  CER-only delta: ${cer_debits - cer_credits:.2f}\n")

    # Report other journals
    for journal in sorted(by_journal.keys()):
        if journal == "CER":
            continue

        print(f"{journal} JOURNAL:")
        print(f"{'Txn ID':<12} {'Description':<35} {'Debit':>12} {'Credit':>12}")
        print("-" * 75)

        for row in by_journal[journal]:
            txn_id = row.get("transaction_id", "").strip() or "(no ID)"
            desc = row.get("description", "").strip()[:32]
            debit = _row_money(row, "debit")
            credit = _row_money(row, "credit")
            print(f"{txn_id:<12} {desc:<35} ${debit:>11.2f} ${credit:>11.2f}")

        print()

    print(f"Total rows in April 2021: {len(rows)}")

def main():
    results = validate_cer_anchor_specific()
    dump_april_2021()

    print("\n" + "="*80)
    print("REPORT COMPLETE")
    print("="*80)
    return 0

if __name__ == "__main__":
    sys.exit(main())
