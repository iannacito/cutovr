#!/usr/bin/env python3
"""
Validate hypothesis: does the CER journal always net to zero for each month?

Test across all 18 available GL files (GLa04888_GL - YYYY-MM.xlsx).
If true for all months, the fix is: broaden plan_total_recoveries_group's
candidate filter from vendor_name=="Expense Recovery" to source_journal_token=="CER".
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

def validate_cer_self_balance():
    """Check if CER journal nets to zero for all 18 months."""
    print("\n" + "="*80)
    print("CER JOURNAL SELF-BALANCE HYPOTHESIS VALIDATION")
    print("="*80)

    base_path = Path("C:/Users/atala/Desktop/SampleLaw PCLawFiles/Original Files")

    # Find all GL files (pattern: GLa04888_GL - YYYY-MM.xlsx)
    gl_files = sorted(base_path.glob("GLa04888_GL - *.xlsx"))

    print(f"\nFound {len(gl_files)} GL files")

    if not gl_files:
        print("[FAIL] No GL files found")
        return 1

    # Track results
    results = []
    months_with_cer = 0
    months_with_imbalance = 0

    for gl_file in gl_files:
        period_label = gl_file.stem.replace("GLa04888_GL - ", "")
        print(f"\n[FILE] {gl_file.name}")

        rows = load_gl_from_excel(str(gl_file))
        if not rows:
            print(f"  ERROR: Could not parse file")
            continue

        # Group rows by month and find CER entries
        cer_by_month = defaultdict(lambda: {"debits": Decimal("0"), "credits": Decimal("0"), "rows": []})

        for row in rows:
            iso_date = parse_gl_date(row.get("date"))
            if not iso_date:
                continue

            month_key = iso_date[:7]
            journal = source_journal_token(row) or ""

            if journal == "CER":
                cer_by_month[month_key]["debits"] += _row_money(row, "debit")
                cer_by_month[month_key]["credits"] += _row_money(row, "credit")
                cer_by_month[month_key]["rows"].append(row)

        # Report CER balance for this file's month
        if cer_by_month:
            for month_key, data in cer_by_month.items():
                debits = data["debits"]
                credits = data["credits"]
                delta = debits - credits

                balanced = round(debits, 2) == round(credits, 2)
                status = "[PASS]" if balanced else "[FAIL]"

                print(f"  {month_key}: {status} DR=${debits:>10.2f} CR=${credits:>10.2f} Delta=${delta:>10.2f}")

                months_with_cer += 1
                if not balanced:
                    months_with_imbalance += 1

                results.append({
                    "file": gl_file.name,
                    "month": month_key,
                    "debits": debits,
                    "credits": credits,
                    "delta": delta,
                    "row_count": len(data["rows"]),
                    "balanced": balanced,
                })
        else:
            print(f"  (no CER activity this month)")

    # Summary
    print(f"\n" + "="*80)
    print("SUMMARY")
    print("="*80)

    print(f"\nMonths with CER activity: {months_with_cer}")
    print(f"Months with imbalance (delta > $0.01): {months_with_imbalance}")

    if months_with_imbalance == 0:
        print(f"\n[PASS] HYPOTHESIS CONFIRMED")
        print(f"All {months_with_cer} months with CER activity have perfectly balanced CER journals.")
        print(f"\nRECOMMENDATION:")
        print(f"  The fix is a one-line change in plan_total_recoveries_group:")
        print(f"  Replace: vendor_name == 'Expense Recovery'")
        print(f"  With:    source_journal_token(row) == 'CER'")
        print(f"\n  This broadens the candidate filter to capture all CER rows,")
        print(f"  not just those tagged 'Expense Recovery'. The balance check")
        print(f"  (sum legs == anchor debit) will now always pass since the entire")
        print(f"  CER journal is guaranteed to self-balance.")
        return 0
    else:
        print(f"\n[FAIL] HYPOTHESIS DOES NOT HOLD")
        print(f"{months_with_imbalance} months have imbalanced CER journals:")
        print()
        for result in results:
            if not result["balanced"]:
                print(f"  {result['file']} / {result['month']}")
                print(f"    Debit: ${result['debits']:.2f}, Credit: ${result['credits']:.2f}, Delta: ${result['delta']:.2f}")
                print(f"    ({result['row_count']} CER rows)")
        return 1

if __name__ == "__main__":
    sys.exit(validate_cer_self_balance())
