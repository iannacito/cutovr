"""Smoke tests: Stripe checkout backend retained, but absent from the
public pricing page.

Run from project root:

    python3 tests/smoke_pricing_stripe.py

The public customer journey is now consultative — pricing is scoped on a
discovery call, so /pricing no longer renders package cards or Stripe
checkout forms. The Stripe checkout *routes* are intentionally kept for a
future private / post-quote payment link, so this suite verifies the
backend still works while the public page stays free of it.

Covers:
  T1 The public /pricing page renders no Stripe checkout forms and no
     package cards, regardless of whether Stripe env vars are set.
  T2 The pricing page never leaks the Stripe secret key (or price IDs)
     into the rendered HTML, even when Stripe is configured.
  T3 The /pricing/checkout/<plan> route still exists and degrades
     gracefully (redirects back to /pricing, no 500) when Stripe is not
     configured.
  T4 With Stripe configured, POSTing /pricing/checkout/<plan> still
     creates a Checkout Session (mocked) and redirects to the Stripe URL
     — the route is retained for future private payment links.
  T5 POSTing to /pricing/checkout/<unknown> (and retired/quote-only
     slugs) returns 404.
  T6 The success and cancel landing routes both render without crashing.
"""

import os
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


def t1_public_pricing_has_no_checkout_forms_or_cards():
    """The consultative pricing page never exposes Stripe checkout — with
    or without Stripe env vars configured."""
    # Unconfigured.
    body = _client().get("/pricing").get_data(as_text=True)
    assert 'action="/pricing/checkout/' not in body, (
        "public /pricing must not render Stripe checkout forms"
    )
    assert 'class="pricing-tier' not in body, "public /pricing must not render package cards"

    # Configured — still no public checkout on the consultative page.
    with mock.patch.dict(
        os.environ,
        {
            "STRIPE_SECRET_KEY": "sk_test_dummy",
            "STRIPE_PRICE_ESSENTIAL": "price_essential_x",
            "STRIPE_PRICE_STANDARD": "price_standard_x",
        },
    ):
        body2 = _client().get("/pricing").get_data(as_text=True)
        assert 'action="/pricing/checkout/' not in body2, (
            "configuring Stripe must not resurface public checkout forms"
        )
    print("T1 OK: public /pricing has no checkout forms or package cards")


def t2_pricing_page_never_leaks_stripe_secret_key():
    sentinel = "sk_test_NEVER_LEAK_ME_1234567890"
    with mock.patch.dict(
        os.environ,
        {
            "STRIPE_SECRET_KEY": sentinel,
            "STRIPE_PRICE_ESSENTIAL": "price_essential_x",
            "STRIPE_PRICE_STANDARD": "price_standard_x",
        },
    ):
        body = _client().get("/pricing").get_data(as_text=True)
        assert sentinel not in body, "Stripe secret key leaked into /pricing HTML!"
        for pid in ("price_essential_x", "price_standard_x"):
            assert pid not in body, f"Stripe price id {pid!r} leaked into HTML"
    print("T2 OK: Stripe secret key + price IDs are never rendered in HTML")


def t3_checkout_route_graceful_when_stripe_not_configured():
    assert not stripe_checkout.stripe_enabled(), (
        "expected stripe_enabled() == False with no env vars"
    )
    # POSTing the retained route when unconfigured should NOT 500 — it
    # redirects back to /pricing with a flashed message.
    r = _client().post("/pricing/checkout/standard", follow_redirects=False)
    assert r.status_code in (302, 303), (
        f"unconfigured checkout should redirect, got {r.status_code}"
    )
    assert "/pricing" in r.headers.get("Location", ""), (
        f"redirect should land on /pricing, got {r.headers.get('Location')!r}"
    )
    print("T3 OK: checkout route degrades gracefully when Stripe is unconfigured")


def t4_checkout_route_creates_session_when_configured():
    """The backend route is retained for future private payment links: with
    Stripe configured, it still creates a session and redirects."""
    with mock.patch.dict(
        os.environ,
        {
            "STRIPE_SECRET_KEY": "sk_test_dummy",
            "STRIPE_PRICE_ESSENTIAL": "price_essential_x",
            "STRIPE_PRICE_STANDARD": "price_standard_x",
        },
    ):
        fake_url = "https://checkout.stripe.com/c/pay/cs_test_FAKEFAKEFAKE"
        with mock.patch.object(
            stripe_checkout, "create_checkout_session", return_value=fake_url,
        ) as m:
            r = _client().post("/pricing/checkout/standard", follow_redirects=False)
            assert r.status_code in (302, 303), (
                f"checkout should redirect to Stripe URL, got {r.status_code}"
            )
            assert r.headers.get("Location") == fake_url, (
                "expected redirect Location to be the Stripe-hosted URL"
            )
            m.assert_called_once()
            args, kwargs = m.call_args
            assert args[0] == "standard", f"plan should be 'standard', got {args!r}"
            assert "base_url" in kwargs, "base_url kwarg missing"
    print("T4 OK: retained checkout route still creates a Stripe session when configured")


def t5_unknown_plan_is_404():
    with mock.patch.dict(
        os.environ,
        {
            "STRIPE_SECRET_KEY": "sk_test_dummy",
            "STRIPE_PRICE_ESSENTIAL": "price_essential_x",
            "STRIPE_PRICE_STANDARD": "price_standard_x",
        },
    ):
        r = _client().post("/pricing/checkout/lifetime", follow_redirects=False)
        assert r.status_code == 404, f"unknown plan slug should 404, got {r.status_code}"
        for retired_slug in ("custom", "complete", "five_year"):
            r2 = _client().post(
                f"/pricing/checkout/{retired_slug}", follow_redirects=False
            )
            assert r2.status_code == 404, (
                f"quote-only/retired slug {retired_slug!r} should 404, got {r2.status_code}"
            )
    print("T5 OK: unknown / quote-only / retired plan slugs return 404")


def t6_success_and_cancel_routes_render():
    r1 = _client().get("/pricing/checkout/success?session_id=cs_test_smoke")
    assert r1.status_code == 200, f"success route should render, got {r1.status_code}"
    body1 = r1.get_data(as_text=True)
    assert "Payment received" in body1, "success page missing confirmation copy"
    assert "cs_test_smoke" in body1

    r2 = _client().get("/pricing/checkout/cancel", follow_redirects=False)
    assert r2.status_code in (302, 303), (
        f"cancel should redirect back to /pricing, got {r2.status_code}"
    )
    assert "/pricing" in r2.headers.get("Location", "")
    print("T6 OK: success page renders; cancel redirects back to /pricing")


if __name__ == "__main__":
    try:
        t1_public_pricing_has_no_checkout_forms_or_cards()
        t2_pricing_page_never_leaks_stripe_secret_key()
        t3_checkout_route_graceful_when_stripe_not_configured()
        t4_checkout_route_creates_session_when_configured()
        t5_unknown_plan_is_404()
        t6_success_and_cancel_routes_render()
        print("\nALL PRICING + STRIPE SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
