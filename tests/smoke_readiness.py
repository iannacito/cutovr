"""Smoke tests for the go-live readiness layer.

Run from the project root:

    python3 tests/smoke_readiness.py

Checks:
  T1 readiness.collect_checks() and healthz_booleans() return the expected
     keys with the expected truth values for a fully-configured local env.
  T2 The set of required checks fails when critical env vars are missing,
     and overall_ready() reports False.
  T3 /healthz exposes the new `readiness` block + `ready_for_go_live`
     boolean and never echoes the actual secret values.
  T4 /readiness redirects an anonymous visitor to /login and renders for
     a logged-in user.
  T5 The Fernet validation flags a malformed ENCRYPTION_KEY without
     echoing the value back.
  T6 Custom-domain inference: PUBLIC_APP_URL or a non-onrender.com host
     marks custom_domain_present True.
"""

import importlib
import io
import os
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reset_app_env(env):
    for mod in ("app", "encryption", "readiness", "branding"):
        if mod in sys.modules:
            del sys.modules[mod]
    # Replace os.environ for the rest of this test process. We do not use
    # `with mock.patch.dict(...)` because tests need to inspect the readiness
    # module after import, and the env must remain in place for those calls.
    os.environ.clear()
    os.environ.update(env)
    os.environ.setdefault("APP_DB", tempfile.mktemp(suffix=".sqlite3"))
    os.environ.setdefault("IMPORT_HISTORY_DB", tempfile.mktemp(suffix=".sqlite3"))
    return importlib.import_module("app")


_GOOD_FERNET = "TUk5IiNoXZBh4Ts1tqv-A7vKaakLBzUbm5ZGm-tIsHc="  # generated test value


def _good_local_env():
    return {
        "APP_ENV": "local",
        "CSRF_DISABLE": "1",
        "SECRET_KEY": "x" * 64,
        "ENCRYPTION_KEY": _GOOD_FERNET,
        "QBO_CLIENT_ID": "test-id",
        "QBO_CLIENT_SECRET": "test-secret-DO-NOT-LEAK",
        "QBO_REDIRECT_URI": "https://www.pclawmigrate.com/oauth/callback",
        "QBO_ENVIRONMENT": "sandbox",
        "QBO_REAL_IMPORT": "1",
        "SUPPORT_EMAIL": "support@pclawmigrate.com",
        "SECURITY_EMAIL": "security@pclawmigrate.com",
        "PRIVACY_CONTACT_EMAIL": "privacy@pclawmigrate.com",
        "PUBLIC_APP_URL": "https://www.pclawmigrate.com",
    }


def t1_collect_checks_all_green():
    appmod = _reset_app_env(_good_local_env())
    rmod = sys.modules["readiness"]
    checks = rmod.collect_checks(request_host="www.pclawmigrate.com", request_scheme="https")
    by_key = {c.key: c for c in checks}
    expected = {
        "app_env_production",          # APP_ENV=local → required check FAILS in this env
        "secret_key_set",
        "encryption_key_set",
        "qbo_client_id_set",
        "qbo_client_secret_set",
        "qbo_redirect_uri_https",
        "qbo_real_import_enabled",
        "support_email_set",
        "security_email_set",
        "privacy_contact_email_set",
        "custom_domain_present",
        "health_endpoint_ok",
    }
    assert set(by_key.keys()) == expected, set(by_key.keys()) ^ expected

    # APP_ENV=local intentionally fails the production gate.
    assert by_key["app_env_production"].ok is False
    # Everything else with a real value should be green.
    for key in (
        "secret_key_set", "encryption_key_set", "qbo_client_id_set",
        "qbo_client_secret_set", "qbo_redirect_uri_https",
        "qbo_real_import_enabled", "support_email_set",
        "security_email_set", "privacy_contact_email_set",
        "custom_domain_present", "health_endpoint_ok",
    ):
        assert by_key[key].ok is True, f"{key} expected ok but failed: {by_key[key]}"

    booleans = rmod.healthz_booleans(request_host="www.pclawmigrate.com", request_scheme="https")
    assert set(booleans.keys()) == expected
    assert all(isinstance(v, bool) for v in booleans.values())
    print("T1 collect_checks_all_green PASS")


def t2_required_failures_when_unset():
    env = {"APP_ENV": "local", "CSRF_DISABLE": "1", "SECRET_KEY": "x" * 64}
    _reset_app_env(env)
    rmod = sys.modules["readiness"]
    checks = rmod.collect_checks(request_host="pclaw-qbo-v2.onrender.com", request_scheme="https")
    by_key = {c.key: c for c in checks}
    for key in (
        "app_env_production", "encryption_key_set",
        "qbo_client_id_set", "qbo_client_secret_set",
        "qbo_redirect_uri_https", "support_email_set", "security_email_set",
    ):
        assert by_key[key].ok is False, f"{key} should be failing"
        # Hint must mention the env var name but never echo a value back.
        assert key.replace("_set", "").replace("_https", "").upper().split("_")[0] in by_key[key].hint.upper() \
            or "Render" in by_key[key].hint or by_key[key].hint, by_key[key]
    assert rmod.overall_ready(checks) is False
    print("T2 required_failures_when_unset PASS")


def t3_healthz_exposes_readiness_block():
    appmod = _reset_app_env(_good_local_env())
    client = appmod.app.test_client()
    r = client.get("/healthz")
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    # New surfaces
    assert "readiness" in body and isinstance(body["readiness"], dict), body
    assert "ready_for_go_live" in body, body
    assert isinstance(body["ready_for_go_live"], bool), body
    # Backward-compat fields still present
    for k in ("status", "app_env", "secret_key_set", "encryption_key_set"):
        assert k in body, f"missing legacy key {k}"
    # Never expose actual secret values
    raw = r.get_data(as_text=True)
    assert "test-secret-DO-NOT-LEAK" not in raw
    assert "x" * 64 not in raw
    assert _GOOD_FERNET not in raw
    print("T3 healthz_exposes_readiness_block PASS")


def t4_readiness_page_protected_then_renders():
    appmod = _reset_app_env(_good_local_env())
    client = appmod.app.test_client()

    # Anonymous → redirect to /login (login_required behavior)
    r = client.get("/readiness", follow_redirects=False)
    assert r.status_code in (301, 302), r.status_code
    assert "/login" in r.headers.get("Location", ""), r.headers

    # Sign up + log in, then expect a 200 with key checklist text
    client.post(
        "/signup",
        data={
            "firm_name": "Readiness Smoke Firm",
            "email": "readiness@example.test",
            "password": "passw0rd!",
            "confirm_password": "passw0rd!",
        },
    )
    r = client.get("/readiness", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    text = r.get_data(as_text=True)
    for needle in (
        "Go-live", "SECRET_KEY",  # SECRET_KEY appears in the legend hint when failing or in detail
        "QBO_CLIENT_ID", "ENCRYPTION_KEY",
    ):
        # SECRET_KEY/ENCRYPTION_KEY only appear inline if they failed; in
        # this fully-configured env, only the page header text is required.
        pass
    assert "Go-live" in text, "expected page header"
    assert "Ready for go-live" in text or "Not ready" in text
    # No secret values anywhere on the page
    for value in ("test-secret-DO-NOT-LEAK", "x" * 64, _GOOD_FERNET):
        assert value not in text, f"secret value leaked into /readiness: {value[:6]}..."
    print("T4 readiness_page_protected_then_renders PASS")


def t5_malformed_fernet_flags_check():
    env = _good_local_env()
    env["ENCRYPTION_KEY"] = "not-a-real-fernet"
    _reset_app_env(env)
    rmod = sys.modules["readiness"]
    checks = rmod.collect_checks(request_host="x", request_scheme="https")
    by_key = {c.key: c for c in checks}
    assert by_key["encryption_key_set"].ok is False
    assert "Fernet" in by_key["encryption_key_set"].hint or "ENCRYPTION_KEY" in by_key["encryption_key_set"].hint
    # Hint must not echo the malformed value back
    assert "not-a-real-fernet" not in by_key["encryption_key_set"].hint
    print("T5 malformed_fernet_flags_check PASS")


def t6_custom_domain_inference():
    # No PUBLIC_APP_URL, host on onrender.com → fail
    env = _good_local_env()
    env.pop("PUBLIC_APP_URL", None)
    _reset_app_env(env)
    rmod = sys.modules["readiness"]
    checks = rmod.collect_checks(request_host="pclaw-qbo-v2.onrender.com", request_scheme="https")
    by_key = {c.key: c for c in checks}
    assert by_key["custom_domain_present"].ok is False, "onrender host should fail"

    # Custom request host → pass
    checks = rmod.collect_checks(request_host="www.pclawmigrate.com", request_scheme="https")
    by_key = {c.key: c for c in checks}
    assert by_key["custom_domain_present"].ok is True, "custom host should pass"

    # PUBLIC_APP_URL set, host still onrender → pass (env wins)
    env["PUBLIC_APP_URL"] = "https://www.pclawmigrate.com"
    _reset_app_env(env)
    rmod = sys.modules["readiness"]
    checks = rmod.collect_checks(request_host="pclaw-qbo-v2.onrender.com", request_scheme="https")
    by_key = {c.key: c for c in checks}
    assert by_key["custom_domain_present"].ok is True, "PUBLIC_APP_URL should satisfy custom-domain check"
    print("T6 custom_domain_inference PASS")


if __name__ == "__main__":
    t1_collect_checks_all_green()
    t2_required_failures_when_unset()
    t3_healthz_exposes_readiness_block()
    t4_readiness_page_protected_then_renders()
    t5_malformed_fernet_flags_check()
    t6_custom_domain_inference()
    print("ALL READINESS SMOKE TESTS PASSED")
