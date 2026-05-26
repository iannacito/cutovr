"""Smoke tests for customer-facing nav hiding internal/operator links.

Run from project root:

    python3 tests/smoke_customer_nav_hides_internal_links.py

Covers:
  T1 A normal logged-in customer (no OPERATOR_EMAILS, no DEMO_MODE) sees
     only the customer-facing nav links and does NOT see Readiness,
     Operator, or Demo links.
  T2 An operator (email in OPERATOR_EMAILS) sees Readiness, Operator,
     and Demo links in addition to the customer nav.
  T3 The Readiness route is still reachable by a normal customer typing
     the URL directly (the link is hidden, not the page) — readiness is
     informational, not secret. (If this changes, this test will catch
     it.)
  T4 Authenticated nav uses plain-English "QuickBooks guide" rather
     than the "QBO guide" abbreviation.
  T5 The onboarding page's "Go-live readiness" CTA button is hidden
     from normal customers (operator-only).
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


ENC_KEY_VALUE = "Yh7m5b1J9P0sR8wQv3KsVJpC1Bl0r2Gn9D6X2g8oZqU="
SECRET_VALUE = "z" * 64


def _reset_app(env: dict):
    for mod in ("app", "operator_panel", "demo_mode", "encryption"):
        if mod in sys.modules:
            del sys.modules[mod]
    base = {
        "APP_DB": tempfile.mktemp(suffix=".sqlite3"),
        "IMPORT_HISTORY_DB": tempfile.mktemp(suffix=".sqlite3"),
        "CSRF_DISABLE": "1",
        "SECRET_KEY": SECRET_VALUE,
        "APP_ENV": "local",
        "ENCRYPTION_KEY": ENC_KEY_VALUE,
        "QBO_CLIENT_ID": "test-client-id",
        "QBO_CLIENT_SECRET": "test-client-secret",
        "QBO_REDIRECT_URI": "https://example.com/oauth/callback",
    }
    for k in ("OPERATOR_EMAILS", "SHOW_OPERATOR_TOOLS", "DEMO_MODE", "APP_DEMO_MODE"):
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


def t1_normal_customer_does_not_see_internal_links():
    appmod = _reset_app({})
    c = appmod.app.test_client()
    _signup(c, "Customer Firm", "alice@customer.test")

    r = c.get("/dashboard")
    assert r.status_code == 200
    body = r.get_data(as_text=True)

    # Customer-facing links a normal customer must see.
    for needle in (
        ">Dashboard<",
        ">Onboarding<",
        ">Checklist<",
        ">QuickBooks<",
        ">QuickBooks guide<",
        ">Support<",
    ):
        assert needle in body, f"normal customer nav missing {needle!r}"

    # Internal links a normal customer must NOT see.
    for needle in (">Readiness<", ">Operator<", ">Demo<"):
        assert needle not in body, (
            f"normal customer nav must NOT expose internal link {needle!r}"
        )

    print("T1 OK: normal customer nav shows only customer-facing links")


def t2_operator_sees_internal_links():
    op_email = "ops@anthro.test"
    appmod = _reset_app({"OPERATOR_EMAILS": op_email})
    c = appmod.app.test_client()
    _signup(c, "Operator Firm", op_email)

    r = c.get("/dashboard")
    assert r.status_code == 200
    body = r.get_data(as_text=True)

    for needle in (">Readiness<", ">Operator<", ">Demo<"):
        assert needle in body, f"operator nav missing internal link {needle!r}"

    print("T2 OK: operator nav exposes Readiness, Operator, Demo links")


def t3_readiness_route_still_reachable_for_logged_in_users():
    # The link is hidden, not the route; readiness is informational and
    # protected only by @login_required. This test pins that contract so
    # we notice if it changes.
    appmod = _reset_app({})
    c = appmod.app.test_client()
    _signup(c, "Customer Firm 3", "alice3@customer.test")
    r = c.get("/readiness")
    assert r.status_code in (200, 302), f"readiness should be reachable, got {r.status_code}"
    print("T3 OK: /readiness route still reachable by logged-in users (link hidden, page not)")


def t4_nav_uses_plain_english_quickbooks_guide():
    appmod = _reset_app({})
    c = appmod.app.test_client()
    _signup(c, "Customer Firm 4", "alice4@customer.test")
    body = c.get("/dashboard").get_data(as_text=True)
    assert ">QuickBooks guide<" in body, "nav should use plain-English 'QuickBooks guide'"
    assert ">QBO guide<" not in body, "nav should not use the QBO abbreviation"
    print("T4 OK: nav uses 'QuickBooks guide' not 'QBO guide'")


def t5_onboarding_readiness_cta_hidden_from_customers():
    appmod = _reset_app({})
    c = appmod.app.test_client()
    _signup(c, "Customer Firm 5", "alice5@customer.test")
    r = c.get("/onboarding")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Go-live readiness" not in body, (
        "onboarding 'Go-live readiness' CTA must be hidden from normal customers"
    )
    print("T5 OK: onboarding 'Go-live readiness' CTA hidden from normal customers")


def t6_onboarding_readiness_cta_visible_to_operators():
    op_email = "ops6@anthro.test"
    appmod = _reset_app({"OPERATOR_EMAILS": op_email})
    c = appmod.app.test_client()
    _signup(c, "Operator Firm 6", op_email)
    r = c.get("/onboarding")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Go-live readiness" in body, (
        "onboarding 'Go-live readiness' CTA should be visible to operators"
    )
    print("T6 OK: onboarding 'Go-live readiness' CTA visible to operators")


if __name__ == "__main__":
    t1_normal_customer_does_not_see_internal_links()
    t2_operator_sees_internal_links()
    t3_readiness_route_still_reachable_for_logged_in_users()
    t4_nav_uses_plain_english_quickbooks_guide()
    t5_onboarding_readiness_cta_hidden_from_customers()
    t6_onboarding_readiness_cta_visible_to_operators()
    print("ALL OK: customer nav hides internal links; operators see them.")
