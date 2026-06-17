"""Smoke tests for the discovery-call CTA wiring and no-public-pricing posture.

Run from project root:

    python3 tests/smoke_discovery_cta.py

The public site no longer sells self-serve plans or shows dollar amounts.
The primary CTA across the public pages books a discovery call. Every CTA
now routes to the in-app booking page (/book-discovery-call), which embeds
the Calendly inline widget. The CTA stays in-app (no new tab / external
link), keeping visitors on the branded Cutovr site.

Covers:
  T1 The public CTAs link to the in-app booking route (/book-discovery-call)
     on landing + pricing + intake, and do NOT open an external new tab.
  T2 The booking page embeds the Calendly inline widget with the exact
     data-url + widget.js script, and applies a CSP that allows Calendly.
     When DISCOVERY_CALL_URL is unset the page falls back to the in-app
     request form so it never dead-ends.
  T3 No public/customer-facing page surfaces a dollar amount or a
     checkout/payment CTA (landing, pricing, intake, quote-request,
     onboarding, intake success).
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-discovery-cta")

FORBIDDEN_AMOUNTS = ("$999", "$1,499", "$1499", "$250", "$299", "$199", "$499")
CHECKOUT_PHRASES = ("Continue to secure payment", "Add to cart", "Proceed to checkout")

DISCOVERY_URL = "https://calendly.com/cutovr-discovery-call/cutovr-discovery-call"
BOOKING_ROUTE = "/book-discovery-call"
WIDGET_SCRIPT = "https://assets.calendly.com/assets/external/widget.js"

# Public, no-auth pages that must never show a price or a checkout CTA.
PUBLIC_PAGES = ("/", "/pricing", "/intake", "/pricing/quote-request",
                "/onboarding", "/pricing/checkout/success", BOOKING_ROUTE)


def _reload_app():
    """(Re)import app + branding so DISCOVERY_CALL_URL env is picked up."""
    import branding
    importlib.reload(branding)
    if "app" in sys.modules:
        appmod = importlib.reload(sys.modules["app"])
    else:
        import app as appmod  # noqa: F401
        appmod = sys.modules["app"]
    return appmod


def t1_ctas_route_to_in_app_booking_page():
    os.environ["DISCOVERY_CALL_URL"] = DISCOVERY_URL
    appmod = _reload_app()
    c = appmod.app.test_client()
    for path in ("/", "/pricing", "/intake"):
        body = c.get(path).get_data(as_text=True)
        assert BOOKING_ROUTE in body, \
            f"{path} CTA should link to the in-app booking route {BOOKING_ROUTE}"
        # The booking CTA stays in-app: it must NOT point straight at the
        # external Calendly URL, and must not open a new tab.
        assert DISCOVERY_URL not in body, \
            f"{path} CTA should not link directly to the external Calendly URL"
    print("T1 OK: public CTAs route to the in-app booking page (no external new tab)")


def t2_booking_page_embeds_calendly_widget():
    os.environ["DISCOVERY_CALL_URL"] = DISCOVERY_URL
    appmod = _reload_app()
    c = appmod.app.test_client()
    r = c.get(BOOKING_ROUTE)
    assert r.status_code == 200, f"{BOOKING_ROUTE} -> {r.status_code}"
    body = r.get_data(as_text=True)
    assert 'class="calendly-inline-widget"' in body, \
        "booking page should render the Calendly inline widget div"
    assert f'data-url="{DISCOVERY_URL}"' in body, \
        "widget div should carry the exact DISCOVERY_CALL_URL as data-url"
    assert WIDGET_SCRIPT in body, \
        "booking page should include the Calendly widget.js script"
    # CSP for this page must allow the Calendly script + frame.
    csp = r.headers.get("Content-Security-Policy", "")
    assert "assets.calendly.com" in csp, \
        "booking-page CSP should allow the Calendly assets origin"
    assert "calendly.com" in csp and "frame-src" in csp, \
        "booking-page CSP should allow the Calendly iframe via frame-src"
    print("T2a OK: booking page embeds the Calendly widget with the exact data-url + script and a permissive CSP")

    # Fallback: if a deploy blanks the Calendly URL the page must not
    # dead-end. The branding default is the real Calendly URL, so to exercise
    # the empty case we blank it on the loaded module directly.
    os.environ.pop("DISCOVERY_CALL_URL", None)
    appmod = _reload_app()
    appmod.branding.DISCOVERY_CALL_URL = ""
    c = appmod.app.test_client()
    r = c.get(BOOKING_ROUTE)
    assert r.status_code == 200, f"{BOOKING_ROUTE} (blank) -> {r.status_code}"
    body = r.get_data(as_text=True)
    assert "calendly-inline-widget" not in body, \
        "with no URL configured the page must not render an empty Calendly widget"
    assert "/pricing/quote-request" in body, \
        "with no URL configured the booking page should fall back to the request form"
    print("T2b OK: with DISCOVERY_CALL_URL blank the booking page falls back to the request form")


def t3_no_public_prices_or_checkout():
    os.environ.pop("DISCOVERY_CALL_URL", None)
    appmod = _reload_app()
    c = appmod.app.test_client()
    for path in PUBLIC_PAGES:
        r = c.get(path)
        assert r.status_code in (200, 302), f"{path} -> {r.status_code}"
        if r.status_code != 200:
            continue
        body = r.get_data(as_text=True)
        for amount in FORBIDDEN_AMOUNTS:
            assert amount not in body, f"{path} must not show {amount!r}"
        for phrase in CHECKOUT_PHRASES:
            assert phrase not in body, f"{path} must not show checkout phrase {phrase!r}"
    print("T3 OK: no public page surfaces a dollar amount or checkout CTA")


if __name__ == "__main__":
    try:
        t1_ctas_route_to_in_app_booking_page()
        t2_booking_page_embeds_calendly_widget()
        t3_no_public_prices_or_checkout()
        print("\nALL DISCOVERY-CTA SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
