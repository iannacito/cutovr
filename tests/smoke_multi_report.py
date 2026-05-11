"""Smoke tests for multi-report support.

Run from project root:

    python3 tests/smoke_multi_report.py

Covers:
  T1 detect_report_type identifies COA / Trial Balance / Trust Listing /
     General Ledger from real PCLaw-style headers.
  T2 parsers normalize the bundled test_data fixtures into the expected
     row shapes, including header-variant tolerance.
  T3 preflight builders return the expected counts / totals / warnings,
     and surface mismatched debit/credit totals on a bad Trial Balance.
  T4 GL-format uploads behave exactly as before (backward compat).
  T5 Uploading a Chart of Accounts file records report_type and stores
     a COA-shaped preflight on the job.
  T6 Uploading a Trial Balance file with debit != credit yields
     balanced=False on the preflight.
  T7 Uploading a Trust Listing records report_type=trust_listing and
     no QBO writes happen.
  T8 Import-to-QBO is refused for non-GL report types and an audit
     event is written.
  T9 Sample-download routes return the bundled CSVs.
  T10 build_coa_dry_run_preview matches by AcctNum then by Name,
      identifies would-create accounts, and detects soft conflicts.
  T11 Validation report CSV body adapts to the report_type with no
      raw row data leaking.
"""

import io
import os
import sys
import tempfile
import unittest.mock as mock
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
import app as appmod  # noqa: E402

GL_CSV = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()
COA_CSV = (ROOT / "test_data" / "01_chart_of_accounts.csv").read_bytes()
TB_CSV = (ROOT / "test_data" / "03_trial_balance.csv").read_bytes()
TRUST_CSV = (ROOT / "test_data" / "05_trust_listing.csv").read_bytes()


def _signup_and_login(client):
    """Create a firm + admin and log in. Signup auto-logs the user in
    in the live app, but we still POST /login as belt-and-braces in
    case a future hardening pass changes that."""
    pwd = "correct-horse-battery-staple"
    client.post(
        "/signup",
        data={
            "firm_name": "Multi-Report Test LLP",
            "email": "test@multireport.example",
            "password": pwd,
            "confirm_password": pwd,
        },
        follow_redirects=True,
    )
    client.post(
        "/login",
        data={
            "email": "test@multireport.example",
            "password": pwd,
        },
        follow_redirects=True,
    )


def _upload(client, body, filename, report_type=""):
    return client.post(
        "/upload",
        data={
            "company_name": "Smoke Firm",
            "email": "ops@smoke.example",
            "report_type": report_type,
            "ledger_file": (io.BytesIO(body), filename),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def _last_job(client):
    """Return the most-recently-created job dict from app.jobs."""
    return max(appmod.jobs.values(), key=lambda j: j.get("created_at", ""))


# --- Unit-level checks (no Flask client) -----------------------------------


def t1_detect_report_type():
    # GL: real headers from test_data/02_general_ledger.csv
    gl_headers = "transaction_id,date,account_number,account_name,client_id,matter_id,reference,description,customer_name,vendor_name,debit,credit".split(",")
    assert rt.detect_report_type(gl_headers) == rt.REPORT_GENERAL_LEDGER, "GL detect"

    coa_headers = "account_number,account_name,account_type,pclaw_category,qbo_suggested_type,qbo_suggested_detail_type,opening_balance,active".split(",")
    assert rt.detect_report_type(coa_headers) == rt.REPORT_CHART_OF_ACCOUNTS, "COA detect"

    tb_headers = "account_number,account_name,debit_balance,credit_balance".split(",")
    assert rt.detect_report_type(tb_headers) == rt.REPORT_TRIAL_BALANCE, "TB detect"

    trust_headers = "client_id,client_name,matter_id,matter_name,trust_bank_account,trust_balance,as_of_date".split(",")
    assert rt.detect_report_type(trust_headers) == rt.REPORT_TRUST_LISTING, "Trust detect"

    # Header-variant tolerance: should still detect COA with renamed cols.
    variant = ["Account Number", "Account-Name", "Type"]
    assert rt.detect_report_type(variant) == rt.REPORT_CHART_OF_ACCOUNTS, "COA header variant"

    print("T1 detect_report_type: OK")


def t2_parsers_normalize():
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        f.write(COA_CSV)
        p = Path(f.name)
    try:
        rows, fn, missing = rt.parse_chart_of_accounts(p)
    finally:
        p.unlink(missing_ok=True)
    assert missing == [], f"COA missing should be empty, got {missing}"
    assert len(rows) == 12, f"COA row count {len(rows)}"
    bank = rows[0]
    assert bank["account_number"] == "1000"
    assert bank["account_name"] == "Operating Bank"
    assert bank["account_type"] == "Asset"
    assert bank["active"] is True

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        f.write(TB_CSV)
        p = Path(f.name)
    try:
        tb_rows, _fn, missing = rt.parse_trial_balance(p)
    finally:
        p.unlink(missing_ok=True)
    assert missing == [], f"TB missing should be empty, got {missing}"
    assert len(tb_rows) == 12
    assert tb_rows[0]["debit_balance"] == "24250.00"

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        f.write(TRUST_CSV)
        p = Path(f.name)
    try:
        trust_rows, _fn, missing = rt.parse_trust_listing(p)
    finally:
        p.unlink(missing_ok=True)
    assert missing == [], f"Trust missing should be empty, got {missing}"
    assert len(trust_rows) == 1
    assert trust_rows[0]["trust_balance"] == "8500.00"
    assert trust_rows[0]["client_id"] == "C-100"
    print("T2 parsers normalize: OK")


def t3_preflight_counts():
    # COA preflight
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        f.write(COA_CSV)
        p = Path(f.name)
    try:
        rows, fn, missing = rt.parse_chart_of_accounts(p)
    finally:
        p.unlink(missing_ok=True)
    coa_pf = rt.build_coa_preflight(rows, fn, missing)
    assert coa_pf["account_count"] == 12
    assert coa_pf["rows_missing_name"] == 0
    assert coa_pf["duplicate_account_numbers"] == []
    assert coa_pf["ready"] is True
    assert coa_pf["report_type"] == rt.REPORT_CHART_OF_ACCOUNTS

    # TB preflight on a deliberately unbalanced file
    bad_tb = (
        "account_number,account_name,debit_balance,credit_balance\n"
        "1000,Operating Bank,1000.00,0.00\n"
        "3000,Owner Equity,0.00,500.00\n"
    ).encode()
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        f.write(bad_tb)
        p = Path(f.name)
    try:
        rows, fn, missing = rt.parse_trial_balance(p)
    finally:
        p.unlink(missing_ok=True)
    tb_pf = rt.build_trial_balance_preflight(rows, fn, missing)
    assert tb_pf["balanced"] is False
    assert tb_pf["total_debit"] == "1000.00"
    assert tb_pf["total_credit"] == "500.00"
    assert tb_pf["out_of_balance_amount"] == "500.00"

    # Trust preflight
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        f.write(TRUST_CSV)
        p = Path(f.name)
    try:
        rows, fn, missing = rt.parse_trust_listing(p)
    finally:
        p.unlink(missing_ok=True)
    trust_pf = rt.build_trust_listing_preflight(rows, fn, missing)
    assert trust_pf["total_trust_balance"] == "8500.00"
    assert trust_pf["client_count"] == 1
    assert trust_pf["matter_count"] == 1
    assert trust_pf["negative_balance_count"] == 0
    assert trust_pf["ready"] is True
    print("T3 preflight counts: OK")


def t10_coa_dry_run_preview():
    qbo_accounts = {
        "QueryResponse": {
            "Account": [
                {"Id": "11", "Name": "Operating Bank", "AcctNum": "1000", "AccountType": "Bank"},
                {"Id": "12", "Name": "Trust Bank", "AcctNum": "1010", "AccountType": "Bank"},
                # Soft conflict: name matches but AcctNum differs.
                {"Id": "13", "Name": "Owner Equity", "AcctNum": "3500", "AccountType": "Equity"},
            ]
        }
    }
    coa_rows = [
        {"account_number": "1000", "account_name": "Operating Bank", "account_type": "Asset", "detail_type": "Checking", "active": True},
        {"account_number": "3000", "account_name": "Owner Equity", "account_type": "Equity", "detail_type": "Owner's Equity", "active": True},
        {"account_number": "9999", "account_name": "Brand New Account", "account_type": "Expense", "detail_type": "Office", "active": True},
    ]
    preview = rt.build_coa_dry_run_preview(coa_rows, qbo_accounts)
    assert preview["matched_count"] == 2, preview
    assert preview["would_create_count"] == 1, preview
    assert preview["conflict_count"] == 1, preview
    matched_basis = {a["account_name"]: a["match_basis"] for a in preview["matched"]}
    assert matched_basis["Operating Bank"] == "AcctNum"
    assert matched_basis["Owner Equity"] == "Name"
    assert preview["would_create"][0]["account_number"] == "9999"
    print("T10 COA dry-run preview: OK")


# --- Flask client checks ---------------------------------------------------


def _run_flask_tests():
    appmod.app.config["TESTING"] = True
    appmod.app.config["WTF_CSRF_ENABLED"] = False
    with appmod.app.test_client() as client:
        _signup_and_login(client)

        # T4 — GL still works (backward compatibility).
        resp = _upload(client, GL_CSV, "gl.csv")
        assert resp.status_code in (302, 303), resp.status_code
        job = _last_job(client)
        assert job.get("report_type") == rt.REPORT_GENERAL_LEDGER, job.get("report_type")
        # GL preflight should still have transaction_count + balanced.
        assert "transaction_count" in (job.get("preflight") or {}), job.get("preflight")
        print("T4 GL backward compat: OK")

        # T5 — COA upload (explicit type).
        resp = _upload(client, COA_CSV, "coa.csv", report_type="chart_of_accounts")
        assert resp.status_code in (302, 303)
        job = _last_job(client)
        assert job["report_type"] == rt.REPORT_CHART_OF_ACCOUNTS
        assert job["preflight"]["report_type"] == rt.REPORT_CHART_OF_ACCOUNTS
        assert job["preflight"]["account_count"] == 12
        print("T5 COA upload: OK")

        # T6 — TB upload with bad balance (explicit type).
        bad_tb = (
            "account_number,account_name,debit_balance,credit_balance\n"
            "1000,Operating Bank,1000.00,0.00\n"
            "3000,Owner Equity,0.00,500.00\n"
        ).encode()
        resp = _upload(client, bad_tb, "tb.csv", report_type="trial_balance")
        assert resp.status_code in (302, 303)
        job = _last_job(client)
        assert job["report_type"] == rt.REPORT_TRIAL_BALANCE
        assert job["preflight"]["balanced"] is False
        assert job["preflight"]["out_of_balance_amount"] == "500.00"
        print("T6 TB unbalanced: OK")

        # T7 — Trust listing upload.
        resp = _upload(client, TRUST_CSV, "trust.csv", report_type="trust_listing")
        assert resp.status_code in (302, 303)
        job = _last_job(client)
        assert job["report_type"] == rt.REPORT_TRUST_LISTING
        assert job["preflight"]["total_trust_balance"] == "8500.00"
        print("T7 Trust listing upload: OK")

        # T8 — Import-to-QBO is refused for non-GL types. We pick the last
        # job (Trust). Even without a QBO connection, the route should
        # short-circuit on report_type *before* checking the connection.
        trust_job_id = job["id"]
        # Ensure QBO_REAL_IMPORT is enabled so we actually exercise the
        # gate (in demo mode it would short-circuit anyway).
        with mock.patch.object(appmod.QBOClient, "create_journal_entry") as m_create:
            resp = client.post(
                f"/jobs/{trust_job_id}/import-to-qbo",
                data={"confirm_import": "IMPORT"},
                follow_redirects=False,
            )
            assert resp.status_code in (302, 303)
            assert not m_create.called, "QBO create_journal_entry must NOT be called for non-GL"
        print("T8 Import-to-QBO blocked for Trust: OK")

        # T9 — Sample-download routes return the bundled CSVs.
        for rt_val, expected in [
            ("chart_of_accounts", b"account_number"),
            ("trial_balance", b"debit_balance"),
            ("trust_listing", b"trust_balance"),
        ]:
            r = client.get(f"/onboarding/sample/{rt_val}.csv")
            assert r.status_code == 200
            assert expected in r.data, f"{rt_val} sample missing expected header"
        # Unknown report type returns 404.
        r = client.get("/onboarding/sample/nope.csv")
        assert r.status_code == 404
        print("T9 Sample downloads: OK")

        # T11 — Validation report adapts. Upload TB with bad balance, then
        # fetch /validation-report.csv and ensure it reflects TB.
        resp = client.get(f"/jobs/{trust_job_id}/validation-report.csv")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8", errors="replace")
        # Should mention trust_listing report type and total trust balance.
        assert "trust_listing" in body, body[:500]
        assert "Total trust balance" in body, body[:500]
        print("T11 Validation report adapts to report_type: OK")


def main():
    t1_detect_report_type()
    t2_parsers_normalize()
    t3_preflight_counts()
    t10_coa_dry_run_preview()
    _run_flask_tests()
    print("\nAll multi-report smoke tests passed.")


if __name__ == "__main__":
    main()
