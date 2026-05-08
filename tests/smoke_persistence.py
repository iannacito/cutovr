"""Persistence smoke test: simulate a restart and verify the app rehydrates.

Run from project root:

    python3 tests/smoke_persistence.py

Checks:
  T1 Upload + connect QBO + import + verify, then `simulate_restart()`
     wipes the in-memory caches. Reading /jobs/<id> still works, the
     dashboard still lists the job, and the QBO tokens are still usable.
  T2 After restart, calling /jobs/<id>/disconnect-qbo clears both the
     cache AND the DB row.
  T3 Encrypted access tokens stored in the DB round-trip correctly
     through Fernet decrypt.
  T4 Cross-firm access still returns 404 after rehydration (the firm_id
     gate works on the DB-backed version too).
  T5 The token columns in qbo_connections never contain plaintext.

QBO API is fully mocked.
"""

import io
import json
import os
import sqlite3
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
from encryption import decrypt_token  # noqa: E402

GL = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()

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
    # Note: appmod.qbo_auth is an instance, so when we patch the bound
    # `get_bearer_token` method, the function is called with just `code`.
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
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
    )


def simulate_restart():
    """Wipe the in-memory caches the way a fresh process would."""
    appmod.jobs.clear()
    appmod.qbo_connections.clear()


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
        # --- Set up firm A and run a full happy-path import -----------------
        c = appmod.app.test_client()
        signup(c, "Firm A", "alice@firm-a.test")

        time.sleep(1.05)
        c.post(
            "/upload",
            data={"company_name": "Client Co", "ledger_file": (io.BytesIO(GL), "gl.csv")},
            content_type="multipart/form-data",
        )
        job_id = sorted(appmod.jobs.keys())[-1]

        # OAuth callback (with state == job_id) writes encrypted tokens to DB.
        with c.session_transaction() as s:
            s["pending_job_id"] = job_id
        c.get(f"/oauth/callback?code=X&state={job_id}&realmId=REALM-A")
        assert appmod.jobs[job_id]["qbo_connected"] is True
        assert appmod.qbo_connections[job_id]["realm_id"] == "REALM-A"

        c.post(f"/jobs/{job_id}/import-to-qbo")
        assert "Imported 5" in appmod.jobs[job_id]["status"]
        assert appmod.jobs[job_id]["verification"]["status"] == "ok"
        print("setup OK: signup + upload + connect + import + auto-verify")

        # === T1 ============================================================
        # Capture key state before restart
        je_count_before = appmod.jobs[job_id]["import_summary"]["qbo_je_count"]
        verification_before = appmod.jobs[job_id]["verification"]["status"]

        simulate_restart()
        assert job_id not in appmod.jobs and job_id not in appmod.qbo_connections

        # Dashboard renders + still lists the job
        r = c.get("/dashboard")
        assert r.status_code == 200 and b"Client Co" in r.data, r.data[:200]

        # Job detail page renders and still shows the company name + import counts
        r = c.get(f"/jobs/{job_id}")
        assert r.status_code == 200, r.status_code
        body = r.data.decode()
        assert "Imported 5 journal entries" in body
        assert "Sandbox X" in body  # company_name from QBO callback
        assert "REALM-A" in body
        # The job dict should be back in memory now (rehydrated)
        assert job_id in appmod.jobs
        assert appmod.jobs[job_id]["qbo_connected"] is True
        assert appmod.jobs[job_id]["import_summary"]["qbo_je_count"] == je_count_before
        assert appmod.jobs[job_id]["verification"]["status"] == verification_before
        print("T1 OK: restart-safe dashboard, job detail, import_summary, verification")

        # Re-running verify after restart must still succeed (uses tokens from DB)
        simulate_restart()
        r = c.post(f"/jobs/{job_id}/verify", follow_redirects=True)
        assert "Verification OK" in r.data.decode()
        print("T1b OK: verify after restart uses DB-backed tokens")

        # === T2 ============================================================
        simulate_restart()
        r = c.post(f"/jobs/{job_id}/disconnect-qbo", follow_redirects=True)
        assert appmod.jobs[job_id]["qbo_connected"] is False
        assert job_id not in appmod.qbo_connections
        # And the DB row is gone
        assert appmod.db.get_qbo_connection(job_id) is None
        print("T2 OK: disconnect after restart wipes both cache and DB")

        # === T3 ============================================================
        # Reconnect to verify token round-trip via DB
        with c.session_transaction() as s:
            s["pending_job_id"] = job_id
        c.get(f"/oauth/callback?code=Y&state={job_id}&realmId=REALM-A2")
        row = appmod.db.get_qbo_connection(job_id)
        assert row["access_token_enc"] and row["access_token_enc"] != "AT_Y"
        assert row["refresh_token_enc"] and row["refresh_token_enc"] != "RT_Y"
        assert decrypt_token(row["access_token_enc"]) == "AT_Y"
        assert decrypt_token(row["refresh_token_enc"]) == "RT_Y"
        print("T3 OK: encrypted tokens in DB decrypt back to plaintext via Fernet")

        # === T4 ============================================================
        # Different firm cannot read the rehydrated job
        c_b = appmod.app.test_client()
        signup(c_b, "Firm B", "bob@firm-b.test")
        simulate_restart()  # force rehydration on next access
        r = c_b.get(f"/jobs/{job_id}")
        assert r.status_code == 404
        # Owner can still see it
        r = c.get(f"/jobs/{job_id}")
        assert r.status_code == 200
        print("T4 OK: cross-firm 404 still works after rehydration")

        # === T5 ============================================================
        # Open the SQLite file directly and confirm no plaintext tokens
        conn = sqlite3.connect(APP_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT access_token_enc, refresh_token_enc FROM qbo_connections").fetchall()
        conn.close()
        assert rows
        for r in rows:
            for col in (r["access_token_enc"], r["refresh_token_enc"]):
                assert col is not None
                assert "AT_" not in col and "RT_" not in col, "plaintext token leaked into DB"
        print("T5 OK: DB contains only ciphertext tokens")
    finally:
        for p in patches:
            p.stop()
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass

    print("\nALL PERSISTENCE SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
