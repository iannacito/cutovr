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

# Keys here are the URL-safe plan slugs used in /pricing/checkout/<plan> and
# in the package-first onboarding flow (Step 1 plan keys map 1:1 to these).
# Each plan resolves its Stripe Price ID from the FIRST environment variable
# in its tuple that is set, so deployments can use either the history-based
# names (preferred, self-documenting) or the original Essential/Standard
# names without any re-config:
#
#   essential (Current year, $999)
#       STRIPE_PRICE_CURRENT_YEAR  ->  STRIPE_PRICE_ESSENTIAL
#   standard  (Up to three years, $1,499)
#       STRIPE_PRICE_UP_TO_THREE_YEARS  ->  STRIPE_PRICE_STANDARD
#
# Complete (3+ years of history) is intentionally absent: it's quote-based,
# so the UI routes it to /support instead of hitting Stripe.
PLAN_ENV_VARS = {
    "essential": ("STRIPE_PRICE_CURRENT_YEAR", "STRIPE_PRICE_ESSENTIAL"),
    "standard": ("STRIPE_PRICE_UP_TO_THREE_YEARS", "STRIPE_PRICE_STANDARD"),
    # Optional add-ons. These are usable on their own as well as alongside
    # a base plan; for the minimal first-cut UI we surface base plans only.
    "extra_year": ("STRIPE_PRICE_EXTRA_YEAR",),
    "priority_turnaround": ("STRIPE_PRICE_PRIORITY_TURNAROUND",),
    "assisted_review": ("STRIPE_PRICE_ASSISTED_REVIEW",),
}

# Plan slugs that show a "Buy now" Stripe button on the pricing page and are
# the paid options in the onboarding flow. Complete is excluded — it routes
# to /support for a quote.
BASE_PLANS = ("essential", "standard")

# Known fixed amounts (in cents) for the base plans, used for receipts and the
# internal notification email. These mirror the public /pricing cards. The
# Stripe Price is still the source of truth for what's actually charged; this
# is only display/record metadata when we can't (or don't) call back to Stripe.
PLAN_AMOUNT_CENTS = {
    "essential": 99900,
    "standard": 149900,
}


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
    """Resolve a plan's Stripe Price ID from its env-var aliases.

    Returns the value of the first env var in the plan's tuple that is set
    (e.g. STRIPE_PRICE_CURRENT_YEAR, falling back to STRIPE_PRICE_ESSENTIAL),
    or "" if none are configured.
    """
    names = PLAN_ENV_VARS.get(plan)
    if not names:
        return ""
    for name in names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return ""


def plan_amount_cents(plan: str) -> Optional[int]:
    """Best-effort fixed price (in cents) for a base plan, else None."""
    return PLAN_AMOUNT_CENTS.get((plan or "").strip().lower())


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
    *,
    success_url: Optional[str] = None,
    cancel_url: Optional[str] = None,
    metadata: Optional[dict] = None,
    client_reference_id: Optional[str] = None,
    return_session: bool = False,
):
    """Create a Stripe Checkout Session for a given plan slug.

    By default returns the Stripe-hosted URL the browser should be redirected
    to. Pass ``return_session=True`` to get the full Session object instead
    (the onboarding flow needs its ``id`` to link the record so the webhook
    can find it later).

    ``success_url`` / ``cancel_url`` override the /pricing defaults so the
    onboarding flow can return the customer to its own Step 3 / Step 2.
    ``metadata`` is merged with ``{"plan": plan}`` and is the durable link
    back to the onboarding record (e.g. the intake id + reference).
    ``client_reference_id`` is echoed back on the session/webhook event.

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
        names = "/".join(PLAN_ENV_VARS[plan])
        raise StripeNotConfigured(
            f"{names} is not set; cannot start checkout for {plan!r}"
        )

    try:
        import stripe  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "The 'stripe' Python package is not installed. "
            "Add it to requirements.txt."
        ) from exc

    stripe.api_key = secret
    default_success, default_cancel = _build_default_urls(base_url)
    merged_metadata = {"plan": plan}
    if metadata:
        # Stripe metadata values must be strings; coerce defensively.
        merged_metadata.update({k: str(v) for k, v in metadata.items() if v is not None})
    kwargs = {
        "mode": "payment",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url or default_success,
        "cancel_url": cancel_url or default_cancel,
        "allow_promotion_codes": True,
        "metadata": merged_metadata,
    }
    if customer_email:
        kwargs["customer_email"] = customer_email
    if client_reference_id:
        kwargs["client_reference_id"] = str(client_reference_id)
    csession = stripe.checkout.Session.create(**kwargs)
    if return_session:
        return csession
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
