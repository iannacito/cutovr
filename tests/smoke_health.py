"""Smoke tests for /healthz and production env validation.

Run from project root:

    python3 tests/smoke_health.py

Checks:
  T1 /healthz returns 200 with status=ok and the expected boolean fields,
     and never includes the actual secret values.
  T2 With APP_ENV=production and missing critical env vars, importing the
     app raises RuntimeError listing what's wrong (and not the values).
  T3 With APP_ENV=production and a malformed ENCRYPTION_KEY, the same
     validator complains specifically about the Fernet format.
"""

import importlib
import os
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reset_app_env(env):
    """Re-import app.py with a fresh env so module-level code re-runs."""
    for mod in ("app", "encryption"):
        if mod in sys.modules:
            del sys.modules[mod]
    with mock.patch.dict(os.environ, env, clear=True):
        # Always need a writable DB path
        os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
        os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
        return importlib.import_module("app")


def t1_healthz_ok():
    env = {
        "APP_ENV": "local",
        "CSRF_DISABLE": "1",
        "SECRET_KEY": "x" * 64,
        "QBO_CLIENT_ID": "test-id",
        "QBO_CLIENT_SECRET": "test-secret",
        "QBO_REDIRECT_URI": "https://example.com/oauth/callback",
    }
    appmod = _reset_app_env(env)
    client = appmod.app.test_client()
    r = client.get("/healthz")
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert body["status"] == "ok", body
    for k in (
        "app_env", "qbo_environment", "qbo_real_import",
        "secret_key_set", "encryption_key_set",
        "qbo_client_id_set", "qbo_redirect_uri_set",
    ):
        assert k in body, f"missing key {k} in {body}"
    # Never expose actual secrets
    raw = r.get_data(as_text=True)
    assert "test-secret" not in raw
    assert "x" * 64 not in raw
    print("T1 healthz_ok PASS")


def t2_production_missing_vars_fails():
    # Provide SECRET_KEY so we get past the early bootstrap check and into
    # the consolidated validator, which is what we're testing here.
    env = {
        "APP_ENV": "production",
        "SECRET_KEY": "y" * 64,
        # Intentionally omit ENCRYPTION_KEY and QBO_*
    }
    try:
        _reset_app_env(env)
    except RuntimeError as e:
        msg = str(e)
        for needle in ("ENCRYPTION_KEY", "QBO_CLIENT_ID",
                       "QBO_CLIENT_SECRET", "QBO_REDIRECT_URI"):
            assert needle in msg, f"expected {needle} in error: {msg}"
        # Validator must not echo the SECRET_KEY value back
        assert "y" * 64 not in msg, "SECRET_KEY value must not appear in error"
        print("T2 production_missing_vars_fails PASS")
        return
    raise AssertionError("expected RuntimeError on missing prod env vars")


def t3_malformed_encryption_key_fails():
    env = {
        "APP_ENV": "production",
        "SECRET_KEY": "y" * 64,
        "ENCRYPTION_KEY": "not-a-valid-fernet-key",
        "QBO_CLIENT_ID": "id",
        "QBO_CLIENT_SECRET": "secret",
        "QBO_REDIRECT_URI": "https://example.com/oauth/callback",
    }
    try:
        _reset_app_env(env)
    except RuntimeError as e:
        msg = str(e)
        assert "ENCRYPTION_KEY" in msg and "Fernet" in msg, msg
        # Must not echo the bad value back (avoid leaking near-secrets in logs)
        assert "not-a-valid-fernet-key" not in msg, msg
        print("T3 malformed_encryption_key_fails PASS")
        return
    raise AssertionError("expected RuntimeError on malformed Fernet key")


if __name__ == "__main__":
    t1_healthz_ok()
    t2_production_missing_vars_fails()
    t3_malformed_encryption_key_fails()
    print("ALL HEALTH SMOKE TESTS PASSED")
