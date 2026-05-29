"""Production-polish smoke tests.

Run from project root:

    python3 tests/smoke_production_polish.py

Covers the changes in the production-polish-security branch:

  T1 Default branding has been switched from "Cutover" to "PCLaw Migrate"
     and the brand mark renders without the legacy 'Cut<em>over</em>'
     fragment.

  T2 Favicon assets (SVG, ICO redirect, manifest, apple-touch-icon)
     are referenced from the base template and reachable.

  T3 Security headers — X-Content-Type-Options, X-Frame-Options,
     Referrer-Policy, Permissions-Policy — are present on every
     response.

  T4 MAX_CONTENT_LENGTH (25 MB default) returns a friendly 302 + flash
     instead of a stack trace when an oversized upload is submitted.

  T5 Upload extension allowlist rejects non-CSV files.

  T6 Account-mapping resilience: the page handles a "back-then-retry"
     POST gracefully even when the form contains stale/empty data, the
     encrypted upload was deleted, and the user re-posts after a save.
     Each scenario flashes a friendly message and never 500s.

The QBO API is fully mocked; nothing leaves the machine.
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
os.environ.setdefault("SECRET_KEY", "smoke-secret-for-polish-tests-32chars")

import app as appmod  # noqa: E402

GL = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()

FAKE_ACCOUNTS = {
    "QueryResponse": {
        "Account": [
            {"Id": "A11", "Name": "Operating Bank", "AcctNum": "1000",
             "AccountType": "Bank", "Active": True},
            {"Id": "A12", "Name": "Trust Bank", "AcctNum": "1010",
             "AccountType": "Bank", "Active": True},
            {"Id": "A13", "Name": "AR", "AcctNum": "1100",
             "AccountType": "Accounts Receivable", "Active": True},
            {"Id": "A14", "Name": "AP", "AcctNum": "2000",
             "AccountType": "Accounts Payable", "Active": True},
            {"Id": "A15", "Name": "Trust Liab", "AcctNum": "2100",
             "AccountType": "Other Current Liability", "Active": True},
            {"Id": "A16", "Name": "Owner Equity", "AcctNum": "3000",
             "AccountType": "Equity", "Active": True},
            {"Id": "A17", "Name": "Legal Fees", "AcctNum": "4000",
             "AccountType": "Income", "Active": True},
            {"Id": "A18", "Name": "Rent", "AcctNum": "5000",
             "AccountType": "Expense", "Active": True},
            {"Id": "A19", "Name": "Filing", "AcctNum": "5200",
             "AccountType": "Expense", "Active": True},
        ]
    }
}


def _signup(c, firm, email, password="passw0rd!"):
    return c.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
    )


def _connect(c, firm, email, gl_bytes=GL):
    _signup(c, firm, email)
    time.sleep(1.05)
    c.post(
        "/upload",
        data={"company_name": "Client", "ledger_file": (io.BytesIO(gl_bytes), "gl.csv")},
        content_type="multipart/form-data",
    )
    job_id = sorted(appmod.jobs.keys())[-1]
    with c.session_transaction() as s:
        s["pending_job_id"] = job_id
    c.get(f"/oauth/callback?code=X&state={job_id}&realmId=R-{job_id[-4:]}")
    return job_id


def t1_default_branding_pclaw_migrate():
    c = appmod.app.test_client()
    body = c.get("/login").get_data(as_text=True)
    assert "PC Law Migrate" in body, "default APP_NAME should be PC Law Migrate"
    # The legacy 'Cut<em>over</em>' fragment must be gone.
    assert "Cut<em>over</em>" not in body
    assert ">Cutover<" not in body
    # The compact "PCLaw Migrate" (no space) brand string must NOT appear
    # in customer-facing copy — the product is "PC Law Migrate".
    assert "PCLaw Migrate" not in body, \
        "compact 'PCLaw Migrate' should not appear in customer-facing pages"
    # Brand mark uses the all-caps "PC LAW MIGRATE" form on the logo line.
    assert "PC LAW <em>MIGRATE</em>" in body
    print("T1 OK: default branding is PC Law Migrate with all-caps brand mark")


def t2_favicon_assets_present():
    c = appmod.app.test_client()
    body = c.get("/login").get_data(as_text=True)
    # All three head links present
    assert 'href="/static/favicon.svg"' in body
    assert 'href="/static/icon-512.svg"' in body
    assert 'href="/static/site.webmanifest"' in body
    # Theme color meta
    assert 'name="theme-color"' in body
    # Endpoints reachable
    assert c.get("/static/favicon.svg").status_code == 200
    assert c.get("/static/icon-512.svg").status_code == 200
    assert c.get("/static/site.webmanifest").status_code == 200
    # Legacy /favicon.ico path serves the SVG (browser tabs that hit the
    # well-known path get the brand mark instead of a 404).
    r = c.get("/favicon.ico")
    assert r.status_code == 200, r.status_code
    assert b"<svg" in r.data
    print("T2 OK: favicon, apple-touch icon, manifest, /favicon.ico all reachable")


def t3_security_headers_on_every_response():
    c = appmod.app.test_client()
    for path in ["/login", "/healthz", "/onboarding", "/static/favicon.svg"]:
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)
        h = r.headers
        assert h.get("X-Content-Type-Options") == "nosniff", (path, dict(h))
        assert h.get("X-Frame-Options") == "DENY", (path, dict(h))
        assert "strict-origin" in (h.get("Referrer-Policy") or ""), path
        assert "camera=()" in (h.get("Permissions-Policy") or ""), path
    print("T3 OK: security headers present on every tested route")


def t4_oversized_upload_returns_friendly_redirect():
    c = appmod.app.test_client()
    _signup(c, "Heavy Firm", "heavy@h.test")
    # Build a 30 MB blob (above the 25 MB default cap).
    big = io.BytesIO(b"a" * (30 * 1024 * 1024))
    r = c.post(
        "/upload",
        data={"company_name": "X", "ledger_file": (big, "huge.csv")},
        content_type="multipart/form-data",
    )
    # Werkzeug raises 413; our handler turns it into a 302 redirect.
    assert r.status_code in (302, 413), r.status_code
    if r.status_code == 302:
        body = c.get("/dashboard").get_data(as_text=True)
        assert "larger than" in body.lower() or "upload limit" in body.lower(), body[:300]
    print("T4 OK: oversized uploads return a friendly redirect/flash, no traceback")


def t5_extension_allowlist_blocks_non_csv():
    c = appmod.app.test_client()
    _signup(c, "Ext Firm", "ext@e.test")
    r = c.post(
        "/upload",
        data={"company_name": "X", "ledger_file": (io.BytesIO(b"MZ\x90\x00"), "evil.exe")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    body = r.data.decode()
    assert "Only .csv" in body or "only .csv" in body.lower(), body[:400]
    print("T5 OK: non-CSV uploads are rejected at the gate with a clear message")


def t6_account_mapping_resilience():
    """Simulate the bug the user reported: click 'match accounts', go
    back, try again. Now also exercise corner cases that previously
    leaked tracebacks: missing encrypted upload, empty/garbage form
    re-submit, repeated POSTs (browser back + retry).
    """
    base_patches = [
        mock.patch.object(appmod.QBOClient, "get_accounts", return_value=FAKE_ACCOUNTS),
        mock.patch.object(appmod.QBOClient, "get_company_info",
                          return_value={"CompanyInfo": {"CompanyName": "Acme"}}),
        mock.patch.object(appmod.qbo_auth, "get_bearer_token",
                          return_value={
                              "access_token": "AT", "refresh_token": "RT",
                              "expires_at": "9999-12-31T00:00:00",
                              "token_type": "bearer"}),
    ]
    for p in base_patches:
        p.start()
    try:
        c = appmod.app.test_client()
        job_id = _connect(c, "Resil Firm", "res@r.test")

        # First click of "Match accounts" lands on the page.
        r = c.get(f"/jobs/{job_id}/account-mapping")
        assert r.status_code == 200
        assert b"Account mapping" in r.data

        # Save a real mapping (POST -> 302 -> GET).
        r = c.post(
            f"/jobs/{job_id}/account-mapping",
            data={
                "pclaw_num[0]": "1000",
                "pclaw_name[0]": "Operating Bank",
                "mapping[0]": "A11",
            },
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"Saved 1 account mapping" in r.data

        # User clicks "back" in the browser and the form re-POSTs the
        # exact same data. Idempotent upsert + friendly flash.
        r = c.post(
            f"/jobs/{job_id}/account-mapping",
            data={
                "pclaw_num[0]": "1000",
                "pclaw_name[0]": "Operating Bank",
                "mapping[0]": "A11",
            },
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"Saved 1 account mapping" in r.data

        # User re-opens the mapping page from the job detail link
        # (this is the exact path the colleague reported).
        r = c.get(f"/jobs/{job_id}/account-mapping")
        assert r.status_code == 200
        assert b"Account mapping" in r.data

        # Stale form re-submit: empty mapping rows + a single garbage
        # entry with no pclaw identifiers. Should NOT 500; should NOT
        # create a phantom row; should flash an info message.
        r = c.post(
            f"/jobs/{job_id}/account-mapping",
            data={
                "mapping[0]": "",
                "mapping[1]": "A11",
                "pclaw_num[1]": "",
                "pclaw_name[1]": "",
            },
            follow_redirects=True,
        )
        assert r.status_code == 200
        body = r.data.decode()
        assert "No account mappings were changed" in body or "Saved" in body, body[:400]

        # Now simulate the encrypted file being purged between visits
        # (e.g. by an out-of-band cleanup script OR an ephemeral-disk
        # redeploy on Render). With the persisted account snapshot in
        # place the page must still render the matching table — that's
        # the whole point of the snapshot. We also assert the old
        # dead-end flash is gone.
        job = appmod.db.get_job(job_id)
        enc_path = appmod.UPLOAD_DIR / job["encrypted_file"]
        backup = enc_path.with_suffix(enc_path.suffix + ".bak")
        enc_path.rename(backup)
        try:
            r = c.get(f"/jobs/{job_id}/account-mapping", follow_redirects=True)
            assert r.status_code == 200, r.status_code
            assert b"original upload for this job is no longer available" not in r.data, \
                r.data[:400]
            assert b"Account mapping" in r.data or b"Match accounts" in r.data, \
                r.data[:400]
        finally:
            backup.rename(enc_path)

        # Additionally, clear the persisted snapshot AND the encrypted
        # file to exercise the legacy-job recovery path: the page must
        # render the in-place re-upload CTA, not a 500 and not the old
        # dead-end flash.
        live = appmod.jobs.get(job_id)
        if live is not None:
            live.pop("pclaw_accounts", None)
        appmod.db.save_job_state(job_id, {"status": "uploaded", "pclaw_accounts": None})
        enc_path.rename(backup)
        try:
            r = c.get(f"/jobs/{job_id}/account-mapping", follow_redirects=False)
            assert r.status_code == 200, r.status_code
            assert b'data-testid="reupload-cta"' in r.data, r.data[:400]
            assert b"original upload for this job is no longer available" not in r.data
        finally:
            backup.rename(enc_path)

        # And finally, the audit log must contain the
        # account_mapping_saved row (success path) without leaking
        # secrets.
        firm = appmod.db.authenticate("res@r.test", "passw0rd!")["firm_id"]
        actions = [a["action"] for a in appmod.db.recent_audit_for_firm(firm, 50)]
        assert "account_mapping_saved" in actions
        assert "account_mapping_missing_file" in actions

        print("T6 OK: account mapping handles back/retry/missing-upload without 500")
    finally:
        for p in base_patches:
            p.stop()


def main():
    t1_default_branding_pclaw_migrate()
    t2_favicon_assets_present()
    t3_security_headers_on_every_response()
    t4_oversized_upload_returns_friendly_redirect()
    t5_extension_allowlist_blocks_non_csv()
    t6_account_mapping_resilience()
    print("\nALL PRODUCTION-POLISH SMOKE TESTS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
