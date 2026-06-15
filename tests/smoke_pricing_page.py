"""Smoke tests for the public pricing page (/pricing) and landing teaser.

Run from project root:

    python3 tests/smoke_pricing_page.py

The site no longer shows public dollar amounts. Pricing is scoped and quoted
on a discovery call, so these tests assert:
  T1 GET /pricing renders publicly (no auth) with the "quote on the discovery
     call" framing and the why-we-quote reasons, and NO dollar amounts.
  T2 The pricing page does NOT use firm-size / number-of-people wording.
  T3 The landing page (/) explains pricing comes from the discovery call and
     links to /pricing — with no dollar amounts.
  T4 /pricing is reachable for authenticated users too (it stays public).
  T5 The pricing page avoids accountant-jargon abbreviations (COA, GL, QBO).
  T6 The pricing page shows no public dollar amounts at all.
  T7 The pricing page surfaces a discovery-call CTA and Calendly pre-call
     form messaging.
"""

import os
import re
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
os.environ.setdefault("SECRET_KEY", "smoke-secret-pricing")

import app as appmod  # noqa: E402

# Dollar amounts that must never appear on public pages.
FORBIDDEN_AMOUNTS = ("$999", "$1,499", "$1499", "$250", "$299", "$199", "$499", "$1,999")


def _get(path, client=None):
    c = client or appmod.app.test_client()
    return c.get(path)


def t1_pricing_renders_with_quote_on_call_framing():
    r = _get("/pricing")
    assert r.status_code == 200, f"GET /pricing -> {r.status_code}"
    body = r.get_data(as_text=True)

    must_contain = [
        # The page explains pricing is given on the discovery call.
        "discovery call",
        "pricing-quote-on-call",
        # Why we scope/quote rather than list prices.
        "Report quality",
        "Migration history",
        "Trust accounting",
        "data complexity",
        # FAQ
        "How much does a migration cost?",
        "What happens on the discovery call?",
        "Does this include QuickBooks setup?",
    ]
    for needle in must_contain:
        assert needle in body, f"/pricing missing expected copy: {needle!r}"
    for amount in FORBIDDEN_AMOUNTS:
        assert amount not in body, f"/pricing must not show {amount!r}"
    print("T1 OK: /pricing renders the quote-on-discovery-call framing, no dollar amounts")


def t2_pricing_avoids_firm_size_language():
    r = _get("/pricing")
    body = r.get_data(as_text=True).lower()
    forbidden_substrings = [
        "number of lawyers",
        "per lawyer",
        "per user",
        "per seat",
        "per person",
        "number of people",
        "firm size",
        "size of your firm",
        "size of firm",
        "headcount",
        "head count",
        "how many lawyers",
        "how many people",
        "how many users",
    ]
    for phrase in forbidden_substrings:
        assert phrase not in body, f"/pricing contains firm-size wording: {phrase!r}"
    print("T2 OK: /pricing has no firm-size / per-person wording")


def t3_landing_explains_quote_from_call_and_links_to_pricing():
    r = _get("/")
    assert r.status_code == 200, f"GET / -> {r.status_code}"
    body = r.get_data(as_text=True)

    must_contain = [
        "Pricing",
        "discovery call",
        "landing-pricing-quote-note",
        'href="/pricing"',
    ]
    for needle in must_contain:
        assert needle in body, f"landing page missing expected pricing copy: {needle!r}"
    for amount in FORBIDDEN_AMOUNTS:
        assert amount not in body, f"landing page must not show {amount!r}"
    print("T3 OK: / explains the quote comes from the discovery call and links to /pricing")


def t4_pricing_reachable_for_authenticated_users():
    c = appmod.app.test_client()
    c.post(
        "/signup",
        data={
            "firm_name": "Pricing Smoke Firm",
            "email": "pricing-smoke@example.com",
            "password": "passw0rd!",
            "confirm_password": "passw0rd!",
        },
    )
    r = c.get("/pricing", follow_redirects=False)
    assert r.status_code == 200, (
        f"/pricing should stay public for authenticated users, got {r.status_code}"
    )
    body = r.get_data(as_text=True)
    assert "discovery call" in body, "/pricing for authed user missing quote-on-call content"
    print("T4 OK: /pricing is reachable while signed in")


def t5_pricing_avoids_jargon_abbreviations():
    r = _get("/pricing")
    body = r.get_data(as_text=True)
    visible = re.sub(r"<[^>]+>", " ", body)
    visible = re.sub(r"&[a-z]+;", " ", visible)
    forbidden_abbrevs = ["COA", "GL", "QBO"]
    for abbrev in forbidden_abbrevs:
        pattern = r"\b" + re.escape(abbrev) + r"\b"
        assert not re.search(pattern, visible), (
            f"/pricing uses accountant abbreviation {abbrev!r} in visible copy"
        )
    print("T5 OK: /pricing avoids COA/GL/QBO abbreviations in visible copy")


def t6_pricing_shows_no_public_dollar_amounts():
    r = _get("/pricing")
    body = r.get_data(as_text=True)
    # No bare dollar figure should appear anywhere in the page body.
    assert not re.search(r"\$\s?\d", body), (
        "/pricing must not surface any public dollar amount"
    )
    print("T6 OK: /pricing shows no public dollar amounts")


def t7_pricing_has_discovery_cta_and_precall_form_copy():
    r = _get("/pricing")
    body = r.get_data(as_text=True)
    assert "pricing-cta-book-discovery" in body, \
        "/pricing should have a 'Book a discovery call' CTA"
    assert "Book a discovery call" in body
    assert "Calendly" in body, "/pricing should mention the Calendly booking form"
    # The CTA falls back to the in-app request form when DISCOVERY_CALL_URL
    # is unset (test env), so the form route is present.
    assert "/pricing/quote-request" in body, \
        "/pricing discovery CTA should fall back to the request form when unset"
    print("T7 OK: /pricing has a discovery-call CTA and Calendly pre-call messaging")


if __name__ == "__main__":
    try:
        t1_pricing_renders_with_quote_on_call_framing()
        t2_pricing_avoids_firm_size_language()
        t3_landing_explains_quote_from_call_and_links_to_pricing()
        t4_pricing_reachable_for_authenticated_users()
        t5_pricing_avoids_jargon_abbreviations()
        t6_pricing_shows_no_public_dollar_amounts()
        t7_pricing_has_discovery_cta_and_precall_form_copy()
        print("\nALL PRICING-PAGE SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
