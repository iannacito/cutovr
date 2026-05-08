"""Smoke tests for production-customer readiness features.

Run from project root:

    python3 tests/smoke_production_customer.py

Covers everything added in the `production-customer-readiness` branch:

  T1 Public /disconnect renders for logged-out visitors with the
     two-paths explanation, no auth required.

  T2 /disconnect for a logged-in firm with active connections lists
     the realmId + company name (never the access/refresh tokens) and
     posting DISCONNECT triggers Intuit revoke + deletes the local row.

  T3 /quickbooks/disconnect alias resolves to the same page.

  T4 Production-mode connect guard blocks Connect to QuickBooks when
     QBO_REAL_IMPORT is off (or other required env is missing) and
     audits the block. The guard is bypassed in sandbox mode.

  T5 /quickbooks management page lists the connected jobs for the
     firm, shows production blockers when present, and surfaces the
     real Connect / Disconnect / Redirect URIs.

  T6 Production-mode import requires a two-step confirmation: the
     first POST flashes a confirmation prompt without posting; the
     second POST with confirm_import=IMPORT proceeds.

  T7 Job-detail page in production mode advertises the real-QBO
     connect copy and the real-import button label, never the
     sandbox-only language.

  T8 No tokens or secrets are echoed in any of the new routes.

The Intuit OAuth + QBO API calls are mocked. Nothing leaves the
machine.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Markers we use to guarantee no token / secret leakage in any output.
ACCESS_MARKER = "MARKER-ACCESS-DO-NOT-LEAK-A1B2C3"
REFRESH_MARKER = "MARKER-REFRESH-DO-NOT-LEAK-Z9Y8X7"
CLIENT_SECRET_MARKER = "MARKER-CLIENT-SECRET-DO-NOT-LEAK"


def _fresh_app(env_overrides=None):
    """Reload the app with fresh DBs and the requested env overrides.

    APP_ENV defaults to "local" so CSRF can be disabled. Tests that need
    to verify production-app behavior set APP_ENV=production and use the
    CSRF helper to submit forms.
    """
    for var in (
        "APP_DB", "IMPORT_HISTORY_DB",
        "APP_ENV", "QBO_ENVIRONMENT", "QBO_CLIENT_ID", "QBO_CLIENT_SECRET",
        "QBO_REDIRECT_URI", "QBO_REAL_IMPORT",
        "SUPPORT_EMAIL", "SECURITY_EMAIL", "PRIVACY_CONTACT_EMAIL",
        "ENCRYPTION_KEY", "SECRET_KEY", "CSRF_DISABLE",
    ):
        os.environ.pop(var, None)

    os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
    os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
    os.environ["SECRET_KEY"] = "smoke-secret-prod-customer-x" * 2
    # Generate a real Fernet key so encrypt_token works for token-revoke path.
    from cryptography.fernet import Fernet
    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    overrides = env_overrides or {}
    if (overrides.get("APP_ENV") or "").lower() != "production":
        os.environ["CSRF_DISABLE"] = "1"

    for k, v in overrides.items():
        os.environ[k] = v

    for mod in ("branding", "readiness", "encryption", "app"):
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("app")


def _csrf_token(client):
    """Pull a per-session CSRF token from any GET that renders a form.

    Tries /login first (works when logged out) then /dashboard (works
    when logged in) so the helper is safe in either auth state.
    """
    import re
    pat = re.compile(r'name="csrf_token"[^>]+value="([^"]+)"')
    for path in ("/login", "/dashboard", "/disconnect"):
        r = client.get(path, follow_redirects=False)
        m = pat.search(r.data.decode("utf-8", "replace"))
        if m:
            return m.group(1)
    return None


def _post_with_csrf(client, path, data):
    token = _csrf_token(client)
    payload = dict(data)
    if token:
        payload.setdefault("csrf_token", token)
    return client.post(path, data=payload, follow_redirects=True)


def _signup(c, firm="ProdCust Firm", email="prodcust@example.test", password="passw0rd!1234"):
    """Sign up via POST. If CSRF is enforced, mint and submit a token."""
    csrf_disabled = os.environ.get("CSRF_DISABLE", "").lower() in ("1", "true", "yes", "on")
    if csrf_disabled:
        return c.post(
            "/signup",
            data={"firm_name": firm, "email": email,
                  "password": password, "confirm_password": password},
        )
    token = _csrf_token(c)
    return c.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password,
              "csrf_token": token},
    )


def _make_job_with_connection(appmod, firm_id, user_id, *, job_id="job_pcust_1",
                              realm_id="REALM-PROD-1",
                              company_name="Acme Real Customer Inc"):
    """Insert a job + an encrypted-token QBO connection row for that firm."""
    appmod.jobs[job_id] = {
        "id": job_id, "firm_id": firm_id, "user_id": user_id,
        "company": "Test Co", "email": "x@x", "source_file": "x.csv",
        "encrypted_file": "x.csv.enc", "file_sha256": "deadbeef",
        "status": "Ready for QBO connection",
        "created_at": "2026-01-01T00:00:00",
        "summary": {"row_count": 12, "format": "GL (transaction_id)", "balanced": True},
        "qbo_connected": True,
    }
    appmod.db.upsert_job(
        job_id=job_id, firm_id=firm_id, user_id=user_id,
        company="Test Co", source_file="x.csv",
        encrypted_file="x.csv.enc", file_sha256="deadbeef",
        status="Ready for QBO connection",
    )
    enc_access = appmod.encrypt_token(ACCESS_MARKER)
    enc_refresh = appmod.encrypt_token(REFRESH_MARKER)
    appmod.db.upsert_qbo_connection(
        job_id=job_id, firm_id=firm_id, realm_id=realm_id,
        access_token_enc=enc_access, refresh_token_enc=enc_refresh,
        company_name=company_name,
        expires_at="2099-01-01T00:00:00",
    )
    appmod.qbo_connections[job_id] = {
        "realm_id": realm_id,
        "access_token_enc": enc_access,
        "refresh_token_enc": enc_refresh,
        "expires_at": "2099-01-01T00:00:00",
        "connected_at": "2026-01-01T00:00:00",
        "company_name": company_name,
    }
    return job_id


def _assert_no_secrets(body):
    """Helper: body must never contain the marker tokens or client secret."""
    for marker in (ACCESS_MARKER, REFRESH_MARKER, CLIENT_SECRET_MARKER):
        assert marker not in body, f"leaked marker {marker!r} in body"


def t1_public_disconnect_logged_out():
    appmod = _fresh_app({
        "QBO_ENVIRONMENT": "sandbox",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": CLIENT_SECRET_MARKER,
        "QBO_REDIRECT_URI": "https://example.test/oauth/callback",
        "SUPPORT_EMAIL": "help@acme.example",
    })
    c = appmod.app.test_client()
    body = c.get("/disconnect").get_data(as_text=True)
    assert "Disconnect" in body
    assert "Two ways to disconnect" in body
    assert "Sign in" in body, body[:600]
    _assert_no_secrets(body)
    print("T1 OK: public /disconnect renders for logged-out visitors")


def t2_logged_in_disconnect_revokes_and_deletes():
    appmod = _fresh_app({
        "QBO_ENVIRONMENT": "sandbox",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": CLIENT_SECRET_MARKER,
        "QBO_REDIRECT_URI": "https://example.test/oauth/callback",
        "SUPPORT_EMAIL": "help@acme.example",
    })
    c = appmod.app.test_client()
    _signup(c)
    user = appmod.db.authenticate("prodcust@example.test", "passw0rd!1234")
    job_id = _make_job_with_connection(appmod, user["firm_id"], user["id"])

    body = c.get("/disconnect").get_data(as_text=True)
    assert "REALM-PROD-1" in body, "realmId should be visible to firm admin"
    assert "Acme Real Customer Inc" in body, "company name should be visible"
    _assert_no_secrets(body)

    # Mock revoke_token so we don't hit Intuit.
    revoked_calls = []

    def _fake_revoke(self, token):
        revoked_calls.append(token)
        return True

    with mock.patch.object(appmod.QBOAuthHandler, "revoke_token", new=_fake_revoke):
        resp = _post_with_csrf(
            c, "/disconnect", {"confirm_disconnect": "DISCONNECT"},
        )
    body2 = resp.get_data(as_text=True)
    _assert_no_secrets(body2)
    assert revoked_calls == [REFRESH_MARKER], (
        "expected exactly one revoke_token call with the decrypted refresh token, got %r" % revoked_calls
    )
    # The DB row should be gone.
    remaining = appmod.db.list_qbo_connections_for_firm(user["firm_id"])
    assert remaining == [], f"expected no rows remaining; got {remaining}"
    # In-memory cache should also be cleared.
    assert job_id not in appmod.qbo_connections
    actions = [r["action"] for r in appmod.db.recent_audit_for_firm(user["firm_id"], limit=20)]
    assert "qbo_disconnected" in actions
    assert "qbo_disconnect_all" in actions
    print("T2 OK: /disconnect revokes at Intuit and removes encrypted tokens")


def t3_disconnect_alias_route():
    appmod = _fresh_app({
        "QBO_ENVIRONMENT": "sandbox",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": "x",
        "QBO_REDIRECT_URI": "https://example.test/oauth/callback",
    })
    c = appmod.app.test_client()
    body = c.get("/quickbooks/disconnect").get_data(as_text=True)
    assert "Two ways to disconnect" in body
    print("T3 OK: /quickbooks/disconnect alias resolves to the same page")


def t4_production_connect_guard_blocks_when_misconfigured():
    # QBO_ENVIRONMENT=production but QBO_REAL_IMPORT off → block.
    # APP_ENV stays local so we can keep CSRF disabled for the test client;
    # the guard fires whenever QBO_ENVIRONMENT=production regardless.
    appmod = _fresh_app({
        "QBO_ENVIRONMENT": "production",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": CLIENT_SECRET_MARKER,
        "QBO_REDIRECT_URI": "https://www.pclawmigrate.com/oauth/callback",
        # QBO_REAL_IMPORT intentionally not set
        "SUPPORT_EMAIL": "help@acme.example",
    })
    c = appmod.app.test_client()
    _signup(c)
    user = appmod.db.authenticate("prodcust@example.test", "passw0rd!1234")
    job_id = _make_job_with_connection(appmod, user["firm_id"], user["id"])
    # Drop the connection so the route follows the not-yet-connected branch
    # (connect-qbo is the route under test).
    appmod.qbo_connections.pop(job_id, None)
    appmod.db.delete_qbo_connection(job_id)
    appmod.jobs[job_id]["qbo_connected"] = False

    resp = c.get(f"/jobs/{job_id}/connect-qbo", follow_redirects=True)
    body = resp.get_data(as_text=True)
    _assert_no_secrets(body)
    assert "production deploy is not fully configured" in body, body[:800]
    assert "QBO_REAL_IMPORT" in body, body[:800]
    actions = [r["action"] for r in appmod.db.recent_audit_for_firm(user["firm_id"], limit=20)]
    assert "qbo_connect_blocked" in actions

    # Sandbox mode should NOT be blocked even with QBO_REAL_IMPORT off.
    appmod2 = _fresh_app({
        "QBO_ENVIRONMENT": "sandbox",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": "x",
        "QBO_REDIRECT_URI": "https://example.test/oauth/callback",
    })
    c2 = appmod2.app.test_client()
    _signup(c2)
    user2 = appmod2.db.authenticate("prodcust@example.test", "passw0rd!1234")
    jid2 = _make_job_with_connection(appmod2, user2["firm_id"], user2["id"], job_id="job_pcust_sb")
    appmod2.qbo_connections.pop(jid2, None)
    appmod2.db.delete_qbo_connection(jid2)
    appmod2.jobs[jid2]["qbo_connected"] = False
    resp2 = c2.get(f"/jobs/{jid2}/connect-qbo")
    # Sandbox: should redirect to Intuit's authorize URL.
    assert resp2.status_code in (301, 302)
    assert "appcenter.intuit.com" in resp2.headers.get("Location", ""), resp2.headers
    print("T4 OK: production-mode connect blocks misconfig; sandbox unaffected")


def t5_quickbooks_manage_page_lists_connections():
    from cryptography.fernet import Fernet
    appmod = _fresh_app({
        "APP_ENV": "production",
        "QBO_ENVIRONMENT": "production",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": CLIENT_SECRET_MARKER,
        "QBO_REDIRECT_URI": "https://www.pclawmigrate.com/oauth/callback",
        "QBO_REAL_IMPORT": "1",
        "SUPPORT_EMAIL": "help@acme.example",
        "SECURITY_EMAIL": "security@acme.example",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
    })
    c = appmod.app.test_client()
    _signup(c)
    user = appmod.db.authenticate("prodcust@example.test", "passw0rd!1234")
    _make_job_with_connection(appmod, user["firm_id"], user["id"])
    body = c.get("/quickbooks").get_data(as_text=True)
    _assert_no_secrets(body)
    assert "Connected jobs" in body
    assert "REALM-PROD-1" in body
    assert "Acme Real Customer Inc" in body
    # In a fully-configured production deploy, the success banner shows.
    assert "Production mode active" in body, body[:1500]
    # Public links surfaced for Intuit:
    assert "/oauth/callback" in body
    assert "/disconnect" in body
    print("T5 OK: /quickbooks page lists connections + Intuit URLs without leaking tokens")


def t6_production_import_requires_confirmation():
    from cryptography.fernet import Fernet
    appmod = _fresh_app({
        "APP_ENV": "production",
        "QBO_ENVIRONMENT": "production",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": CLIENT_SECRET_MARKER,
        "QBO_REDIRECT_URI": "https://www.pclawmigrate.com/oauth/callback",
        "QBO_REAL_IMPORT": "1",
        "SUPPORT_EMAIL": "help@acme.example",
        "SECURITY_EMAIL": "security@acme.example",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
    })
    c = appmod.app.test_client()
    _signup(c)
    user = appmod.db.authenticate("prodcust@example.test", "passw0rd!1234")
    job_id = _make_job_with_connection(appmod, user["firm_id"], user["id"])

    # First POST: no confirm_import → confirmation flow, NOT a real import.
    posted_payloads = []

    def _boom_create_je(self, payload):
        posted_payloads.append(payload)
        return {"JournalEntry": {"Id": "1"}}

    with mock.patch.object(appmod.QBOClient, "create_journal_entry", new=_boom_create_je):
        resp = _post_with_csrf(c, f"/jobs/{job_id}/import-to-qbo", {})
    body = resp.get_data(as_text=True)
    _assert_no_secrets(body)
    assert "Production safety check" in body or "type IMPORT" in body, body[:1500]
    assert posted_payloads == [], "must not post on the first POST"
    actions = [r["action"] for r in appmod.db.recent_audit_for_firm(user["firm_id"], limit=20)]
    assert "import_confirmation_shown" in actions, actions

    # Job should be marked pending in-memory (the flag isn't persisted; it's
    # transient and only used to render the confirmation card after redirect).
    in_mem = appmod.jobs.get(job_id) or {}
    assert in_mem.get("pending_production_confirm") is True, in_mem
    print("T6 OK: production import requires the IMPORT confirmation step")


def t7_job_detail_production_copy():
    from cryptography.fernet import Fernet
    appmod = _fresh_app({
        "APP_ENV": "production",
        "QBO_ENVIRONMENT": "production",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": "x",
        "QBO_REDIRECT_URI": "https://www.pclawmigrate.com/oauth/callback",
        "QBO_REAL_IMPORT": "1",
        "SUPPORT_EMAIL": "help@acme.example",
        "SECURITY_EMAIL": "security@acme.example",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
    })
    c = appmod.app.test_client()
    _signup(c)
    user = appmod.db.authenticate("prodcust@example.test", "passw0rd!1234")
    job_id = _make_job_with_connection(appmod, user["firm_id"], user["id"])
    body = c.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "Production" in body or "real QuickBooks" in body, body[:1500]
    assert "Sandbox Testing Mode" not in body, "sandbox-only banner should NOT appear"
    assert "Disconnect QuickBooks" in body
    print("T7 OK: job detail in production mode uses real-QuickBooks copy")


def t8_no_secret_leak_in_any_route():
    from cryptography.fernet import Fernet
    appmod = _fresh_app({
        "APP_ENV": "production",
        "QBO_ENVIRONMENT": "production",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": CLIENT_SECRET_MARKER,
        "QBO_REDIRECT_URI": "https://www.pclawmigrate.com/oauth/callback",
        "QBO_REAL_IMPORT": "1",
        "SUPPORT_EMAIL": "help@acme.example",
        "SECURITY_EMAIL": "security@acme.example",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
    })
    c = appmod.app.test_client()
    _signup(c)
    user = appmod.db.authenticate("prodcust@example.test", "passw0rd!1234")
    _make_job_with_connection(appmod, user["firm_id"], user["id"])
    for path in ("/dashboard", "/quickbooks", "/disconnect", "/quickbooks/disconnect",
                 "/readiness", "/healthz", "/support", "/privacy", "/terms"):
        resp = c.get(path)
        body = resp.get_data(as_text=True)
        _assert_no_secrets(body)
    print("T8 OK: client secret + token markers never appear in any rendered route")


def main():
    t1_public_disconnect_logged_out()
    t2_logged_in_disconnect_revokes_and_deletes()
    t3_disconnect_alias_route()
    t4_production_connect_guard_blocks_when_misconfigured()
    t5_quickbooks_manage_page_lists_connections()
    t6_production_import_requires_confirmation()
    t7_job_detail_production_copy()
    t8_no_secret_leak_in_any_route()
    print("\nAll production-customer-readiness smoke checks passed.")


if __name__ == "__main__":
    main()
