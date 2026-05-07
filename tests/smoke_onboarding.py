"""Onboarding / template / preflight smoke tests.

Run from project root:

    python3 tests/smoke_onboarding.py

Covers:
  T1 /onboarding renders 200 with the required-column reference and
     beta disclaimer, and links to both CSV downloads.
  T2 /onboarding/template.csv returns a CSV body with every required
     header and a Content-Disposition attachment header.
  T3 /onboarding/sample.csv returns a CSV body with the GL header.
  T4 build_preflight_summary() against a balanced sample yields
     balanced=True, ready=True, transaction_count=2, line_count=4,
     and lists no missing required columns.
  T5 build_preflight_summary() against an unbalanced/missing-column
     sample reports the bad columns and balanced=False, ready=False.
  T6 The job-detail page after upload shows the preflight panel with
     the calculated totals and surfaces 'Sandbox' / 'Production' env
     status.
  T7 Logged-in nav contains the Onboarding link.
"""

import io
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-onboarding-secret")

import app as appmod  # noqa: E402
from preflight import build_preflight_summary  # noqa: E402


GL_BYTES = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()


def signup(client, firm, email, password="passw0rd!"):
    return client.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
    )


def t1_onboarding_page_renders():
    c = appmod.app.test_client()
    r = c.get("/onboarding")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    for needle in (
        "Onboarding",
        "transaction_id",
        "account_number",
        "account_name",
        "debit",
        "credit",
        "Download sample CSV template",
        "private beta",
        "Map accounts",
    ):
        assert needle in body, f"missing {needle!r} in /onboarding"
    assert "/onboarding/template.csv" in body
    assert "/onboarding/sample.csv" in body
    print("T1 OK: /onboarding renders with required columns + beta disclaimer")


def t2_template_csv_download():
    c = appmod.app.test_client()
    r = c.get("/onboarding/template.csv")
    assert r.status_code == 200
    assert r.mimetype == "text/csv", r.mimetype
    cd = r.headers.get("Content-Disposition", "")
    assert "attachment" in cd and "pclaw_qbo_template.csv" in cd, cd
    body = r.get_data(as_text=True)
    header = body.splitlines()[0]
    for col in ("transaction_id", "date", "account_number",
                "account_name", "debit", "credit", "memo"):
        assert col in header, f"template csv missing column {col!r}: {header}"
    print("T2 OK: /onboarding/template.csv has every required column header")


def t3_sample_csv_download():
    c = appmod.app.test_client()
    r = c.get("/onboarding/sample.csv")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    body = r.get_data(as_text=True)
    header = body.splitlines()[0]
    for col in ("transaction_id", "date", "account_number",
                "account_name", "debit", "credit"):
        assert col in header, f"sample csv missing {col!r}: {header}"
    assert "JE-0001" in body
    print("T3 OK: /onboarding/sample.csv returns a multi-transaction GL")


def t4_preflight_summary_balanced():
    rows = [
        {"transaction_id": "JE-1", "date": "2026-04-01",
         "account_number": "1000", "account_name": "Cash",
         "debit": "1000.00", "credit": "0.00"},
        {"transaction_id": "JE-1", "date": "2026-04-01",
         "account_number": "3000", "account_name": "Equity",
         "debit": "0.00", "credit": "1000.00"},
        {"transaction_id": "JE-2", "date": "2026-04-02",
         "account_number": "1100", "account_name": "AR",
         "debit": "500.00", "credit": "0.00"},
        {"transaction_id": "JE-2", "date": "2026-04-02",
         "account_number": "4000", "account_name": "Revenue",
         "debit": "0.00", "credit": "500.00"},
    ]
    s = build_preflight_summary(rows)
    assert s["transaction_count"] == 2, s
    assert s["line_count"] == 4
    assert s["total_debits"] == "1500.00"
    assert s["total_credits"] == "1500.00"
    assert s["balanced"] is True
    assert s["unique_account_count"] == 4
    assert s["missing_required_columns"] == []
    assert s["ready"] is True
    print("T4 OK: preflight summary on balanced rows reports ready=True")


def t5_preflight_summary_unbalanced_and_missing_cols():
    rows = [
        {"transaction_id": "JE-1", "date": "2026-04-01",
         "account_number": "1000", "account_name": "Cash",
         "debit": "1000.00", "credit": "0.00"},
        {"transaction_id": "JE-1", "date": "",  # missing date
         "account_number": "", "account_name": "",  # missing account
         "debit": "0.00", "credit": "900.00"},  # unbalanced
    ]
    fieldnames = ["transaction_id", "date", "account_number",
                  "account_name", "debit"]  # missing 'credit'
    s = build_preflight_summary(rows, fieldnames=fieldnames)
    assert s["balanced"] is False
    assert s["missing_required_columns"] == ["credit"], s["missing_required_columns"]
    assert s["rows_missing_account"] == 1
    assert s["rows_missing_date"] == 1
    assert s["ready"] is False
    print("T5 OK: preflight summary surfaces missing column + imbalance")


def t6_job_detail_shows_preflight():
    c = appmod.app.test_client()
    signup(c, "Onboarding Firm", "ob@o.test")
    time.sleep(1.05)
    r = c.post(
        "/upload",
        data={"company_name": "OB Client",
              "ledger_file": (io.BytesIO(GL_BYTES), "gl.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    body = r.get_data(as_text=True)
    assert "Import preflight" in body, body[:500]
    assert "Total debits" in body
    assert "Total credits" in body
    # qbo_environment defaults to sandbox in test env
    assert "Sandbox" in body or "Production" in body
    assert "Unmapped accounts" in body
    print("T6 OK: job-detail page renders preflight panel after upload")


def t7_logged_in_nav_has_onboarding_link():
    c = appmod.app.test_client()
    signup(c, "NavCheck Firm", "nav@n.test")
    r = c.get("/dashboard")
    body = r.get_data(as_text=True)
    assert 'href="/onboarding"' in body, "logged-in nav missing /onboarding link"
    print("T7 OK: logged-in nav contains Onboarding link")


def t8_unauth_nav_has_onboarding_link():
    c = appmod.app.test_client()
    r = c.get("/login")
    body = r.get_data(as_text=True)
    assert 'href="/onboarding"' in body, "logged-out nav missing /onboarding link"
    print("T8 OK: logged-out nav contains Onboarding link")


if __name__ == "__main__":
    try:
        t1_onboarding_page_renders()
        t2_template_csv_download()
        t3_sample_csv_download()
        t4_preflight_summary_balanced()
        t5_preflight_summary_unbalanced_and_missing_cols()
        t6_job_detail_shows_preflight()
        t7_logged_in_nav_has_onboarding_link()
        t8_unauth_nav_has_onboarding_link()
        print("\nALL ONBOARDING / PREFLIGHT SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
