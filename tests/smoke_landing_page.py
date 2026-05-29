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
        # Hero / value prop
        "PCLaw",
        "QuickBooks Online",
        "under an hour",
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
        "fraction of the cost",
        # CTAs
        "Start your migration",
        "Sign up",
    ]
    for needle in must_contain:
        assert needle in body, f"landing page missing expected copy: {needle!r}"
    print("T1 OK: / renders landing page with hero, coverage, steps, compare, CTAs")


def t2_landing_links_to_public_routes():
    r = _get("/")
    body = r.get_data(as_text=True)
    for path in ("/signup", "/login", "/onboarding", "/support"):
        assert f'href="{path}"' in body, f"landing page missing link to {path}"
    print("T2 OK: landing links to /signup, /login, /onboarding, /support")


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


if __name__ == "__main__":
    try:
        t1_landing_renders_with_marketing_content()
        t2_landing_links_to_public_routes()
        t3_public_routes_render_unauthenticated()
        t4_authenticated_user_redirected_to_dashboard()
        t5_landing_has_no_legacy_cutover_branding()
        print("\nALL LANDING-PAGE SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
