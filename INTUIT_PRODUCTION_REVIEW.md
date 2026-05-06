# Intuit Production Review

This document is a checklist for moving the app from a sandbox-only Intuit
Developer registration to a production-cleared one. It also explains how the
new public Privacy / Terms / Support pages fit into the Intuit app submission
form.

> **Legal-review caveat.** The starter copy in `templates/privacy.html` and
> `templates/terms.html` is intended to make the submission form fillable
> while you finish a real review with counsel. Do not point Intuit at
> `https://your-domain/privacy` and `/terms` until your lawyer has signed
> off on the actual content. The starter is a *scaffold*, not a contract.

---

## 1. Public URLs Intuit will ask for

When you submit the production-readiness form in the Intuit Developer
dashboard, three URLs are required:

| Intuit field        | Local route   | What it has to be                          |
| ------------------- | ------------- | ------------------------------------------ |
| End-User License    | `/terms`      | Plain-language ToS (no advice, beta caveat). |
| Privacy Policy      | `/privacy`    | What you collect, store, retain, share.    |
| Host Domain         | `/`           | Public marketing or sign-in page (already login). |
| Launch URL / Connect button | `/login` or `/dashboard` | Where users land after Intuit redirects them in. |
| Disconnect URL      | (in-app)      | The job-detail page has a *Disconnect QuickBooks* form. |

The new templates (`privacy.html`, `terms.html`, `support.html`) ship today
in the `go-live-foundation` branch and are linked from the site footer on
every page, including the unauthenticated login and signup screens.

## 2. Customize the placeholders before submission

Before submitting the Intuit production form, do a find-and-replace across
the new templates:

- `support@your-domain.example` &rarr; your real support inbox.
- `security@your-domain.example` &rarr; your real security inbox (can be the same).
- The starter "Last updated" date in `privacy.html` / `terms.html`.
- The "Sub-processors" list in `privacy.html` (Render, Intuit, your email
  vendor) &mdash; add or remove based on what your deployment actually uses.
- Anything your counsel marks up.

Do **not** paste real customer names or secrets into these templates. They
are public pages.

## 3. OAuth + scope sanity check

- App is registered at <https://developer.intuit.com>.
- Redirect URI in the Intuit dashboard matches `QBO_REDIRECT_URI` exactly
  (trailing slash matters, http vs https matters).
- Scope: `com.intuit.quickbooks.accounting`. The app does not request
  `payments`, `payroll`, or `openid` scopes.
- The disconnect path is exposed in the UI (job detail page &rarr; *Disconnect
  QuickBooks*) and removes the encrypted token from the database.

## 4. What the reviewer will see when they connect

When an Intuit reviewer clicks the *Connect* button you provided:

1. They land on `/login` (or `/signup` if you point them there).
2. They create an account &mdash; the signup screen surfaces both the Privacy
   and Terms links above the *Create firm workspace* button.
3. They upload `sample_pclaw_gl.csv` (provided in the repo) or any
   reasonable PCLaw export, click *Connect to QuickBooks*, complete OAuth
   against the Intuit sandbox, and run an import.
4. They see the new import receipt (job-detail page) with the imported
   journal-entry IDs listed.
5. They can run a *Reverse import* against the same job. In QuickBooks they
   will see the offsetting entries with `DocNumber` starting `REV-` and
   every line description prefixed `REVERSAL`.

## 5. Required reading for the reviewer (not strictly required but useful)

- `PRODUCTION_READINESS.md` &mdash; the operational checklist (env vars,
  rotation, backups).
- `SECURITY_NOTES.md` &mdash; encryption posture and threat model.
- `REVERSAL_NOTES.md` &mdash; the accounting model behind the reversal
  workflow.

## 6. Production cutover (separate from this doc)

When you're cleared to write to a real production QuickBooks company:

1. Create or convert the Intuit app to a production app.
2. Update Render env: `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`,
   `QBO_REDIRECT_URI`, `QBO_ENVIRONMENT=production`.
3. Run through the §9 pre-launch smoke checklist in `PRODUCTION_READINESS.md`
   against the live URL.
4. Test against a low-stakes company first (e.g. your own bookkeeping)
   before exposing to a paying customer.
