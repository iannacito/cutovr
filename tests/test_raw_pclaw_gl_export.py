"""Tests for the raw PCLaw General Ledger export template.

A fresh PCLaw GL export (the shape most firms send first) has the columns
``Date, Account, Description, Debit, Credit`` and no ``transaction_id``.
That shape is NOT the richer transaction-grouped format consumed by
``pclaw_pipeline`` (which posts JournalEntries to QuickBooks); it flows
through the tolerant legacy ``pclaw_parser`` path that prepares a QBO
import CSV. These tests pin that the raw template is:

  * recognised as a General Ledger (not mis-detected as something else),
  * routed to the legacy parser (``is_gl_format`` is False), and
  * parsed into balanced rows,

and that a malformed export gets a specific, plain-English error rather
than a generic failure.
"""

import csv

import report_types as rt
from pclaw_pipeline import is_gl_format
from pclaw_parser import detect_gl_columns, parse_pclaw_csv

RAW_HEADERS = ["Date", "Account", "Description", "Debit", "Credit"]
SAMPLE_PATH = "sample_pclaw_gl.csv"


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# --- the bundled raw-export fixture ---------------------------------------


def test_sample_raw_export_detected_as_general_ledger():
    assert rt.detect_report_type(RAW_HEADERS) == rt.REPORT_GENERAL_LEDGER


def test_sample_raw_export_uses_legacy_parser_not_pipeline():
    # No transaction_id column -> the rich pipeline is NOT selected; the
    # tolerant legacy parser handles it.
    assert is_gl_format(RAW_HEADERS) is False
    mapping, missing = detect_gl_columns(RAW_HEADERS)
    assert missing == []
    assert mapping["date"] == "Date"
    assert mapping["account"] == "Account"
    assert mapping["debit"] == "Debit"
    assert mapping["credit"] == "Credit"


def test_sample_raw_export_parses_and_balances():
    rows = parse_pclaw_csv(SAMPLE_PATH)
    assert rows, "expected the bundled sample to parse to at least one row"
    total_debit = sum(float(r["debit"]) for r in rows)
    total_credit = sum(float(r["credit"]) for r in rows)
    assert round(total_debit, 2) == round(total_credit, 2)
    # First row mirrors the fixture's opening-balance line.
    assert rows[0]["account"] == "Cash Operating"
    assert rows[0]["debit"] == "1000.00"


# --- header variants real PCLaw exports use -------------------------------


def test_posting_date_and_gl_account_variants(tmp_path):
    csv_text = (
        "Posting Date,GL Account,Memo,Debit,Credit\n"
        "2026-04-01,Cash Operating,Opening,1000.00,0.00\n"
        "2026-04-01,Equity,Opening,0.00,1000.00\n"
    )
    path = _write(tmp_path, "variant.csv", csv_text)
    rows = parse_pclaw_csv(path)
    assert len(rows) == 2
    assert rows[0]["txn_date"] == "2026-04-01"
    assert rows[0]["account"] == "Cash Operating"


def test_signed_amount_column_substitutes_for_debit_credit(tmp_path):
    csv_text = (
        "Date,Account,Description,Amount\n"
        "2026-04-01,Cash Operating,Opening,1000.00\n"
        "2026-04-01,Equity,Opening,-1000.00\n"
    )
    path = _write(tmp_path, "signed.csv", csv_text)
    rows = parse_pclaw_csv(path)
    assert rows[0]["debit"] == "1000.00" and rows[0]["credit"] == "0.00"
    assert rows[1]["credit"] == "1000.00" and rows[1]["debit"] == "0.00"


# --- malformed export -> specific message ---------------------------------


def test_missing_date_column_raises_specific_error(tmp_path):
    csv_text = (
        "Account,Description,Debit,Credit\n"
        "Cash Operating,Opening,1000.00,0.00\n"
    )
    path = _write(tmp_path, "no_date.csv", csv_text)
    try:
        parse_pclaw_csv(path)
        assert False, "expected a ValueError for a missing date column"
    except ValueError as exc:
        msg = str(exc)
        assert "date column" in msg.lower()


def test_missing_amount_columns_raises_specific_error(tmp_path):
    csv_text = (
        "Date,Account,Description\n"
        "2026-04-01,Cash Operating,Opening\n"
    )
    path = _write(tmp_path, "no_amounts.csv", csv_text)
    try:
        parse_pclaw_csv(path)
        assert False, "expected a ValueError for missing debit/credit"
    except ValueError as exc:
        assert "amount" in str(exc).lower() or "debit" in str(exc).lower()
