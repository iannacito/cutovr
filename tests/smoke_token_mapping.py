"""Token-refresh + account-mapping smoke test.

Run from project root:

    python3 tests/smoke_token_mapping.py

Covers:
  T1 Stored access token expired → refresh helper exchanges the refresh
     token, persists the new (encrypted) tokens, and the import succeeds.
  T2 Refresh failure → user sees a 'connection expired' flash and an
     audit row is logged.
  T3 Mapping page lists unique PCLaw accounts from the uploaded GL CSV
     and saves user-chosen QBO account IDs into the DB.
  T4 Saved mappings take priority over auto-match in the next import.
  T5 Import with missing accounts sets job.unmapped_accounts (so the
     job-detail page can render the link to the mapping UI) and does
     NOT post any JEs.
  T6 Cross-firm access to /jobs/<id>/account-mapping returns 404.

QBO API and refresh endpoint are fully mocked; nothing leaves the machine.
"""

import io
import os
import sys
import tempfile
import time
import unittest.mock as mock
from datetime import datetime, timedelta
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
from encryption import encrypt_token, decrypt_token  # noqa: E402

GL = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()

FAKE_ACCOUNTS = {
    "QueryResponse": {
        "Account": [
            {"Id": "A11", "Name": "Operating Bank", "AcctNum": "1000", "AccountType": "Bank", "Active": True},
            {"Id": "A12", "Name": "Trust Bank", "AcctNum": "1010", "AccountType": "Bank", "Active": True},
            {"Id": "A13", "Name": "Accounts Receivable", "AcctNum": "1100", "AccountType": "Accounts Receivable", "Active": True},
            {"Id": "A14", "Name": "Accounts Payable", "AcctNum": "2000", "AccountType": "Accounts Payable", "Active": True},
            {"Id": "A15", "Name": "Client Trust Liability", "AcctNum": "2100", "AccountType": "Other Current Liability", "Active": True},
            {"Id": "A16", "Name": "Owner Equity", "AcctNum": "3000", "AccountType": "Equity", "Active": True},
            {"Id": "A17", "Name": "Legal Fees Revenue", "AcctNum": "4000", "AccountType": "Income", "Active": True},
            {"Id": "A18", "Name": "Rent Expense", "AcctNum": "5000", "AccountType": "Expense", "Active": True},
            {"Id": "A19", "Name": "Filing Fees Expense", "AcctNum": "5200", "AccountType": "Expense", "Active": True},
        ]
    }
}

posted = []
created_jes = {}


def fake_je(self, p):
    n = len(posted) + 1
    je = {"Id": str(900 + n), "DocNumber": f"D{n}", "TxnDate": p["TxnDate"], "Line": list(p["Line"])}
    created_jes[je["Id"]] = je
    posted.append((je["Id"], p))
    return {"JournalEntry": je}


def fake_query(self, sql):
    if "FROM JournalEntry" in sql and "Id =" in sql:
        je_id = sql.split("Id = '")[1].rstrip("'")
        je = created_jes.get(je_id)
        return {"QueryResponse": {"JournalEntry": [je]} if je else {}}
    return {"QueryResponse": {}}


def fake_company_info(self):
    return {"CompanyInfo": {"CompanyName": "Sandbox Map", "LegalName": "X", "Country": "US"}}


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


def signup(client, firm, email, password="passw0rd!"):
    return client.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
    )


def setup_and_connect(client, firm_name, email, gl_bytes=GL):
    """Sign up + upload + complete OAuth callback. Returns job_id."""
    signup(client, firm_name, email)
    time.sleep(1.05)
    client.post(
        "/upload",
        data={"company_name": "Client", "ledger_file": (io.BytesIO(gl_bytes), "gl.csv")},
        content_type="multipart/form-data",
    )
    job_id = sorted(appmod.jobs.keys())[-1]
    with client.session_transaction() as s:
        s["pending_job_id"] = job_id
    client.get(f"/oauth/callback?code=X&state={job_id}&realmId=R-{job_id[-4:]}")
    return job_id


def main():
    appmod.QBO_REAL_IMPORT = True
    base_patches = [
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
    for p in base_patches:
        p.start()

    refresh_calls = []

    def fake_refresh(refresh_token):
        refresh_calls.append(refresh_token)
        # Intuit rotates the refresh token each time; we mimic that.
        return {
            "access_token": f"AT2_{refresh_token}",
            "refresh_token": f"RT2_{refresh_token}",
            "expires_at": (datetime.utcnow() + timedelta(hours=1)).isoformat(),
            "token_type": "bearer",
        }

    refresh_patch = mock.patch.object(
        appmod.qbo_auth, "refresh_access_token", new=fake_refresh
    )

    try:
        # === T1: expired access token -> refresh -> import succeeds ========
        c = appmod.app.test_client()
        job_id = setup_and_connect(c, "Firm A", "alice@a.test")

        # Force the stored access token to look expired.
        past = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
        with appmod.db._conn() as conn:  # type: ignore[attr-defined]
            conn.execute(
                "UPDATE qbo_connections SET expires_at = ? WHERE job_id = ?",
                (past, job_id),
            )
        appmod.qbo_connections[job_id]["expires_at"] = past

        with refresh_patch:
            r = c.post(f"/jobs/{job_id}/import-to-qbo", follow_redirects=True)
        assert "Imported 5" in appmod.jobs[job_id]["status"], appmod.jobs[job_id]["status"]
        assert len(refresh_calls) == 1, refresh_calls
        # New tokens were saved in DB and decrypt back correctly.
        row = appmod.db.get_qbo_connection(job_id)
        assert row["access_token_enc"] and decrypt_token(row["access_token_enc"]).startswith("AT2_")
        assert row["refresh_token_enc"] and decrypt_token(row["refresh_token_enc"]).startswith("RT2_")
        # Audit log captured the refresh
        actions = [a["action"] for a in appmod.db.recent_audit_for_firm(
            appmod.db.authenticate("alice@a.test", "passw0rd!")["firm_id"], 50)]
        assert "qbo_token_refreshed" in actions, actions
        print("T1 OK: expired token refreshed and persisted; import succeeded")

        # === T2: refresh failure -> friendly error + audit ================
        c2 = appmod.app.test_client()
        job_id2 = setup_and_connect(c2, "Firm B", "bob@b.test")
        past2 = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
        with appmod.db._conn() as conn:  # type: ignore[attr-defined]
            conn.execute("UPDATE qbo_connections SET expires_at = ? WHERE job_id = ?",
                         (past2, job_id2))
        appmod.qbo_connections[job_id2]["expires_at"] = past2

        def boom(rt):
            raise RuntimeError("HTTP 400: invalid_grant")

        with mock.patch.object(appmod.qbo_auth, "refresh_access_token", new=boom):
            r = c2.post(f"/jobs/{job_id2}/import-to-qbo", follow_redirects=True)
        body = r.data.decode()
        assert "QuickBooks connection expired" in body, body[:200]
        bob_firm = appmod.db.authenticate("bob@b.test", "passw0rd!")["firm_id"]
        actions = [a["action"] for a in appmod.db.recent_audit_for_firm(bob_firm, 50)]
        assert "qbo_token_refresh_failed" in actions
        print("T2 OK: refresh failure shows friendly error and audits it")

        # === T3: mapping page lists PCLaw accounts and saves mappings =====
        c3 = appmod.app.test_client()
        job_id3 = setup_and_connect(c3, "Firm C", "carol@c.test")
        firm_c = appmod.db.authenticate("carol@c.test", "passw0rd!")["firm_id"]

        r = c3.get(f"/jobs/{job_id3}/account-mapping")
        assert r.status_code == 200
        body = r.data.decode()
        for label in ["Operating Bank", "Trust Bank", "Accounts Receivable",
                      "Accounts Payable", "Owner Equity"]:
            assert label in body, label
        # Both auto-match suggestions and an unmapped row would render —
        # but with this account list every PCLaw row has a perfect AcctNum
        # match, so there should be at least one Auto-match badge.
        assert "Auto-match" in body

        # Save a mapping that overrides one PCLaw account to a different QBO
        # account (Operating Bank 1000 -> A12 Trust Bank, on purpose).
        # We submit the form fields shaped exactly like the template renders.
        post_data = {
            "pclaw_num[0]": "1000",
            "pclaw_name[0]": "Operating Bank",
            "mapping[0]": "A12",  # override
            "pclaw_num[1]": "3000",
            "pclaw_name[1]": "Owner Equity",
            "mapping[1]": "A16",
        }
        r = c3.post(f"/jobs/{job_id3}/account-mapping", data=post_data, follow_redirects=True)
        assert "Saved 2 account mapping" in r.data.decode()
        realm_c = appmod.qbo_connections[job_id3]["realm_id"]
        saved = appmod.db.list_account_mappings(firm_c, realm_c)
        keys = {(m["pclaw_account_number"], m["pclaw_account_name"]) for m in saved}
        assert ("1000", "Operating Bank") in keys
        assert ("3000", "Owner Equity") in keys
        # Override stuck
        op = next(m for m in saved if m["pclaw_account_number"] == "1000")
        assert op["qbo_account_id"] == "A12", op
        print("T3 OK: mapping page lists accounts and saves DB rows")

        # === T4: saved mappings used in the next import ====================
        # We posted with REAL_IMPORT on, so the import will run with the
        # override now. Capture which QBO account ID was used for the
        # 'Operating Bank' line.
        posted.clear()
        created_jes.clear()
        # Ensure the file isn't blocked as a duplicate from prior runs in
        # a different realm — it isn't, because c3 is a different firm.
        r = c3.post(f"/jobs/{job_id3}/import-to-qbo")
        assert "Imported 5" in appmod.jobs[job_id3]["status"], appmod.jobs[job_id3]["status"]
        # Find the JE that has the Operating Bank line
        used_account_ids = set()
        for je_id, payload in posted:
            for line in payload["Line"]:
                used_account_ids.add(line["JournalEntryLineDetail"]["AccountRef"]["value"])
        assert "A12" in used_account_ids, ("override mapping not honored", used_account_ids)
        # And A11 (the original auto-match for 1000) should NOT have been used
        assert "A11" not in used_account_ids, ("auto-match A11 leaked through", used_account_ids)
        print("T4 OK: saved override mapping took priority over auto-match")

        # === T5: missing accounts -> unmapped_accounts populated, no JEs ==
        # Wipe the QBO accounts so nothing matches
        empty_accounts = {"QueryResponse": {"Account": [
            {"Id": "X1", "Name": "Sales", "AcctNum": "9999", "AccountType": "Income", "Active": True},
            {"Id": "X2", "Name": "Cost", "AcctNum": "9998", "AccountType": "Expense", "Active": True},
        ]}}
        c5 = appmod.app.test_client()
        job_id5 = setup_and_connect(c5, "Firm D", "dave@d.test")
        before = len(posted)
        with mock.patch.object(appmod.QBOClient, "get_accounts", return_value=empty_accounts):
            r = c5.post(f"/jobs/{job_id5}/import-to-qbo", follow_redirects=True)
        assert appmod.jobs[job_id5]["status"] == "Import blocked: unmapped accounts"
        assert appmod.jobs[job_id5].get("unmapped_accounts"), appmod.jobs[job_id5].get("unmapped_accounts")
        assert len(posted) == before, "no JEs should have been posted"
        # Job-detail page surfaces the mapping link
        r = c5.get(f"/jobs/{job_id5}")
        body = r.data.decode()
        assert "Open the account mapping page" in body
        assert f"/jobs/{job_id5}/account-mapping" in body
        print("T5 OK: missing accounts blocked, banner links to mapping page")

        # === T6: cross-firm access to mapping is 404 ======================
        c6 = appmod.app.test_client()
        signup(c6, "Other Firm", "eve@e.test")
        r = c6.get(f"/jobs/{job_id3}/account-mapping")
        assert r.status_code == 404
        r = c6.post(f"/jobs/{job_id3}/account-mapping", data={"mapping[0]": "X1"})
        assert r.status_code == 404
        print("T6 OK: cross-firm mapping access blocked")
    finally:
        for p in base_patches:
            p.stop()
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass

    print("\nALL TOKEN+MAPPING SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
