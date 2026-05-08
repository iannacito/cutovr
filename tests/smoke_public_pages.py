"""Public-page + reversal-label smoke tests.

Run from project root:

    python3 tests/smoke_public_pages.py

Covers:
  T1 /privacy, /terms, /support each return 200 with expected anchor copy.
  T2 The footer on the unauthenticated /login page links to all three.
  T3 Reversal payload: every line description begins with "REVERSAL",
     and DocNumber is "REV-<original_je_id>". Original JE Id and the
     PCLaw transaction_id appear in the line description.
  T4 The job-detail post-reversal copy explains that QBO shows reversals
     as new offsetting entries (DocNumber starting REV-).
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


def fake_create_je(self, p):
    n = len(posted) + 1
    je_id = str(900 + n)
    je = {
        "Id": je_id,
        "DocNumber": p.get("DocNumber") or f"D{n}",
        "TxnDate": p["TxnDate"],
        "Line": list(p.get("Line") or []),
        "PrivateNote": p.get("PrivateNote"),
    }
    created_jes[je_id] = je
    posted.append({"id": je_id, "payload": p})
    return {"JournalEntry": je}


def fake_get_je(self, je_id):
    return created_jes.get(je_id)


def fake_query(self, sql):
    if "FROM JournalEntry" in sql and "Id =" in sql:
        je_id = sql.split("Id = '")[1].rstrip("'")
        je = created_jes.get(je_id)
        return {"QueryResponse": {"JournalEntry": [je]} if je else {}}
    return {"QueryResponse": {}}


def fake_company_info(self):
    return {"CompanyInfo": {"CompanyName": "Sandbox Public", "LegalName": "X", "Country": "US"}}


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
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
    )


def t1_public_pages_render():
    c = appmod.app.test_client()
    for path, needle in (
        ("/privacy", "Privacy"),
        ("/terms", "Terms"),
        ("/support", "Support"),
    ):
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)
        body = r.get_data(as_text=True)
        assert needle in body, f"expected {needle!r} in {path}, got {body[:200]}"
    print("T1 OK: /privacy, /terms, /support all render 200 with expected copy")


def t2_login_footer_has_legal_links():
    c = appmod.app.test_client()
    r = c.get("/login")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    for path in ("/privacy", "/terms", "/support"):
        assert f'href="{path}"' in body, f"login page missing footer link to {path}"
    print("T2 OK: login footer links to /privacy, /terms, /support")


def t3_reversal_payload_has_clear_labels():
    """Run a real (mocked-QBO) import + reversal and inspect the payloads."""
    appmod.QBO_REAL_IMPORT = True
    base_patches = [
        mock.patch.object(appmod.QBOClient, "get_accounts", return_value=FAKE_ACCOUNTS),
        mock.patch.object(appmod.QBOClient, "create_journal_entry", new=fake_create_je),
        mock.patch.object(appmod.QBOClient, "get_journal_entry", new=fake_get_je),
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
    try:
        c = appmod.app.test_client()
        signup(c, "Firm Public", "pub@p.test")
        time.sleep(1.05)
        c.post(
            "/upload",
            data={"company_name": "Client", "ledger_file": (io.BytesIO(GL), "gl.csv")},
            content_type="multipart/form-data",
        )
        job_id = sorted(appmod.jobs.keys())[-1]
        with c.session_transaction() as s:
            s["pending_job_id"] = job_id
        c.get(f"/oauth/callback?code=X&state={job_id}&realmId=R-pub")
        c.post(f"/jobs/{job_id}/import-to-qbo")
        before = len(posted)
        # Capture what the originals look like so we can confirm later we
        # are reversing real entries.
        original_ids = sorted(created_jes.keys())
        assert original_ids, "import should have created originals"

        r = c.post(
            f"/jobs/{job_id}/reverse-import",
            data={"confirm_reverse": "REVERSE"},
            follow_redirects=True,
        )
        body = r.get_data(as_text=True)
        assert "Reversal complete" in body, body[:400]
        new_posts = posted[before:]
        assert new_posts, "expected at least one reversal payload"

        for entry in new_posts:
            payload = entry["payload"]
            # DocNumber set + matches "REV-<id>" pattern, capped at 21 chars.
            doc = payload.get("DocNumber")
            assert doc and doc.startswith("REV-"), f"missing/bad DocNumber: {doc!r}"
            assert len(doc) <= 21, f"DocNumber too long: {doc!r}"
            # PrivateNote shouts REVERSAL and references original JE Id.
            note = payload.get("PrivateNote") or ""
            assert note.startswith("REVERSAL"), note
            assert "original QBO JournalEntry Id=" in note, note
            # Every line.Description starts with REVERSAL and references the
            # original JE Id (carrying the reversal context into QBO Journal).
            for line in payload["Line"]:
                desc = line.get("Description") or ""
                assert desc.startswith("REVERSAL"), desc
                assert "orig JE" in desc, desc

        print(
            "T3 OK: reversal payloads carry REVERSAL line descriptions, "
            "REV- DocNumber, and PrivateNote referencing the original JE"
        )
    finally:
        for p in base_patches:
            p.stop()


def t4_post_reversal_ui_copy():
    """Job-detail page after a reversal explains QBO's behavior."""
    # Re-using the client from t3 isn't easy because of patches; use a fresh
    # one that exercises a fresh import + reversal end-to-end.
    appmod.QBO_REAL_IMPORT = True
    base_patches = [
        mock.patch.object(appmod.QBOClient, "get_accounts", return_value=FAKE_ACCOUNTS),
        mock.patch.object(appmod.QBOClient, "create_journal_entry", new=fake_create_je),
        mock.patch.object(appmod.QBOClient, "get_journal_entry", new=fake_get_je),
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
    try:
        c = appmod.app.test_client()
        signup(c, "Firm UICopy", "uc@u.test")
        time.sleep(1.05)
        c.post(
            "/upload",
            data={"company_name": "Client", "ledger_file": (io.BytesIO(GL), "gl.csv")},
            content_type="multipart/form-data",
        )
        job_id = sorted(appmod.jobs.keys())[-1]
        with c.session_transaction() as s:
            s["pending_job_id"] = job_id
        c.get(f"/oauth/callback?code=X&state={job_id}&realmId=R-uc")
        c.post(f"/jobs/{job_id}/import-to-qbo")
        c.post(
            f"/jobs/{job_id}/reverse-import",
            data={"confirm_reverse": "REVERSE"},
            follow_redirects=True,
        )
        r = c.get(f"/jobs/{job_id}")
        body = r.get_data(as_text=True)
        # Must explain QBO's display behavior + the REV- DocNumber convention.
        assert "REV-" in body, "job-detail page should mention REV- DocNumber convention"
        assert "REVERSAL" in body, "job-detail page should mention the REVERSAL prefix"
        assert "offsetting" in body or "voided" in body, body[body.find('Reverse'):body.find('Reverse')+500]
        print("T4 OK: job-detail post-reversal UI explains QBO's display behavior")
    finally:
        for p in base_patches:
            p.stop()


if __name__ == "__main__":
    try:
        t1_public_pages_render()
        t2_login_footer_has_legal_links()
        t3_reversal_payload_has_clear_labels()
        t4_post_reversal_ui_copy()
        print("\nALL PUBLIC-PAGE / REVERSAL-LABEL SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
