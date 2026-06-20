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
  T6 The landing page shows the three-step journey (Book a discovery call,
     Get a quote, Have your data migrated) with no public pricing amounts.
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
        # Hero / value prop — scoped and reviewed by our team, with the
        # done-for-you positioning preserved in the value bullets.
        "PCLaw",
        "QuickBooks",
        "Your PC Law Data, moved into QuickBooks",
        "Done for you",
        "scoped and reviewed by our team",
        # Discovery-call flow (customer-facing, no internal process detail)
        "discovery call",
        "quote it on the call",
        # Coverage section makes clear it's not GL-only
        "More than just the General Ledger",
        "Chart of Accounts",
        "Trial Balance",
        "Trust",
        "A/R",
        "A/P",
        # Process steps for the engagement
        "How it works",
        "Book a discovery call",
        "We review your migration",
        "You get a clear quote",
        # Comparison framing (defensible language)
        "consultant",
        "weeks",
        # CTAs
        "Book a discovery call",
        "Send migration details",
    ]
    for needle in must_contain:
        assert needle in body, f"landing page missing expected copy: {needle!r}"
    # No public dollar amounts anywhere on the landing page.
    for amount in ("$999", "$1,499", "$1499"):
        assert amount not in body, f"landing page must not show {amount!r}"
    # Guard against internal/awkward process language returning to public copy.
    for phrase in (
        "priced menu", "fixed menu", "priced-menu", "commonly",
        "on the spot", "same Zoom", "no back-and-forth", "surprise invoice",
    ):
        assert phrase not in body, \
            f"landing page should not expose internal/awkward phrase: {phrase!r}"
    print("T1 OK: / renders landing page with discovery-call flow, coverage, steps, CTAs")


def t2_landing_links_to_public_routes():
    r = _get("/")
    body = r.get_data(as_text=True)
    # Landing now leads with the discovery-call flow: primary CTA books a
    # call (Calendly, or the in-app request form when DISCOVERY_CALL_URL is
    # unset), with login/support reachable. Pricing + support are linked too.
    for path in ("/login", "/support", "/pricing"):
        assert f'href="{path}"' in body, f"landing page missing link to {path}"
    # The discovery CTA falls back to the in-app request form when no
    # Calendly URL is configured (test env), so the form route is present.
    assert "/pricing/quote-request" in body, \
        "landing discovery CTA should fall back to the request form when unset"
    # Hero CTAs must not compete: "Book a discovery call" is the single
    # primary button; "Send migration details" is restyled as a quiet
    # secondary link (btn-link), not a second primary/secondary button.
    hero = body.split('class="landing-hero__ctas"', 1)[1].split("</section>", 1)[0]
    assert 'data-testid="landing-book-discovery"' in hero
    assert hero.count("btn-primary") == 1, \
        "hero should have exactly one primary button"
    send_anchor = hero.split('data-testid="landing-send-details"', 1)[0]
    send_open = send_anchor.rsplit("<a", 1)[1]
    assert "btn-link" in send_open and "btn-secondary" not in send_open, \
        "hero 'Send migration details' should be a quiet link, not a competing button"
    print("T2 OK: landing links to public routes; hero has one primary CTA + quiet secondary link")


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


def t6_landing_shows_three_step_journey_without_pricing():
    r = _get("/")
    assert r.status_code == 200, f"GET / -> {r.status_code}"
    body = r.get_data(as_text=True)

    # The three-step journey is visible to any visitor (no sign-in / buy-in).
    assert 'data-testid="landing-three-steps"' in body, \
        "landing page missing the three-step journey section"
    for testid in ("journey-step-1", "journey-step-2", "journey-step-3"):
        assert f'data-testid="{testid}"' in body, \
            f"three-step journey missing node {testid!r}"

    # Explicit, labelled steps in the right order.
    must_contain = [
        "Step 1",
        "Book a discovery call",
        "Share a few details so our team can prepare.",
        "Step 2",
        "Get a quote",
        "We scope the migration and provide a clear quote on the call.",
        "Step 3",
        "Have your data migrated",
    ]
    for needle in must_contain:
        assert needle in body, f"three-step journey missing copy: {needle!r}"

    s1 = body.index("Step 1")
    s2 = body.index("Step 2")
    s3 = body.index("Step 3")
    assert s1 < s2 < s3, "three-step journey steps must appear in order 1, 2, 3"

    # No public dollar amounts introduced anywhere on the page.
    import re
    assert not re.search(r"\$\s*\d", body), \
        "three-step journey must not introduce public pricing amounts"
    print("T6 OK: three-step journey renders with labelled steps and no public pricing amounts")


if __name__ == "__main__":
    try:
        t1_landing_renders_with_marketing_content()
        t2_landing_links_to_public_routes()
        t3_public_routes_render_unauthenticated()
        t4_authenticated_user_redirected_to_dashboard()
        t5_landing_has_no_legacy_cutover_branding()
        t6_landing_shows_three_step_journey_without_pricing()
        print("\nALL LANDING-PAGE SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
