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

from gl_row_quality import classify_gl_rows, is_blank_row, is_droppable_row
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

    # Truly empty rows (blank trailing rows from Excel, etc.) are skipped
    # so the counts the user sees reflect the real ledger content. Rows
    # with a real amount but a missing or unparseable date still count
    # against ``rows_missing_date`` so the preflight blocks them.
    # Also exclude PCLaw subtotal rows (non-zero amount, no transaction_id).
    significant_rows = [r for r in rows if not is_droppable_row(r)]

    quality = classify_gl_rows(significant_rows)

    for row in significant_rows:
        debits += money(row.get("debit"))
        credits += money(row.get("credit"))
        key = _row_account_key(row)
        if key:
            accounts.add(key)
        else:
            rows_missing_account += 1
        if not (row.get("date") or "").strip():
            # Only flag rows that carry amounts — zero-activity account-header
            # rows (no date AND no debit/credit) are handled by _classify_row
            # as "zero_activity" and must not block the import gate.
            if money(row.get("debit")) != 0 or money(row.get("credit")) != 0:
                rows_missing_date += 1

    transactions = group_rows_by_transaction(significant_rows) if significant_rows else {}

    rows_unparseable_date = quality.counts_by_kind().get("unparseable_date", 0)
    rows_single_sided = quality.counts_by_kind().get("single_sided", 0)
    beginning_balance_row_count = len(quality.beginning_balance_rows)

    summary = {
        "transaction_count": len(transactions),
        "line_count": len(significant_rows),
        "blank_rows_skipped": quality.blank_rows,
        "total_debits": f"{debits:.2f}",
        "total_credits": f"{credits:.2f}",
        "balanced": (debits == credits) and len(significant_rows) > 0,
        "unique_accounts": sorted(accounts),
        "unique_account_count": len(accounts),
        "missing_required_columns": missing_columns,
        "rows_missing_account": rows_missing_account,
        "rows_missing_date": rows_missing_date,
        "rows_unparseable_date": rows_unparseable_date,
        "rows_single_sided": rows_single_sided,
        "beginning_balance_row_count": beginning_balance_row_count,
        "problem_rows": [r.to_dict() for r in quality.problem_rows],
        "beginning_balance_rows": [r.to_dict() for r in quality.beginning_balance_rows],
    }
    summary["ready"] = (
        not missing_columns
        and summary["balanced"]
        and summary["line_count"] > 0
        and rows_missing_account == 0
        and rows_missing_date == 0
        and rows_unparseable_date == 0
        and beginning_balance_row_count == 0
    )
    return summary


def evaluate_import_gate(rows, fieldnames=None):
    """Deterministic go/no-go check run immediately before a QBO write.

    This is the *last* safety gate before journal entries are posted. The
    Step 5 page already blocks on the preflight summary attached at upload
    time, but a direct POST to the import route could bypass that page —
    so we recompute the same deterministic checks here against the exact
    rows about to be posted. Fail closed.

    Returns ``(ok, blockers)`` where ``blockers`` is a list of plain-English
    ``{"headline", "action"}`` dicts. ``ok`` is True only when nothing is
    actionable. No row contents, account numbers, or amounts are placed in
    the blocker text — only safe, customer-facing guidance.
    """
    summary = build_preflight_summary(rows, fieldnames)
    blockers = []

    if summary["missing_required_columns"]:
        blockers.append({
            "headline": "Your file is missing one or more required columns.",
            "action": "Re-export the general ledger from PCLaw using the "
                      "sample template so every required column is present, "
                      "then upload again.",
        })
    if summary["line_count"] == 0:
        blockers.append({
            "headline": "We didn't find any transactions to send.",
            "action": "Check that you uploaded the general-ledger export "
                      "(not an empty file) and try again.",
        })
    if summary["line_count"] and not summary["balanced"]:
        blockers.append({
            "headline": "Your debits and credits don't match.",
            "action": "A balanced ledger needs debits to equal credits. "
                      "Re-export the general ledger from PCLaw for a closed "
                      "period and re-upload.",
        })
    if summary["rows_missing_account"]:
        blockers.append({
            "headline": "Some rows are missing an account.",
            "action": "Open the validation report to see which rows need an "
                      "account, fix them in the CSV, and re-upload.",
        })
    if summary["rows_missing_date"] or summary["rows_unparseable_date"]:
        blockers.append({
            "headline": "Some rows are missing a usable date.",
            "action": "Make sure every row has a date your software can read "
                      "(YYYY-MM-DD works well), then re-upload.",
        })
    if summary["beginning_balance_row_count"]:
        blockers.append({
            "headline": "Your file still contains beginning-balance rows.",
            "action": "Move beginning balances to the Starting Balances step, "
                      "then re-upload the general ledger.",
        })

    return (len(blockers) == 0, blockers)


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
