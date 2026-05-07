"""
Preflight summary for an uploaded PCLaw GL file.

Builds a structured, customer-friendly checklist describing the state of the
ledger before it is posted to QuickBooks. Used by the upload route to attach
a `preflight` block to a job and by the job-detail page to render it.

The returned shape is intentionally small and JSON-friendly so it can be
serialized into the job dict, persisted, and read by tests without pulling
in a heavy schema library.

Sensitive content (raw rows, descriptions, account numbers) is NOT echoed
in the returned messages — only counts and lists of column names. That keeps
flash messages and the job-detail page safe to share with support.
"""

from decimal import Decimal

from pclaw_pipeline import (
    GL_REQUIRED_COLUMNS,
    group_rows_by_transaction,
    money,
)


def _row_account_key(row):
    """Stable display string for an account in this row.

    Falls back across number / name so the preflight summary still shows
    something reasonable on partially-filled rows.
    """
    num = (row.get("account_number") or "").strip()
    name = (row.get("account_name") or "").strip()
    if num and name:
        return f"{num} {name}"
    return num or name or ""


def build_preflight_summary(rows, fieldnames=None):
    """Return a structured preflight summary for a list of GL rows.

    Parameters
    ----------
    rows : list[dict]
        Rows from `pclaw_pipeline.load_general_ledger_csv`. May be empty.
    fieldnames : list[str] | None
        The CSV header row, used to detect missing required columns. If
        omitted, the keys of the first row are used.

    Returns
    -------
    dict
        A summary safe to render in templates and persist on the job.
    """
    rows = rows or []
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    missing_columns = [c for c in GL_REQUIRED_COLUMNS if c not in fieldnames]

    debits = Decimal("0.00")
    credits = Decimal("0.00")
    accounts = set()
    rows_missing_account = 0
    rows_missing_date = 0

    for row in rows:
        debits += money(row.get("debit"))
        credits += money(row.get("credit"))
        key = _row_account_key(row)
        if key:
            accounts.add(key)
        else:
            rows_missing_account += 1
        if not (row.get("date") or "").strip():
            rows_missing_date += 1

    transactions = group_rows_by_transaction(rows) if rows else {}

    summary = {
        "transaction_count": len(transactions),
        "line_count": len(rows),
        "total_debits": f"{debits:.2f}",
        "total_credits": f"{credits:.2f}",
        "balanced": (debits == credits) and len(rows) > 0,
        "unique_accounts": sorted(accounts),
        "unique_account_count": len(accounts),
        "missing_required_columns": missing_columns,
        "rows_missing_account": rows_missing_account,
        "rows_missing_date": rows_missing_date,
    }
    summary["ready"] = (
        not missing_columns
        and summary["balanced"]
        and summary["line_count"] > 0
        and rows_missing_account == 0
        and rows_missing_date == 0
    )
    return summary


def friendly_validation_message(exc_or_msg):
    """Translate a raw ValueError / pipeline error into beginner-friendly copy.

    Returns a (headline, action) tuple that templates can render. The
    original message is never re-exposed verbatim — we want the customer
    to see something they can act on without leaking row contents into
    flash messages or audit logs.
    """
    msg = str(exc_or_msg or "").strip()
    low = msg.lower()
    if "missing required columns" in low:
        return (
            "The CSV is missing one or more required columns.",
            "Open the sample template from the onboarding page and make sure "
            "your export has every column from the list "
            "(transaction_id, date, account_number, account_name, debit, credit).",
        )
    if "does not balance" in low:
        return (
            "A transaction in your ledger does not balance.",
            "Re-export the general ledger from PCLaw and confirm the date "
            "range is closed in PCLaw — debits and credits must match for each "
            "transaction id.",
        )
    if "no qbo account match" in low or "no qbo match" in low:
        return (
            "An account in the ledger has not been mapped to QuickBooks yet.",
            "Open the Map accounts page on the job and pair every PCLaw "
            "account with the matching QuickBooks Online account before "
            "importing.",
        )
    if "fewer than 2 lines" in low:
        return (
            "A transaction in your ledger has only one side (no offsetting line).",
            "Each PCLaw transaction must have at least one debit row and one "
            "credit row. Re-export the GL from PCLaw and try again.",
        )
    if "no date" in low:
        return (
            "A row in your ledger is missing a date.",
            "Open the CSV in a spreadsheet, fill in the missing date column "
            "(YYYY-MM-DD), and re-upload.",
        )
    if "no account_number or account_name" in low:
        return (
            "A row in your ledger is missing both account number and account name.",
            "Each row needs at least one of account_number or account_name. "
            "Fix the missing rows in the CSV and re-upload.",
        )
    return (
        "We could not process the ledger.",
        "Compare your CSV to the sample template on the onboarding page. "
        "If the columns match and the file still fails, contact support.",
    )
