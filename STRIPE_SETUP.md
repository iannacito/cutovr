# Stripe Checkout setup

This app uses **server-side Stripe Checkout Sessions** to sell the three
fixed-price PCLaw → QuickBooks migration packages. The "Custom" tier is
quote-based and routes to `/support` instead of Stripe.

The integration is entirely env-var driven. No Stripe keys live in the
repo, and no Stripe key is ever rendered into the HTML — secret keys
are only used inside `stripe_checkout.py` on the server.

## Plans + Stripe prices

Create these as one-off (mode: payment) Prices in Stripe Dashboard →
Products. Use any product names you like; the Stripe **Price ID** is
the thing you wire in.

| Plan slug   | Public label              | Amount  | Env var                         |
|-------------|---------------------------|---------|---------------------------------|
| `essential` | Essential — Current Year  | $499    | `STRIPE_PRICE_ESSENTIAL`        |
| `standard`  | Standard — 3-Year History | $999    | `STRIPE_PRICE_STANDARD`         |
| `complete`  | Complete — 5-Year History | $1,999  | `STRIPE_PRICE_COMPLETE`         |

Optional add-ons (not yet exposed as standalone buttons; reserved for
future UI):

| Add-on               | Amount     | Env var                            |
|----------------------|------------|------------------------------------|
| Extra historical year| $250/year  | `STRIPE_PRICE_EXTRA_YEAR`          |
| Priority turnaround  | $299       | `STRIPE_PRICE_PRIORITY_TURNAROUND` |
| Assisted review call | $199       | `STRIPE_PRICE_ASSISTED_REVIEW`     |

## Required env vars

Set these in Render Dashboard → Settings → Environment (do **not**
commit them):

```
STRIPE_SECRET_KEY=sk_live_...          # or sk_test_... for staging
STRIPE_PRICE_ESSENTIAL=price_...
STRIPE_PRICE_STANDARD=price_...
STRIPE_PRICE_COMPLETE=price_...
```

Optional / future:

```
STRIPE_PRICE_EXTRA_YEAR=price_...
STRIPE_PRICE_PRIORITY_TURNAROUND=price_...
STRIPE_PRICE_ASSISTED_REVIEW=price_...
STRIPE_WEBHOOK_SECRET=whsec_...        # required if you set up a webhook
```

`PUBLIC_APP_URL` (already documented elsewhere) is reused to build the
success/cancel return URLs. If it's not set, the request's own host is
used as a fallback — fine for local dev but **set it in production**
so customers always return to the canonical domain.

## Behavior when env vars are missing

The pricing page never crashes. If `STRIPE_SECRET_KEY` or a plan's
price-ID env var is missing:

- The "Buy <plan>" button is replaced with the legacy
  "Start with <plan>" sign-up link, so the page is still useful.
- A small line of fine print shows
  "Online checkout is being set up — contact support to purchase today."
- `POST /pricing/checkout/<plan>` redirects back to `/pricing` with a
  flashed info message instead of returning 500.

This makes it safe to ship the UI changes before all Stripe products
exist, and safe to demo on staging without billing anyone.

## Routes

| Method | Path                              | Purpose                                        |
|--------|-----------------------------------|------------------------------------------------|
| GET    | `/pricing`                        | Public pricing page (cards + add-ons + FAQ).   |
| POST   | `/pricing/checkout/<plan>`        | Create Checkout Session, 303 to Stripe URL.    |
| GET    | `/pricing/checkout/success`       | Stripe `success_url` lands here.               |
| GET    | `/pricing/checkout/cancel`        | Stripe `cancel_url` lands here, back to /pricing. |

Only `essential`, `standard`, and `complete` are valid `<plan>` values
on the checkout POST. Anything else returns 404. The "Custom" tier
deliberately does **not** hit Stripe — it links to `/support` so the
team can quote it.

## Testing locally

```bash
export STRIPE_SECRET_KEY=sk_test_...
export STRIPE_PRICE_ESSENTIAL=price_test_essential
export STRIPE_PRICE_STANDARD=price_test_standard
export STRIPE_PRICE_COMPLETE=price_test_complete
python3 app.py
```

Then visit http://localhost:5000/pricing and click any "Buy …" button.
Stripe will redirect to a test-mode hosted checkout page. Use card
`4242 4242 4242 4242` with any future expiry to complete payment.

To run the smoke tests (no real Stripe traffic, all calls are mocked):

```bash
python3 tests/smoke_pricing_stripe.py
python3 tests/smoke_pricing_page.py
```

## Webhooks (optional, future)

`stripe_checkout.verify_webhook(payload, signature)` will verify a
Stripe webhook using `STRIPE_WEBHOOK_SECRET`. No webhook route is
mounted yet — when one is needed, mount it as a CSRF-exempt POST
endpoint and pass the raw request body + `Stripe-Signature` header
through `verify_webhook`. Never process the body without verifying.
