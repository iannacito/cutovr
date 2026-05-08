"""Smoke tests for the /operator panel: gating + secret-free rendering.

Run from project root:

    python3 tests/smoke_operator_panel.py

Verifies:
  T1 With OPERATOR_EMAILS unset: panel feature is disabled.
       - Anonymous /operator → redirect to /login (login_required runs first).
       - A logged-in non-operator user → 404 on /operator.
       - Nav HTML on /dashboard does NOT contain "Operator" link.
       - is_operator_user(user) returns False.
  T2 With OPERATOR_EMAILS set but the logged-in user not in it: 404.
       - Nav still hides the Operator link.
  T3 With OPERATOR_EMAILS containing the logged-in user's email:
       - GET /operator returns 200 with cross-firm metrics.
       - Nav on /dashboard DOES contain "Operator" link.
       - GET /operator/firm/<id> for a real firm returns 200.
       - GET /operator/firm/<missing> returns 404.
  T4 SHOW_OPERATOR_TOOLS=0 force-disables the panel even if the
     allowlist is populated. Operator email is denied (404).
  T5 Rendered pages do NOT leak: SECRET_KEY/APP_SECRET, ENCRYPTION_KEY,
     QBO_CLIENT_SECRET, the literal Fernet token bytes.
"""

import importlib
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


GL_FILE = ROOT / "test_data" / "02_general_ledger.csv"
GL_BYTES = GL_FILE.read_bytes() if GL_FILE.exists() else b""


# Sentinel secrets we'll inject and then assert never appear in any page.
SECRET_VALUE = "z" * 64
QBO_SECRET_VALUE = "qbosec-" + "x" * 40
# Real Fernet key (32 url-safe base64 bytes); avoids the malformed-key error.
ENC_KEY_VALUE = "Yh7m5b1J9P0sR8wQv3KsVJpC1Bl0r2Gn9D6X2g8oZqU="


def _reset_app(env: dict):
    """Re-import app + operator_panel with a fresh env so module-level
    constants and the env-time gating both reflect the test's settings."""
    for mod in ("app", "operator_panel", "encryption"):
        if mod in sys.modules:
            del sys.modules[mod]
    # Wipe any prior test env by creating a controlled os.environ dict.
    base = {
        "APP_DB": tempfile.mktemp(suffix=".sqlite3"),
        "IMPORT_HISTORY_DB": tempfile.mktemp(suffix=".sqlite3"),
        "CSRF_DISABLE": "1",
        "SECRET_KEY": SECRET_VALUE,
        "APP_ENV": "local",
        "ENCRYPTION_KEY": ENC_KEY_VALUE,
        "QBO_CLIENT_ID": "test-client-id",
        "QBO_CLIENT_SECRET": QBO_SECRET_VALUE,
        "QBO_REDIRECT_URI": "https://example.com/oauth/callback",
    }
    # Start from a clean slate so OPERATOR_EMAILS / SHOW_OPERATOR_TOOLS
    # really are absent when the test wants them absent.
    for k in (
        "OPERATOR_EMAILS", "SHOW_OPERATOR_TOOLS",
    ):
        os.environ.pop(k, None)
    base.update(env)
    for k, v in base.items():
        os.environ[k] = v
    return importlib.import_module("app")


def _signup(client, firm, email, password="passw0rd!1234"):
    return client.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
        follow_redirects=False,
    )


def _login(client, email, password="passw0rd!1234"):
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


def _assert_no_secrets(html: str, label: str):
    """Belt-and-suspenders: never let a sentinel secret appear in HTML."""
    leaks = []
    for needle, name in [
        (SECRET_VALUE, "SECRET_KEY"),
        (QBO_SECRET_VALUE, "QBO_CLIENT_SECRET"),
        (ENC_KEY_VALUE, "ENCRYPTION_KEY"),
    ]:
        if needle in html:
            leaks.append(name)
    assert not leaks, f"{label} leaked secrets: {leaks}"


def t1_unset_disables_panel():
    appmod = _reset_app({})  # OPERATOR_EMAILS deliberately unset
    op = sys.modules["operator_panel"]

    assert op.operator_panel_enabled() is False
    assert op.get_operator_emails() == set()
    assert op.is_operator_user({"email": "anyone@x.com"}) is False
    assert op.is_operator_user(None) is False

    # Anonymous → login_required wins
    c = appmod.app.test_client()
    r = c.get("/operator")
    assert r.status_code == 302 and "/login" in r.headers.get("Location", ""), r.status_code

    # Logged-in non-operator → 404 (because operator_required aborts)
    _signup(c, "Acme Law", "alice@acme.test")
    r = c.get("/operator")
    assert r.status_code == 404, r.status_code

    # Nav must not contain the Operator link
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert b">Operator<" not in r.data, "Operator nav link should be hidden when allowlist is empty"

    # Per-firm route also hidden
    r = c.get("/operator/firm/1")
    assert r.status_code == 404

    print("T1 OK: unset OPERATOR_EMAILS fully hides the panel")


def t2_allowlist_does_not_match():
    appmod = _reset_app({"OPERATOR_EMAILS": "ops@example.com"})
    c = appmod.app.test_client()
    _signup(c, "Acme Law", "alice@acme.test")

    op = sys.modules["operator_panel"]
    assert op.operator_panel_enabled() is True
    user = appmod.db.authenticate("alice@acme.test", "passw0rd!1234")
    assert op.is_operator_user(user) is False

    r = c.get("/operator")
    assert r.status_code == 404, r.status_code
    r = c.get("/operator/firm/1")
    assert r.status_code == 404

    r = c.get("/dashboard")
    assert b">Operator<" not in r.data, "Non-operator user should not see the Operator nav link"

    print("T2 OK: allowlist set but user not in it → 404 + nav hidden")


def t3_allowed_operator_sees_panel():
    appmod = _reset_app({"OPERATOR_EMAILS": "ops@example.com, alice@acme.test"})
    c = appmod.app.test_client()
    _signup(c, "Acme Law", "alice@acme.test")
    # Add a second firm so cross-firm rendering exercises a real list
    c2 = appmod.app.test_client()
    _signup(c2, "Bravo Firm", "bob@bravo.test")
    if GL_BYTES:
        c2.post(
            "/upload",
            data={"company_name": "Bravo Co", "ledger_file": (io.BytesIO(GL_BYTES), "gl.csv")},
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    op = sys.modules["operator_panel"]
    user = appmod.db.authenticate("alice@acme.test", "passw0rd!1234")
    assert op.is_operator_user(user) is True

    r = c.get("/dashboard")
    assert r.status_code == 200
    assert b">Operator<" in r.data, "Operator nav link should be visible for allowlisted user"
    _assert_no_secrets(r.get_data(as_text=True), "/dashboard")

    r = c.get("/operator")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # Has expected metrics labels
    for needle in ("Total firms", "Total users", "QuickBooks", "Operator"):
        assert needle in body, f"missing {needle!r} in operator dashboard"
    # Has both firm rows visible (cross-firm metric)
    assert "Acme Law" in body and "Bravo Firm" in body
    _assert_no_secrets(body, "/operator")

    # Per-firm detail
    bob = appmod.db.authenticate("bob@bravo.test", "passw0rd!1234")
    bravo_firm_id = bob["firm_id"]
    r = c.get(f"/operator/firm/{bravo_firm_id}")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "Bravo Firm" in body
    assert "bob@bravo.test" in body
    _assert_no_secrets(body, f"/operator/firm/{bravo_firm_id}")

    # Missing firm
    r = c.get("/operator/firm/9999")
    assert r.status_code == 404

    print("T3 OK: allowlisted operator sees panel; nav shows; per-firm view works")


def t4_show_operator_tools_zero_disables():
    appmod = _reset_app({
        "OPERATOR_EMAILS": "alice@acme.test",
        "SHOW_OPERATOR_TOOLS": "0",
    })
    op = sys.modules["operator_panel"]
    assert op.operator_panel_enabled() is False
    user = {"email": "alice@acme.test"}
    assert op.is_operator_user(user) is False

    c = appmod.app.test_client()
    _signup(c, "Acme Law", "alice@acme.test")
    r = c.get("/operator")
    assert r.status_code == 404, r.status_code
    r = c.get("/dashboard")
    assert b">Operator<" not in r.data
    print("T4 OK: SHOW_OPERATOR_TOOLS=0 disables panel even with allowlist set")


def t5_no_secret_leakage():
    """Cross-cut with t3: walk every operator route and assert no sentinel
    secret is rendered. Already checked inline; this re-asserts after a
    fresh import to catch drift if someone adds a debug dump later."""
    appmod = _reset_app({"OPERATOR_EMAILS": "alice@acme.test"})
    c = appmod.app.test_client()
    _signup(c, "Acme Law", "alice@acme.test")
    if GL_BYTES:
        c.post(
            "/upload",
            data={"company_name": "Acme Co", "ledger_file": (io.BytesIO(GL_BYTES), "gl.csv")},
            content_type="multipart/form-data",
            follow_redirects=False,
        )
    alice = appmod.db.authenticate("alice@acme.test", "passw0rd!1234")
    for path in ["/operator", f"/operator/firm/{alice['firm_id']}"]:
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)
        _assert_no_secrets(r.get_data(as_text=True), path)
    print("T5 OK: operator pages render without leaking SECRET_KEY/QBO_CLIENT_SECRET/ENCRYPTION_KEY")


if __name__ == "__main__":
    t1_unset_disables_panel()
    t2_allowlist_does_not_match()
    t3_allowed_operator_sees_panel()
    t4_show_operator_tools_zero_disables()
    t5_no_secret_leakage()
    print("\nALL OPERATOR PANEL SMOKE TESTS PASSED")
