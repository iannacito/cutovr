# Calendly discovery-call lead capture

How prospects book discovery calls, and how Cutovr captures every booking
into its own database and an operator **Leads** view.

## How it works

1. **Calendly owns the form + booking UI.** Prospects fill out the form
   built into Calendly (custom questions) and book a discovery call. Cutovr
   does not rebuild that form.
2. **Calendly sends Cutovr a webhook** when a call is scheduled or canceled.
3. **Cutovr stores each booking as a lead** (idempotent on the Calendly
   invitee URI) and notifies the team by email with the form answers.
4. **Operators view leads** at `/operator/leads` inside Cutovr.

The prospect still receives Calendly's own confirmation email. Cutovr can
optionally send an additional branded "we received your details" email
(off by default — see `CALENDLY_CONFIRMATION_EMAIL`). Customer-facing emails
point prospects at **support@cutovr.com** for any follow-up.

## One-time setup (Dan)

### 1. Calendly: create the discovery-call event + questions

- Create or open the discovery-call event type (or a routing form).
- Add the custom questions you want answered before the call. The team's
  internal email and the Leads view pick up these answers automatically.
  A few question labels are auto-mapped to dedicated columns (matched
  case-insensitively, by substring):
  - "Firm" / "Company" / "Law firm" / "Organization" → **Firm**
  - "Clio rep name" / "Clio representative" → **Clio rep name**
  - "Clio rep email" / "Clio email" → **Clio rep email**
  - "Phone" / "Mobile" / "Cell" → **Phone**
  - Every other question is still stored and shown under **Form answers**.

### 2. Point the website CTA at Calendly

Set the booking CTA env var so the "Book a discovery call" button links to
your Calendly booking / routing-form URL:

```
DISCOVERY_CALL_URL=https://calendly.com/your-org/discovery-call
```

### 3. Create the Calendly webhook subscription

Create a webhook subscription (Calendly **API/Integrations → Webhooks**, or
via the API) pointing at the Cutovr endpoint:

```
https://www.cutovr.com/integrations/calendly/webhook
```

Subscribe to these events:

- `invitee.created`  — a call was booked
- `invitee.canceled` — a booked call was canceled
- `routing_form_submission.created` — *(optional)* only if you use a routing form

When you create the subscription Calendly returns a **signing key**. Put it
in `CALENDLY_WEBHOOK_SIGNING_KEY` (below) so Cutovr can verify each delivery.

### 4. Render environment variables

| Variable | Required? | Purpose |
|---|---|---|
| `DISCOVERY_CALL_URL` | Recommended | The Calendly link used by the website "Book a discovery call" CTA. |
| `CALENDLY_WEBHOOK_SIGNING_KEY` | Recommended | Calendly's per-subscription signing key. Cutovr verifies the `Calendly-Webhook-Signature` HMAC against it. |
| `CALENDLY_WEBHOOK_SECRET` | Optional | Simpler shared-secret gate (alternative to the signing key). Cutovr compares it against `?secret=...` or the `X-Calendly-Webhook-Secret` header. |
| `CALENDLY_API_TOKEN` | Optional | Personal Access Token used to *enrich* a booking by fetching the full invitee record (incl. all question answers) from the Calendly API. If unset, Cutovr stores whatever the webhook payload provides and marks enrichment `skipped`. |
| `CALENDLY_CONFIRMATION_EMAIL` | Optional | Set to `1` to also send a Cutovr-branded "we received your details" email to the prospect (in addition to Calendly's own confirmation). Off by default. Never sent for cancellations or when SMTP is unconfigured. |
| `INTERNAL_INTAKE_EMAILS` | Recommended | Comma-separated internal recipients for the "new discovery call" notification. Falls back to `SUPPORT_EMAIL` if unset. |
| `SUPPORT_EMAIL` | Recommended | Customer-facing contact mailbox. Set to `support@cutovr.com` in production. It's echoed in the optional prospect confirmation email and the team notification; when left at the deploy placeholder, the lead emails fall back to `support@cutovr.com`. |
| `OPERATOR_EMAILS` | Required for Leads view | Allowlist of operator emails that can see `/operator/leads`. |
| SMTP / `MAIL_*` vars | Recommended | Standard email config (see `email_sender.py`). Without it, leads are still captured; only the emails are skipped. |

#### Authenticity policy

- If **either** `CALENDLY_WEBHOOK_SIGNING_KEY` or `CALENDLY_WEBHOOK_SECRET`
  is configured, a delivery **must** pass that check or it's rejected with
  `401`.
- If **neither** is configured, deliveries are accepted but logged as
  `unverified-open` so a first test booking works. Configure one before
  going live.

Secrets (signing key, API token, webhook secret) are never logged or shown
in the UI.

### 5. Test

1. Book a test discovery call through the Calendly link.
2. Confirm an internal notification email arrives (if SMTP + recipients set).
3. Log in as an operator and open **Leads** (`/operator/leads`) — the test
   booking appears with the firm, Clio rep, meeting time, and form answers.
4. Cancel the test booking in Calendly and confirm the lead flips to
   **Canceled**.

## Endpoint reference

- `POST /integrations/calendly/webhook` — Calendly webhook receiver.
- `GET  /operator/leads` — operator Leads list (auth-gated; 404 for others).
- `GET  /operator/leads/<id>` — per-lead detail with all form answers.
