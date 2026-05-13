"""Smoke tests for account-number visibility and the QBO orientation page.

Run from project root:

    python3 tests/smoke_account_numbers_and_qbo_guide.py

Covers two customer-readiness features:

  A1  Account-mapping template shows PCLaw account number alongside the
      account name (rendered inline as <code>1000</code> · Operating Bank).
  A2  Account-mapping template gracefully handles accounts with no number
      (renders the "No account number" secondary line).
  A3  Preview-import template renders an explicit PCLaw # column and a
      QBO # column so customers can confirm account numbers at a glance.
  A4  Opening-balance template renders QBO AcctNum alongside the QBO
      account name on each per-account row.
  A5  build_account_quality_preview returns the new qbo_acct_num and
      pclaw_account_number/pclaw_account_name fields on each row.

  G1  The public /quickbooks-guide page renders 200 with the expected
      plain-language sections.
  G2  The QBO-guide page is linked from the migration checklist.
  G3  The QBO-guide page is linked from the authenticated nav.
"""

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-acctnum")

import app as appmod  # noqa: E402
import migration_quality  # noqa: E402
import opening_balance  # noqa: E402


def _signup_and_login(client, email, firm):
    pwd = "passw0rd!1234"
    r = client.post("/signup", data={
        "firm_name": firm,
        "email": email,
        "password": pwd,
        "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


def a1_account_mapping_shows_number_with_name():
    from flask import render_template
    fake_job = {"id": "demo", "company": "A1 Co"}
    fake_conn = {"company_name": "QBO Co", "realm_id": "R-A1"}
    fake_rows = [
        {"idx": 0, "pclaw_name": "Operating Bank", "pclaw_number": "1000",
         "current_qbo_id": None, "is_saved": False, "is_suggestion": False},
    ]
    fake_accounts = [
        {"Id": "A11", "Name": "Bank Operating", "AcctNum": "1000",
         "AccountType": "Bank"},
    ]
    with appmod.app.test_request_context("/jobs/demo/account-mapping"):
        body = render_template(
            "account-mapping.html",
            job=fake_job, qbo_connection=fake_conn,
            rows=fake_rows, qbo_accounts=fake_accounts,
            user={"email": "a1@x.test"}, firm={"name": "A1 Firm"},
        )
    # PCLaw number sits next to the PCLaw name with the · separator.
    assert "<code>1000</code>" in body, \
        "expected PCLaw account number rendered as <code> next to the name"
    assert "Operating Bank" in body
    # QBO option label keeps name + (AcctNum) form for the dropdown.
    assert "Bank Operating" in body and "(1000)" in body
    print("A1 OK: account mapping shows PCLaw number alongside name")


def a2_account_mapping_handles_missing_number():
    from flask import render_template
    fake_rows = [
        {"idx": 0, "pclaw_name": "Misc Petty Cash", "pclaw_number": None,
         "current_qbo_id": None, "is_saved": False, "is_suggestion": False},
    ]
    with appmod.app.test_request_context("/jobs/demo/account-mapping"):
        body = render_template(
            "account-mapping.html",
            job={"id": "demo", "company": "A2 Co"},
            qbo_connection={"company_name": "QBO Co", "realm_id": "R-A2"},
            rows=fake_rows, qbo_accounts=[],
            user={"email": "a2@x.test"}, firm={"name": "A2 Firm"},
        )
    assert "Misc Petty Cash" in body
    assert "No account number" in body, \
        "expected friendly 'No account number' fallback for blank numbers"
    print("A2 OK: account mapping gracefully shows accounts with no number")


def a3_preview_import_template_has_account_number_columns():
    """Render preview-import.html directly with a hand-built preview."""
    from flask import render_template
    fake_job = {"id": "demo", "company": "A3 Co"}
    fake_conn = {"company_name": "QBO Co", "realm_id": "R-A3"}
    fake_preview = {
        "journal_entry_count": 1,
        "transaction_count_total": 1,
        "line_count": 2,
        "total_debits": "100.00",
        "total_credits": "100.00",
        "balanced": True,
        "unique_account_count": 2,
        "mapping_mode": "number",
        "mapped_account_count": 2,
        "unmapped_account_count": 0,
        "would_post": True,
        "accounts": [
            {
                "pclaw_display": "1000 Operating Bank",
                "pclaw_account_number": "1000",
                "pclaw_account_name": "Operating Bank",
                "mapped": True,
                "qbo_account_id": "A11",
                "qbo_account_name": "Bank Operating",
                "qbo_acct_num": "1000",
                "line_count": 1,
            },
            {
                "pclaw_display": "Owner Equity",
                "pclaw_account_number": None,
                "pclaw_account_name": "Owner Equity",
                "mapped": True,
                "qbo_account_id": "A99",
                "qbo_account_name": "Owners Equity",
                "qbo_acct_num": None,
                "line_count": 1,
            },
        ],
        "unmapped_accounts": [],
        "customers": [],
        "vendors": [],
        "blocked_transactions": [],
        "sample_lines": [],
    }
    with appmod.app.test_request_context("/jobs/demo/preview-import"):
        body = render_template(
            "preview-import.html",
            job=fake_job, qbo_connection=fake_conn,
            preview=fake_preview, preview_error=None,
            user={"email": "a3@x.test"}, firm={"name": "A3 Firm"},
        )
    # Column headers exist for both PCLaw # and QBO #.
    assert "<th>PCLaw #</th>" in body, "missing PCLaw # column header"
    assert "<th>QBO #</th>" in body, "missing QBO # column header"
    # Both numbers render as code blocks for the account that has them.
    assert body.count("<code>1000</code>") >= 2, \
        f"expected PCLaw 1000 and QBO 1000 codes; body excerpt: {body[:500]}"
    # The numberless row falls back to 'No number' rather than blank.
    assert "No number" in body, \
        "preview should render 'No number' fallback for missing AcctNum"
    print("A3 OK: preview-import shows PCLaw # and QBO # columns")


def a4_opening_balance_template_renders_qbo_acct_num():
    from flask import render_template
    fake_plan = {
        "as_of_date": "2026-04-01",
        "line_count": 1,
        "omitted_zero_rows": 0,
        "total_debit": "100.00",
        "total_credit": "100.00",
        "balanced": True,
        "blocker_count": 0,
        "blockers": [],
        "warnings": [],
        "has_blockers": False,
        "lines": [
            {
                "account_number": "1000",
                "account_name": "Operating Bank",
                "qbo_account_id": "A11",
                "qbo_account_name": "Bank Operating",
                "qbo_account_type": "Bank",
                "qbo_acct_num": "1000",
                "debit": "100.00",
                "credit": "0.00",
                "blocker": None,
            },
        ],
    }
    with appmod.app.test_request_context("/jobs/demo/opening-balance"):
        body = render_template(
            "opening-balance.html",
            job={"id": "demo", "company": "A4 Co"},
            qbo_connection={"company_name": "QBO Co", "realm_id": "R-A4"},
            plan=fake_plan,
            confirmation_phrase="POST OPENING BALANCE",
            confirmation_error=None,
            qbo_error=None,
            user={"email": "a4@x.test"}, firm={"name": "A4 Firm"},
        )
    # PCLaw number appears as a <code> tag for the account.
    assert "<code>1000</code>" in body
    # QBO AcctNum is shown alongside the QBO account name.
    assert "Bank Operating" in body
    # Both PCLaw + QBO numbers should render at least twice across the row.
    assert body.count(">1000<") >= 2 or body.count("<code>1000</code>") >= 2, \
        "opening balance row should show both PCLaw and QBO account numbers"
    print("A4 OK: opening-balance shows QBO AcctNum alongside QBO name")


def a5_account_quality_preview_includes_account_numbers():
    qbo_accounts = {
        "QueryResponse": {
            "Account": [
                {"Id": "A11", "Name": "Bank Operating", "AcctNum": "1000",
                 "AccountType": "Bank"},
                {"Id": "A99", "Name": "Owners Equity", "AcctNum": "",
                 "AccountType": "Equity"},
            ]
        }
    }
    rows = [
        {"transaction_id": "JE1", "date": "2026-04-01",
         "account_number": "1000", "account_name": "Operating Bank",
         "debit": "100.00", "credit": "0.00", "description": ""},
        {"transaction_id": "JE1", "date": "2026-04-01",
         "account_number": "", "account_name": "Owners Equity",
         "debit": "0.00", "credit": "100.00", "description": ""},
    ]
    preview = migration_quality.build_dry_run_preview(
        rows=rows, qbo_accounts_response=qbo_accounts, saved_mappings=[],
    )
    by_name = {a["pclaw_account_name"]: a for a in preview["accounts"]}
    assert "Operating Bank" in by_name, by_name
    op = by_name["Operating Bank"]
    assert op["pclaw_account_number"] == "1000"
    assert op["qbo_acct_num"] == "1000", op
    eq = by_name["Owners Equity"]
    # No PCLaw number in the row -> stored as None.
    assert eq["pclaw_account_number"] is None
    # QBO account has no AcctNum -> qbo_acct_num is None (not "").
    assert eq["qbo_acct_num"] is None
    print("A5 OK: account quality preview carries PCLaw + QBO account numbers")


def g1_qbo_guide_renders_with_expected_sections():
    c = appmod.app.test_client()
    r = c.get("/quickbooks-guide")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    expected_sections = [
        "New to QuickBooks Online",
        "What PCLaw Migrate posts into QuickBooks",
        "What does <em>not</em> happen automatically",
        "Where to find things in QuickBooks",
        "Chart of accounts",
        "Journal entries",
        "Customers and vendors",
        "Reports and balance checks",
        "After import",
    ]
    for needle in expected_sections:
        assert needle in body, f"QBO guide missing section: {needle!r}"
    # Plain-language reassurances about what does NOT happen.
    for must in (
        "does not replace",
        "does not connect",
        "does not change",
    ):
        assert must in body, f"QBO guide missing 'what does not happen' line: {must!r}"
    print("G1 OK: /quickbooks-guide renders with expected plain-English sections")


def g2_qbo_guide_linked_from_migration_checklist():
    c = appmod.app.test_client()
    _signup_and_login(c, "g2@x.test", "G2 Firm")
    r = c.get("/migration-checklist")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "/quickbooks-guide" in body, \
        "migration checklist should link to /quickbooks-guide"
    assert "New to QuickBooks Online" in body, \
        "migration checklist should surface the QBO guide CTA copy"
    print("G2 OK: migration checklist links to the QBO guide")


def g3_qbo_guide_linked_from_authenticated_nav():
    c = appmod.app.test_client()
    _signup_and_login(c, "g3@x.test", "G3 Firm")
    body = c.get("/dashboard").get_data(as_text=True)
    assert 'href="/quickbooks-guide"' in body, \
        "authenticated nav should link to /quickbooks-guide"
    print("G3 OK: authenticated nav exposes the QBO guide link")


if __name__ == "__main__":
    try:
        a1_account_mapping_shows_number_with_name()
        a2_account_mapping_handles_missing_number()
        a3_preview_import_template_has_account_number_columns()
        a4_opening_balance_template_renders_qbo_acct_num()
        a5_account_quality_preview_includes_account_numbers()
        g1_qbo_guide_renders_with_expected_sections()
        g2_qbo_guide_linked_from_migration_checklist()
        g3_qbo_guide_linked_from_authenticated_nav()
        print("\nALL ACCOUNT-NUMBER + QBO-GUIDE SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
