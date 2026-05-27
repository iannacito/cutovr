"""Smoke tests for customer-facing nav hiding internal/operator links.

Run from project root:

    python3 tests/smoke_customer_nav_hides_internal_links.py

Covers:
  T1 A normal logged-in customer (no OPERATOR_EMAILS, no DEMO_MODE) sees
     exactly the three customer-facing nav links — Migration, QuickBooks,
     Support — and does NOT see Dashboard, Onboarding, Checklist,
     QuickBooks guide, Readiness, Operator, or Demo as separate top-level
     nav items.
  T2 An operator (email in OPERATOR_EMAILS) sees Readiness, Operator,
     and Demo links in addition to the customer nav.
  T3 The Readiness route is still reachable by a normal customer typing
     the URL directly (the link is hidden, not the page) — readiness is
     informational, not secret. (If this changes, this test will catch
     it.)
  T4 The QuickBooks guide is no longer a separate top-level nav item but
     stays reachable from inside the QuickBooks page (a link on
     /quickbooks points at /quickbooks-guide). The direct route also
     still works. No "QBO" abbreviation appears in the nav.
  T5 The onboarding page's "Go-live readiness" CTA button is hidden
     from normal customers (operator-only).
  T7 The /dashboard, /onboarding, /migration-checklist routes still
     respond 200 so bookmarked links from the previous nav don't 404.
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


def _nav_block(body: str) -> str:
    # Isolate the primary <nav>...</nav> so assertions about top-level nav
    # links don't accidentally match identical link text inside page
    # content (e.g. an in-page "QuickBooks guide" link on /quickbooks).
    start = body.find('<nav aria-label="Primary">')
    if start == -1:
        return body
    end = body.find("</nav>", start)
    return body[start:end] if end != -1 else body[start:]


def t1_normal_customer_does_not_see_internal_links():
    appmod = _reset_app({})
    c = appmod.app.test_client()
    _signup(c, "Customer Firm", "alice@customer.test")

    r = c.get("/dashboard")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    nav = _nav_block(body)

    # The three customer-facing top-level nav links a normal customer
    # must see.
    for needle in (">Migration<", ">QuickBooks<", ">Support<"):
        assert needle in nav, f"normal customer nav missing {needle!r}"

    # Items removed from top-level customer nav in the simplification.
    # Their routes still work (see t7) but they're no longer in the chrome.
    for needle in (
        ">Dashboard<",
        ">Onboarding<",
        ">Checklist<",
        ">QuickBooks guide<",
    ):
        assert needle not in nav, (
            f"top-level customer nav must no longer expose {needle!r} "
            f"(it should be reachable from inside Migration or QuickBooks)"
        )

    # Internal links a normal customer must NOT see anywhere in nav.
    for needle in (">Readiness<", ">Operator<", ">Demo<"):
        assert needle not in nav, (
            f"normal customer nav must NOT expose internal link {needle!r}"
        )

    print("T1 OK: normal customer nav shows only Migration / QuickBooks / Support")


def t2_operator_sees_internal_links():
    op_email = "ops@anthro.test"
    appmod = _reset_app({"OPERATOR_EMAILS": op_email})
    c = appmod.app.test_client()
    _signup(c, "Operator Firm", op_email)

    r = c.get("/dashboard")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    nav = _nav_block(body)

    for needle in (">Readiness<", ">Operator<", ">Demo<"):
        assert needle in nav, f"operator nav missing internal link {needle!r}"

    # Operator still sees the three simplified customer nav links too.
    for needle in (">Migration<", ">QuickBooks<", ">Support<"):
        assert needle in nav, f"operator nav missing customer link {needle!r}"

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


def t4_quickbooks_guide_reachable_from_quickbooks_page_not_top_nav():
    appmod = _reset_app({})
    c = appmod.app.test_client()
    _signup(c, "Customer Firm 4", "alice4@customer.test")

    # Top-level nav no longer offers a separate "QuickBooks guide" link
    # and never uses the QBO abbreviation. The route /quickbooks-guide
    # is still public so existing bookmarks keep working.
    body = c.get("/dashboard").get_data(as_text=True)
    nav = _nav_block(body)
    assert ">QuickBooks guide<" not in nav, (
        "top-level nav should no longer have a separate 'QuickBooks guide' item"
    )
    assert "QBO" not in nav, "nav should not use the QBO abbreviation"

    # The QuickBooks manage page surfaces the guide inline so customers
    # who land on /quickbooks can still get to it in one click.
    qbo_body = c.get("/quickbooks").get_data(as_text=True)
    assert "/quickbooks-guide" in qbo_body, (
        "/quickbooks page must link to the QuickBooks guide so it stays "
        "discoverable after being removed from the top-level nav"
    )

    # Direct route still works.
    r = c.get("/quickbooks-guide")
    assert r.status_code == 200, "/quickbooks-guide direct route must keep working"

    print("T4 OK: QuickBooks guide is reachable from /quickbooks (not a top-nav item)")


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


def t7_pre_simplification_routes_still_work_for_bookmarks():
    # The nav was simplified to three items, but the underlying routes
    # for the removed top-level items must still resolve so any bookmark
    # someone saved from the old nav keeps working.
    appmod = _reset_app({})
    c = appmod.app.test_client()
    _signup(c, "Customer Firm 7", "alice7@customer.test")
    for path in ("/dashboard", "/onboarding", "/migration-checklist", "/quickbooks-guide"):
        r = c.get(path)
        assert r.status_code in (200, 302), (
            f"bookmarked route {path} must still respond, got {r.status_code}"
        )
    print("T7 OK: /dashboard, /onboarding, /migration-checklist, /quickbooks-guide still respond")


if __name__ == "__main__":
    t1_normal_customer_does_not_see_internal_links()
    t2_operator_sees_internal_links()
    t3_readiness_route_still_reachable_for_logged_in_users()
    t4_quickbooks_guide_reachable_from_quickbooks_page_not_top_nav()
    t5_onboarding_readiness_cta_hidden_from_customers()
    t6_onboarding_readiness_cta_visible_to_operators()
    t7_pre_simplification_routes_still_work_for_bookmarks()
    print("ALL OK: customer nav simplified to Migration/QuickBooks/Support; operator gates and bookmarked routes intact.")
