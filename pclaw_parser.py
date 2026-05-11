from pathlib import Path
import csv
from decimal import Decimal

from csv_safety import sanitize_csv_cell

REQUIRED_COLUMNS = ["Date", "Account", "Description", "Debit", "Credit"]

def _clean_money(value):
    value = (value or "0").replace(",", "").replace("$", "").strip()
    if not value:
        return Decimal("0")
    return Decimal(value)

def parse_pclaw_csv(file_path):
    """Parse a PCLaw-style GL CSV into normalized rows."""
    file_path = Path(file_path)
    with file_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ValueError(f"Missing required columns: {', '.join(missing)}")

        rows = []
        for row in reader:
            debit = _clean_money(row.get("Debit"))
            credit = _clean_money(row.get("Credit"))
            amount = debit - credit
            rows.append(
                {
                    "txn_date": row.get("Date", "").strip(),
                    "account": row.get("Account", "").strip(),
                    "memo": row.get("Description", "").strip(),
                    "amount": f"{amount:.2f}",
                    "debit": f"{debit:.2f}",
                    "credit": f"{credit:.2f}",
                }
            )
        return rows

def export_qbo_csv(rows, output_path):
    """Write a QBO-style journal CSV from normalized rows."""
    output_path = Path(output_path)
    total_debit = sum(float(r["debit"]) for r in rows)
    total_credit = sum(float(r["credit"]) for r in rows)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["JournalNo", "TxnDate", "Account", "Memo", "Amount"],
        )
        writer.writeheader()
        for idx, row in enumerate(rows, start=1):
            # CSV formula-injection defense: PCLaw memo / account text is
            # user-controlled. If a cell begins with `=`, `+`, `-`, `@`,
            # or a tab/CR, Excel / Sheets will treat it as a formula.
            # Prepending a tick neutralizes that without altering the
            # text the recipient sees on screen. See csv_safety.py.
            writer.writerow(
                {
                    "JournalNo": idx,
                    "TxnDate": sanitize_csv_cell(row["txn_date"]),
                    "Account": sanitize_csv_cell(row["account"]),
                    "Memo": sanitize_csv_cell(row["memo"]),
                    "Amount": sanitize_csv_cell(row["amount"]),
                }
            )

    return {
        "row_count": len(rows),
        "total_debit": round(total_debit, 2),
        "total_credit": round(total_credit, 2),
        "balanced": round(total_debit, 2) == round(total_credit, 2),
    }