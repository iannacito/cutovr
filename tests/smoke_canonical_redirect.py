"""Smoke tests for the production canonical-domain redirect.

Run from project root:

    python3 tests/smoke_canonical_redirect.py

Covers:
  T1 Production + default Render host -> redirects root to www.pclawmigrate.com.
  T2 Path and query string are preserved verbatim across the redirect.
  T3 Custom domain (www.pclawmigrate.com) does NOT redirect.
  T4 Demo host (demo.pclawmigrate.com) does NOT redirect.
  T5 /healthz on the Render host stays direct (200, no redirect) so
     Render deploy health probes don't get bounced to a different origin.
  T6 Local dev (APP_ENV=local) never redirects, even from the Render host.
  T7 Non-idempotent methods (POST) are not redirected, so OAuth callbacks
     and webhooks keep their method + body if they ever land on the
     default host.
  T8 CANONICAL_REDIRECT_FROM_HOSTS env override works (extra source host
     is honoured) and CANONICAL_REDIRECT_TO env override controls target.
"""

import importlib
import os
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _prod_env(**overrides):
    """Minimum env to import app.py with APP_ENV=production successfully."""
    env = {
        "APP_ENV": "production",
        "SECRET_KEY": "z" * 64,
        "ENCRYPTION_KEY": "ZmFrZS1mZXJuZXQta2V5LWZvci10ZXN0aW5nLW9ubHk9PQ==",
        "QBO_CLIENT_ID": "test-client-id",
        "QBO_CLIENT_SECRET": "test-client-secret",
        "QBO_REDIRECT_URI": "https://www.pclawmigrate.com/oauth/callback",
        "CSRF_DISABLE": "0",
        "APP_DB": tempfile.mktemp(suffix=".sqlite3"),
        "IMPORT_HISTORY_DB": tempfile.mktemp(suffix=".sqlite3"),
        # We don't want the production validator to fail on Fernet — supply a
        # real Fernet key.
    }
    # Replace ENCRYPTION_KEY with a real Fernet-shaped key generated here so
    # we don't depend on the placeholder above being valid.
    from cryptography.fernet import Fernet  # noqa: WPS433
    env["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    env.update(overrides)
    return env


def _local_env(**overrides):
    env = {
        "APP_ENV": "local",
        "SECRET_KEY": "z" * 64,
        "CSRF_DISABLE": "1",
        "APP_DB": tempfile.mktemp(suffix=".sqlite3"),
        "IMPORT_HISTORY_DB": tempfile.mktemp(suffix=".sqlite3"),
    }
    env.update(overrides)
    return env


def _reload_app(env):
    """Re-import app.py with a fresh environment so module-level code re-runs."""
    for mod in ("app", "encryption"):
        if mod in sys.modules:
            del sys.modules[mod]
    with mock.patch.dict(os.environ, env, clear=True):
        return importlib.import_module("app")


def _client_with_host(appmod):
    """Return a test client and a helper to GET against a given host header.

    We use ``base_url`` on a per-request basis (via environ_overrides) so each
    test can pick the host without rebuilding the client.
    """
    return appmod.app.test_client()


def _get(client, host, path, scheme="https", method="GET"):
    return client.open(
        path,
        method=method,
        base_url=f"{scheme}://{host}",
    )


def t1_prod_render_host_redirects_root():
    appmod = _reload_app(_prod_env())
    client = _client_with_host(appmod)
    r = _get(client, "pclaw-qbo-v2.onrender.com", "/")
    assert r.status_code == 301, r.status_code
    assert r.headers["Location"] == "https://www.pclawmigrate.com/", r.headers["Location"]
    print("T1 prod_render_host_redirects_root PASS")


def t2_path_and_query_preserved():
    appmod = _reload_app(_prod_env())
    client = _client_with_host(appmod)
    r = _get(client, "pclaw-qbo-v2.onrender.com", "/privacy?x=1&y=two")
    assert r.status_code == 301, r.status_code
    assert r.headers["Location"] == (
        "https://www.pclawmigrate.com/privacy?x=1&y=two"
    ), r.headers["Location"]
    # Path-only (no query) must not gain a trailing "?".
    r2 = _get(client, "pclaw-qbo-v2.onrender.com", "/terms")
    assert r2.status_code == 301
    assert r2.headers["Location"] == "https://www.pclawmigrate.com/terms", r2.headers["Location"]
    print("T2 path_and_query_preserved PASS")


def t3_custom_domain_does_not_redirect():
    appmod = _reload_app(_prod_env())
    client = _client_with_host(appmod)
    r = _get(client, "www.pclawmigrate.com", "/healthz")
    # /healthz should answer 200 directly, never with a redirect to itself.
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert body and body.get("status") == "ok"
    print("T3 custom_domain_does_not_redirect PASS")


def t4_demo_host_does_not_redirect():
    appmod = _reload_app(_prod_env())
    client = _client_with_host(appmod)
    # Demo host on the custom-domain hierarchy must be left alone.
    r = _get(client, "demo.pclawmigrate.com", "/healthz")
    assert r.status_code == 200, r.status_code
    # Hypothetical demo Render host (if added later) is also not in the
    # default redirect list.
    r2 = _get(client, "pclaw-qbo-demo.onrender.com", "/healthz")
    assert r2.status_code == 200, r2.status_code
    print("T4 demo_host_does_not_redirect PASS")


def t5_healthz_on_render_host_not_redirected():
    appmod = _reload_app(_prod_env())
    client = _client_with_host(appmod)
    r = _get(client, "pclaw-qbo-v2.onrender.com", "/healthz")
    assert r.status_code == 200, (
        "Render deploy health probe must see direct 200 on /healthz, "
        f"got {r.status_code} (Location={r.headers.get('Location')})"
    )
    body = r.get_json()
    assert body and body.get("status") == "ok"
    print("T5 healthz_on_render_host_not_redirected PASS")


def t6_local_dev_does_not_redirect():
    appmod = _reload_app(_local_env())
    client = _client_with_host(appmod)
    # Even pretending the local app is reached via the prod Render host
    # must not trigger a redirect when APP_ENV is local/dev.
    r = _get(client, "pclaw-qbo-v2.onrender.com", "/")
    assert r.status_code != 301, "local dev must never issue the canonical redirect"
    # /healthz on local should still be reachable as well.
    r2 = _get(client, "pclaw-qbo-v2.onrender.com", "/healthz")
    assert r2.status_code == 200, r2.status_code
    print("T6 local_dev_does_not_redirect PASS")


def t7_post_not_redirected():
    appmod = _reload_app(_prod_env())
    client = _client_with_host(appmod)
    # POSTs are passed through so OAuth callbacks / webhooks that hit the
    # default host (shouldn't happen, but defence in depth) keep their body.
    r = _get(client, "pclaw-qbo-v2.onrender.com", "/login", method="POST")
    assert r.status_code != 301, (
        f"POST must not be redirected with 301 (got Location="
        f"{r.headers.get('Location')})"
    )
    print("T7 post_not_redirected PASS")


def t8_env_overrides_honoured():
    # Pretend the operator added a second canonical-source host and a
    # different target origin.
    env = _prod_env(
        CANONICAL_REDIRECT_FROM_HOSTS=(
            "pclaw-qbo-v2.onrender.com,old-host.example.com"
        ),
        CANONICAL_REDIRECT_TO="https://canonical.example.com",
    )
    appmod = _reload_app(env)
    client = _client_with_host(appmod)
    r = _get(client, "old-host.example.com", "/foo?bar=baz")
    assert r.status_code == 301, r.status_code
    assert r.headers["Location"] == (
        "https://canonical.example.com/foo?bar=baz"
    ), r.headers["Location"]
    # Original default host still works too.
    r2 = _get(client, "pclaw-qbo-v2.onrender.com", "/")
    assert r2.status_code == 301
    assert r2.headers["Location"] == "https://canonical.example.com/"
    print("T8 env_overrides_honoured PASS")


if __name__ == "__main__":
    t1_prod_render_host_redirects_root()
    t2_path_and_query_preserved()
    t3_custom_domain_does_not_redirect()
    t4_demo_host_does_not_redirect()
    t5_healthz_on_render_host_not_redirected()
    t6_local_dev_does_not_redirect()
    t7_post_not_redirected()
    t8_env_overrides_honoured()
    print("ALL CANONICAL REDIRECT SMOKE TESTS PASSED")
