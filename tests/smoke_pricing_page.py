"""Smoke tests for the public pricing page (/pricing) and landing teaser.

Run from project root:

    python3 tests/smoke_pricing_page.py

Covers:
  T1 GET /pricing renders publicly (no auth) and includes the four
     packages, dollar amounts, and the FAQ headings.
  T2 The pricing page does NOT use firm-size / number-of-people wording.
  T3 The landing page (/) includes a short pricing teaser with the three
     headline tiers and a link to /pricing.
  T4 /pricing is reachable for authenticated users too (it stays public,
     and authenticated users should not be redirected away from it).
  T5 The pricing page avoids accountant-jargon abbreviations (COA, GL,
     QBO) in customer-facing copy.
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


def _get(path, client=None):
    c = client or appmod.app.test_client()
    return c.get(path)


def t1_pricing_renders_with_packages_and_faq():
    r = _get("/pricing")
    assert r.status_code == 200, f"GET /pricing -> {r.status_code}"
    body = r.get_data(as_text=True)

    must_contain = [
        # Package names
        "Essential",
        "Standard",
        "Complete",
        "Custom",
        # History framing
        "Current Year",
        "3-Year History",
        "5-Year History",
        # Prices
        "$499",
        "$999",
        "$1,999",
        # Quote-based tier signal
        "Quote",
        # "Most common" badge for default tier
        "Most common",
        # Add-ons
        "Extra historical year",
        "Priority turnaround",
        "Assisted review call",
        "$250",
        "$299",
        "$199",
        # FAQ questions (exact phrasings from the requirements)
        "What if I am not sure how far back to go?",
        "Do I need to know which reports to upload?",
        "Does this include QuickBooks setup?",
        "What happens after I pay?",
    ]
    for needle in must_contain:
        assert needle in body, f"/pricing missing expected copy: {needle!r}"
    print("T1 OK: /pricing renders packages, prices, add-ons, and FAQ")


def t2_pricing_avoids_firm_size_language():
    r = _get("/pricing")
    body = r.get_data(as_text=True).lower()
    # Pricing is explicitly NOT based on firm size. Guard against drift.
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


def t3_landing_has_pricing_section_linking_to_pricing_page():
    r = _get("/")
    assert r.status_code == 200, f"GET / -> {r.status_code}"
    body = r.get_data(as_text=True)

    must_contain = [
        # Section eyebrow + headline cues
        "Pricing",
        "history",
        # Three teaser cards (names + prices)
        "Essential",
        "Standard",
        "Complete",
        "$499",
        "$999",
        "$1,999",
        # Recommended badge on the teaser too
        "Most common",
        # Link to full pricing page
        'href="/pricing"',
    ]
    for needle in must_contain:
        assert needle in body, f"landing page missing expected pricing teaser: {needle!r}"
    print("T3 OK: / has pricing teaser with three tiers and link to /pricing")


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
    assert "$999" in body, "/pricing for authed user missing tier content"
    print("T4 OK: /pricing is reachable while signed in")


def t5_pricing_avoids_jargon_abbreviations():
    """Audience is lawyers, not accountants — the pricing page should not
    rely on COA / GL / QBO abbreviations."""
    r = _get("/pricing")
    body = r.get_data(as_text=True)

    # Bound the check to visible text (drop HTML tags) so we don't trip on
    # things like CSS class names or aria attributes.
    visible = re.sub(r"<[^>]+>", " ", body)
    visible = re.sub(r"&[a-z]+;", " ", visible)

    # Match abbreviations only as whole words so we don't false-positive on
    # words that happen to contain those letters.
    forbidden_abbrevs = ["COA", "GL", "QBO"]
    for abbrev in forbidden_abbrevs:
        pattern = r"\b" + re.escape(abbrev) + r"\b"
        assert not re.search(pattern, visible), (
            f"/pricing uses accountant abbreviation {abbrev!r} in visible copy"
        )
    print("T5 OK: /pricing avoids COA/GL/QBO abbreviations in visible copy")


if __name__ == "__main__":
    try:
        t1_pricing_renders_with_packages_and_faq()
        t2_pricing_avoids_firm_size_language()
        t3_landing_has_pricing_section_linking_to_pricing_page()
        t4_pricing_reachable_for_authenticated_users()
        t5_pricing_avoids_jargon_abbreviations()
        print("\nALL PRICING-PAGE SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
