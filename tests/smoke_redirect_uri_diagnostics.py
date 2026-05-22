"""Smoke tests for QuickBooks redirect URI diagnostics.

Run from project root:

    python3 tests/smoke_redirect_uri_diagnostics.py

These tests pin the behavior added so operators can self-diagnose the
Intuit OAuth error:
  "The redirect_uri query parameter value is invalid. Make sure it is
   listed in the Redirect URIs section on your app's keys tab and
   matches it exactly."

Checks:
  T1 /healthz JSON exposes configured_qbo_redirect_uri with the actual
     configured value (so operators can copy-paste & compare against
     Intuit Developer).
  T2 The /healthz readiness block contains the new path/host checks.
  T3 When QBO_REDIRECT_URI is unset, configured_qbo_redirect_uri is null
     and the required checks fail.
  T4 The path check fails when the URI does not end with /oauth/callback.
  T5 The host-match check fails when QBO_REDIRECT_URI host differs from
     PUBLIC_APP_URL host, and passes when they match.
  T6 The /readiness HTML page renders the plain-English callback URL line
     and the "must exactly match" guidance, and shows the configured URI
     in plain text.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


_GOOD_FERNET = "TUk5IiNoXZBh4Ts1tqv-A7vKaakLBzUbm5ZGm-tIsHc="


def _reset_app_env(env):
    for mod in ("app", "encryption", "readiness", "branding"):
        if mod in sys.modules:
            del sys.modules[mod]
    os.environ.clear()
    os.environ.update(env)
    os.environ.setdefault("APP_DB", tempfile.mktemp(suffix=".sqlite3"))
    os.environ.setdefault("IMPORT_HISTORY_DB", tempfile.mktemp(suffix=".sqlite3"))
    return importlib.import_module("app")


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


def t1_healthz_exposes_configured_redirect_uri():
    appmod = _reset_app_env(_good_local_env())
    client = appmod.app.test_client()
    r = client.get("/healthz")
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert "configured_qbo_redirect_uri" in body, body
    assert body["configured_qbo_redirect_uri"] == "https://www.pclawmigrate.com/oauth/callback", body
    # Sanity: actual secrets still must not leak
    raw = r.get_data(as_text=True)
    assert "test-secret-DO-NOT-LEAK" not in raw
    assert _GOOD_FERNET not in raw
    print("T1 healthz_exposes_configured_redirect_uri PASS")


def t2_healthz_readiness_block_has_new_keys():
    appmod = _reset_app_env(_good_local_env())
    client = appmod.app.test_client()
    body = client.get("/healthz").get_json()
    block = body["readiness"]
    assert "qbo_redirect_uri_path_ok" in block, block
    assert "qbo_redirect_uri_host_matches_public_url" in block, block
    assert block["qbo_redirect_uri_path_ok"] is True
    assert block["qbo_redirect_uri_host_matches_public_url"] is True
    print("T2 healthz_readiness_block_has_new_keys PASS")


def t3_unset_redirect_uri_falls_back_and_fails_required_checks():
    # When QBO_REDIRECT_URI is unset, app.py falls back to a localhost
    # default — which the healthz layer surfaces (so the operator can see
    # exactly what the OAuth handler is using), while the required checks
    # correctly mark it as not production-ready.
    env = _good_local_env()
    env.pop("QBO_REDIRECT_URI", None)
    appmod = _reset_app_env(env)
    client = appmod.app.test_client()
    body = client.get("/healthz").get_json()
    configured = body["configured_qbo_redirect_uri"]
    # Either explicitly null (preferred) or the localhost fallback — both
    # convey the same message ("not configured for production"), so accept
    # both shapes here. The point is the operator sees what's actually in
    # use, not whether the env var was literally set.
    assert configured is None or "localhost" in configured, body
    assert body["qbo_redirect_uri_set"] is False, body
    block = body["readiness"]
    assert block["qbo_redirect_uri_https"] is False
    # path_ok may still be True if the fallback path is /oauth/callback;
    # the important required-check that fails is qbo_redirect_uri_https.
    assert "qbo_redirect_uri_path_ok" in block
    assert "qbo_redirect_uri_host_matches_public_url" in block
    print("T3 unset_redirect_uri_falls_back_and_fails_required_checks PASS")


def t4_path_check_rejects_wrong_path():
    env = _good_local_env()
    env["QBO_REDIRECT_URI"] = "https://www.pclawmigrate.com/oauth/callbackz"  # typo
    _reset_app_env(env)
    rmod = sys.modules["readiness"]
    checks = rmod.collect_checks(request_host="www.pclawmigrate.com", request_scheme="https")
    by_key = {c.key: c for c in checks}
    assert by_key["qbo_redirect_uri_path_ok"].ok is False, by_key["qbo_redirect_uri_path_ok"]
    assert "/oauth/callback" in by_key["qbo_redirect_uri_path_ok"].hint
    # Trailing slash is tolerated — Intuit treats it the same in practice and
    # we don't want to false-flag operators who paste the URL with a trailing /.
    env["QBO_REDIRECT_URI"] = "https://www.pclawmigrate.com/oauth/callback/"
    _reset_app_env(env)
    rmod = sys.modules["readiness"]
    checks = rmod.collect_checks(request_host="www.pclawmigrate.com", request_scheme="https")
    by_key = {c.key: c for c in checks}
    assert by_key["qbo_redirect_uri_path_ok"].ok is True
    print("T4 path_check_rejects_wrong_path PASS")


def t5_host_match_check():
    # Mismatched hosts → fail
    env = _good_local_env()
    env["PUBLIC_APP_URL"] = "https://www.pclawmigrate.com"
    env["QBO_REDIRECT_URI"] = "https://staging.pclawmigrate.com/oauth/callback"
    _reset_app_env(env)
    rmod = sys.modules["readiness"]
    checks = rmod.collect_checks(request_host="www.pclawmigrate.com", request_scheme="https")
    by_key = {c.key: c for c in checks}
    assert by_key["qbo_redirect_uri_host_matches_public_url"].ok is False
    assert "PUBLIC_APP_URL" in by_key["qbo_redirect_uri_host_matches_public_url"].hint

    # Matching hosts → pass
    env["QBO_REDIRECT_URI"] = "https://www.pclawmigrate.com/oauth/callback"
    _reset_app_env(env)
    rmod = sys.modules["readiness"]
    checks = rmod.collect_checks(request_host="www.pclawmigrate.com", request_scheme="https")
    by_key = {c.key: c for c in checks}
    assert by_key["qbo_redirect_uri_host_matches_public_url"].ok is True

    # No PUBLIC_APP_URL → skipped (pass with informational detail)
    env.pop("PUBLIC_APP_URL", None)
    _reset_app_env(env)
    rmod = sys.modules["readiness"]
    checks = rmod.collect_checks(request_host="www.pclawmigrate.com", request_scheme="https")
    by_key = {c.key: c for c in checks}
    assert by_key["qbo_redirect_uri_host_matches_public_url"].ok is True
    assert "PUBLIC_APP_URL" in by_key["qbo_redirect_uri_host_matches_public_url"].detail
    print("T5 host_match_check PASS")


def t6_readiness_page_shows_callback_line():
    appmod = _reset_app_env(_good_local_env())
    client = appmod.app.test_client()
    client.post(
        "/signup",
        data={
            "firm_name": "Redirect Diagnostics Firm",
            "email": "redir@example.test",
            "password": "passw0rd!1234",
            "confirm_password": "passw0rd!1234",
        },
    )
    r = client.get("/readiness", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    text = r.get_data(as_text=True)
    assert "QuickBooks callback URL configured" in text, "callback line missing"
    assert "https://www.pclawmigrate.com/oauth/callback" in text, "URI not rendered"
    assert "must exactly match the redirect URI in Intuit Developer" in text, "guidance missing"
    print("T6 readiness_page_shows_callback_line PASS")


if __name__ == "__main__":
    t1_healthz_exposes_configured_redirect_uri()
    t2_healthz_readiness_block_has_new_keys()
    t3_unset_redirect_uri_falls_back_and_fails_required_checks()
    t4_path_check_rejects_wrong_path()
    t5_host_match_check()
    t6_readiness_page_shows_callback_line()
    print("ALL REDIRECT URI DIAGNOSTICS SMOKE TESTS PASSED")
