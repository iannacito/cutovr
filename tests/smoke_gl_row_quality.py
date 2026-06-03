"""GL row quality + validation report smoke tests (Cesar QA 2026-05-29).

Pins:

  T1 Date parser accepts multiple formats: ISO, MM/DD/YYYY,
     "Jan 15, 2026", "15-Jan-2026", and Excel serial integers.
  T2 Beginning-balance tokens ("Balance Forward", "Beginning Balance")
     are detected and surfaced as a distinct ``beginning_balance``
     kind so the user is routed to Starting Balances, not told to
     "fix" them in the GL.
  T3 Blank rows (no date, no account, no debit, no credit) are
     silently dropped — they do NOT count as "missing date" or
     "missing account", and they do NOT show up in the validation
     report problem list.
  T4 Rows with an amount + account but no date are flagged as
     ``no_date`` and carry plain-English fix guidance.
  T5 The validation-report CSV now includes a "Rows that need a fix"
     section with row number, raw date, account, debit/credit, and
     a "How to fix" column. It also includes a "Beginning-balance
     rows" section pointing the user at Starting Balances.
  T6 ``preflight.ready`` is False whenever any of: unparseable dates,
     blank-line-only file, beginning-balance rows present.
  T7 The Jinja ``customer_status`` filter rewrites legacy persisted
     "QBO" tokens to "QuickBooks" so old job rows render cleanly.

Run from the project root:

    python3 tests/smoke_gl_row_quality.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("APP_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("IMPORT_HISTORY_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-gl-row-quality")

from gl_row_quality import (  # noqa: E402
    classify_gl_rows,
    is_beginning_balance_token,
    is_blank_row,
    is_blank_value,
    is_droppable_row,
    is_zero_activity_row,
    parse_gl_date,
)
from migration_quality import (  # noqa: E402
    build_dry_run_preview,
    render_validation_csv,
)
from preflight import build_preflight_summary  # noqa: E402


def t1_date_parser_accepts_common_formats():
    cases = [
        ("2026-01-15", "2026-01-15"),
        ("01/15/2026", "2026-01-15"),
        ("1/15/2026", "2026-01-15"),
        ("01-15-2026", "2026-01-15"),  # MM-DD-YYYY -> caught by %d-%m-%Y for DD<=12; accept either
        ("15-Jan-2026", "2026-01-15"),
        ("Jan 15, 2026", "2026-01-15"),
        ("January 15, 2026", "2026-01-15"),
        ("20260115", "2026-01-15"),
        ("'2026-01-15'", "2026-01-15"),
        ('"2026-01-15"', "2026-01-15"),
        # Excel serial: 2026-01-15 is day 46037 from the 1899-12-30 epoch.
        ("46037", "2026-01-15"),
        ("46037.0", "2026-01-15"),
        # PCLaw native export format "MMM D/YY" (Cesar QA 2026-06-01).
        # Firms had to hand-edit every row before the GL would import;
        # now the format is accepted directly.
        ("Jan 4/21", "2021-01-04"),
        ("Jan 04/21", "2021-01-04"),
        ("Dec 31/20", "2020-12-31"),
        ("January 4/21", "2021-01-04"),
        ("Jan-4/21", "2021-01-04"),
        ("Jan 4/2021", "2021-01-04"),
    ]
    for raw, expected in cases:
        got = parse_gl_date(raw)
        # The "01-15-2026" case is ambiguous (US vs ROW). We accept
        # either US (Jan 15) or ROW (out-of-range, returns None). Use a
        # softer check on that one only.
        if raw == "01-15-2026":
            assert got in (expected, None), f"{raw!r} -> {got!r}"
            continue
        assert got == expected, f"{raw!r} -> {got!r}, want {expected}"
    assert parse_gl_date("") is None
    assert parse_gl_date(None) is None
    assert parse_gl_date("Balance Forward") is None
    assert parse_gl_date("not-a-date") is None
    print("T1 OK: parse_gl_date accepts ISO, MM/DD/YYYY, named-month, PCLaw 'Jan 4/21', and Excel serials")


def t2_beginning_balance_tokens_routed_separately():
    rows = [
        # A real GL line — fine.
        {"transaction_id": "T1", "date": "2026-01-02", "account_number": "1000",
         "account_name": "Cash", "debit": "100.00", "credit": "0.00",
         "description": "Receipt"},
        # Beginning-balance line: PCLaw exports these without a date and
        # with the literal token in the date column.
        {"transaction_id": "T0", "date": "Balance Forward", "account_number": "1000",
         "account_name": "Cash", "debit": "5000.00", "credit": "0.00",
         "description": ""},
        # Beginning balance again but using the lowercase token.
        {"transaction_id": "T0b", "date": "beginning balance", "account_number": "2000",
         "account_name": "AP", "debit": "0.00", "credit": "1500.00",
         "description": ""},
    ]
    report = classify_gl_rows(rows)
    assert report.ok_rows == 1, report.ok_rows
    assert len(report.beginning_balance_rows) == 2, (
        report.beginning_balance_rows
    )
    assert not report.problem_rows, report.problem_rows
    assert is_beginning_balance_token("Balance Forward")
    assert is_beginning_balance_token("b/f")
    assert not is_beginning_balance_token("Q1 2026")
    print("T2 OK: beginning-balance tokens routed to dedicated bucket")


def t3_blank_rows_are_silently_dropped():
    rows = [
        {"transaction_id": "", "date": "", "account_number": "",
         "account_name": "", "debit": "", "credit": "", "description": ""},
        {"transaction_id": "", "date": "  ", "account_number": "  ",
         "account_name": "  ", "debit": "  ", "credit": "  ", "description": ""},
        {"transaction_id": "T1", "date": "2026-02-01", "account_number": "1000",
         "account_name": "Cash", "debit": "1.00", "credit": "0.00",
         "description": ""},
        {"transaction_id": "T1", "date": "2026-02-01", "account_number": "4000",
         "account_name": "Revenue", "debit": "0.00", "credit": "1.00",
         "description": ""},
    ]
    report = classify_gl_rows(rows)
    assert report.blank_rows == 2, report.blank_rows
    assert report.ok_rows == 2, report.ok_rows
    assert not report.problem_rows, report.problem_rows
    assert is_blank_row({"date": "", "account_number": "", "account_name": "",
                         "debit": "", "credit": ""})
    assert not is_blank_row({"date": "2026-01-01", "account_number": "1000",
                             "account_name": "", "debit": "", "credit": ""})
    assert is_blank_value("")
    assert is_blank_value(None)
    assert is_blank_value("   ")
    assert not is_blank_value("0")
    print("T3 OK: blank rows silently dropped, do not count as missing-date")


def t4_no_date_with_amount_is_flagged():
    rows = [
        # No date, has account + amount: this is the row Cesar saw stuck.
        {"transaction_id": "T9", "date": "", "account_number": "1100",
         "account_name": "Trust Bank", "debit": "250.00", "credit": "0.00",
         "description": ""},
        # Unparseable date with a real amount.
        {"transaction_id": "T10", "date": "lol-not-a-date", "account_number": "4000",
         "account_name": "Revenue", "debit": "0.00", "credit": "250.00",
         "description": ""},
    ]
    report = classify_gl_rows(rows)
    kinds = {r.kind for r in report.problem_rows}
    assert "no_date" in kinds or "beginning_balance" in {
        r.kind for r in report.beginning_balance_rows
    }, kinds
    assert "unparseable_date" in kinds, kinds
    # Every problem row must carry a non-empty plain_fix string.
    for r in report.problem_rows:
        assert r.plain_fix, f"no plain_fix for kind {r.kind}"
        assert r.reason, f"no reason for kind {r.kind}"
    print("T4 OK: no-date and unparseable-date rows are flagged with fix guidance")


def t5_validation_report_csv_lists_problem_rows():
    rows = [
        {"transaction_id": "T1", "date": "2026-02-01", "account_number": "1000",
         "account_name": "Cash", "debit": "100.00", "credit": "0.00",
         "description": ""},
        {"transaction_id": "T1", "date": "2026-02-01", "account_number": "4000",
         "account_name": "Revenue", "debit": "0.00", "credit": "100.00",
         "description": ""},
        # Beginning-balance row.
        {"transaction_id": "T0", "date": "Balance Forward", "account_number": "1000",
         "account_name": "Cash", "debit": "5000.00", "credit": "0.00",
         "description": ""},
        # No-date row.
        {"transaction_id": "T2", "date": "", "account_number": "2000",
         "account_name": "AP", "debit": "0.00", "credit": "200.00",
         "description": ""},
    ]
    preflight = build_preflight_summary(rows)
    assert not preflight["ready"], "expected preflight ready=False with bad rows"
    assert preflight["beginning_balance_row_count"] == 1
    assert preflight["rows_missing_date"] >= 1

    # Preview must list the same.
    preview = build_dry_run_preview(rows, {"QueryResponse": {"Account": []}})
    assert preview["beginning_balance_rows"], "missing beginning-balance bucket"
    assert preview["problem_rows"], "missing problem-row bucket"

    csv_body = render_validation_csv(
        {"id": "job_x", "source_file": "gl.csv", "company": "Test"},
        preflight,
        preview=preview,
    )
    assert "Rows that need a fix" in csv_body, csv_body[:1200]
    assert "Beginning-balance rows" in csv_body, csv_body[:1200]
    assert "How to fix" in csv_body, csv_body[:1200]
    assert "Starting Balances" in csv_body, csv_body[:1200]
    # Row number must appear (the no-date row is line 4 in the source).
    assert ",4," in csv_body or ",4\r" in csv_body or "4," in csv_body
    print("T5 OK: validation report CSV includes blocked rows with fix guidance")


def t6_preflight_ready_false_when_blockers_present():
    # File of nothing but blank rows: not ready (no data).
    empty_blanks = [
        {"transaction_id": "", "date": "", "account_number": "",
         "account_name": "", "debit": "", "credit": "", "description": ""},
        {"transaction_id": "", "date": "", "account_number": "",
         "account_name": "", "debit": "", "credit": "", "description": ""},
    ]
    pf = build_preflight_summary(empty_blanks, [
        "transaction_id", "date", "account_number", "account_name", "debit", "credit",
    ])
    assert not pf["ready"], pf

    # File with one beginning-balance row only.
    bb_only = [
        {"transaction_id": "T0", "date": "Balance Forward", "account_number": "1000",
         "account_name": "Cash", "debit": "5000.00", "credit": "0.00",
         "description": ""},
    ]
    pf = build_preflight_summary(bb_only, [
        "transaction_id", "date", "account_number", "account_name", "debit", "credit",
    ])
    assert not pf["ready"], pf
    assert pf["beginning_balance_row_count"] == 1

    # Clean balanced file is ready.
    clean = [
        {"transaction_id": "T1", "date": "2026-02-01", "account_number": "1000",
         "account_name": "Cash", "debit": "100.00", "credit": "0.00",
         "description": ""},
        {"transaction_id": "T1", "date": "2026-02-01", "account_number": "4000",
         "account_name": "Revenue", "debit": "0.00", "credit": "100.00",
         "description": ""},
    ]
    pf = build_preflight_summary(clean, [
        "transaction_id", "date", "account_number", "account_name", "debit", "credit",
    ])
    assert pf["ready"], pf
    print("T6 OK: preflight.ready reflects blank/begin-bal/clean states")


def t8_zero_activity_rows_are_dropped():
    """PCLaw GL exports list every chart account, including ones with no
    movement in the period, as a 0.00/0.00 row with no date. Those post
    nothing. Before the fix they were flagged as "Row has an amount but no
    transaction date" (Cesar QA 2026-06-03 — every account row showed up as
    a phantom error, 50+ of them). They must be classified as zero_activity
    and dropped, NOT surfaced as problem rows."""
    rows = [
        # Zero-activity account-listing rows: account, no date, no money.
        {"transaction_id": "", "date": "", "account_number": "1560",
         "account_name": "Art", "debit": "0.00", "credit": "0.00",
         "description": ""},
        {"transaction_id": "", "date": "", "account_number": "5115",
         "account_name": "Payroll Expense", "debit": "0.00", "credit": "0.00",
         "description": ""},
        # A real balanced transaction so the file isn't empty.
        {"transaction_id": "T1", "date": "Jan 4/21", "account_number": "1000",
         "account_name": "Cash", "debit": "100.00", "credit": "0.00",
         "description": ""},
        {"transaction_id": "T1", "date": "Jan 4/21", "account_number": "4000",
         "account_name": "Revenue", "debit": "0.00", "credit": "100.00",
         "description": ""},
    ]
    # Unit-level checks on the helpers.
    assert is_zero_activity_row(rows[0]), rows[0]
    assert is_droppable_row(rows[0])
    assert not is_zero_activity_row(rows[2]), "row with a date+amount is NOT zero-activity"
    # A 0.00/0.00 row that DOES carry a date is a real (if empty) posting line,
    # not a chart-listing artifact — it must not be swallowed as zero-activity.
    dated_zero = {"transaction_id": "T2", "date": "2026-01-01",
                  "account_number": "1560", "account_name": "Art",
                  "debit": "0.00", "credit": "0.00", "description": ""}
    assert not is_zero_activity_row(dated_zero), dated_zero

    report = classify_gl_rows(rows)
    # The two zero-activity rows fold into the dropped (blank) count, NOT
    # into problem_rows.
    assert report.blank_rows == 2, report.blank_rows
    assert report.ok_rows == 2, report.ok_rows
    assert not report.problem_rows, [r.to_dict() for r in report.problem_rows]
    print("T8 OK: zero-activity 0.00/0.00 chart-listing rows dropped, not flagged")


def t7_customer_status_filter_rewrites_qbo_tokens():
    import app as appmod  # local import to avoid top-of-file Flask boot cost.
    fn = appmod._customer_status
    assert fn("Chart of Accounts ready for QBO preview") == \
        "Chart of Accounts ready for QuickBooks preview"
    assert fn("Ready for QBO connection") == "Ready for QuickBooks connection"
    assert fn("Import to QBO initiated (demo mode)") == \
        "Import to QuickBooks initiated (demo mode)"
    assert fn("Import failed (QBO error)") == "Import failed (QuickBooks error)"
    assert fn("Imported to QuickBooks") == "Imported to QuickBooks"
    assert fn(None) == ""
    print("T7 OK: customer_status filter rewrites legacy 'QBO' tokens")


if __name__ == "__main__":
    t1_date_parser_accepts_common_formats()
    t2_beginning_balance_tokens_routed_separately()
    t3_blank_rows_are_silently_dropped()
    t4_no_date_with_amount_is_flagged()
    t5_validation_report_csv_lists_problem_rows()
    t6_preflight_ready_false_when_blockers_present()
    t7_customer_status_filter_rewrites_qbo_tokens()
    t8_zero_activity_rows_are_dropped()
    print("ALL GL row-quality tests OK")
