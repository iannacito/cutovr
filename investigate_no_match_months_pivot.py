#!/usr/bin/env python3
"""
Pivot-table-style investigation for Jan 2021 and 3 no-match months (2022-01, 2022-04, 2022-05).

Level 1: Whole month total (sanity)
Level 2: Per source journal totals
Level 3: Per transaction within unbalanced journals
Bonus: 2-row combination check for the no-match months
"""

import sys
import os
import csv
import io
from pathlib import Path
from collections import defaultdict
from decimal import Decimal
from itertools import combinations

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

def pivot_month(month_key: str, rows: list[dict]):
    """Produce Level 1/2/3 pivot for a month."""
    print(f"\n{'='*80}")
    print(f"PIVOT: {month_key}")
    print(f"{'='*80}")

    # Level 1: Whole month total
    total_debits = sum(_row_money(r, "debit") for r in rows)
    total_credits = sum(_row_money(r, "credit") for r in rows)

    print(f"\n[LEVEL 1] WHOLE MONTH TOTAL")
    print(f"  Debits:  ${total_debits:>12.2f}")
    print(f"  Credits: ${total_credits:>12.2f}")
    print(f"  Delta:   ${(total_debits - total_credits):>12.2f}")

    if round(total_debits, 2) == round(total_credits, 2):
        print(f"  [PASS] Month balances")
    else:
        print(f"  [WARN] Month does NOT balance (sanity issue)")

    # Level 2: Per source journal
    by_journal = defaultdict(lambda: {"debits": Decimal("0"), "credits": Decimal("0"), "rows": []})

    for row in rows:
        journal = source_journal_token(row) or "UNKNOWN"
        by_journal[journal]["debits"] += _row_money(row, "debit")
        by_journal[journal]["credits"] += _row_money(row, "credit")
        by_journal[journal]["rows"].append(row)

    print(f"\n[LEVEL 2] PER SOURCE JOURNAL")
    print(f"{'Journal':<12} {'Debits':>12} {'Credits':>12} {'Delta':>12} {'Status'}")
    print("-" * 55)

    unbalanced_journals = []
    for journal in sorted(by_journal.keys()):
        data = by_journal[journal]
        debits = data["debits"]
        credits = data["credits"]
        delta = debits - credits

        status = "OK" if round(debits, 2) == round(credits, 2) else "IMBALANCED"
        print(f"{journal:<12} ${debits:>11.2f} ${credits:>11.2f} ${delta:>11.2f} {status}")

        if round(debits, 2) != round(credits, 2):
            unbalanced_journals.append(journal)

    # Level 3: Per transaction in unbalanced journals
    if unbalanced_journals:
        for journal in unbalanced_journals:
            print(f"\n[LEVEL 3] {journal} JOURNAL — PER TRANSACTION")
            print(f"{'Entry Number':<15} {'Description':<35} {'Debit':>12} {'Credit':>12}")
            print("-" * 80)

            by_txn = defaultdict(lambda: {"debits": Decimal("0"), "credits": Decimal("0"), "desc": ""})

            for row in by_journal[journal]["rows"]:
                txn_id = row.get("transaction_id", "").strip() or "(no ID)"
                desc = row.get("description", "").strip()[:32]
                debit = _row_money(row, "debit")
                credit = _row_money(row, "credit")

                by_txn[txn_id]["debits"] += debit
                by_txn[txn_id]["credits"] += credit
                by_txn[txn_id]["desc"] = desc

            for txn_id in sorted(by_txn.keys(), key=lambda x: x if x != "(no ID)" else "zzz"):
                data = by_txn[txn_id]
                print(f"{txn_id:<15} {data['desc']:<35} ${data['debits']:>11.2f} ${data['credits']:>11.2f}")

    return unbalanced_journals, by_journal

def check_two_row_combinations(month_key: str, target_delta: Decimal, by_journal: dict):
    """Check if any 2-row combination in any unbalanced journal closes the gap."""
    print(f"\n[BONUS] TWO-ROW COMBINATION CHECK for {month_key}")

    for journal in sorted(by_journal.keys()):
        data = by_journal[journal]
        if round(data["debits"], 2) == round(data["credits"], 2):
            continue  # Skip balanced journals

        print(f"\n  Scanning {journal} journal for 2-row combinations that close ${target_delta:.2f}...")

        rows_with_values = [r for r in data["rows"] if _row_money(r, "debit") > 0 or _row_money(r, "credit") > 0]

        found = False
        for r1, r2 in combinations(rows_with_values, 2):
            r1_debit = _row_money(r1, "debit")
            r1_credit = _row_money(r1, "credit")
            r2_debit = _row_money(r2, "debit")
            r2_credit = _row_money(r2, "credit")

            r1_net = r1_debit - r1_credit
            r2_net = r2_debit - r2_credit
            combined_net = r1_net + r2_net

            if round(combined_net, 2) == round(abs(target_delta), 2):
                r1_txn = r1.get("transaction_id", "").strip() or "(no ID)"
                r2_txn = r2.get("transaction_id", "").strip() or "(no ID)"
                r1_desc = r1.get("description", "").strip()[:30]
                r2_desc = r2.get("description", "").strip()[:30]

                print(f"    FOUND: {r1_txn} ({r1_desc}) + {r2_txn} ({r2_desc})")
                print(f"      Row1 net: ${r1_net:.2f}, Row2 net: ${r2_net:.2f}, Combined: ${combined_net:.2f}")
                found = True

        if not found:
            print(f"    No 2-row combination closes the gap.")

def main():
    """Process Jan 2021 and the 3 no-match months."""
    base_path = Path("C:/Users/atala/Desktop/SampleLaw PCLawFiles/Original Files")

    # Target files
    target_files = [
        ("2021-01", "GLa04888_GL - 2021-01.xlsx"),
        ("2022-01", "GLa04888_GL - 2022-01.xlsx"),
        ("2022-04", "GLa04888_GL - 2022-04.xlsx"),
        ("2022-05", "GLa04888_GL - 2022-05.xlsx"),
    ]

    print("\n" + "="*80)
    print("PIVOT-TABLE INVESTIGATION: JAN 2021 + NO-MATCH MONTHS")
    print("="*80)

    for month_key, filename in target_files:
        filepath = base_path / filename
        rows = load_gl_from_excel(str(filepath))

        if not rows:
            print(f"\n[ERROR] Could not load {filename}")
            continue

        # Filter to just this month
        month_rows = []
        for row in rows:
            iso_date = parse_gl_date(row.get("date"))
            if iso_date and iso_date[:7] == month_key:
                month_rows.append(row)

        if not month_rows:
            print(f"\n[ERROR] No rows found for {month_key}")
            continue

        # Pivot
        unbalanced_journals, by_journal = pivot_month(month_key, month_rows)

        # Bonus: 2-row check for no-match months
        if month_key in ["2022-01", "2022-04", "2022-05"]:
            total_debits = sum(_row_money(r, "debit") for r in month_rows)
            total_credits = sum(_row_money(r, "credit") for r in month_rows)
            target_delta = total_debits - total_credits
            check_two_row_combinations(month_key, target_delta, by_journal)

    print(f"\n" + "="*80)
    print("INVESTIGATION COMPLETE")
    print("="*80)
    return 0

if __name__ == "__main__":
    sys.exit(main())
