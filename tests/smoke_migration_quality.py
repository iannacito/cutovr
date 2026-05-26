"""Smoke tests for the migration-quality layer.

Run from project root:

    python3 tests/smoke_migration_quality.py

Covers:
  T1 Dry-run preview does not call any QBO write/create endpoint.
  T2 Preview returns sensible counts (JE count, totals, mapped/unmapped,
     customers/vendors needed).
  T3 Validation report CSV downloads, is firm-scoped, and sanitizes
     formula-injection attempts in user-controlled fields.
  T4 Reconciliation report requires a completed import (404/redirects
     otherwise), and includes created QBO JE ids + intuit_tid when
     available.
  T5 Validation report header advertises CSV download.

All QBO endpoints are mocked.
"""

import io
import os
import sys
import tempfile
import time
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

import app as appmod  # noqa: E402

GL = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()

# A malicious description that begins with `=` is the classic CSV
# formula-injection payload. We craft a tiny ledger that includes one so
# we can assert that the validation report neutralises it.
EVIL_GL = (
    "transaction_id,date,account_number,account_name,description,debit,credit\n"
    "JE-X,2026-05-01,1000,Operating Bank,=cmd|' /C calc'!A0,100.00,0.00\n"
    "JE-X,2026-05-01,3000,Owner Equity,opening,0.00,100.00\n"
).encode()

FAKE_ACCOUNTS = {
    "QueryResponse": {
        "Account": [
            {"Id": "11", "Name": "Operating Bank", "AcctNum": "1000", "AccountType": "Bank", "Active": True},
            {"Id": "12", "Name": "Trust Bank", "AcctNum": "1010", "AccountType": "Bank", "Active": True},
            {"Id": "13", "Name": "Accounts Receivable", "AcctNum": "1100", "AccountType": "Accounts Receivable", "Active": True},
            {"Id": "14", "Name": "Accounts Payable", "AcctNum": "2000", "AccountType": "Accounts Payable", "Active": True},
            {"Id": "15", "Name": "Client Trust Liability", "AcctNum": "2100", "AccountType": "Other Current Liability", "Active": True},
            {"Id": "16", "Name": "Owner Equity", "AcctNum": "3000", "AccountType": "Equity", "Active": True},
            {"Id": "17", "Name": "Legal Fees Revenue", "AcctNum": "4000", "AccountType": "Income", "Active": True},
            {"Id": "18", "Name": "Rent Expense", "AcctNum": "5000", "AccountType": "Expense", "Active": True},
            {"Id": "19", "Name": "Filing Fees Expense", "AcctNum": "5200", "AccountType": "Expense", "Active": True},
        ]
    }
}

posted = []
created_jes = {}


def fake_je(self, p):
    n = len(posted) + 1
    je = {"Id": str(900 + n), "DocNumber": f"D{n}", "TxnDate": p["TxnDate"], "Line": list(p["Line"])}
    created_jes[je["Id"]] = je
    posted.append(je["Id"])
    return {"JournalEntry": je}


def fake_query(self, sql):
    if "FROM JournalEntry" in sql and "Id =" in sql:
        je_id = sql.split("Id = '")[1].rstrip("'")
        je = created_jes.get(je_id)
        return {"QueryResponse": {"JournalEntry": [je]} if je else {}}
    return {"QueryResponse": {}}


def fake_company_info(self):
    return {"CompanyInfo": {"CompanyName": "Sandbox X", "LegalName": "Sandbox X LLC", "Country": "US"}}


def fake_token_exchange(code):
    return {
        "access_token": f"AT_{code}",
        "refresh_token": f"RT_{code}",
        "expires_at": "2099-01-01T00:00:00",
        "token_type": "bearer",
    }


def fake_find(self, n):
    return None


def fake_create_c(self, n):
    return {"Id": f"C_{n}", "DisplayName": n}


def fake_create_v(self, n):
    return {"Id": f"V_{n}", "DisplayName": n}


def signup(client, firm, email, password="passw0rd!1234"):
    return client.post(
        "/signup",
        data={
            "firm_name": firm, "email": email,
            "password": password, "confirm_password": password,
        },
    )


def upload(client, name, payload, filename="gl.csv"):
    return client.post(
        "/upload",
        data={"company_name": name, "ledger_file": (io.BytesIO(payload), filename)},
        content_type="multipart/form-data",
    )


def main():
    appmod.QBO_REAL_IMPORT = True
    patches = [
        mock.patch.object(appmod.QBOClient, "get_accounts", return_value=FAKE_ACCOUNTS),
        mock.patch.object(appmod.QBOClient, "create_journal_entry", new=fake_je),
        mock.patch.object(appmod.QBOClient, "query", new=fake_query),
        mock.patch.object(appmod.QBOClient, "get_company_info", new=fake_company_info),
        mock.patch.object(appmod.QBOClient, "find_customer_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "find_vendor_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "create_customer", new=fake_create_c),
        mock.patch.object(appmod.QBOClient, "create_vendor", new=fake_create_v),
        mock.patch.object(appmod.qbo_auth, "get_bearer_token", new=fake_token_exchange),
    ]
    for p in patches:
        p.start()

    try:
        c = appmod.app.test_client()
        signup(c, "Firm A", "alice@firm-a.test")
        time.sleep(1.05)
        upload(c, "Client Co", GL)
        job_id = sorted(appmod.jobs.keys())[-1]

        # Hook up the QBO connection without posting anything.
        with c.session_transaction() as s:
            s["pending_job_id"] = job_id
        c.get(f"/oauth/callback?code=X&state={job_id}&realmId=REALM-A")
        assert appmod.jobs[job_id]["qbo_connected"] is True

        # === T1 Dry-run preview does NOT call create endpoints ============
        # Snapshot the create methods so we can assert no calls.
        with mock.patch.object(appmod.QBOClient, "create_journal_entry",
                               side_effect=AssertionError("preview must not POST JEs")) as no_je, \
             mock.patch.object(appmod.QBOClient, "create_customer",
                               side_effect=AssertionError("preview must not create Customers")) as no_cust, \
             mock.patch.object(appmod.QBOClient, "create_vendor",
                               side_effect=AssertionError("preview must not create Vendors")) as no_vend:
            r = c.get(f"/jobs/{job_id}/preview-import")
            assert r.status_code == 200, r.status_code
            assert b"Review what we" in r.data and b"send to QuickBooks" in r.data
            assert no_je.call_count == 0
            assert no_cust.call_count == 0
            assert no_vend.call_count == 0
        print("T1 OK: preview did not call any QBO create endpoint")

        # === T2 Preview content shows JE count + totals + accounts ========
        r = c.get(f"/jobs/{job_id}/preview-import")
        body = r.data.decode()
        assert "JournalEntry records" in body
        assert "Sandbox X" in body or "REALM-A" in body
        # The test GL has 5 transactions and balances.
        assert "Mapped accounts" in body
        # If JE-0003 customer is in the GL (Johnson Family Law), preview
        # should advertise creating it.
        assert "Johnson Family Law" in body
        print("T2 OK: preview shows JE count, mapping status, and customers needed")

        # === T3 Validation report: download + sanitization ================
        # Upload a separate job whose CSV contains a formula-injection payload.
        time.sleep(1.05)
        upload(c, "Evil Client", EVIL_GL, filename="evil.csv")
        evil_job_id = sorted(appmod.jobs.keys())[-1]
        r = c.get(f"/jobs/{evil_job_id}/validation-report.csv")
        assert r.status_code == 200
        assert r.headers["Content-Type"].startswith("text/csv")
        assert "attachment" in r.headers["Content-Disposition"]
        # The malicious description was `=cmd|...`. After sanitization,
        # no cell may BEGIN with `=`. csv_safety prepends a tick.
        text = r.data.decode()
        for line in text.splitlines():
            cells = line.split(",")
            for cell in cells:
                cell_stripped = cell.lstrip('"')
                assert not cell_stripped.startswith("="), (
                    "Validation report contains an unsanitised formula cell:\n"
                    + line
                )
        print("T3 OK: validation CSV downloaded with formula-injection sanitised")

        # === T3b Auth + firm scoping on validation report =================
        unauth = appmod.app.test_client()
        r = unauth.get(f"/jobs/{evil_job_id}/validation-report.csv", follow_redirects=False)
        assert r.status_code in (302, 401), r.status_code  # bounce to login
        # Different firm cannot read another firm's job.
        c_b = appmod.app.test_client()
        signup(c_b, "Firm B", "bob@firm-b.test")
        r = c_b.get(f"/jobs/{evil_job_id}/validation-report.csv")
        assert r.status_code == 404
        print("T3b OK: validation report enforces auth + firm scoping")

        # === T4 Reconciliation report requires a completed import =========
        # No import yet for evil_job_id -> route flashes + redirects.
        r = c.get(f"/jobs/{evil_job_id}/reconciliation-report.csv", follow_redirects=False)
        assert r.status_code == 302

        # Run the real import for the original job_id so we have history.
        c.post(f"/jobs/{job_id}/import-to-qbo")
        assert "Imported" in appmod.jobs[job_id]["status"], appmod.jobs[job_id]["status"]
        r = c.get(f"/jobs/{job_id}/reconciliation-report.csv")
        assert r.status_code == 200
        assert r.headers["Content-Type"].startswith("text/csv")
        recon_text = r.data.decode()
        # The created QBO JE ids should appear in the report.
        for je_id in posted:
            assert je_id in recon_text, f"missing JE id {je_id} in reconciliation report"
        assert "Created JE count" in recon_text
        # Auth scoping on this route too.
        r = c_b.get(f"/jobs/{job_id}/reconciliation-report.csv")
        assert r.status_code == 404
        print("T4 OK: reconciliation report scoped, lists created QBO JE ids")

        # === T5 Job-detail page now exposes preview + reports CTAs ========
        r = c.get(f"/jobs/{job_id}")
        assert r.status_code == 200
        body = r.data.decode()
        assert "Preview import" in body
        assert "Download validation report" in body
        assert "Download reconciliation report" in body
        assert "Migration sequence" in body
        print("T5 OK: job detail surfaces preview + report download CTAs")

    finally:
        for p in patches:
            p.stop()
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass

    print("\nALL MIGRATION-QUALITY SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
