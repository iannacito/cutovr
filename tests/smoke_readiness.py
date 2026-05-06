"""Smoke tests for the go-live readiness layer.

Run from the project root:

    python3 tests/smoke_readiness.py

Checks:
  T1 readiness.collect_checks returns the expected keys with no secret values
     in the messages.
  T2 With a fully-configured production-style env, every required check
     passes and overall_status reports ready.
  T3 With placeholders / missing values, required checks fail and the
     messages do not echo placeholder/secret content back.
  T4 GET /admin/readiness requires login (redirects to /login).
  T5 A logged-in user gets a 200 with the checklist rendered, and the
     response never contains the actual SECRET_KEY/ENCRYPTION_KEY/
     QBO_CLIENT_SECRET values.
  T6 /healthz now includes the per-check booleans plus the
     ready_for_production summary, still without leaking secrets.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet  # noqa: E402


def _reset_app(env):
    """Re-import app.py with the given env so module-level code re-runs."""
    for mod in ("app", "encryption", "branding", "readiness"):
        if mod in sys.modules:
            del sys.modules[mod]
    base_env = {
        "APP_DB": tempfile.mktemp(suffix=".sqlite3"),
        "IMPORT_HISTORY_DB": tempfile.mktemp(suffix=".sqlite3"),
    }
    base_env.update(env)
    # CSRF_DISABLE is only safe to use outside production. The app refuses to
    # boot with both APP_ENV=production and CSRF_DISABLE=1 set.
    if base_env.get("APP_ENV", "local").lower() not in ("production", "prod"):
        base_env.setdefault("CSRF_DISABLE", "1")
    # Replace os.environ wholesale so module-level reads see exactly this env.
    os.environ.clear()
    os.environ.update(base_env)
    return importlib.import_module("app")


def _full_prod_env():
    return {
        "APP_ENV": "production",
        "SECRET_KEY": "S" * 64,
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "QBO_CLIENT_ID": "real-client-id",
        "QBO_CLIENT_SECRET": "real-client-secret-value",
        "QBO_REDIRECT_URI": "https://www.pclawmigrate.com/oauth/callback",
        "QBO_ENVIRONMENT": "production",
        "QBO_REAL_IMPORT": "1",
        "SUPPORT_EMAIL": "support@pclawmigrate.com",
        "SECURITY_EMAIL": "security@pclawmigrate.com",
        "PRIVACY_CONTACT_EMAIL": "privacy@pclawmigrate.com",
        "PUBLIC_APP_URL": "https://www.pclawmigrate.com",
    }


def t1_collect_checks_shape():
    appmod = _reset_app({"APP_ENV": "local", "SECRET_KEY": "x" * 64})
    checks = appmod.readiness.collect_checks(request_host="localhost:5000")
    keys = {c["key"] for c in checks}
    expected = {
        "app_env_production", "secret_key", "encryption_key",
        "qbo_client_id", "qbo_client_secret", "qbo_redirect_uri",
        "qbo_real_import", "support_email", "security_email",
        "privacy_contact_email", "custom_domain", "health_endpoint",
    }
    missing = expected - keys
    assert not missing, f"missing readiness keys: {missing}"
    for c in checks:
        assert "x" * 64 not in c["message"], "secret value leaked into message"
    print("T1 collect_checks_shape PASS")


def t2_full_prod_passes_required():
    env = _full_prod_env()
    appmod = _reset_app(env)
    checks = appmod.readiness.collect_checks(request_host="www.pclawmigrate.com")
    summary = appmod.readiness.overall_status(checks)
    failing_required = [c for c in checks if c["severity"] == "required" and not c["ok"]]
    assert not failing_required, f"required checks should all pass, but failed: {failing_required}"
    assert summary["all_required_ok"] is True
    print("T2 full_prod_passes_required PASS")


def t3_placeholder_fails_with_clean_message():
    env = {
        "APP_ENV": "local",
        "SECRET_KEY": "short",
        "ENCRYPTION_KEY": "not-a-fernet-key",
        # Leave QBO_CLIENT_ID / SECRET unset → placeholder
        "QBO_REDIRECT_URI": "http://localhost:5000/oauth/callback",
    }
    appmod = _reset_app(env)
    checks = appmod.readiness.collect_checks(request_host="localhost:5000")
    by_key = {c["key"]: c for c in checks}
    assert by_key["secret_key"]["ok"] is False
    assert by_key["encryption_key"]["ok"] is False
    assert by_key["qbo_client_id"]["ok"] is False
    assert by_key["qbo_client_secret"]["ok"] is False
    # Custom domain on localhost should not be considered configured.
    assert by_key["custom_domain"]["ok"] is False
    # Make sure we don't echo bad values back in messages.
    for c in checks:
        assert "not-a-fernet-key" not in c["message"], (
            f"encryption-key value leaked into message: {c}"
        )
    print("T3 placeholder_fails_with_clean_message PASS")


def _signup_and_login(client):
    client.post(
        "/signup",
        data={
            "firm_name": "Smoke Firm",
            "email": "ready@example.test",
            "password": "passw0rd!",
            "confirm_password": "passw0rd!",
        },
    )


def t4_admin_readiness_requires_login():
    appmod = _reset_app({"APP_ENV": "local", "SECRET_KEY": "x" * 64})
    client = appmod.app.test_client()
    r = client.get("/admin/readiness", follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    assert "/login" in r.headers.get("Location", ""), r.headers
    print("T4 admin_readiness_requires_login PASS")


def t5_admin_readiness_renders_for_logged_in_user():
    env = _full_prod_env()
    # Run as APP_ENV=local so the production validator in app.py doesn't run
    # at import time; the readiness page itself reflects the *current* env
    # values without needing IS_PRODUCTION.
    env["APP_ENV"] = "local"
    appmod = _reset_app(env)
    client = appmod.app.test_client()
    _signup_and_login(client)
    r = client.get("/admin/readiness")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "Go-live" in body
    assert "QBO_CLIENT_ID configured" in body
    assert "SECRET_KEY configured" in body
    # Must NEVER expose actual secret values.
    assert "S" * 64 not in body, "SECRET_KEY value leaked into rendered page"
    assert "real-client-secret-value" not in body, "QBO_CLIENT_SECRET leaked"
    assert env["ENCRYPTION_KEY"] not in body, "ENCRYPTION_KEY value leaked"
    print("T5 admin_readiness_renders_for_logged_in_user PASS")


def t6_healthz_extended():
    env = _full_prod_env()
    env["APP_ENV"] = "local"  # avoid prod validator at import time
    appmod = _reset_app(env)
    client = appmod.app.test_client()
    r = client.get("/healthz")
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert body["status"] == "ok"
    assert "ready_for_production" in body
    assert "checks" in body and isinstance(body["checks"], dict)
    for k in (
        "app_env_production", "secret_key", "encryption_key",
        "qbo_client_id", "qbo_client_secret", "qbo_redirect_uri",
        "support_email", "security_email", "health_endpoint",
    ):
        assert k in body["checks"], f"missing readiness key {k} in /healthz checks"
    raw = r.get_data(as_text=True)
    assert "S" * 64 not in raw
    assert "real-client-secret-value" not in raw
    assert env["ENCRYPTION_KEY"] not in raw
    # Back-compat: existing smoke_health.py keys still present.
    for k in (
        "app_env", "qbo_environment", "qbo_real_import",
        "secret_key_set", "encryption_key_set",
        "qbo_client_id_set", "qbo_redirect_uri_set",
    ):
        assert k in body, f"missing back-compat key {k}"
    print("T6 healthz_extended PASS")


if __name__ == "__main__":
    t1_collect_checks_shape()
    t2_full_prod_passes_required()
    t3_placeholder_fails_with_clean_message()
    t4_admin_readiness_requires_login()
    t5_admin_readiness_renders_for_logged_in_user()
    t6_healthz_extended()
    print("\nALL READINESS SMOKE TESTS PASSED")
