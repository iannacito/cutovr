"""Lightweight PCLaw GL CSV parser used by the legacy /upload route.

Real PCLaw GL exports from different firms use different header
conventions — sometimes "Posting Date", sometimes "GL Account",
sometimes a single signed "Amount" column instead of debit + credit.
This parser auto-detects those variations through ``detect_gl_columns``
instead of forcing the user back to the upload screen with a "column
not found" error. The richer "transaction history" pipeline that
posts to QuickBooks lives in ``pclaw_pipeline.py``.

What we auto-detect:

- ``date`` columns named ``Date``, ``TxnDate``, ``Transaction Date``,
  ``Posting Date``, ``GL Date``, ``Entry Date``, ``Trans Date``.
- ``account`` columns named ``Account``, ``GL Account``, ``Account
  Name``, ``Account Number``, ``Ledger Account``.
- ``description / memo`` named ``Description``, ``Memo``, ``Details``,
  ``Notes``, ``Narrative``, ``Reference``.
- ``debit`` / ``credit`` named ``Debit``, ``Credit``, ``Debit Amount``,
  ``Credit Amount``; or a single signed ``Amount`` / ``Net Amount`` /
  ``Net`` column where negative values mean credit.
"""

from pathlib import Path
import csv
from decimal import Decimal
from io import StringIO

from csv_safety import sanitize_csv_cell
from csv_decode import open_csv_text


# Logical column -> ordered list of header synonyms we accept (matched
# case-insensitively, ignoring non-alphanumerics so "GL Date", "gl_date",
# and "GL-Date" all collapse to the same key).
GL_COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "date": (
        "Date", "TxnDate", "Transaction Date", "Posting Date",
        "Post Date", "GL Date", "Entry Date", "Trans Date", "Doc Date",
        "Journal Date",
    ),
    "account": (
        "Account", "Account Name", "Account Number", "GL Account",
        "Acct", "AcctNum", "Ledger Account", "Acct Name",
    ),
    "description": (
        "Description", "Memo", "Notes", "Narrative", "Details",
        "Reference", "Reference Description", "Line Description",
    ),
    "debit": (
        "Debit", "Debit Amount", "DR", "Dr Amount", "Debits",
    ),
    "credit": (
        "Credit", "Credit Amount", "CR", "Cr Amount", "Credits",
    ),
    "amount": (
        "Amount", "Net Amount", "Net", "Signed Amount",
        "Transaction Amount", "Posting Amount",
    ),
    "client": (
        "Client", "Client ID", "Client Name", "Client No",
    ),
    "matter": (
        "Matter", "Matter ID", "Matter Name", "Matter No",
    ),
}


def _norm_header(name) -> str:
    """Normalize a header for matching: lowercase, alphanumerics only."""
    if name is None:
        return ""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def detect_gl_columns(fieldnames):
    """Return ``(mapping, missing_logical)`` for a GL CSV's headers.

    ``mapping`` is a dict ``logical -> original header`` for every
    logical column we resolved. ``missing_logical`` lists the columns
    we couldn't satisfy from any synonym. The customer-facing message
    on the upload screen reads from this so we can say "no date column"
    instead of "Missing required columns: Date".

    ``debit`` / ``credit`` count as resolved when a signed ``amount``
    column is present — we'll synthesize them from it at row time.
    """
    if not fieldnames:
        return {}, list(GL_COLUMN_SYNONYMS.keys())
    by_norm: dict[str, str] = {}
    for raw in fieldnames:
        by_norm.setdefault(_norm_header(raw), raw)

    mapping: dict[str, str] = {}
    for logical, synonyms in GL_COLUMN_SYNONYMS.items():
        for syn in synonyms:
            key = _norm_header(syn)
            if key in by_norm:
                mapping[logical] = by_norm[key]
                break

    missing: list[str] = []
    for logical in ("date", "account", "debit", "credit"):
        if logical in mapping:
            continue
        # A signed amount column substitutes for both debit and credit.
        if logical in ("debit", "credit") and "amount" in mapping:
            continue
        missing.append(logical)
    # Description is optional; we'll fall back to a blank memo.
    return mapping, missing


# Kept for backwards compatibility with code that imports the constant.
REQUIRED_COLUMNS = ["Date", "Account", "Description", "Debit", "Credit"]


def _clean_money(value):
    s = (value or "0").replace(",", "").replace("$", "").strip()
    if not s:
        return Decimal("0")
    negative = False
    # Accounting-style negatives: "(1,234.56)" -> -1234.56.
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    try:
        result = Decimal(s or "0")
    except Exception:
        result = Decimal("0")
    return -result if negative else result


def parse_pclaw_csv(file_path):
    """Parse a PCLaw-style GL CSV into normalized rows.

    Header detection is tolerant: any of the synonyms listed in
    ``GL_COLUMN_SYNONYMS`` resolves a logical column, and a signed
    ``Amount`` column substitutes for a missing debit/credit pair.
    """
    file_path = Path(file_path)
    text, _encoding_used = open_csv_text(file_path)
    with StringIO(text) as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        mapping, missing = detect_gl_columns(fieldnames)
        if missing:
            friendly = {
                "date": "a date column (Date, Posting Date, GL Date, …)",
                "account": "an account column (Account, GL Account, …)",
                "debit": "debit and credit columns (or a signed Amount column)",
                "credit": "debit and credit columns (or a signed Amount column)",
            }
            seen: list[str] = []
            for k in missing:
                label = friendly.get(k, k)
                if label not in seen:
                    seen.append(label)
            raise ValueError(
                "We couldn't find " + "; ".join(seen) + " in this CSV."
            )

        date_col = mapping.get("date")
        account_col = mapping.get("account")
        desc_col = mapping.get("description")
        debit_col = mapping.get("debit")
        credit_col = mapping.get("credit")
        amount_col = mapping.get("amount")

        rows = []
        for row in reader:
            if debit_col or credit_col:
                debit = _clean_money(row.get(debit_col) if debit_col else "")
                credit = _clean_money(row.get(credit_col) if credit_col else "")
            else:
                signed = _clean_money(row.get(amount_col) if amount_col else "0")
                if signed >= 0:
                    debit, credit = signed, Decimal("0")
                else:
                    debit, credit = Decimal("0"), -signed
            amount = debit - credit
            rows.append(
                {
                    "txn_date": (row.get(date_col, "") or "").strip(),
                    "account": (row.get(account_col, "") or "").strip(),
                    "memo": (row.get(desc_col, "") or "").strip() if desc_col else "",
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
