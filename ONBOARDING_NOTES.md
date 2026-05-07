# Onboarding & import-prep notes

This page documents the customer-facing onboarding layer added on the
`onboarding-import-prep` branch. The intended audience is law-firm staff
with no familiarity with the codebase or with QuickBooks integrations.

## What ships in this layer

1. **Onboarding guide** — `GET /onboarding`
   - Public page (no login required) so prospective customers can read it
     before signing up.
   - Linked from the primary nav for both logged-in and logged-out users.
   - Walks through every step of the workflow: PCLaw export → upload →
     preflight → mapping → connect → confirm → import → verify → reverse.
   - Lists the required CSV columns with examples.
   - Includes a private-beta disclaimer at the top: do not upload a full
     client ledger on the first run; review mapped accounts before
     posting.
   - Template: `templates/onboarding.html`.

2. **CSV downloads**
   - `GET /onboarding/template.csv` — a minimal, hand-curated 4-line CSV
     covering the required columns. Safe to upload as a smoke test.
   - `GET /onboarding/sample.csv` — the larger multi-transaction GL from
     `test_data/02_general_ledger.csv` (trust, A/R, A/P, expenses).
   - Both return `Content-Disposition: attachment` so they download
     rather than render in the browser.

3. **Import preflight summary**
   - Computed by `preflight.build_preflight_summary(rows, fieldnames)`.
   - Attached to the job dict on upload as `job["preflight"]`.
   - Rendered as a card on the job-detail page above Step 01.
   - Surfaces: transaction count, line count, total debits / credits,
     balanced (yes/no), unique account count, missing required columns,
     unmapped account count, QuickBooks connection status,
     sandbox/production environment status, rows missing date /
     account.

4. **Friendly validation messages**
   - `preflight.friendly_validation_message(exc)` translates the raw
     pipeline `ValueError` into a `(headline, action)` pair.
   - The upload route flashes the friendly text and stores the same
     pair on `job["last_validation_error"]`. The job-detail page
     renders that as a prominent banner with a link to the onboarding
     guide.
   - The raw exception text is never echoed to the user — that keeps
     ledger contents out of flash messages and audit logs.

5. **Beta disclaimer banner on the dashboard**
   - Yellow `warning-panel` card directly under the hero. Tells the
     user to start with a small slice of the GL and to review the
     mapping before posting. Links to the onboarding page.

## What did **not** change

- Production import confirmation flow (`type IMPORT to confirm`) is
  unchanged.
- Disconnect flow (`/disconnect`, `/quickbooks/disconnect`,
  `/jobs/<id>/disconnect-qbo`) is unchanged.
- Reversal flow is unchanged.
- Duplicate-protection rules are unchanged.
- Account mapping page is unchanged (the preflight panel just adds a
  link to it when unmapped accounts exist).

## Workflow as documented in-app

1. Export the PCLaw General Ledger as CSV.
2. Upload from the dashboard.
3. Review the import preflight on the job page.
4. Map PCLaw accounts to QuickBooks accounts.
5. Connect QuickBooks Online.
6. Confirm the production import (type `IMPORT`).
7. Import — one QuickBooks JournalEntry per PCLaw transaction.
8. Verify in QuickBooks (re-fetch totals, spot-check Journal report).
9. Reverse if needed (type `REVERSE`).

## Tests

`tests/smoke_onboarding.py` covers:

- `/onboarding` renders 200 with the required-column reference and
  beta disclaimer; links to both CSV downloads.
- `/onboarding/template.csv` returns a CSV with every required header
  and an attachment Content-Disposition.
- `/onboarding/sample.csv` returns the multi-transaction GL.
- `build_preflight_summary` computes correct totals, unique accounts,
  balanced flag, and `ready=True` for a balanced sample.
- `build_preflight_summary` flags missing columns, unbalanced totals,
  rows missing date / account, and `ready=False`.
- The job-detail page after upload includes the preflight panel with
  totals + sandbox/production env status.
- The logged-in and logged-out nav both contain the Onboarding link.

Run with:

    python3 tests/smoke_onboarding.py

The full smoke suite (existing tests + this one) was run on
`onboarding-import-prep` and all pass.

## Files added / changed

Added:
  - `preflight.py`
  - `templates/onboarding.html`
  - `tests/smoke_onboarding.py`
  - `ONBOARDING_NOTES.md`

Changed:
  - `app.py` — preflight wiring on `/upload`, three new routes
    (`/onboarding`, `/onboarding/template.csv`, `/onboarding/sample.csv`),
    extra context vars on `/jobs/<id>`, friendly validation flash.
  - `templates/_base.html` — Onboarding link in primary nav (both nav
    states).
  - `templates/dashboard.html` — onboarding CTA + beta disclaimer card.
  - `templates/job-detail.html` — preflight summary card; banner for
    `job.last_validation_error`.
