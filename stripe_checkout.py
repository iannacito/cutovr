"""Stripe Checkout integration for the /pricing page.

The pricing model is history-based, not firm-size-based:

  Essential        $999    Current Year
  Standard         $1,499  Up to 3 Years  (most common)
  Complete                 3+ years of history; quote-based
                           (NOT Stripe; handled via /support)

Add-ons:
  Extra historical year     $250
  Priority turnaround       $299
  Assisted review call      $199

Stripe is configured purely through environment variables — there are no
secrets in the repo and no Stripe keys leak into the rendered HTML. The
secret key is only read inside this module and only used in server-side
requests to api.stripe.com.

If Stripe is not configured (e.g. local dev, staging without Stripe yet),
``is_configured()`` returns ``False`` for the affected plans and the UI
falls back to a "Online checkout is being set up. Contact support to
purchase." disabled state. The /pricing page itself never crashes.

This module deliberately uses lazy imports for ``stripe`` so that the
rest of the app keeps booting even if the ``stripe`` Python package is
not installed (e.g. running tests against an older requirements.txt).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Plan registry
# ---------------------------------------------------------------------------

# Keys here are the URL-safe plan slugs used in /pricing/checkout/<plan>.
# The env var is the name of the Stripe Price ID for that line item.
#
# Complete (3+ years of history) is intentionally absent:
# it's quote-based, so the UI links to /support instead of hitting Stripe.
PLAN_ENV_VARS = {
    "essential": "STRIPE_PRICE_ESSENTIAL",
    "standard": "STRIPE_PRICE_STANDARD",
    # Optional add-ons. These are usable on their own as well as alongside
    # a base plan; for the minimal first-cut UI we surface base plans only.
    "extra_year": "STRIPE_PRICE_EXTRA_YEAR",
    "priority_turnaround": "STRIPE_PRICE_PRIORITY_TURNAROUND",
    "assisted_review": "STRIPE_PRICE_ASSISTED_REVIEW",
}

# Plan slugs that show a "Buy now" Stripe button on the pricing page.
# Complete is excluded — it routes to /support for a quote.
BASE_PLANS = ("essential", "standard")


@dataclass(frozen=True)
class PlanConfig:
    slug: str
    price_id: Optional[str]

    @property
    def is_configured(self) -> bool:
        return bool(self.price_id)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _stripe_secret_key() -> str:
    return (os.environ.get("STRIPE_SECRET_KEY") or "").strip()


def _price_id_for(plan: str) -> str:
    env = PLAN_ENV_VARS.get(plan)
    if not env:
        return ""
    return (os.environ.get(env) or "").strip()


def stripe_enabled() -> bool:
    """True iff a Stripe secret key is configured at all.

    A False return means the entire integration is off; the UI should
    show the "checkout is being set up" message for every paid plan.
    """
    return bool(_stripe_secret_key())


def plan_configured(plan: str) -> bool:
    """True iff this specific plan has both a secret key and a price ID."""
    return stripe_enabled() and bool(_price_id_for(plan))


def plan_configs() -> dict[str, PlanConfig]:
    """Return a snapshot of plan configuration for templates.

    Templates use this to decide whether to render a checkout button or
    a friendly disabled state. The secret key is NEVER included here.
    """
    enabled = stripe_enabled()
    out: dict[str, PlanConfig] = {}
    for slug in BASE_PLANS:
        price_id = _price_id_for(slug) if enabled else ""
        out[slug] = PlanConfig(slug=slug, price_id=price_id or None)
    return out


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------

class StripeNotConfigured(Exception):
    """Raised when the caller tried to start checkout for a plan that
    Stripe isn't configured for."""


class StripeUnknownPlan(Exception):
    """Raised when the caller asked for a plan slug we don't recognize."""


def _build_default_urls(base_url: str) -> tuple[str, str]:
    base_url = (base_url or "").rstrip("/")
    success = f"{base_url}/pricing/checkout/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel = f"{base_url}/pricing/checkout/cancel"
    return success, cancel


def create_checkout_session(
    plan: str,
    base_url: str,
    customer_email: Optional[str] = None,
):
    """Create a Stripe Checkout Session for a given plan slug.

    Returns the URL the browser should be redirected to (Stripe-hosted).

    Raises:
        StripeUnknownPlan: plan slug isn't in the registry.
        StripeNotConfigured: Stripe key or price ID for the plan is missing.
        RuntimeError: the ``stripe`` Python package is not installed.
    """
    if plan not in PLAN_ENV_VARS:
        raise StripeUnknownPlan(plan)
    secret = _stripe_secret_key()
    if not secret:
        raise StripeNotConfigured("STRIPE_SECRET_KEY is not set")
    price_id = _price_id_for(plan)
    if not price_id:
        raise StripeNotConfigured(
            f"{PLAN_ENV_VARS[plan]} is not set; cannot start checkout for {plan!r}"
        )

    try:
        import stripe  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "The 'stripe' Python package is not installed. "
            "Add it to requirements.txt."
        ) from exc

    stripe.api_key = secret
    success_url, cancel_url = _build_default_urls(base_url)
    kwargs = {
        "mode": "payment",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "allow_promotion_codes": True,
        "metadata": {"plan": plan},
    }
    if customer_email:
        kwargs["customer_email"] = customer_email
    csession = stripe.checkout.Session.create(**kwargs)
    # ``url`` is the Stripe-hosted checkout page.
    return csession.url


# ---------------------------------------------------------------------------
# Webhook helper (optional / future use)
# ---------------------------------------------------------------------------

def verify_webhook(payload: bytes, signature: str):
    """Verify a Stripe webhook signature using STRIPE_WEBHOOK_SECRET.

    Returns the parsed event dict on success. Raises on failure.

    We never process webhooks without a verified signature — there's no
    "trust the body" fallback. Callers can ignore unknown event types.
    """
    secret = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()
    if not secret:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not set")
    try:
        import stripe  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The 'stripe' Python package is not installed."
        ) from exc
    return stripe.Webhook.construct_event(payload, signature, secret)
