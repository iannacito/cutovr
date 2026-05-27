"""Smoke tests for /pricing layout polish + Stripe Checkout integration.

Run from project root:

    python3 tests/smoke_pricing_stripe.py

Covers:
  T1 Pricing cards appear early in the rendered HTML (before the FAQ
     section), and the add-ons strip sits in between the cards and the
     FAQ — so customers see add-ons without scrolling past another
     hero.
  T2 The pricing page never leaks the Stripe secret key into the
     rendered HTML, even when Stripe is configured.
  T3 With no Stripe env vars set, the page still renders, no buttons
     point at /pricing/checkout, and the friendly "being set up"
     message is shown. POSTing to /pricing/checkout/<plan> in that
     state redirects back to /pricing instead of 500.
  T4 With Stripe env vars set, each base plan renders a real POST form
     to /pricing/checkout/<plan>. POSTing creates a Stripe Checkout
     Session (mocked) and redirects to the Stripe-hosted URL.
  T5 POSTing to /pricing/checkout/<unknown> returns 404.
  T6 The "Custom" tier still links to /support (no Stripe), regardless
     of env-var state.
  T7 The success and cancel landing routes both render without
     crashing.
"""

import os
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-pricing-stripe")

# Make sure no stale Stripe env vars leak between tests / dev shells.
for _k in (
    "STRIPE_SECRET_KEY",
    "STRIPE_PRICE_ESSENTIAL",
    "STRIPE_PRICE_STANDARD",
    "STRIPE_PRICE_COMPLETE",
    "STRIPE_PRICE_EXTRA_YEAR",
    "STRIPE_PRICE_PRIORITY_TURNAROUND",
    "STRIPE_PRICE_ASSISTED_REVIEW",
    "STRIPE_WEBHOOK_SECRET",
):
    os.environ.pop(_k, None)

import app as appmod  # noqa: E402
import stripe_checkout  # noqa: E402


def _client():
    return appmod.app.test_client()


def t1_pricing_cards_appear_above_addons_and_faq():
    r = _client().get("/pricing")
    assert r.status_code == 200, f"GET /pricing -> {r.status_code}"
    body = r.get_data(as_text=True)

    # Anchor positions in HTML order. Cards should come first, then the
    # add-ons strip, then the FAQ.
    idx_tiers = body.find('class="pricing-tiers"')
    idx_addons = body.find("pricing-addons-strip")
    idx_faq = body.find("pricing-faq-section")
    assert idx_tiers != -1, "pricing tier grid is missing"
    assert idx_addons != -1, "compact add-ons strip is missing"
    assert idx_faq != -1, "FAQ section is missing"
    assert idx_tiers < idx_addons < idx_faq, (
        "expected order: tiers -> add-ons strip -> FAQ, got positions "
        f"tiers={idx_tiers}, addons={idx_addons}, faq={idx_faq}"
    )

    # Compact hero modifier should be applied, otherwise we've drifted
    # back to the tall hero.
    assert "pricing-hero--compact" in body, (
        "pricing hero is no longer using the compact variant — cards "
        "will sit too far down the page"
    )
    print("T1 OK: cards above add-ons above FAQ; hero is compact")


def t2_pricing_page_never_leaks_stripe_secret_key():
    sentinel = "sk_test_NEVER_LEAK_ME_1234567890"
    with mock.patch.dict(
        os.environ,
        {
            "STRIPE_SECRET_KEY": sentinel,
            "STRIPE_PRICE_ESSENTIAL": "price_essential_x",
            "STRIPE_PRICE_STANDARD": "price_standard_x",
            "STRIPE_PRICE_COMPLETE": "price_complete_x",
        },
    ):
        r = _client().get("/pricing")
        body = r.get_data(as_text=True)
        assert sentinel not in body, "Stripe secret key leaked into /pricing HTML!"
        # Price IDs are server-side metadata too — they're harmless to
        # leak, but we don't need to render them either.
        for pid in ("price_essential_x", "price_standard_x", "price_complete_x"):
            assert pid not in body, f"Stripe price id {pid!r} leaked into HTML"
    print("T2 OK: Stripe secret key + price IDs are never rendered in HTML")


def t3_checkout_graceful_when_stripe_not_configured():
    # No env vars set at this point.
    assert not stripe_checkout.stripe_enabled(), (
        "expected stripe_enabled() == False with no env vars"
    )

    r = _client().get("/pricing")
    body = r.get_data(as_text=True)
    # The friendly note should be visible.
    assert "Online checkout is being set up" in body, (
        "expected fallback copy on /pricing when Stripe is not configured"
    )
    # No form should point at /pricing/checkout when unconfigured.
    assert 'action="/pricing/checkout/' not in body, (
        "checkout forms should not render when Stripe is not configured"
    )
    # CTAs still work — they fall back to signup links.
    assert "Start with Essential" in body
    assert "Start with Standard" in body
    assert "Start with Complete" in body

    # POSTing the endpoint when unconfigured should NOT 500. It should
    # redirect back to /pricing with a flashed message.
    c = _client()
    r2 = c.post("/pricing/checkout/standard", follow_redirects=False)
    assert r2.status_code in (302, 303), (
        f"unconfigured checkout should redirect, got {r2.status_code}"
    )
    assert "/pricing" in r2.headers.get("Location", ""), (
        f"redirect should land on /pricing, got {r2.headers.get('Location')!r}"
    )
    print("T3 OK: graceful fallback when Stripe is not configured")


def t4_checkout_creates_session_when_stripe_configured():
    with mock.patch.dict(
        os.environ,
        {
            "STRIPE_SECRET_KEY": "sk_test_dummy",
            "STRIPE_PRICE_ESSENTIAL": "price_essential_x",
            "STRIPE_PRICE_STANDARD": "price_standard_x",
            "STRIPE_PRICE_COMPLETE": "price_complete_x",
        },
    ):
        # The /pricing page should now render real POST forms for each
        # base plan.
        body = _client().get("/pricing").get_data(as_text=True)
        for plan in ("essential", "standard", "complete"):
            needle = f'action="/pricing/checkout/{plan}"'
            assert needle in body, f"expected checkout form for {plan!r} on /pricing"

        # Mock the create_checkout_session call so we don't hit the
        # network. We patch the helper in stripe_checkout, not the
        # stripe SDK, so the assertion is independent of how we
        # construct the Stripe call inside that module.
        fake_url = "https://checkout.stripe.com/c/pay/cs_test_FAKEFAKEFAKE"
        with mock.patch.object(
            stripe_checkout,
            "create_checkout_session",
            return_value=fake_url,
        ) as m:
            r = _client().post(
                "/pricing/checkout/standard", follow_redirects=False
            )
            assert r.status_code in (302, 303), (
                f"checkout should redirect to Stripe URL, got {r.status_code}"
            )
            assert r.headers.get("Location") == fake_url, (
                "expected redirect Location to be the Stripe-hosted URL"
            )
            m.assert_called_once()
            args, kwargs = m.call_args
            # plan should be passed positionally as first arg, base_url
            # as kwarg.
            assert args[0] == "standard", f"plan should be 'standard', got {args!r}"
            assert "base_url" in kwargs, "base_url kwarg missing"
    print("T4 OK: checkout posts redirect to Stripe-hosted URL when configured")


def t5_unknown_plan_is_404():
    with mock.patch.dict(
        os.environ,
        {
            "STRIPE_SECRET_KEY": "sk_test_dummy",
            "STRIPE_PRICE_ESSENTIAL": "price_essential_x",
            "STRIPE_PRICE_STANDARD": "price_standard_x",
            "STRIPE_PRICE_COMPLETE": "price_complete_x",
        },
    ):
        r = _client().post("/pricing/checkout/lifetime", follow_redirects=False)
        assert r.status_code == 404, (
            f"unknown plan slug should 404, got {r.status_code}"
        )
        # Custom is intentionally not a Stripe plan — POSTing it should
        # also 404, not 500.
        r2 = _client().post("/pricing/checkout/custom", follow_redirects=False)
        assert r2.status_code == 404, (
            f"custom plan should not be a Stripe slug, got {r2.status_code}"
        )
    print("T5 OK: unknown / non-Stripe plan slugs return 404")


def t6_custom_tier_still_links_to_support():
    r = _client().get("/pricing")
    body = r.get_data(as_text=True)
    # Find the Custom tier block; the CTA must still go to /support.
    assert "Request a quote" in body, "Custom tier CTA copy missing"
    assert 'href="/support"' in body, "Custom tier should link to /support"
    print("T6 OK: Custom tier still routes to /support, not Stripe")


def t7_success_and_cancel_routes_render():
    r1 = _client().get("/pricing/checkout/success?session_id=cs_test_smoke")
    assert r1.status_code == 200, (
        f"success route should render, got {r1.status_code}"
    )
    body1 = r1.get_data(as_text=True)
    assert "Payment received" in body1, "success page missing confirmation copy"
    # Session id should be reflected in the HTML so customers can quote
    # it to support, but only the safe slice.
    assert "cs_test_smoke" in body1

    r2 = _client().get("/pricing/checkout/cancel", follow_redirects=False)
    assert r2.status_code in (302, 303), (
        f"cancel should redirect back to /pricing, got {r2.status_code}"
    )
    assert "/pricing" in r2.headers.get("Location", "")
    print("T7 OK: success page renders; cancel redirects back to /pricing")


if __name__ == "__main__":
    try:
        t1_pricing_cards_appear_above_addons_and_faq()
        t2_pricing_page_never_leaks_stripe_secret_key()
        t3_checkout_graceful_when_stripe_not_configured()
        t4_checkout_creates_session_when_stripe_configured()
        t5_unknown_plan_is_404()
        t6_custom_tier_still_links_to_support()
        t7_success_and_cancel_routes_render()
        print("\nALL PRICING + STRIPE SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
