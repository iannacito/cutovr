"""Smoke tests for the public landing page (/) and CTA routing.

Run from project root:

    python3 tests/smoke_landing_page.py

Covers:
  T1 GET / for an unauthenticated visitor renders the landing page (200)
     with hero, value prop, process steps, comparison, and CTAs.
  T2 The landing page links to /signup, /login, /onboarding, /support.
  T3 Public routes (/login, /signup, /onboarding, /privacy, /terms,
     /support, /disconnect) all still render without authentication.
  T4 An authenticated visitor on / is redirected to /dashboard
     (so we don't break the protected flow).
  T5 The landing page does NOT contain the legacy product name "Cutover".
"""

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
os.environ.setdefault("SECRET_KEY", "smoke-secret-landing")

import app as appmod  # noqa: E402


def _get(path, client=None):
    c = client or appmod.app.test_client()
    return c.get(path)


def t1_landing_renders_with_marketing_content():
    r = _get("/")
    assert r.status_code == 200, f"GET / -> {r.status_code}"
    body = r.get_data(as_text=True)

    must_contain = [
        # Hero / value prop — leads with hands-off, handled-for-you
        # convenience (not speed/cost).
        "PCLaw",
        "QuickBooks",
        "handled for you",
        "answer a few questions",
        # Coverage section makes clear it's not GL-only
        "More than just the General Ledger",
        "Chart of Accounts",
        "Trial Balance",
        "Trust",
        "A/R",
        "A/P",
        # Process steps
        "How it works",
        "Export from PCLaw",
        "Upload to",
        "Validate",
        "Connect QuickBooks",
        "Review",
        "Verify",
        # Comparison framing (defensible language)
        "consultant",
        "weeks",
        "thousands",
        "fraction of",
        # Consultative, discovery-call CTAs (no public self-serve checkout)
        "Book a discovery call",
        "Send migration details",
        # Positive framing of scoped pricing (no "unavailable" feel)
        "scoped",
    ]
    for needle in must_contain:
        assert needle in body, f"landing page missing expected copy: {needle!r}"
    print("T1 OK: / renders landing page with hero, coverage, steps, compare, discovery CTAs")


def t1b_landing_has_no_public_prices_or_package_cards():
    """The consultative flow must not surface self-serve prices or package
    cards on the landing page — pricing is scoped on a discovery call."""
    r = _get("/")
    body = r.get_data(as_text=True)
    for stale in ("$999", "$1,499", "$1,999", "$499"):
        assert stale not in body, f"landing page still shows public price {stale}"
    # The legacy plan teaser cards are gone.
    assert "landing-plan-essential" not in body, "legacy package card still present"
    assert "landing-plan-standard" not in body, "legacy package card still present"
    assert "landing-price-anchor" not in body, "legacy 'From $999' anchor still present"
    print("T1b OK: landing page has no public prices or package cards")


def t2_landing_links_to_public_routes():
    r = _get("/")
    body = r.get_data(as_text=True)
    # Discovery-first journey: the landing page routes prospects to the
    # public request form (/onboarding/start) and keeps the supporting
    # public pages reachable. Self-serve signup is no longer a hero CTA.
    for path in ("/login", "/onboarding", "/onboarding/start", "/support", "/pricing"):
        assert f'href="{path}"' in body, f"landing page missing link to {path}"
    print("T2 OK: landing links to /login, /onboarding, /onboarding/start, /support, /pricing")


def t2b_get_started_cta_routes_to_discovery_not_packages():
    """The primary Get Started / discovery CTA must point at the discovery
    flow (booking link or the public request form), never the retired
    package-selection workflow or a Stripe checkout."""
    r = _get("/")
    body = r.get_data(as_text=True)
    # No links into the retired multi-step package picker.
    assert "onboarding_step1" not in body
    assert "/pricing/checkout" not in body, "landing CTA must not hit Stripe checkout"
    # When DISCOVERY_CALL_URL is unset (default in tests), the CTA falls
    # back to the public request form.
    assert 'data-testid="landing-book-call"' in body
    print("T2b OK: Get Started CTA routes to discovery flow, not packages/Stripe")


def t3_public_routes_render_unauthenticated():
    c = appmod.app.test_client()
    expectations = [
        ("/login", "Sign in"),
        ("/signup", None),  # may say "Create" or similar; just check 200
        ("/onboarding", "Onboarding"),
        ("/privacy", "Privacy"),
        ("/terms", "Terms"),
        ("/support", "Support"),
        ("/disconnect", None),
    ]
    for path, needle in expectations:
        r = c.get(path)
        assert r.status_code in (200, 302), f"{path} -> {r.status_code}"
        if needle and r.status_code == 200:
            body = r.get_data(as_text=True)
            assert needle in body, f"{path} missing {needle!r}"
    print("T3 OK: /login, /signup, /onboarding, /privacy, /terms, /support, /disconnect all reachable")


def t4_authenticated_user_redirected_to_dashboard():
    c = appmod.app.test_client()
    # Sign up creates a user + logs in.
    c.post(
        "/signup",
        data={
            "firm_name": "Landing Smoke Firm",
            "email": "landing-smoke@example.com",
            "password": "passw0rd!1234",
            "confirm_password": "passw0rd!1234",
        },
    )
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (301, 302), f"/ for auth user should redirect, got {r.status_code}"
    location = r.headers.get("Location", "")
    assert "/dashboard" in location, f"/ for auth user should redirect to /dashboard, got {location!r}"
    print("T4 OK: authenticated visitors on / are redirected to /dashboard")


def t5_landing_has_no_legacy_cutover_branding():
    r = _get("/")
    body = r.get_data(as_text=True)
    # The product was previously called "Cutover" — guard against regressions.
    # We allow the lowercase generic word "cutover" (e.g. "cutover reconciliation")
    # but reject the standalone product wordmark.
    assert ">Cutover<" not in body, "landing page should not show legacy Cutover wordmark"
    assert "Cutover &mdash;" not in body, "landing page should not show legacy Cutover product name"
    print("T5 OK: landing page does not show legacy Cutover product name")


def t6_discovery_call_url_external_when_set_else_fallback():
    """DISCOVERY_CALL_URL drives the Book-a-discovery-call CTA.

    When set, the CTA links straight to the external booking URL (opened in
    a new tab). When unset, it gracefully falls back to the public request
    form (/onboarding/start) — never a broken external link.
    """
    booking = "https://cal.example.com/cutovr/discovery"
    prev = os.environ.get("DISCOVERY_CALL_URL")
    try:
        os.environ["DISCOVERY_CALL_URL"] = booking
        body = _get("/").get_data(as_text=True)
        assert f'href="{booking}"' in body, "external booking URL not used when set"
        assert 'target="_blank"' in body, "external booking link should open in a new tab"

        os.environ.pop("DISCOVERY_CALL_URL", None)
        body = _get("/").get_data(as_text=True)
        assert booking not in body, "stale external URL leaked after unset"
        assert 'href="/onboarding/start"' in body, (
            "discovery CTA should fall back to the public request form when "
            "DISCOVERY_CALL_URL is missing"
        )
    finally:
        if prev is None:
            os.environ.pop("DISCOVERY_CALL_URL", None)
        else:
            os.environ["DISCOVERY_CALL_URL"] = prev
    print("T6 OK: DISCOVERY_CALL_URL links external when set, falls back when missing")


if __name__ == "__main__":
    try:
        t1_landing_renders_with_marketing_content()
        t1b_landing_has_no_public_prices_or_package_cards()
        t2_landing_links_to_public_routes()
        t2b_get_started_cta_routes_to_discovery_not_packages()
        t3_public_routes_render_unauthenticated()
        t4_authenticated_user_redirected_to_dashboard()
        t5_landing_has_no_legacy_cutover_branding()
        t6_discovery_call_url_external_when_set_else_fallback()
        print("\nALL LANDING-PAGE SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
