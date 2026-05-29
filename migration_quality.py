"""
Migration quality layer: dry-run preview, validation report, reconciliation report.

These three reports give a law-firm customer the confidence to post a PCLaw
ledger to QuickBooks Online by letting them inspect exactly what will happen
*before* anything is written, and exactly what *did* happen after.

Nothing in this module calls QBO write/create endpoints. The preview uses
the same parsing + mapping logic the real importer uses, but stops short of
posting. The validation and reconciliation reports are read-only renderings
of state we already have (preflight summary, account mapping, import
history).

All CSV output is run through ``csv_safety.sanitize_csv_cell`` so a
malicious description / account name in a PCLaw export cannot turn an
opened report into a spreadsheet formula. See ``csv_safety.py`` for the
threat model.
"""

from __future__ import annotations

import csv
from collections import OrderedDict
from decimal import Decimal
from io import StringIO
from typing import Iterable, Optional

from csv_safety import sanitize_csv_cell
from gl_row_quality import classify_gl_rows, is_blank_row
from pclaw_pipeline import (
    GL_REQUIRED_COLUMNS,
    build_account_mapping_from_names,
    build_account_mapping_from_numbers,
    build_account_type_index,
    derive_entity_hint,
    find_unmapped_accounts,
    group_rows_by_transaction,
    money,
)


def _dollar(value) -> str:
    """Format a Decimal/str as a fixed-2 dollar amount (no $ sign)."""
    if value is None or value == "":
        return "0.00"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return f"{value:.2f}"


def build_dry_run_preview(
    rows: list[dict],
    qbo_accounts_response: dict,
    saved_mappings: Optional[list[dict]] = None,
    *,
    sample_limit: int = 10,
) -> dict:
    """Return a non-destructive preview of what an import would post.

    No QBO write endpoint is invoked. ``qbo_accounts_response`` is the
    same payload returned by ``QBOClient.get_accounts()``.

    The shape is intentionally JSON-friendly and beginner-readable so it
    can be rendered in templates and dumped into reports.
    """
    saved_mappings = saved_mappings or []

    # Mirror the importer's decision: prefer number-based matching when any
    # numbers are present (stable across renames in QBO), otherwise fall
    # back to name matching.
    auto_by_number = build_account_mapping_from_numbers(qbo_accounts_response)
    auto_by_name = build_account_mapping_from_names(qbo_accounts_response)

    if auto_by_number or any(m.get("pclaw_account_number") for m in saved_mappings):
        mapping = dict(auto_by_number)
        mapping_mode = "number"
        for m in saved_mappings:
            if m.get("pclaw_account_number"):
                mapping[str(m["pclaw_account_number"])] = m["qbo_account_id"]
    else:
        mapping = dict(auto_by_name)
        mapping_mode = "name"
        for m in saved_mappings:
            if m.get("pclaw_account_name"):
                mapping[m["pclaw_account_name"]] = m["qbo_account_id"]

    account_type_index = build_account_type_index(qbo_accounts_response)
    qbo_name_by_id = {
        a.get("Id"): a.get("Name")
        for a in qbo_accounts_response.get("QueryResponse", {}).get("Account", [])
        if a.get("Id")
    }
    qbo_acctnum_by_id = {
        a.get("Id"): (a.get("AcctNum") or "").strip() or None
        for a in qbo_accounts_response.get("QueryResponse", {}).get("Account", [])
        if a.get("Id")
    }

    # Drop truly blank rows before grouping. They contribute nothing and,
    # left in place, get bucketed as their own zero-sum "transaction".
    rows = [r for r in (rows or []) if not is_blank_row(r)]
    quality = classify_gl_rows(rows)

    grouped = group_rows_by_transaction(rows) if rows else OrderedDict()
    unmapped_accounts = sorted(find_unmapped_accounts(rows, mapping, mapping_mode)) if rows else []

    debits = Decimal("0.00")
    credits = Decimal("0.00")
    unique_accounts: OrderedDict[str, dict] = OrderedDict()
    customers_needed: dict[str, dict] = {}
    vendors_needed: dict[str, dict] = {}
    blocked_transactions: list[dict] = []
    sample_lines: list[dict] = []

    for txn_id, txn_rows in grouped.items():
        txn_debits = sum(money(r["debit"]) for r in txn_rows)
        txn_credits = sum(money(r["credit"]) for r in txn_rows)
        blockers: list[str] = []
        if txn_debits != txn_credits:
            blockers.append(
                f"unbalanced (debits={_dollar(txn_debits)}, credits={_dollar(txn_credits)})"
            )
        if len([r for r in txn_rows if money(r["debit"]) or money(r["credit"])]) < 2:
            blockers.append("fewer than 2 posting lines")

        for r in txn_rows:
            debit = money(r["debit"])
            credit = money(r["credit"])
            debits += debit
            credits += credit
            if debit and credit:
                blockers.append("row has both debit and credit set")

            key = r.get("account_number") if mapping_mode == "number" else r.get("account_name")
            display = f"{(r.get('account_number') or '').strip()} {(r.get('account_name') or '').strip()}".strip()
            mapped = bool(key and mapping.get(key))
            qbo_id = mapping.get(key) if mapped else None
            entry = unique_accounts.setdefault(
                display or "(blank)",
                {
                    "pclaw_display": display or "(blank)",
                    "pclaw_account_number": (r.get("account_number") or "").strip() or None,
                    "pclaw_account_name": (r.get("account_name") or "").strip() or None,
                    "mapping_key": key or "",
                    "mapped": mapped,
                    "qbo_account_id": qbo_id,
                    "qbo_account_name": qbo_name_by_id.get(qbo_id) if qbo_id else None,
                    "qbo_acct_num": qbo_acctnum_by_id.get(qbo_id) if qbo_id else None,
                    "line_count": 0,
                },
            )
            entry["line_count"] += 1

            if mapped:
                qbo_type = account_type_index.get(qbo_id)
                hint = derive_entity_hint(r, qbo_type)
                if hint:
                    kind, name = hint
                    bucket = customers_needed if kind == "Customer" else vendors_needed
                    rec = bucket.setdefault(name, {"name": name, "kind": kind, "lines": 0})
                    rec["lines"] += 1

            if len(sample_lines) < sample_limit and (debit or credit):
                sample_lines.append({
                    "transaction_id": txn_id,
                    "date": r.get("date"),
                    "account": display,
                    "qbo_account_id": qbo_id,
                    "qbo_acct_num": qbo_acctnum_by_id.get(qbo_id) if qbo_id else None,
                    "posting_type": "Debit" if debit else "Credit",
                    "amount": _dollar(debit if debit else credit),
                    "description": (r.get("description") or "")[:120],
                    "mapped": mapped,
                })

        if blockers:
            blocked_transactions.append({
                "transaction_id": txn_id,
                "line_count": len(txn_rows),
                "reasons": sorted(set(blockers)),
            })

    mapped_count = sum(1 for v in unique_accounts.values() if v["mapped"])
    unmapped_count = sum(1 for v in unique_accounts.values() if not v["mapped"])

    je_count = len(grouped) - len(blocked_transactions)
    je_count = max(je_count, 0)

    return {
        "would_post": (
            unmapped_count == 0
            and not blocked_transactions
            and not quality.problem_rows
            and not quality.beginning_balance_rows
            and je_count > 0
            and debits == credits
        ),
        "mapping_mode": mapping_mode,
        "journal_entry_count": je_count,
        "transaction_count_total": len(grouped),
        "line_count": len(rows or []),
        "blank_rows_skipped": quality.blank_rows,
        "total_debits": _dollar(debits),
        "total_credits": _dollar(credits),
        "balanced": debits == credits and (debits + credits) > 0,
        "unique_account_count": len(unique_accounts),
        "mapped_account_count": mapped_count,
        "unmapped_account_count": unmapped_count,
        "unmapped_accounts": unmapped_accounts,
        "accounts": list(unique_accounts.values()),
        "customers": sorted(customers_needed.values(), key=lambda x: x["name"].lower()),
        "vendors": sorted(vendors_needed.values(), key=lambda x: x["name"].lower()),
        "blocked_transactions": blocked_transactions,
        "problem_rows": [r.to_dict() for r in quality.problem_rows],
        "beginning_balance_rows": [r.to_dict() for r in quality.beginning_balance_rows],
        "row_quality_counts": quality.counts_by_kind(),
        "sample_lines": sample_lines,
        "missing_required_columns": [
            c for c in GL_REQUIRED_COLUMNS
            if not rows or c not in (rows[0].keys() if rows else [])
        ] if rows else list(GL_REQUIRED_COLUMNS),
    }


def render_validation_csv(job: dict, preflight: dict, preview: Optional[dict] = None) -> str:
    """Render a per-job validation report as CSV.

    The report is a small key/value table the user can hand to their
    accountant. It deliberately keeps the row layout simple so opening
    the file in any spreadsheet renders cleanly. All cells are
    sanitized through ``csv_safety``.

    The body adapts to the job's report_type. GL jobs render the classic
    Transactions/Lines/Debits/Credits block; COA / Trial Balance / Trust
    Listing jobs render report-specific counts so the same download
    works as the system-of-record for any supported report.
    """
    output = StringIO()
    writer = csv.writer(output)

    def write(label, value):
        writer.writerow([
            sanitize_csv_cell(label),
            sanitize_csv_cell("" if value is None else str(value)),
        ])

    writer.writerow([sanitize_csv_cell("Field"), sanitize_csv_cell("Value")])
    write("Job ID", job.get("id"))
    write("File name", job.get("source_file"))
    write("Company (firm-supplied)", job.get("company"))
    write("Created (UTC)", (job.get("created_at") or "")[:19].replace("T", " "))
    write("File SHA-256", job.get("file_sha256"))
    summary = job.get("summary") or {}
    write("Format detected", summary.get("format"))
    write("Rows parsed", summary.get("row_count"))

    report_type = (
        preflight.get("report_type")
        or summary.get("report_type")
        or job.get("report_type")
        or "general_ledger"
    )
    write("Report type", report_type)
    write("Report label", preflight.get("report_label") or summary.get("format") or "General Ledger")

    write("--- Preflight ---", "")
    if report_type == "chart_of_accounts":
        write("Accounts in file", preflight.get("account_count"))
        write("Rows missing name", preflight.get("rows_missing_name"))
        write("Rows missing type", preflight.get("rows_missing_type"))
        write("Inactive accounts", preflight.get("inactive_account_count"))
        write(
            "Duplicate account numbers",
            ", ".join(preflight.get("duplicate_account_numbers") or []) or "None",
        )
        for t, count in (preflight.get("type_counts") or []):
            write(f"  type: {t}", count)
        write(
            "Missing required columns",
            ", ".join(preflight.get("missing_required_columns") or []) or "None",
        )
    elif report_type == "trial_balance":
        write("Accounts in file", preflight.get("account_count"))
        write("Unique accounts", preflight.get("unique_account_count"))
        write("Total debits", preflight.get("total_debit"))
        write("Total credits", preflight.get("total_credit"))
        write("Balanced", "Yes" if preflight.get("balanced") else "No")
        write("Out-of-balance amount", preflight.get("out_of_balance_amount"))
        write("Rows missing account", preflight.get("rows_missing_account"))
        write(
            "Missing required columns",
            ", ".join(preflight.get("missing_required_columns") or []) or "None",
        )
    elif report_type == "trust_listing":
        write("Rows in file", preflight.get("row_count"))
        write("Distinct clients", preflight.get("client_count"))
        write("Distinct matters", preflight.get("matter_count"))
        write("Total trust balance", preflight.get("total_trust_balance"))
        write("Negative balances", preflight.get("negative_balance_count"))
        write("Rows missing identifier", preflight.get("rows_missing_identifier"))
        for bank, count in (preflight.get("trust_bank_accounts") or []):
            write(f"  trust bank: {bank}", count)
        write(
            "Missing required columns",
            ", ".join(preflight.get("missing_required_columns") or []) or "None",
        )
    else:
        write("Transactions", preflight.get("transaction_count"))
        write("Lines", preflight.get("line_count"))
        write("Total debits", preflight.get("total_debits"))
        write("Total credits", preflight.get("total_credits"))
        write("Balanced", "Yes" if preflight.get("balanced") else "No")
        write("Unique accounts", preflight.get("unique_account_count"))
        write(
            "Missing required columns",
            ", ".join(preflight.get("missing_required_columns") or []) or "None",
        )
        write("Rows missing account", preflight.get("rows_missing_account"))
        write("Rows missing date", preflight.get("rows_missing_date"))

    if preview is not None:
        write("--- Mapping preview ---", "")
        write("Mapping mode", preview.get("mapping_mode"))
        write("Mapped accounts", preview.get("mapped_account_count"))
        write("Unmapped accounts", preview.get("unmapped_account_count"))
        write("Unmapped account list", "; ".join(preview.get("unmapped_accounts") or []) or "None")
        write("Blocked transactions", len(preview.get("blocked_transactions") or []))
        write("Customers needed", len(preview.get("customers") or []))
        write("Vendors needed", len(preview.get("vendors") or []))
        write("Would post", "Yes" if preview.get("would_post") else "No")

    if job.get("last_validation_error"):
        ve = job["last_validation_error"]
        write("--- Validation error ---", "")
        write("Headline", ve.get("headline"))
        write("Action", ve.get("action"))

    if job.get("unmapped_accounts"):
        write("--- Unmapped from last attempt ---", "")
        for a in job["unmapped_accounts"]:
            write("Unmapped account", a)

    if job.get("last_import_id"):
        write("--- Import status ---", "")
        write("Last import id", job.get("last_import_id"))
        write("Status", job.get("status"))

    # Rows that need a fix in the customer's CSV. This is the single
    # most-asked-for block — Cesar's QA on 2026-05-29 showed the report
    # was reporting counts but never naming the specific rows the user
    # had to edit. Each row carries its 1-based source line, a
    # plain-English reason, and a concrete fix suggestion.
    problem_rows = (preflight.get("problem_rows") or []) + (
        preview.get("problem_rows") if preview else []
    )
    # Deduplicate by (row_number, kind) — the preflight and the preview
    # can both surface the same row, e.g. a no-date row that is also
    # single-sided. Keep the preflight entry first so source line numbers
    # match the user's CSV.
    seen_rows: set[tuple] = set()
    deduped_problem_rows: list[dict] = []
    for r in problem_rows:
        key = (r.get("row_number"), r.get("kind"))
        if key in seen_rows:
            continue
        seen_rows.add(key)
        deduped_problem_rows.append(r)

    if deduped_problem_rows:
        writer.writerow([])
        writer.writerow([
            sanitize_csv_cell("--- Rows that need a fix ---"),
            sanitize_csv_cell(""),
        ])
        writer.writerow([
            sanitize_csv_cell("Row number (CSV)"),
            sanitize_csv_cell("Transaction reference"),
            sanitize_csv_cell("Date (raw)"),
            sanitize_csv_cell("Account"),
            sanitize_csv_cell("Debit"),
            sanitize_csv_cell("Credit"),
            sanitize_csv_cell("Issue"),
            sanitize_csv_cell("Reason"),
            sanitize_csv_cell("How to fix"),
        ])
        for r in deduped_problem_rows:
            writer.writerow([
                sanitize_csv_cell(r.get("row_number")),
                sanitize_csv_cell(r.get("transaction_id") or ""),
                sanitize_csv_cell(r.get("raw_date") or ""),
                sanitize_csv_cell(r.get("account") or ""),
                sanitize_csv_cell(r.get("debit") or ""),
                sanitize_csv_cell(r.get("credit") or ""),
                sanitize_csv_cell(r.get("kind") or ""),
                sanitize_csv_cell(r.get("reason") or ""),
                sanitize_csv_cell(r.get("plain_fix") or ""),
            ])

    # Beginning-balance rows broken out separately so the user knows
    # to move them to the Starting Balances upload instead of trying
    # to "fix" them in the general ledger.
    bb_rows = (preflight.get("beginning_balance_rows") or []) + (
        preview.get("beginning_balance_rows") if preview else []
    )
    seen_bb: set[tuple] = set()
    deduped_bb_rows: list[dict] = []
    for r in bb_rows:
        key = (r.get("row_number"), r.get("account"))
        if key in seen_bb:
            continue
        seen_bb.add(key)
        deduped_bb_rows.append(r)
    if deduped_bb_rows:
        writer.writerow([])
        writer.writerow([
            sanitize_csv_cell("--- Beginning-balance rows (move to Starting Balances) ---"),
            sanitize_csv_cell(""),
        ])
        writer.writerow([
            sanitize_csv_cell("Row number (CSV)"),
            sanitize_csv_cell("Account"),
            sanitize_csv_cell("Debit"),
            sanitize_csv_cell("Credit"),
            sanitize_csv_cell("Why this is here"),
        ])
        for r in deduped_bb_rows:
            writer.writerow([
                sanitize_csv_cell(r.get("row_number")),
                sanitize_csv_cell(r.get("account") or ""),
                sanitize_csv_cell(r.get("debit") or ""),
                sanitize_csv_cell(r.get("credit") or ""),
                sanitize_csv_cell(r.get("reason") or ""),
            ])

    # Per-account detail block (one row per unique account).
    if preview and preview.get("accounts"):
        writer.writerow([])
        writer.writerow([
            sanitize_csv_cell("PCLaw account"),
            sanitize_csv_cell("Mapping key"),
            sanitize_csv_cell("Mapped?"),
            sanitize_csv_cell("QBO account id"),
            sanitize_csv_cell("QBO account name"),
            sanitize_csv_cell("Line count"),
        ])
        for a in preview["accounts"]:
            writer.writerow([
                sanitize_csv_cell(a["pclaw_display"]),
                sanitize_csv_cell(a["mapping_key"]),
                sanitize_csv_cell("Yes" if a["mapped"] else "No"),
                sanitize_csv_cell(a["qbo_account_id"] or ""),
                sanitize_csv_cell(a["qbo_account_name"] or ""),
                sanitize_csv_cell(a["line_count"]),
            ])

    return output.getvalue()


def build_reconciliation_report(
    job: dict,
    import_record: Optional[dict],
    verification: Optional[dict] = None,
    reversal: Optional[dict] = None,
) -> dict:
    """Return a structured post-import reconciliation summary.

    ``import_record`` is a row from ``ImportHistory.get_history_for_job``
    (latest successful). ``verification`` is ``job["verification"]`` if
    present. ``reversal`` is the reversal dict on the import_record.
    """
    txns = (import_record or {}).get("transactions") or []
    qbo_je_ids = [t.get("qbo_je_id") for t in txns if t.get("qbo_je_id")]
    intuit_tid = (job.get("last_error") or {}).get("intuit_tid")

    return {
        "job_id": job.get("id"),
        "company": (import_record or {}).get("company_name") or job.get("company"),
        "import_id": (import_record or {}).get("id"),
        "imported_at": (import_record or {}).get("created_at"),
        "status": (import_record or {}).get("status") or job.get("status"),
        "created_je_count": (import_record or {}).get("transaction_count") or len(txns),
        "debit_total": (import_record or {}).get("debit_total"),
        "credit_total": (import_record or {}).get("credit_total"),
        "qbo_je_ids": qbo_je_ids,
        "transactions": txns,
        "verification": verification or job.get("verification"),
        "intuit_tid": intuit_tid,
        "support_reference": intuit_tid,
        "reversal": reversal or (import_record or {}).get("reversal"),
    }


def render_reconciliation_csv(report: dict) -> str:
    """Render the reconciliation report as a CSV download. Sanitized."""
    output = StringIO()
    writer = csv.writer(output)

    def write(label, value):
        writer.writerow([
            sanitize_csv_cell(label),
            sanitize_csv_cell("" if value is None else str(value)),
        ])

    writer.writerow([sanitize_csv_cell("Field"), sanitize_csv_cell("Value")])
    write("Job ID", report.get("job_id"))
    write("QuickBooks company", report.get("company"))
    write("Import id", report.get("import_id"))
    write("Imported at (UTC)", (report.get("imported_at") or "")[:19].replace("T", " "))
    write("Status", report.get("status"))
    write("Created JE count", report.get("created_je_count"))
    write("Total posted debits", report.get("debit_total"))
    write("Total posted credits", report.get("credit_total"))
    if report.get("intuit_tid"):
        write("Intuit support reference (intuit_tid)", report.get("intuit_tid"))

    v = report.get("verification") or {}
    if v:
        write("--- Verification ---", "")
        write("Verification status", v.get("status"))
        write("QBO debits (re-fetched)", v.get("qbo_debit_total"))
        write("QBO credits (re-fetched)", v.get("qbo_credit_total"))
        write("JE count match", "Yes" if v.get("je_count_match") else "No")
        write("Debits match", "Yes" if v.get("debits_match") else "No")
        write("Credits match", "Yes" if v.get("credits_match") else "No")
        if v.get("not_found_ids"):
            write("JE ids missing in QBO", ", ".join(v["not_found_ids"]))
        write("Verified at (UTC)", (v.get("verified_at") or "")[:19].replace("T", " "))

    rev = report.get("reversal")
    if rev:
        write("--- Reversal ---", "")
        write("Reversal status", rev.get("status"))
        write("Reversed at (UTC)", (rev.get("reversed_at") or "")[:19].replace("T", " "))
        if rev.get("error"):
            write("Reversal error", rev.get("error"))

    writer.writerow([])
    writer.writerow([
        sanitize_csv_cell("PCLaw transaction"),
        sanitize_csv_cell("QBO JournalEntry Id"),
        sanitize_csv_cell("QBO DocNumber"),
        sanitize_csv_cell("Txn date"),
    ])
    for t in report.get("transactions") or []:
        writer.writerow([
            sanitize_csv_cell(t.get("transaction_id")),
            sanitize_csv_cell(t.get("qbo_je_id")),
            sanitize_csv_cell(t.get("doc_number")),
            sanitize_csv_cell(t.get("txn_date")),
        ])

    return output.getvalue()
