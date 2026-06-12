"""Smoke tests for the public pricing page (/pricing).

Run from project root:

    python3 tests/smoke_pricing_page.py

Pricing is now consultative: we scope each firm's migration on a discovery
call and give a clear price afterward, instead of publishing fixed package
prices. These tests cover:

  T1 GET /pricing renders publicly (no auth) and explains that pricing is
     scoped after a discovery call, with the dual CTAs (book a call /
     submit migration details).
  T2 The pricing page does NOT show public package prices ($999/$1,499/etc),
     package names, or "Custom"/history-tier framing.
  T3 The pricing page does NOT use firm-size / number-of-people wording.
  T4 /pricing stays public for authenticated users too.
  T5 The pricing page avoids accountant-jargon abbreviations (COA, GL, QBO)
     in customer-facing copy.
  T6 The pricing CTAs route to the discovery flow, never a Stripe checkout
     or the retired package-selection workflow.
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


def t1_pricing_explains_scoped_pricing_with_ctas():
    r = _get("/pricing")
    assert r.status_code == 200, f"GET /pricing -> {r.status_code}"
    body = r.get_data(as_text=True)

    must_contain = [
        # Scoped-after-discovery framing.
        "scoped",
        "discovery call",
        "clear",
        "price",
        # The reasons pricing varies (history, report quality, trust, scope).
        "history",
        "trust",
        # Dual CTAs.
        "Book a discovery call",
        "Submit migration details",
        # Reassuring, not "unavailable".
        "before any work begins",
    ]
    for needle in must_contain:
        assert needle in body, f"/pricing missing expected copy: {needle!r}"
    print("T1 OK: /pricing explains scoped pricing + discovery CTAs")


def t2_pricing_has_no_public_prices_or_package_cards():
    r = _get("/pricing")
    body = r.get_data(as_text=True)
    visible = re.sub(r"<[^>]+>", " ", body)

    # No public self-serve amounts.
    for amount in ("$999", "$1,499", "$1,999", "$499", "$250", "$299", "$199"):
        assert amount not in body, f"/pricing still shows public price {amount}"

    # No package names / history-tier framing / Custom column.
    for label in ("Essential", "Standard", "Complete", "Current Year",
                  "Up to 3 Years", "Most common"):
        assert label not in body, f"/pricing still surfaces package label {label!r}"
    assert not re.search(r"\bCustom\b", visible), "/pricing still shows a Custom tier"

    # No leftover pricing-tier cards.
    assert 'class="pricing-tier' not in body, "/pricing still renders package cards"
    print("T2 OK: /pricing has no public prices, package cards, or tier labels")


def t3_pricing_avoids_firm_size_language():
    r = _get("/pricing")
    body = r.get_data(as_text=True).lower()
    forbidden_substrings = [
        "number of lawyers", "per lawyer", "per user", "per seat",
        "per person", "number of people", "firm size", "size of your firm",
        "size of firm", "headcount", "head count", "how many lawyers",
        "how many people", "how many users",
    ]
    for phrase in forbidden_substrings:
        assert phrase not in body, f"/pricing contains firm-size wording: {phrase!r}"
    print("T3 OK: /pricing has no firm-size / per-person wording")


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
    assert "discovery call" in body, "/pricing for authed user missing scoped content"
    print("T4 OK: /pricing is reachable while signed in")


def t5_pricing_avoids_jargon_abbreviations():
    r = _get("/pricing")
    body = r.get_data(as_text=True)
    visible = re.sub(r"<[^>]+>", " ", body)
    visible = re.sub(r"&[a-z]+;", " ", visible)
    for abbrev in ("COA", "GL", "QBO"):
        pattern = r"\b" + re.escape(abbrev) + r"\b"
        assert not re.search(pattern, visible), (
            f"/pricing uses accountant abbreviation {abbrev!r} in visible copy"
        )
    print("T5 OK: /pricing avoids COA/GL/QBO abbreviations in visible copy")


def t6_pricing_ctas_route_to_discovery_not_stripe():
    r = _get("/pricing")
    body = r.get_data(as_text=True)
    # No public Stripe checkout forms, no retired package picker.
    assert "/pricing/checkout" not in body, "/pricing must not expose Stripe checkout"
    assert "onboarding_step1" not in body
    # The "submit details" CTA points at the public request form.
    assert 'href="/onboarding/start"' in body, "/pricing missing request-form CTA"
    print("T6 OK: /pricing CTAs route to the discovery flow, not Stripe/packages")


if __name__ == "__main__":
    try:
        t1_pricing_explains_scoped_pricing_with_ctas()
        t2_pricing_has_no_public_prices_or_package_cards()
        t3_pricing_avoids_firm_size_language()
        t4_pricing_reachable_for_authenticated_users()
        t5_pricing_avoids_jargon_abbreviations()
        t6_pricing_ctas_route_to_discovery_not_stripe()
        print("\nALL PRICING-PAGE SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
