"""Smoke tests for the discovery-call CTA wiring and no-public-pricing posture.

Run from project root:

    python3 tests/smoke_discovery_cta.py

The public site no longer sells self-serve plans or shows dollar amounts.
The primary CTA across the public pages books a discovery call. When
DISCOVERY_CALL_URL is configured it points at that (external Calendly) link
and opens in a new tab; when it is unset it falls back to the in-app
"Send migration details" request form so the CTA is never dead.

Covers:
  T1 With DISCOVERY_CALL_URL set, the public CTAs link to that URL and open
     in a new tab (target/rel), on landing + pricing + intake.
  T2 With DISCOVERY_CALL_URL unset, the same CTAs fall back to the in-app
     request form (/pricing/quote-request) and do NOT open a new tab.
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

DISCOVERY_URL = "https://calendly.com/cutovr/discovery"

# Public, no-auth pages that must never show a price or a checkout CTA.
PUBLIC_PAGES = ("/", "/pricing", "/intake", "/pricing/quote-request",
                "/onboarding", "/pricing/checkout/success")


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


def t1_cta_uses_discovery_url_when_configured():
    os.environ["DISCOVERY_CALL_URL"] = DISCOVERY_URL
    appmod = _reload_app()
    c = appmod.app.test_client()
    for path in ("/", "/pricing", "/intake"):
        body = c.get(path).get_data(as_text=True)
        assert DISCOVERY_URL in body, f"{path} CTA should link to DISCOVERY_CALL_URL"
        # The external link opens in a new tab safely.
        assert 'target="_blank"' in body and 'rel="noopener"' in body, \
            f"{path} external discovery CTA should open in a new tab with rel=noopener"
    print("T1 OK: configured DISCOVERY_CALL_URL drives the CTA (new tab) on landing/pricing/intake")


def t2_cta_falls_back_to_request_form_when_unset():
    os.environ.pop("DISCOVERY_CALL_URL", None)
    appmod = _reload_app()
    c = appmod.app.test_client()
    for path in ("/", "/pricing", "/intake"):
        body = c.get(path).get_data(as_text=True)
        assert "/pricing/quote-request" in body, \
            f"{path} CTA should fall back to the in-app request form when unset"
        assert DISCOVERY_URL not in body, \
            f"{path} should not contain a stale external URL when unset"
    print("T2 OK: unset DISCOVERY_CALL_URL falls back to the in-app request form")


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
        t1_cta_uses_discovery_url_when_configured()
        t2_cta_falls_back_to_request_form_when_unset()
        t3_no_public_prices_or_checkout()
        print("\nALL DISCOVERY-CTA SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
