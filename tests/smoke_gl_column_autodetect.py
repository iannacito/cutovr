"""Smoke tests for GL column auto-detection.

Real PCLaw GL exports from different firms use different header
conventions:

  * "Posting Date" instead of "Date"
  * "GL Account" / "Ledger Account" instead of "Account"
  * "Memo" / "Narrative" / "Notes" instead of "Description"
  * A single signed "Amount" / "Net Amount" column instead of
    "Debit" + "Credit"

Previously the legacy ``parse_pclaw_csv`` required the exact header
names ``Date, Account, Description, Debit, Credit`` and bailed out on
anything else, forcing the user to hand-edit the CSV. This test
verifies the new ``detect_gl_columns`` helper and tolerant parser
recognise common variations.
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pclaw_parser import (  # noqa: E402
    detect_gl_columns,
    parse_pclaw_csv,
    GL_COLUMN_SYNONYMS,
)


def _write_csv(rows, fieldnames):
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8",
    )
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    f.close()
    return Path(f.name)


def g1_detects_canonical_headers():
    """The canonical Date/Account/Description/Debit/Credit set resolves."""
    mapping, missing = detect_gl_columns(
        ["Date", "Account", "Description", "Debit", "Credit"]
    )
    assert mapping["date"] == "Date", mapping
    assert mapping["account"] == "Account", mapping
    assert mapping["description"] == "Description", mapping
    assert mapping["debit"] == "Debit", mapping
    assert mapping["credit"] == "Credit", mapping
    assert missing == [], missing
    print("G1 OK: canonical headers resolve without missing columns")


def g2_detects_posting_date_and_gl_account():
    """Common variation: 'Posting Date', 'GL Account', 'Memo'."""
    mapping, missing = detect_gl_columns(
        ["Posting Date", "GL Account", "Memo", "Debit Amount", "Credit Amount"]
    )
    assert mapping["date"] == "Posting Date", mapping
    assert mapping["account"] == "GL Account", mapping
    assert mapping["description"] == "Memo", mapping
    assert mapping["debit"] == "Debit Amount", mapping
    assert mapping["credit"] == "Credit Amount", mapping
    assert missing == [], missing
    print(
        "G2 OK: 'Posting Date' / 'GL Account' / 'Memo' / "
        "'Debit Amount' / 'Credit Amount' all auto-detected"
    )


def g3_signed_amount_substitutes_for_debit_credit():
    """A signed 'Amount' column counts as both debit and credit."""
    mapping, missing = detect_gl_columns(
        ["Trans Date", "Ledger Account", "Notes", "Amount"]
    )
    assert mapping["date"] == "Trans Date", mapping
    assert mapping["account"] == "Ledger Account", mapping
    assert mapping["amount"] == "Amount", mapping
    assert missing == [], (
        f"signed Amount column should satisfy debit/credit requirement: "
        f"{missing}"
    )
    print(
        "G3 OK: a signed 'Amount' column satisfies the debit/credit "
        "requirement (no manual column edit needed)"
    )


def g4_missing_required_lists_only_what_is_missing():
    """Truly empty CSV reports the missing logical columns."""
    mapping, missing = detect_gl_columns(["Foo", "Bar"])
    assert "date" in missing, missing
    assert "account" in missing, missing
    # Both debit AND credit appear in missing when neither nor amount is present.
    assert "debit" in missing and "credit" in missing, missing
    print(
        "G4 OK: an unrecognised header set reports each missing logical "
        "column (date / account / debit / credit)"
    )


def g5_parser_handles_variant_headers_end_to_end():
    """End-to-end: a CSV with PCLaw header variants parses cleanly."""
    csv_path = _write_csv(
        [
            {
                "Posting Date": "2024-01-01",
                "GL Account": "1000 - Operating Bank",
                "Memo": "Retainer",
                "Debit Amount": "1,000.00",
                "Credit Amount": "0.00",
            },
            {
                "Posting Date": "2024-01-01",
                "GL Account": "4000 - Legal Fees Income",
                "Memo": "Retainer",
                "Debit Amount": "0.00",
                "Credit Amount": "1,000.00",
            },
        ],
        fieldnames=[
            "Posting Date", "GL Account", "Memo",
            "Debit Amount", "Credit Amount",
        ],
    )
    rows = parse_pclaw_csv(csv_path)
    csv_path.unlink()
    assert len(rows) == 2, rows
    assert rows[0]["txn_date"] == "2024-01-01", rows[0]
    assert rows[0]["account"] == "1000 - Operating Bank", rows[0]
    assert rows[0]["memo"] == "Retainer", rows[0]
    assert rows[0]["debit"] == "1000.00", rows[0]
    assert rows[1]["credit"] == "1000.00", rows[1]
    print(
        "G5 OK: end-to-end parse of a CSV with Posting Date / GL Account "
        "/ Memo / Debit Amount / Credit Amount headers"
    )


def g6_parser_handles_signed_amount_column():
    """End-to-end: a signed-Amount-only CSV splits into debit + credit."""
    csv_path = _write_csv(
        [
            {"Date": "2024-01-01", "Account": "1000", "Amount": "1000.00"},
            {"Date": "2024-01-01", "Account": "4000", "Amount": "-1000.00"},
            # Accounting-style negative: (500) -> -500.
            {"Date": "2024-01-02", "Account": "5100", "Amount": "(500.00)"},
        ],
        fieldnames=["Date", "Account", "Amount"],
    )
    rows = parse_pclaw_csv(csv_path)
    csv_path.unlink()
    assert rows[0]["debit"] == "1000.00", rows[0]
    assert rows[0]["credit"] == "0.00", rows[0]
    assert rows[1]["debit"] == "0.00", rows[1]
    assert rows[1]["credit"] == "1000.00", rows[1]
    assert rows[2]["debit"] == "0.00", rows[2]
    assert rows[2]["credit"] == "500.00", rows[2]
    print(
        "G6 OK: signed Amount column (positives + accounting-style "
        "negatives) splits into debit / credit"
    )


def g7_friendly_error_when_essentials_missing():
    """Truly garbled CSV raises a plain-English error, not 'KeyError'."""
    csv_path = _write_csv(
        [{"Foo": "x", "Bar": "y"}],
        fieldnames=["Foo", "Bar"],
    )
    try:
        parse_pclaw_csv(csv_path)
    except ValueError as e:
        msg = str(e)
        # Customer-facing wording: no "KeyError", no python-jargon.
        assert "couldn't find" in msg.lower(), msg
        assert "date" in msg.lower(), msg
        assert "account" in msg.lower(), msg
    else:
        raise AssertionError("expected ValueError for garbled CSV")
    finally:
        csv_path.unlink()
    print(
        "G7 OK: garbled CSV raises a friendly ValueError mentioning the "
        "missing logical columns (no python KeyError leaks)"
    )


def main():
    g1_detects_canonical_headers()
    g2_detects_posting_date_and_gl_account()
    g3_signed_amount_substitutes_for_debit_credit()
    g4_missing_required_lists_only_what_is_missing()
    g5_parser_handles_variant_headers_end_to_end()
    g6_parser_handles_signed_amount_column()
    g7_friendly_error_when_essentials_missing()
    print("\nALL GL COLUMN AUTO-DETECT SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
