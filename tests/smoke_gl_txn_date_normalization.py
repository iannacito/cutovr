"""GL TxnDate normalization smoke tests (Cesar QA 2026-06-01).

Background
----------
Cesar reuploaded the GL on the demo site and the import still failed.
Two root causes:

  1. PCLaw's fresh export writes the date as "Jan 4/21" (MMM D/YY).
     The date parser did not know that format, so those rows were
     flagged unparseable and dropped — which then surfaced downstream
     as the confusing "fewer than 2 posting lines" failure once a
     balanced pair lost one of its sides.

  2. Even when a date *was* parseable, the QBO JournalEntry payload
     sent ``first_row["date"]`` straight through. A non-ISO string
     like "Jan 4/21" is rejected by QuickBooks, so the whole import
     failed.

Pins
----
  D1 ``normalize_txn_date`` turns the PCLaw native format and the other
     accepted formats into ISO ``YYYY-MM-DD``.
  D2 ``build_journal_entry_payload`` emits an ISO ``TxnDate`` even when
     the source rows carry the PCLaw native "Jan 4/21" string.
  D3 An unreadable date raises a plain-English error that names the
     offending value and transaction (no raw stack trace, no QBO 400).
  D4 The shipped sample GL (test_data/02_general_ledger.csv) plans and
     builds balanced payloads cleanly — no "fewer than 2 posting lines".

Run from the project root:

    python3 tests/smoke_gl_txn_date_normalization.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("APP_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("IMPORT_HISTORY_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-gl-txn-date")

from pclaw_pipeline import (  # noqa: E402
    build_journal_entry_payload,
    load_general_ledger_csv,
    normalize_txn_date,
    plan_balanced_payloads,
)


def _two_line_rows(date_value):
    return [
        {
            "transaction_id": "JE-T1",
            "date": date_value,
            "account_number": "1000",
            "account_name": "Operating Bank",
            "debit": "100.00",
            "credit": "0.00",
            "description": "Opening cash",
        },
        {
            "transaction_id": "JE-T1",
            "date": date_value,
            "account_number": "3000",
            "account_name": "Owner Equity",
            "debit": "0.00",
            "credit": "100.00",
            "description": "Opening cash",
        },
    ]


def d1_normalize_accepts_pclaw_and_iso():
    cases = {
        "Jan 4/21": "2021-01-04",
        "Dec 31/20": "2020-12-31",
        "2026-01-15": "2026-01-15",
        "01/15/2026": "2026-01-15",
        "15-Jan-2026": "2026-01-15",
    }
    for raw, expected in cases.items():
        got = normalize_txn_date(raw)
        assert got == expected, f"{raw!r} -> {got!r}, want {expected}"
    print("D1 OK: normalize_txn_date coerces PCLaw 'Jan 4/21' and friends to ISO")


def d2_payload_txn_date_is_iso_from_pclaw_native():
    mapping = {"1000": "10", "3000": "30"}
    payload = build_journal_entry_payload(
        "JE-T1", _two_line_rows("Jan 4/21"), mapping, mapping_mode="number"
    )
    assert payload["TxnDate"] == "2021-01-04", payload["TxnDate"]
    assert len(payload["Line"]) == 2, payload["Line"]
    print("D2 OK: build_journal_entry_payload emits ISO TxnDate from 'Jan 4/21'")


def d3_unreadable_date_raises_plain_english():
    mapping = {"1000": "10", "3000": "30"}
    try:
        build_journal_entry_payload(
            "JE-BAD", _two_line_rows("not-a-date"), mapping, mapping_mode="number"
        )
    except ValueError as e:
        msg = str(e)
        assert "not-a-date" in msg, msg
        assert "JE-BAD" in msg, msg
        assert "Jan 4/21" in msg, msg  # tells the user the accepted format
        print("D3 OK: unreadable date raises a plain-English, row-identifying error")
        return
    raise AssertionError("expected ValueError for an unreadable date")


def d4_sample_gl_imports_cleanly():
    rows = load_general_ledger_csv(ROOT / "test_data" / "02_general_ledger.csv")
    # Map every PCLaw account number present to a dummy QBO id.
    mapping = {}
    for r in rows:
        num = (r.get("account_number") or "").strip()
        if num:
            mapping[num] = f"QBO-{num}"
    payloads, posted_ids = plan_balanced_payloads(rows, mapping, mapping_mode="number")
    assert payloads, "sample GL produced no payloads"
    assert posted_ids, "sample GL posted no transactions"
    for p in payloads:
        # Every payload must balance and carry an ISO date QBO will accept.
        assert len(p["Line"]) >= 2, p
        assert p["TxnDate"][:4].isdigit() and p["TxnDate"][4] == "-", p["TxnDate"]
    print(f"D4 OK: sample GL builds {len(payloads)} balanced payload(s) cleanly")


if __name__ == "__main__":
    d1_normalize_accepts_pclaw_and_iso()
    d2_payload_txn_date_is_iso_from_pclaw_native()
    d3_unreadable_date_raises_plain_english()
    d4_sample_gl_imports_cleanly()
    print("\nALL GL TxnDate normalization SMOKE TESTS PASSED")
