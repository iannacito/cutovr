"""Phase 2 customer-controls smoke test.

Run from project root:

    python3 tests/smoke_customer_controls.py

Covers the changes in the phase2-customer-controls branch:

  T1 Configurable branding: APP_NAME, COMPANY_NAME, SUPPORT_EMAIL,
     SECURITY_EMAIL, PRIVACY_CONTACT_EMAIL appear in the rendered public
     pages and footer when set via env, and defaults to "PCLaw Migrate"
     / placeholder addresses when unset.

  T2 /healthz reports placeholder-email status.

  T3 QBO error parser: produces a friendly summary + next-action for
     known QBO errors and falls back gracefully for unknown ones.

  T4 Job purge: requires "DELETE" confirmation, deletes encrypted
     files + job row + qbo connection, and PRESERVES the import
     history row so duplicate-protection still blocks a re-upload of
     the same file content into the same realm.

  T5 Job purge UI copy explains the difference between "Delete local"
     and "Reverse in QuickBooks".

  T6 Import-failure error display surfaces the parsed hint on the job
     detail page (summary + collapsible technical detail).

  T7 /firm/imports lists imports across all firm jobs, shows the parent
     job link, and is firm-scoped (other firms not visible).

The QBO API is fully mocked; nothing leaves the machine.
"""

import importlib
import io
import os
import sys
import tempfile
import time
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _fresh_app(env_overrides=None):
    """Reload the app with fresh DBs and the requested env overrides.

    Required because branding values are captured at import time; tests
    that swap APP_NAME need a clean import.
    """
    for var in (
        "APP_DB", "IMPORT_HISTORY_DB", "APP_NAME", "COMPANY_NAME",
        "SUPPORT_EMAIL", "SECURITY_EMAIL", "PRIVACY_CONTACT_EMAIL",
    ):
        os.environ.pop(var, None)

    os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
    os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
    os.environ.setdefault("CSRF_DISABLE", "1")
    os.environ.setdefault("SECRET_KEY", "smoke-secret-customer-controls")
    for k, v in (env_overrides or {}).items():
        os.environ[k] = v

    for mod in ("branding", "app"):
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("app")


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
    je = {"Id": str(900 + n), "DocNumber": f"D{n}", "TxnDate": p["TxnDate"], "Line": list(p.get("Line") or [])}
    created_jes[je["Id"]] = je
    posted.append(je["Id"])
    return {"JournalEntry": je}


def fake_query(self, sql):
    if "FROM JournalEntry" in sql and "Id =" in sql:
        je_id = sql.split("Id = '")[1].rstrip("'")
        je = created_jes.get(je_id)
        return {"QueryResponse": {"JournalEntry": [je]} if je else {}}
    return {"QueryResponse": {}}


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


def login(client, email, password="passw0rd!1234"):
    return client.post("/login", data={"email": email, "password": password})


def logout(client):
    return client.post("/logout")


def t1_branding_overrides_render():
    appmod = _fresh_app({
        "APP_NAME": "Acme Migrate",
        "COMPANY_NAME": "Acme Software, Inc.",
        "SUPPORT_EMAIL": "help@acme.example",
        "SECURITY_EMAIL": "sec@acme.example",
        "PRIVACY_CONTACT_EMAIL": "privacy@acme.example",
    })
    c = appmod.app.test_client()
    sup = c.get("/support").get_data(as_text=True)
    assert "help@acme.example" in sup, "support page should show overridden SUPPORT_EMAIL"
    assert "sec@acme.example" in sup, "support page should show overridden SECURITY_EMAIL"
    assert "your-domain.example" not in sup, "support page should not leak placeholder when overrides set"

    priv = c.get("/privacy").get_data(as_text=True)
    assert "privacy@acme.example" in priv, "privacy page should show PRIVACY_CONTACT_EMAIL"
    assert "Acme Migrate" in priv, "privacy page title should reflect APP_NAME"

    base = c.get("/login").get_data(as_text=True)
    assert "Acme Software, Inc." in base, "footer should show COMPANY_NAME"
    print("T1 OK: branding env overrides flow into public pages and footer")


def t1b_branding_defaults():
    appmod = _fresh_app()
    c = appmod.app.test_client()
    sup = c.get("/support").get_data(as_text=True)
    assert "support@your-domain.example" in sup, "default SUPPORT_EMAIL should appear"
    assert "security@your-domain.example" in sup, "default SECURITY_EMAIL should appear"
    base = c.get("/login").get_data(as_text=True)
    assert "PC Law Migrate" in base, "default COMPANY_NAME should be PC Law Migrate"
    print("T1b OK: default branding shows PC Law Migrate and placeholder support/security emails")


def t2_healthz_reports_placeholder():
    # Branding-flag visibility moved to /healthz/detailed when /healthz
    # was locked down to status-only. We use the HEALTHZ_TOKEN to read it.
    token = "ctrl-token-12345"
    appmod = _fresh_app({"HEALTHZ_TOKEN": token})
    c = appmod.app.test_client()
    # Public /healthz is locked down — never leaks branding flags.
    pub = c.get("/healthz").get_json()
    assert pub == {"status": "ok"}, pub
    j = c.get(f"/healthz/detailed?token={token}").get_json()
    assert j["status"] == "ok", j
    # Defaults are placeholders, so the *_set flags should be False.
    assert j["branding_support_email_set"] is False, j
    assert j["branding_security_email_set"] is False, j

    appmod = _fresh_app({
        "SUPPORT_EMAIL": "real@example.com",
        "SECURITY_EMAIL": "real-sec@example.com",
        "HEALTHZ_TOKEN": token,
    })
    c = appmod.app.test_client()
    j = c.get(f"/healthz/detailed?token={token}").get_json()
    assert j["branding_support_email_set"] is True, j
    assert j["branding_security_email_set"] is True, j
    print("T2 OK: /healthz/detailed reports branding placeholder vs real")


def t3_qbo_error_parser_unit():
    appmod = _fresh_app()
    parse = appmod.qbo_error_hint.parse

    p = parse('QBO returned 401: {"Fault":{"Error":[{"Message":"AuthenticationFailed","Detail":"Token expired","code":"3200"}]}}')
    assert p["status_code"] == 401, p
    assert "expired" in p["summary"].lower(), p
    assert p["action"] and "reconnect" in p["action"].lower(), p

    p2 = parse('QBO returned 400: {"Fault":{"Error":[{"Message":"Validation","Detail":"This account is inactive"}]}}')
    assert p2["status_code"] == 400, p2
    assert "inactive" in p2["summary"].lower() or "inactive" in (p2["action"] or "").lower(), p2
    assert p2["technical_detail"].startswith("QBO returned 400"), p2

    # Unknown error: still produces a non-empty summary, no action.
    p3 = parse("Random network blip")
    assert p3["summary"]
    assert p3["technical_detail"] == "Random network blip"
    print("T3 OK: qbo_error_hint parses known QBO faults and falls back gracefully")


def _do_signed_in_import(appmod, email_suffix, realm="REALM-CC"):
    """Sign up, upload, fake-connect, real-import. Returns (client, job_id)."""
    appmod.QBO_REAL_IMPORT = True
    posted.clear()
    created_jes.clear()
    c = appmod.app.test_client()
    signup(c, f"Firm {email_suffix}", f"{email_suffix}@x.test")
    time.sleep(1.05)
    c.post(
        "/upload",
        data={"company_name": "ClientCo", "ledger_file": (io.BytesIO(GL), "gl.csv")},
        content_type="multipart/form-data",
    )
    job_id = sorted(appmod.jobs.keys())[-1]
    appmod.qbo_connections[job_id] = {
        "realm_id": realm,
        "company_name": "Sandbox CC",
        "access_token_enc": appmod.encrypt_token("a"),
        "refresh_token_enc": appmod.encrypt_token("b"),
        "expires_at": "2099-01-01",
        "connected_at": "x",
    }
    appmod.db.upsert_qbo_connection(
        job_id=job_id, firm_id=appmod.jobs[job_id]["firm_id"], realm_id=realm,
        access_token_enc=appmod.encrypt_token("a"),
        refresh_token_enc=appmod.encrypt_token("b"),
        company_name="Sandbox CC", expires_at="2099-01-01",
    )
    appmod.jobs[job_id]["qbo_connected"] = True
    return c, job_id


def t4_purge_preserves_history():
    appmod = _fresh_app()
    patches = [
        mock.patch.object(appmod.QBOClient, "get_accounts", return_value=FAKE_ACCOUNTS),
        mock.patch.object(appmod.QBOClient, "create_journal_entry", new=fake_je),
        mock.patch.object(appmod.QBOClient, "query", new=fake_query),
        mock.patch.object(appmod.QBOClient, "find_customer_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "find_vendor_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "create_customer", new=fake_create_c),
        mock.patch.object(appmod.QBOClient, "create_vendor", new=fake_create_v),
    ]
    for p in patches:
        p.start()
    try:
        c, job_id = _do_signed_in_import(appmod, "t4_purge")
        c.post(f"/jobs/{job_id}/import-to-qbo")
        assert "Imported" in appmod.jobs[job_id]["status"]
        # Confirm the import history row exists.
        sha = appmod.jobs[job_id]["file_sha256"]
        assert appmod.history.has_completed_import(sha, "REALM-CC") is not None

        encrypted_path = appmod.UPLOAD_DIR / appmod.jobs[job_id]["encrypted_file"]
        assert encrypted_path.exists(), "pre-condition: encrypted file should exist"

        # Without confirmation: refuse to delete.
        r = c.post(f"/jobs/{job_id}/delete", data={}, follow_redirects=True)
        body = r.get_data(as_text=True)
        assert "Deletion not confirmed" in body, body[:300]
        assert appmod.jobs.get(job_id) is not None, "job must still exist"
        assert encrypted_path.exists(), "encrypted file must still exist after refusal"

        # With wrong text: refuse.
        r = c.post(f"/jobs/{job_id}/delete", data={"confirm_delete": "yes"}, follow_redirects=True)
        assert "Deletion not confirmed" in r.get_data(as_text=True)
        assert appmod.jobs.get(job_id) is not None

        # With "DELETE": purge.
        r = c.post(f"/jobs/{job_id}/delete", data={"confirm_delete": "DELETE"}, follow_redirects=True)
        body = r.get_data(as_text=True)
        assert "Local job data deleted" in body, body[:300]
        assert "does NOT remove" in body, body[:400]
        assert appmod.jobs.get(job_id) is None
        assert appmod.db.get_job(job_id) is None
        assert appmod.db.get_qbo_connection(job_id) is None
        assert not encrypted_path.exists(), "encrypted file should be gone"
        # Critical: import history is preserved so duplicate guard still works.
        assert appmod.history.has_completed_import(sha, "REALM-CC") is not None, \
            "import_history row must be preserved across local job purge"
        print("T4 OK: purge requires DELETE confirm; preserves import_history for duplicate guard")
    finally:
        for p in patches:
            p.stop()


def t5_purge_ui_copy_explains_difference():
    appmod = _fresh_app()
    patches = [
        mock.patch.object(appmod.QBOClient, "get_accounts", return_value=FAKE_ACCOUNTS),
        mock.patch.object(appmod.QBOClient, "create_journal_entry", new=fake_je),
        mock.patch.object(appmod.QBOClient, "query", new=fake_query),
        mock.patch.object(appmod.QBOClient, "find_customer_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "find_vendor_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "create_customer", new=fake_create_c),
        mock.patch.object(appmod.QBOClient, "create_vendor", new=fake_create_v),
    ]
    for p in patches:
        p.start()
    try:
        c, job_id = _do_signed_in_import(appmod, "t5_copy")
        c.post(f"/jobs/{job_id}/import-to-qbo")
        body = c.get(f"/jobs/{job_id}").get_data(as_text=True)
        # The danger-zone section must explain that QBO entries are NOT touched.
        assert "Delete local job data" in body
        assert "does <strong>not</strong> delete" in body or "does not delete" in body
        # Mentions the duplicate-history preservation.
        assert "duplicate-import history" in body
        # Has an explicit DELETE confirmation field.
        assert 'name="confirm_delete"' in body
        print("T5 OK: job-detail UI distinguishes 'delete local' from 'reverse in QBO'")
    finally:
        for p in patches:
            p.stop()


def t6_import_error_display():
    """Force a QBO 4xx and confirm the parsed hint shows on the job page."""
    appmod = _fresh_app()
    QBOError = appmod.QBOError

    def fake_je_fail(self, p):
        raise QBOError(
            'QBO returned 401: {"Fault":{"Error":[{"Message":"AuthenticationFailed","Detail":"Token expired","code":"3200"}]}}',
            status_code=401,
            body="...",
        )

    patches = [
        mock.patch.object(appmod.QBOClient, "get_accounts", return_value=FAKE_ACCOUNTS),
        mock.patch.object(appmod.QBOClient, "create_journal_entry", new=fake_je_fail),
        mock.patch.object(appmod.QBOClient, "find_customer_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "find_vendor_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "create_customer", new=fake_create_c),
        mock.patch.object(appmod.QBOClient, "create_vendor", new=fake_create_v),
    ]
    for p in patches:
        p.start()
    try:
        c, job_id = _do_signed_in_import(appmod, "t6_err")
        c.post(f"/jobs/{job_id}/import-to-qbo")
        # Job page should render the parsed hint.
        body = c.get(f"/jobs/{job_id}").get_data(as_text=True)
        assert "Last import error" in body, body[body.find('detail-page'):body.find('detail-page')+800]
        # Friendly summary instead of raw JSON.
        assert "expired" in body.lower(), body[:600]
        # Likely-next-step bubble.
        assert "Likely next step" in body
        # Technical detail collapsible carries the raw payload.
        assert "AuthenticationFailed" in body
        # HTTP status surfaced.
        assert "HTTP 401" in body
        print("T6 OK: import error renders parsed summary + collapsible technical detail")
    finally:
        for p in patches:
            p.stop()


def t7_firm_imports_view_is_scoped():
    appmod = _fresh_app()
    patches = [
        mock.patch.object(appmod.QBOClient, "get_accounts", return_value=FAKE_ACCOUNTS),
        mock.patch.object(appmod.QBOClient, "create_journal_entry", new=fake_je),
        mock.patch.object(appmod.QBOClient, "query", new=fake_query),
        mock.patch.object(appmod.QBOClient, "find_customer_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "find_vendor_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "create_customer", new=fake_create_c),
        mock.patch.object(appmod.QBOClient, "create_vendor", new=fake_create_v),
    ]
    for p in patches:
        p.start()
    try:
        # Firm A imports; firm B should not see firm A's imports.
        cA, job_a = _do_signed_in_import(appmod, "t7_a", realm="REALM-A")
        cA.post(f"/jobs/{job_a}/import-to-qbo")
        assert "Imported" in appmod.jobs[job_a]["status"], appmod.jobs[job_a]["status"]

        # New client = new session = new firm.
        cB = appmod.app.test_client()
        signup(cB, "Firm B", "t7_b@x.test")
        rB = cB.get("/firm/imports")
        assert rB.status_code == 200
        bodyB = rB.get_data(as_text=True)
        # Firm B has no imports yet — must not show firm A's job.
        assert job_a not in bodyB, "firm B must not see firm A's job id"
        assert "No imports recorded" in bodyB, bodyB[bodyB.find('All imports'):bodyB.find('All imports')+400]

        # Firm A view: shows the import row + parent job link.
        rA = cA.get("/firm/imports")
        bodyA = rA.get_data(as_text=True)
        assert job_a in bodyA, "firm A view must include the job id"
        assert "REALM-A" in bodyA, "firm A view must show realmId"
        assert "ClientCo" in bodyA, "firm A view must show parent job company"
        print("T7 OK: /firm/imports is firm-scoped and shows parent job context")
    finally:
        for p in patches:
            p.stop()


if __name__ == "__main__":
    t1_branding_overrides_render()
    t1b_branding_defaults()
    t2_healthz_reports_placeholder()
    t3_qbo_error_parser_unit()
    t4_purge_preserves_history()
    t5_purge_ui_copy_explains_difference()
    t6_import_error_display()
    t7_firm_imports_view_is_scoped()
    print("\nALL CUSTOMER-CONTROLS SMOKE TESTS PASSED")
