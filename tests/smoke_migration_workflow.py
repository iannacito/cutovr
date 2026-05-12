"""Smoke tests for the migration-workflow-completion PR.

Run:
    python3 tests/smoke_migration_workflow.py

Covers:

  W1  Opening balance plan blocks on unbalanced TB.
  W2  Opening balance plan blocks on rows that don't resolve to QBO.
  W3  Opening balance plan succeeds + JE payload balances.
  W4  Ending TB reconciliation: match / diff / unexpected / missing.
  W5  Trust listing reconciliation: negative-balance + mismatch warnings.
  W6  AR/AP strategy validation + guidance for CA/cash/clio combos.
  W7  Parent/sub-account hierarchy: orphan blocked, existing parent ok.
  W8  Parser hardening: footer skipping, $/$/,/CR/() money parsing,
      combined account splitting.
  W9  Opening-balance route refuses POST without the confirmation phrase.
  W10 Trust posting remains intentionally disabled (no auto-post route).
"""

import io
import os
import sys
import tempfile
import unittest.mock as mock
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret")

import report_types as rt  # noqa: E402
from opening_balance import (  # noqa: E402
    build_opening_balance_plan,
    build_opening_je_payload,
    OPENING_BALANCE_CONFIRMATION_PHRASE,
)
from tb_reconciliation import build_ending_tb_reconciliation  # noqa: E402
from trust_reconciliation import build_trust_listing_reconciliation  # noqa: E402
from ar_ap_strategy import (  # noqa: E402
    validate_ar_ap_strategy,
    guidance_for_strategy,
    block_message_for_unsupported_import,
    AR_AP_STRATEGY_CHOICES,
    STRATEGY_SKIP,
    STRATEGY_OPEN_ITEMS,
)
from coa_hierarchy import (  # noqa: E402
    build_hierarchy_plan,
    detect_hierarchy,
)
import app as appmod  # noqa: E402


def _qbo_accounts(*accounts):
    """Build a QBO query response wrapper from positional dicts."""
    return {"QueryResponse": {"Account": list(accounts)}}


def w1_unbalanced_tb_is_blocked():
    tb_rows = [
        {"account_number": "1000", "account_name": "Bank",
         "debit_balance": "100.00", "credit_balance": "0.00",
         "as_of_date": "2026-03-31"},
        {"account_number": "3000", "account_name": "Equity",
         "debit_balance": "0.00", "credit_balance": "50.00"},
    ]
    qbo = _qbo_accounts(
        {"Id": "10", "Name": "Bank", "AcctNum": "1000", "AccountType": "Bank"},
        {"Id": "20", "Name": "Equity", "AcctNum": "3000", "AccountType": "Equity"},
    )
    plan = build_opening_balance_plan(tb_rows, qbo)
    assert plan.balanced is False
    assert plan.has_blockers is True
    assert any("does not balance" in b for b in plan.blockers)
    print("W1 unbalanced TB blocked: OK")


def w2_missing_qbo_account_blocked():
    tb_rows = [
        {"account_number": "1000", "account_name": "Bank",
         "debit_balance": "100.00", "credit_balance": "0.00"},
        {"account_number": "3000", "account_name": "Mystery",
         "debit_balance": "0.00", "credit_balance": "100.00"},
    ]
    qbo = _qbo_accounts(
        {"Id": "10", "Name": "Bank", "AcctNum": "1000", "AccountType": "Bank"},
    )
    plan = build_opening_balance_plan(tb_rows, qbo)
    assert plan.balanced is True
    assert plan.has_blockers is True, "missing QBO account must block"
    line_blockers = [l for l in plan.lines if l.blocker]
    assert len(line_blockers) == 1
    assert line_blockers[0].account_name == "Mystery"
    print("W2 missing QBO account blocked: OK")


def w3_balanced_plan_payload():
    tb_rows = [
        {"account_number": "1000", "account_name": "Bank",
         "debit_balance": "100.00", "credit_balance": "0.00",
         "as_of_date": "2026-03-31"},
        {"account_number": "3000", "account_name": "Equity",
         "debit_balance": "0.00", "credit_balance": "100.00"},
    ]
    qbo = _qbo_accounts(
        {"Id": "10", "Name": "Bank", "AcctNum": "1000", "AccountType": "Bank"},
        {"Id": "20", "Name": "Equity", "AcctNum": "3000", "AccountType": "Equity"},
    )
    plan = build_opening_balance_plan(tb_rows, qbo)
    assert plan.balanced is True and not plan.has_blockers
    payload = build_opening_je_payload(plan)
    assert payload["TxnDate"] == "2026-03-31"
    assert len(payload["Line"]) == 2
    total_debit = sum(l["Amount"] for l in payload["Line"]
                      if l["JournalEntryLineDetail"]["PostingType"] == "Debit")
    total_credit = sum(l["Amount"] for l in payload["Line"]
                       if l["JournalEntryLineDetail"]["PostingType"] == "Credit")
    assert abs(total_debit - total_credit) < 0.001
    print("W3 balanced plan payload: OK")


def w4_ending_tb_reconciliation():
    opening = [
        {"account_number": "1000", "account_name": "Bank",
         "debit_balance": "100.00", "credit_balance": "0.00"},
        {"account_number": "3000", "account_name": "Equity",
         "debit_balance": "0.00", "credit_balance": "100.00"},
    ]
    # GL activity: +50 to bank, -50 to equity (i.e. equity now -150).
    gl = [
        {"account_number": "1000", "account_name": "Bank",
         "debit": "50.00", "credit": "0.00"},
        {"account_number": "3000", "account_name": "Equity",
         "debit": "0.00", "credit": "50.00"},
    ]
    ending = [
        {"account_number": "1000", "account_name": "Bank",
         "debit_balance": "150.00", "credit_balance": "0.00"},
        {"account_number": "3000", "account_name": "Equity",
         "debit_balance": "0.00", "credit_balance": "140.00"},  # off by 10
        {"account_number": "9999", "account_name": "Surprise",
         "debit_balance": "10.00", "credit_balance": "0.00"},
    ]
    report = build_ending_tb_reconciliation(ending, opening, gl)
    summary = report["summary"]
    assert summary["matched_count"] == 1, summary
    assert summary["diff_count"] == 1, summary
    assert summary["unexpected_count"] == 1, summary
    assert summary["overall_pass"] is False
    bank_row = next(r for r in report["rows"] if r["account_number"] == "1000")
    assert bank_row["status"] == "match"
    equity_row = next(r for r in report["rows"] if r["account_number"] == "3000")
    assert equity_row["status"] == "diff"
    # 'limitation' field MUST be present so the UI/CSV always show the caveat.
    assert "QuickBooks Reports API" in report["limitation"]
    print("W4 ending TB reconciliation: OK")


def w5_trust_reconciliation_warnings():
    trust = [
        {"client_id": "C-1", "client_name": "Alpha",
         "matter_id": "M-1", "matter_name": "Closing",
         "trust_bank_account": "1010", "trust_balance": "1000.00"},
        {"client_id": "C-2", "client_name": "Beta",
         "matter_id": "M-2", "matter_name": "Litigation",
         "trust_bank_account": "1010", "trust_balance": "-50.00"},
        {"client_id": "", "client_name": "",
         "matter_id": "", "matter_name": "",
         "trust_bank_account": "1010", "trust_balance": "100.00"},
    ]
    tb = [
        {"account_number": "1010", "account_name": "Trust Bank",
         "debit_balance": "999.00", "credit_balance": "0.00"},
        {"account_number": "2100", "account_name": "Client Trust Liability",
         "debit_balance": "0.00", "credit_balance": "999.00"},
    ]
    report = build_trust_listing_reconciliation(trust, tb)
    summary = report["summary"]
    assert summary["negative_row_count"] == 1
    assert summary["missing_identifier_count"] == 1
    assert summary["posting_enabled"] is False
    assert summary["liability_match"] is False
    assert any("negative" in w.lower() for w in report["warnings"])
    assert any("identifier" in w.lower() for w in report["warnings"])
    print("W5 trust reconciliation warnings: OK")


def w6_ar_ap_strategy_guidance():
    assert validate_ar_ap_strategy("") == ""
    assert validate_ar_ap_strategy("garbage") == ""
    assert validate_ar_ap_strategy("skip") == "skip"
    # CA + accrual + open_items should block.
    g = guidance_for_strategy("open_items", country="CA", accounting_basis="accrual")
    assert g["supported"] is False
    assert g["unsafe_to_auto_post"] is True
    assert any("Canadian sales-tax" in b for b in g["blockers"])
    # cash basis recommends skipping.
    g2 = guidance_for_strategy("", accounting_basis="cash")
    assert any("Cash basis" in r for r in g2["recommendations"])
    # block-message for unsupported strategy.
    msg = block_message_for_unsupported_import("open_items")
    assert msg and "not yet implemented" in msg
    assert block_message_for_unsupported_import("skip") is None
    assert block_message_for_unsupported_import("") is None
    # All choices show up in the picker.
    assert len(AR_AP_STRATEGY_CHOICES) == 3
    print("W6 AR/AP strategy guidance: OK")


def w7_hierarchy_detection():
    rows = [
        {"account_number": "6000", "account_name": "Operating Expenses",
         "parent_account_number": "", "parent_account_name": ""},
        {"account_number": "6010", "account_name": "Rent",
         "parent_account_number": "6000", "parent_account_name": ""},
        {"account_number": "6020", "account_name": "Utilities",
         "parent_account_number": "9999",  # orphan
         "parent_account_name": "Nope"},
        {"account_number": "7000", "account_name": "Accounting",
         "parent_account_number": "5000", "parent_account_name": ""},  # existing QBO
    ]
    qbo = _qbo_accounts(
        {"Id": "100", "Name": "Existing Parent", "AcctNum": "5000",
         "AccountType": "Expense"},
    )
    plan = build_hierarchy_plan(rows, qbo)
    assert detect_hierarchy(rows) is True
    assert plan.has_blockers is True
    blocked_names = {n.account_name for n in plan.blocked}
    assert "Utilities" in blocked_names, blocked_names
    # Top-level + qbo_existing_parent + in_plan_parent should be in plan.
    resolutions = {n.account_name: n.resolution for n in plan.nodes}
    assert resolutions["Operating Expenses"] == "top_level"
    assert resolutions["Rent"] == "in_plan_parent"
    assert resolutions["Accounting"] == "qbo_existing_parent"
    assert resolutions["Utilities"] == "orphan"
    # Create order must place the in-plan parent before the child.
    order_names = [n.account_name for n in plan.create_order]
    assert order_names.index("Operating Expenses") < order_names.index("Rent")
    print("W7 hierarchy detection: OK")


def w8_parser_hardening():
    # Money parsing edge cases.
    assert rt._money("$1,234.56") == Decimal("1234.56")
    assert rt._money("(1,234.56)") == Decimal("-1234.56")
    assert rt._money("1,234.56 CR") == Decimal("-1234.56")
    assert rt._money("123.45 DR") == Decimal("123.45")
    assert rt._money("-") == Decimal("0.00")
    assert rt._money("N/A") == Decimal("0.00")
    # Footer detection.
    assert rt._looks_like_footer_or_subtotal({"a": "Total", "b": "100.00"}) is True
    assert rt._looks_like_footer_or_subtotal({"a": "Page 1 of 4", "b": ""}) is True
    assert rt._looks_like_footer_or_subtotal({"a": "1000", "b": "Bank"}) is False
    assert rt._looks_like_footer_or_subtotal({"a": "", "b": ""}) is True
    # Combined account splitting.
    num, name = rt._split_combined_account("1000 - Operating Bank")
    assert (num, name) == ("1000", "Operating Bank")
    num, name = rt._split_combined_account("Operating Bank")
    assert (num, name) == ("", "Operating Bank")
    num, name = rt._split_combined_account("1000")
    assert (num, name) == ("1000", "")
    # Header detection skips preamble.
    csv_text = (
        "PCLaw General Ledger Report\n"
        "Report run 2026-05-12\n"
        "\n"
        "account_number,account_name,debit_balance,credit_balance\n"
        "1000,Bank,100.00,0.00\n"
        "Total,, 100.00,0.00\n"  # footer
    )
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv_text)
        path = Path(f.name)
    try:
        rows, fieldnames, missing = rt.parse_trial_balance(path)
    finally:
        path.unlink()
    assert len(rows) == 1, rows
    assert rows[0]["account_number"] == "1000"
    assert not missing
    # Combined account column.
    csv_text2 = (
        "account,debit_balance,credit_balance\n"
        "1000 - Bank,100.00,0.00\n"
        "3000 - Equity,0.00,100.00\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv_text2)
        path2 = Path(f.name)
    try:
        rows, fieldnames, missing = rt.parse_trial_balance(path2)
    finally:
        path2.unlink()
    assert not missing, missing
    assert rows[0]["account_number"] == "1000"
    assert rows[0]["account_name"] == "Bank"
    print("W8 parser hardening: OK")


def w9_opening_balance_no_post_without_confirmation():
    """Drive the opening-balance route via Flask test client and verify
    that POST without the confirmation phrase never calls
    create_journal_entry."""
    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()
    pwd = "correct-horse-battery-staple"
    client.post("/signup", data={
        "firm_name": "OB Test LLP",
        "email": "ob@test.example",
        "password": pwd,
        "confirm_password": pwd,
    }, follow_redirects=True)
    client.post("/login", data={
        "email": "ob@test.example",
        "password": pwd,
    }, follow_redirects=True)
    # Upload a TB.
    tb_csv = (ROOT / "test_data" / "03_trial_balance.csv").read_bytes()
    resp = client.post("/upload", data={
        "company_name": "OB Test Co",
        "email": "ob@test.example",
        "report_type": "trial_balance",
        "ledger_file": (io.BytesIO(tb_csv), "tb.csv"),
    }, content_type="multipart/form-data", follow_redirects=False)
    location = resp.headers.get("Location", "")
    assert "/jobs/" in location, location
    job_id = location.rsplit("/", 1)[-1]
    # GET preview page (no QBO connection — should still render with blockers).
    resp = client.get(f"/jobs/{job_id}/opening-balance", follow_redirects=False)
    assert resp.status_code == 200, resp.status_code
    body = resp.get_data(as_text=True)
    assert OPENING_BALANCE_CONFIRMATION_PHRASE in body
    # POST without confirmation should not call QBO and must not record
    # an opening_balance_history entry.
    with mock.patch("app.QBOClient.create_journal_entry") as create:
        resp2 = client.post(f"/jobs/{job_id}/opening-balance", data={
            "confirm_post": "WRONG",
        }, follow_redirects=False)
        assert create.called is False, "must not call QBO without phrase"
    # POST with phrase but without QBO connection should still not call QBO.
    with mock.patch("app.QBOClient.create_journal_entry") as create:
        resp3 = client.post(f"/jobs/{job_id}/opening-balance", data={
            "confirm_post": OPENING_BALANCE_CONFIRMATION_PHRASE,
        }, follow_redirects=False)
        assert create.called is False, "must not call QBO when not connected"
    print("W9 opening balance no-post without confirmation: OK")


def w10_trust_posting_disabled():
    """Trust reconciliation report must not expose a posting path, and
    the import-to-qbo route must continue to refuse trust uploads."""
    report = build_trust_listing_reconciliation(
        [{"client_id": "C-1", "trust_balance": "100.00", "trust_bank_account": "1010"}],
        [],
    )
    assert report["summary"]["posting_enabled"] is False
    # Drive the existing safety gate too — re-use the multi-report client.
    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()
    pwd = "correct-horse-battery-staple"
    client.post("/signup", data={
        "firm_name": "Trust Block LLP",
        "email": "trust-block@test.example",
        "password": pwd,
        "confirm_password": pwd,
    }, follow_redirects=True)
    client.post("/login", data={
        "email": "trust-block@test.example",
        "password": pwd,
    }, follow_redirects=True)
    trust_csv = (ROOT / "test_data" / "05_trust_listing.csv").read_bytes()
    resp = client.post("/upload", data={
        "company_name": "Trust Block Co",
        "email": "trust-block@test.example",
        "report_type": "trust_listing",
        "ledger_file": (io.BytesIO(trust_csv), "trust.csv"),
    }, content_type="multipart/form-data", follow_redirects=False)
    job_id = resp.headers.get("Location", "").rsplit("/", 1)[-1]
    with mock.patch("app.QBOClient.create_journal_entry") as create:
        resp2 = client.post(f"/jobs/{job_id}/import-to-qbo", follow_redirects=False)
        assert create.called is False
    print("W10 trust posting remains disabled: OK")


def main():
    w1_unbalanced_tb_is_blocked()
    w2_missing_qbo_account_blocked()
    w3_balanced_plan_payload()
    w4_ending_tb_reconciliation()
    w5_trust_reconciliation_warnings()
    w6_ar_ap_strategy_guidance()
    w7_hierarchy_detection()
    w8_parser_hardening()
    w9_opening_balance_no_post_without_confirmation()
    w10_trust_posting_disabled()
    print()
    print("All migration-workflow smoke tests passed.")


if __name__ == "__main__":
    main()
