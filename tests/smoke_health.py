"""Smoke tests for /healthz and production env validation.

Run from project root:

    python3 tests/smoke_health.py

Checks:
  T1 /healthz returns 200 with ONLY {status: 'ok'} — no config, readiness
     flags, redirect URI, or environment names are leaked publicly.
  T2 /healthz/detailed is gated: anonymous requests get 404.
  T3 /healthz/detailed unlocks with an operator login session and returns
     the full diagnostic payload (booleans only, never raw secrets).
  T4 /healthz/detailed unlocks with the HEALTHZ_TOKEN secret passed via
     query string or X-Healthz-Token header.
  T5 With APP_ENV=production and missing critical env vars, importing the
     app raises RuntimeError listing what's wrong (and not the values).
  T6 With APP_ENV=production and a malformed ENCRYPTION_KEY, the same
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
    """Re-import app.py with a fresh env so module-level code re-runs.

    We rewrite os.environ in-place (not via a context manager) so the
    environment persists for subsequent request handlers — the operator
    + token gates on /healthz/detailed read os.environ at request time.
    """
    for mod in ("app", "encryption", "operator_panel", "readiness"):
        if mod in sys.modules:
            del sys.modules[mod]
    os.environ.clear()
    for k, v in env.items():
        os.environ[k] = v
    os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
    os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
    return importlib.import_module("app")


def t1_healthz_public_is_minimal():
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
    # Must be ONLY status: ok — nothing else.
    assert body == {"status": "ok"}, body
    raw = r.get_data(as_text=True)
    # Defensive: ensure none of the formerly leaked fields appear.
    for forbidden in (
        "qbo_environment",
        "qbo_redirect_uri",
        "configured_qbo_redirect_uri",
        "readiness",
        "ready_for_go_live",
        "encryption_key_set",
        "example.com",
        "test-secret",
        "x" * 64,
    ):
        assert forbidden not in raw, (
            f"public /healthz must not expose {forbidden!r}: {raw}"
        )
    print("T1 healthz_public_is_minimal PASS")


def t2_healthz_detailed_blocks_anon():
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
    r = client.get("/healthz/detailed")
    assert r.status_code == 404, r.status_code
    print("T2 healthz_detailed_blocks_anon PASS")


def t3_healthz_detailed_operator_unlocks():
    env = {
        "APP_ENV": "local",
        "CSRF_DISABLE": "1",
        "SECRET_KEY": "x" * 64,
        "OPERATOR_EMAILS": "op@example.com",
        "QBO_CLIENT_ID": "test-id",
        "QBO_CLIENT_SECRET": "test-secret",
        "QBO_REDIRECT_URI": "https://example.com/oauth/callback",
    }
    appmod = _reset_app_env(env)
    client = appmod.app.test_client()
    client.post(
        "/signup",
        data={
            "firm_name": "Op Firm",
            "email": "op@example.com",
            "password": "passw0rd!1234",
            "confirm_password": "passw0rd!1234",
        },
    )
    r = client.get("/healthz/detailed")
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    for k in (
        "status", "app_env", "qbo_environment", "qbo_real_import",
        "secret_key_set", "encryption_key_set",
        "qbo_client_id_set", "qbo_redirect_uri_set",
        "configured_qbo_redirect_uri",
        "readiness", "ready_for_go_live",
    ):
        assert k in body, f"missing key {k} in {body}"
    raw = r.get_data(as_text=True)
    # Never echo raw secrets, even for operators.
    assert "test-secret" not in raw
    assert "x" * 64 not in raw
    print("T3 healthz_detailed_operator_unlocks PASS")


def t4_healthz_detailed_token_unlocks():
    env = {
        "APP_ENV": "local",
        "CSRF_DISABLE": "1",
        "SECRET_KEY": "x" * 64,
        "HEALTHZ_TOKEN": "monitor-only-secret",
        "QBO_CLIENT_ID": "test-id",
        "QBO_CLIENT_SECRET": "test-secret",
        "QBO_REDIRECT_URI": "https://example.com/oauth/callback",
    }
    appmod = _reset_app_env(env)
    client = appmod.app.test_client()
    # Wrong token -> 404
    r = client.get("/healthz/detailed?token=wrong")
    assert r.status_code == 404, r.status_code
    # Right token via query string -> 200
    r = client.get("/healthz/detailed?token=monitor-only-secret")
    assert r.status_code == 200, r.status_code
    assert "qbo_environment" in r.get_json()
    # Right token via header -> 200
    r = client.get(
        "/healthz/detailed",
        headers={"X-Healthz-Token": "monitor-only-secret"},
    )
    assert r.status_code == 200, r.status_code
    print("T4 healthz_detailed_token_unlocks PASS")


def t5_production_missing_vars_fails():
    env = {
        "APP_ENV": "production",
        "SECRET_KEY": "y" * 64,
    }
    try:
        _reset_app_env(env)
    except RuntimeError as e:
        msg = str(e)
        for needle in ("ENCRYPTION_KEY", "QBO_CLIENT_ID",
                       "QBO_CLIENT_SECRET", "QBO_REDIRECT_URI"):
            assert needle in msg, f"expected {needle} in error: {msg}"
        assert "y" * 64 not in msg, "SECRET_KEY value must not appear in error"
        print("T5 production_missing_vars_fails PASS")
        return
    raise AssertionError("expected RuntimeError on missing prod env vars")


def t6_malformed_encryption_key_fails():
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
        assert "not-a-valid-fernet-key" not in msg, msg
        print("T6 malformed_encryption_key_fails PASS")
        return
    raise AssertionError("expected RuntimeError on malformed Fernet key")


if __name__ == "__main__":
    t1_healthz_public_is_minimal()
    t2_healthz_detailed_blocks_anon()
    t3_healthz_detailed_operator_unlocks()
    t4_healthz_detailed_token_unlocks()
    t5_production_missing_vars_fails()
    t6_malformed_encryption_key_fails()
    print("ALL HEALTH SMOKE TESTS PASSED")
