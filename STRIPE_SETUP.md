# Stripe Checkout setup

This app uses **server-side Stripe Checkout Sessions** to sell the two
fixed-price PCLaw → QuickBooks migration packages. The "Complete" tier
(3+ years of history) is quote-based and routes to `/support`
instead of Stripe.

The integration is entirely env-var driven. No Stripe keys live in the
repo, and no Stripe key is ever rendered into the HTML — secret keys
are only used inside `stripe_checkout.py` on the server.

## Plans + Stripe prices

Create these as one-off (mode: payment) Prices in Stripe Dashboard →
Products. Use any product names you like; the Stripe **Price ID** is
the thing you wire in.

| Plan slug   | Public label              | Amount  | Env var (preferred)             | Env var (legacy alias)   |
|-------------|---------------------------|---------|---------------------------------|--------------------------|
| `essential` | Current Year              | $999    | `STRIPE_PRICE_CURRENT_YEAR`     | `STRIPE_PRICE_ESSENTIAL` |
| `standard`  | Up to 3 Years             | $1,499  | `STRIPE_PRICE_UP_TO_THREE_YEARS`| `STRIPE_PRICE_STANDARD`  |

Either env var name works for each plan — the app reads the preferred
name first and falls back to the legacy alias, so existing Render
configs keep working without change. Set only one per plan.

The **Complete** tier (3+ years of history) is quote-based
and does **not** go through Stripe Checkout. On the pricing page its
CTA routes to `/support`; in the onboarding flow it routes to
`/onboarding/quote` so the team can follow up with a tailored quote.

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
STRIPE_SECRET_KEY=sk_live_...              # or sk_test_... for staging
STRIPE_PRICE_CURRENT_YEAR=price_...        # alias: STRIPE_PRICE_ESSENTIAL
STRIPE_PRICE_UP_TO_THREE_YEARS=price_...   # alias: STRIPE_PRICE_STANDARD
STRIPE_WEBHOOK_SECRET=whsec_...            # required for the onboarding webhook
```

### Email (confirmation + internal notification)

The onboarding flow sends a customer confirmation and an internal
notification after the customer uploads their reports on Step 3. It
reuses the existing SMTP helper (`email_sender.py`) — no new provider.
Emails are best-effort: if SMTP isn't configured the flow still works
and never claims an email was sent.

```
SMTP_HOST=...            # alias: MAIL_SERVER
SMTP_USER=...            # alias: SMTP_USERNAME / MAIL_USERNAME
SMTP_PASSWORD=...        # alias: MAIL_PASSWORD
SMTP_FROM=...            # alias: SMTP_FROM_EMAIL / MAIL_DEFAULT_SENDER
INTERNAL_INTAKE_EMAILS=team@yourfirm.com   # comma-separated; who gets the internal notice
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

> **Note:** `STRIPE_PRICE_COMPLETE` is no longer required. The previous
> fixed-price "Complete" tier has been replaced with a quote-based
> "Complete" tier (3+ years of history) that does not flow
> through Stripe Checkout.

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

Only `essential` and `standard` are valid `<plan>` values on the
checkout POST. Anything else returns 404. The "Complete" tier
deliberately does **not** hit Stripe — it links to `/support` so the
team can quote it.

### Onboarding flow routes

The guided onboarding flow has its own payment-gated path with a
durable, DB-backed purchase record (so a webhook can mark payment out
of band):

| Method | Path                              | Purpose                                                        |
|--------|-----------------------------------|----------------------------------------------------------------|
| POST   | `/onboarding/step-2`              | Persist firm details, create record, 303 to Stripe (paid) or `/onboarding/quote`. |
| GET    | `/onboarding/payment/return`      | Stripe `success_url`; verifies payment server-side, unlocks Step 3. |
| GET    | `/onboarding/payment/cancel`      | Stripe `cancel_url`; friendly message, back to Step 2.         |
| GET    | `/onboarding/quote`               | Quote-plan confirmation (no Stripe); notifies the team.        |
| GET/POST | `/onboarding/step-3`            | Gated on paid status; uploads reports, sends both emails.      |
| POST   | `/onboarding/stripe/webhook`      | CSRF-exempt; verifies signature, marks paid (idempotent).      |

Step 3 is gated: a paid plan can't reach it until the record is marked
paid (by the success-return verify **or** the webhook). The quote plan
is never payment-gated. The plaintext account password is never stored
— it is hashed-and-discarded, with the username kept as an auth
placeholder.

When Stripe isn't configured: in **production** Step 2 returns a 503
with a friendly "payment is not available" message and does not
advance; in **non-production** it marks a clearly-labelled demo
simulation so the flow stays demoable end-to-end (no card charged).

## Testing locally

```bash
export STRIPE_SECRET_KEY=sk_test_...
export STRIPE_PRICE_ESSENTIAL=price_test_essential
export STRIPE_PRICE_STANDARD=price_test_standard
python3 app.py
```

Then visit http://localhost:5000/pricing and click any "Buy …" button.
Stripe will redirect to a test-mode hosted checkout page. Use card
`4242 4242 4242 4242` with any future expiry to complete payment.

To run the smoke tests (no real Stripe traffic, all calls are mocked):

```bash
python3 tests/smoke_pricing_stripe.py
python3 tests/smoke_pricing_page.py
python3 tests/smoke_onboarding_stripe_email.py
```

## Webhooks

The onboarding flow mounts a CSRF-exempt webhook at
`POST /onboarding/stripe/webhook`. It verifies the `Stripe-Signature`
header against `STRIPE_WEBHOOK_SECRET` via
`stripe_checkout.verify_webhook(payload, signature)`, then handles
`checkout.session.completed` by marking the linked record paid. The
handler is idempotent (replaying the same event does not change
`paid_at`) and never logs secrets or PII. A missing
`STRIPE_WEBHOOK_SECRET` returns 503; a bad signature returns 400 and
changes nothing.

In the Stripe Dashboard → Developers → Webhooks, add an endpoint
pointing at `https://<your-domain>/onboarding/stripe/webhook`,
subscribe to `checkout.session.completed`, and copy the signing secret
into `STRIPE_WEBHOOK_SECRET`.
