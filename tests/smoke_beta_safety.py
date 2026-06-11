"""Smoke test: beta-safety UX/error-handling for QBO connect.

Run from project root:

    python3 tests/smoke_beta_safety.py

Covers the changes in the beta-safety-qbo-sandbox-ux branch:

  T1 Sandbox banner appears on the dashboard when QBO_ENVIRONMENT=sandbox
     and is suppressed when QBO_ENVIRONMENT=production.

  T2 Sandbox banner + beginner-friendly Connect copy appears on the job
     detail page when QBO is configured + sandbox.

  T3 OAuth callback with `error=access_denied` flashes a clear,
     beginner-friendly message that mentions sandbox guidance, and does
     NOT include the generic "QuickBooks authorization failed: ..." prefix.

  T4 OAuth callback with no code/realm (Intuit's "Uh oh" silent redirect)
     flashes a friendly explanation, not "Invalid OAuth callback parameters".

  T5 OAuth error callback messages include the configured SUPPORT_EMAIL
     when it's a real address, and DO NOT leak the placeholder default.

  T6 Token-exchange failures from Intuit are caught and shown as a
     beginner-friendly message, with no raw exception body or secrets in
     the user-facing flash. Audit row records the OAuth callback error.

The QBO API and Intuit OAuth endpoints are fully mocked; nothing leaves
the machine.
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


def _fresh_app(env_overrides=None):
    """Reload the app with fresh DBs and the requested env overrides."""
    for var in (
        "APP_DB", "IMPORT_HISTORY_DB",
        "QBO_ENVIRONMENT", "QBO_CLIENT_ID", "QBO_CLIENT_SECRET",
        "QBO_REDIRECT_URI",
        "SUPPORT_EMAIL", "SECURITY_EMAIL", "PRIVACY_CONTACT_EMAIL",
    ):
        os.environ.pop(var, None)

    os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
    os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
    os.environ.setdefault("CSRF_DISABLE", "1")
    os.environ.setdefault("SECRET_KEY", "smoke-secret-beta-safety")
    for k, v in (env_overrides or {}).items():
        os.environ[k] = v

    for mod in ("branding", "app"):
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("app")


def _signup(c, firm="Beta Firm", email="beta@example.test", password="passw0rd!1234"):
    return c.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
    )


def _make_job(appmod, c, firm_id, user_id):
    """Create a minimal job row in the DB for OAuth-callback tests.

    The OAuth callback path requires `state` to resolve to a job that
    belongs to the logged-in firm. We don't need a real upload here.
    """
    job_id = "job_betasafety_1"
    appmod.jobs[job_id] = {
        "id": job_id, "firm_id": firm_id, "user_id": user_id,
        "company": "Test Co", "email": "x@x", "source_file": "x.csv",
        "encrypted_file": "x.csv.enc", "file_sha256": "deadbeef",
        "status": "File uploaded (encrypted)",
        "created_at": "2026-01-01T00:00:00",
        "summary": {}, "qbo_connected": False,
    }
    appmod.db.upsert_job(
        job_id=job_id, firm_id=firm_id, user_id=user_id,
        company="Test Co", source_file="x.csv",
        encrypted_file="x.csv.enc", file_sha256="deadbeef",
        status="File uploaded (encrypted)",
    )
    return job_id


def t1_dashboard_sandbox_banner():
    # Sandbox: banner present.
    appmod = _fresh_app({
        "QBO_ENVIRONMENT": "sandbox",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": "secret",
        "QBO_REDIRECT_URI": "https://example.test/oauth/callback",
    })
    c = appmod.app.test_client()
    _signup(c)
    body = c.get("/dashboard").get_data(as_text=True)
    assert "Sandbox mode" in body, "expected sandbox banner on dashboard"
    assert "test company" in body.lower(), body[:400]

    # Production: banner suppressed.
    appmod2 = _fresh_app({
        "QBO_ENVIRONMENT": "production",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": "secret",
        "QBO_REDIRECT_URI": "https://example.test/oauth/callback",
    })
    c2 = appmod2.app.test_client()
    _signup(c2)
    body2 = c2.get("/dashboard").get_data(as_text=True)
    assert "Sandbox mode" not in body2, \
        "sandbox banner should NOT render in production"
    print("T1 OK: dashboard sandbox banner toggles on QBO_ENVIRONMENT")


def t2_job_detail_sandbox_copy():
    appmod = _fresh_app({
        "QBO_ENVIRONMENT": "sandbox",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": "secret",
        "QBO_REDIRECT_URI": "https://example.test/oauth/callback",
    })
    c = appmod.app.test_client()
    _signup(c)
    user = appmod.db.authenticate("beta@example.test", "passw0rd!1234")
    job_id = _make_job(appmod, c, user["firm_id"], user["id"])
    body = c.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "Sandbox Testing Mode" in body, "expected sandbox banner on job detail"
    assert "sandbox QuickBooks company" in body, \
        "expected explicit sandbox-company guidance"
    assert "production approval" in body.lower(), \
        "expected production-credentials explanation"
    print("T2 OK: job detail shows sandbox banner + beginner connect copy")


def t3_oauth_access_denied_friendly():
    appmod = _fresh_app({
        "QBO_ENVIRONMENT": "sandbox",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": "secret",
        "QBO_REDIRECT_URI": "https://example.test/oauth/callback",
    })
    c = appmod.app.test_client()
    _signup(c)
    user = appmod.db.authenticate("beta@example.test", "passw0rd!1234")
    job_id = _make_job(appmod, c, user["firm_id"], user["id"])
    resp = c.get(
        f"/oauth/callback?error=access_denied&state={job_id}",
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "cancelled" in body.lower(), body[:400]
    assert "Sandbox Testing Mode" in body, body[:400]
    assert "QuickBooks authorization failed:" not in body, \
        "old generic-prefix message should be gone"
    print("T3 OK: access_denied callback gives friendly cancel message")


def t4_oauth_missing_params_friendly():
    appmod = _fresh_app({
        "QBO_ENVIRONMENT": "sandbox",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": "secret",
        "QBO_REDIRECT_URI": "https://example.test/oauth/callback",
    })
    c = appmod.app.test_client()
    _signup(c)
    resp = c.get("/oauth/callback", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "Invalid OAuth callback parameters" not in body, \
        "old terse error should be replaced"
    assert "Uh oh" in body, body[:400]
    assert "Sandbox Testing Mode" in body, body[:400]
    print("T4 OK: missing-params callback explains Intuit's 'Uh oh' page")


def t5_support_email_inclusion():
    # Real support email: appears in the flash text.
    appmod = _fresh_app({
        "QBO_ENVIRONMENT": "sandbox",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": "secret",
        "QBO_REDIRECT_URI": "https://example.test/oauth/callback",
        "SUPPORT_EMAIL": "help@acme.example",
    })
    c = appmod.app.test_client()
    _signup(c)
    resp = c.get("/oauth/callback?error=invalid_scope", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "help@acme.example" in body, "support email should appear in error flash"

    # Placeholder default: must NOT leak.
    appmod2 = _fresh_app({
        "QBO_ENVIRONMENT": "sandbox",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": "secret",
        "QBO_REDIRECT_URI": "https://example.test/oauth/callback",
    })
    c2 = appmod2.app.test_client()
    _signup(c2)
    resp2 = c2.get("/oauth/callback?error=invalid_scope", follow_redirects=True)
    body2 = resp2.get_data(as_text=True)
    # Header has a "Support" nav link, so check for the placeholder address
    # specifically rather than the word "support" alone.
    assert "your-domain.example" not in body2, \
        "placeholder support email must not leak into error flash"
    print("T5 OK: error flashes include real support email, never placeholder")


def t6_token_exchange_failure_friendly():
    appmod = _fresh_app({
        "QBO_ENVIRONMENT": "sandbox",
        "QBO_CLIENT_ID": "ABxxxx", "QBO_CLIENT_SECRET": "secret",
        "QBO_REDIRECT_URI": "https://example.test/oauth/callback",
        "SUPPORT_EMAIL": "help@acme.example",
    })
    c = appmod.app.test_client()
    _signup(c)
    user = appmod.db.authenticate("beta@example.test", "passw0rd!1234")
    job_id = _make_job(appmod, c, user["firm_id"], user["id"])

    # Simulate Intuit rejecting the code (e.g., wrong company / non-sandbox
    # company picked against sandbox creds). The exception body could
    # contain client_id / sensitive substrings; the user must NOT see them.
    secret_marker = "client_secret=zzz-do-not-leak"

    def _boom(self, code):
        raise RuntimeError(f"400 Bad Request: invalid_grant {secret_marker}")

    with mock.patch.object(appmod.QBOAuthHandler, "get_bearer_token", new=_boom):
        resp = c.get(
            f"/oauth/callback?code=ABC&state={job_id}&realmId=REALM-1",
            follow_redirects=True,
        )
    body = resp.get_data(as_text=True)
    assert secret_marker not in body, \
        "raw exception body must not be flashed to user"
    assert "rejected this app" in body and "credentials" in body, body[:600]
    assert "Sandbox Testing Mode" in body, body[:400]
    assert "help@acme.example" in body, "support email should appear"

    # Audit row should record the failure (truncated detail OK).
    rows = appmod.db.recent_audit_for_firm(user["firm_id"], limit=20)
    actions = [r["action"] for r in rows]
    assert "oauth_token_exchange_failed" in actions, actions
    print("T6 OK: token exchange failures hidden from user, audited for ops")


def main():
    t1_dashboard_sandbox_banner()
    t2_job_detail_sandbox_copy()
    t3_oauth_access_denied_friendly()
    t4_oauth_missing_params_friendly()
    t5_support_email_inclusion()
    t6_token_exchange_failure_friendly()
    print("\nAll beta-safety smoke checks passed.")


if __name__ == "__main__":
    main()
