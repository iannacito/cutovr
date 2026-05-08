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
| Launch URL / Connect button | `/` (redirects to login or dashboard) | Where users land after Intuit redirects them in. |
| Disconnect URL      | `/disconnect` (alias `/quickbooks/disconnect`) | Public page explaining how to disconnect, with a logged-in revoke flow. |
| Redirect URI (OAuth) | `/oauth/callback` | Must match `QBO_REDIRECT_URI` exactly. |

### Exact production URLs to register with Intuit

After the custom domain (`https://www.pclawmigrate.com`) is live, the URLs
to enter in the Intuit Developer dashboard are:

| Field | Value |
| --- | --- |
| Host Domain / Launch URL | `https://www.pclawmigrate.com` |
| OAuth 2.0 Redirect URI | `https://www.pclawmigrate.com/oauth/callback` |
| Disconnect URL | `https://www.pclawmigrate.com/disconnect` |
| End User License Agreement | `https://www.pclawmigrate.com/terms` |
| Privacy Policy | `https://www.pclawmigrate.com/privacy` |

The disconnect URL is a real route (added in
`production-customer-readiness`). Logged-out visitors see an explanation
of both the QuickBooks-side and app-side disconnect paths. A signed-in
firm admin sees a list of their currently connected QuickBooks companies
and can confirm with `DISCONNECT` to revoke tokens at Intuit and delete
the encrypted token rows from this app.

The new templates (`privacy.html`, `terms.html`, `support.html`,
`disconnect.html`, `quickbooks-manage.html`) are linked from the site
footer / nav on every page, including the unauthenticated login and
signup screens.

## 2. Customize the placeholders before submission

The contact addresses, app name, and operating-company name shown on the
public pages are now driven by environment variables, not hardcoded in the
templates. Set these in Render → Service → Environment before submitting the
Intuit production form:

| Env var | Where it shows | Default |
| --- | --- | --- |
| `APP_NAME` | Page title, brand mark in header | `PCLaw Migrate` |
| `COMPANY_NAME` | Footer copyright | `PCLaw Migrate` |
| `SUPPORT_EMAIL` | `/support` &mdash; "Email …" line | `support@your-domain.example` |
| `SECURITY_EMAIL` | `/support` &mdash; "Reporting a security issue" | `security@your-domain.example` |
| `PRIVACY_CONTACT_EMAIL` | `/privacy` &mdash; "Contact" | falls back to `SUPPORT_EMAIL` |

Hit `/healthz` after the deploy: `branding_support_email_set` and
`branding_security_email_set` should both be `true`. If they read `false`,
the `@your-domain.example` placeholders are still being shown.

You will still need to edit by hand:

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
- The disconnect path is exposed at the public `/disconnect` URL and
  separately on each job detail page. Both paths attempt a server-side
  revoke at Intuit's `https://developer.api.intuit.com/v2/oauth2/tokens/revoke`
  endpoint and then delete the encrypted refresh token from this app's
  database, so a successful disconnect always removes the local token
  even if the Intuit revoke call fails.

## 3.1. Production-mode connect guard

When `QBO_ENVIRONMENT=production`, the *Connect to QuickBooks* button is
gated by a startup-time configuration check. If any of the following are
missing or wrong, clicking *Connect* fails closed with a clear, secret-
free message instead of starting OAuth against a real customer company:

- `QBO_CLIENT_ID` / `QBO_CLIENT_SECRET` are not configured.
- `QBO_REDIRECT_URI` is missing, points at `localhost`, or is not HTTPS.
- `QBO_REAL_IMPORT` is not `1` (we never want to start a real OAuth flow
  if the import path will fall back to demo).
- `APP_ENV` is not `production` (Secure cookies / strict env validation
  must be on).
- `SUPPORT_EMAIL` is still the deploy-default placeholder.

These checks live in `app._qbo_production_blockers()` and are also
surfaced on the `/quickbooks` connection-management page so the operator
can see exactly which env vars to set in Render before flipping the
deploy live.

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

## 5.1. Intuit transaction id (`intuit_tid`) capture

Intuit returns an `intuit_tid` HTTP response header on every QuickBooks
API call, including the OAuth token endpoint. It is an opaque request id
(no token / secret material) and is what Intuit support staff use to
look up a specific request in their logs.

The app captures `intuit_tid` from every QBO/Intuit response where one
is available:

- `QBOAuthHandler.get_bearer_token` and `refresh_access_token` (OAuth
  token exchange and refresh). Returned in the result dict and stored
  on the handler as `last_intuit_tid`.
- `QBOClient.query`, `get_accounts`, `get_company_info`,
  `get_journal_entry`, `create_journal_entry`, `create_customer`, and
  `create_vendor` (all v3 entity calls). Stored on the client as
  `last_intuit_tid` after every call (success or failure) and attached
  to the `QBOError.intuit_tid` attribute on non-2xx responses.

Where the `intuit_tid` shows up:

- **Audit log** &mdash; `qbo_connected`, `oauth_token_exchange_failed`, and
  `import_failed` rows include `intuit_tid=<id>` in their `details`
  column when the upstream response carried one. Visible to firm
  operators at `/firm/audit`.
- **Job-detail page** (`/jobs/<id>`) &mdash; the *Last import error* panel
  shows the Intuit support reference id beside the technical detail
  collapsible whenever the failure carried one.
- **Flash messages** &mdash; user-facing import / connect / verify error
  flashes append `(Intuit support reference: <id>)` so a non-technical
  user can quote it to support without having to dig through the UI.

What we deliberately do **not** include in audit rows, flashes, UI, or
the job-detail technical-detail blob:

- Client secret, access token, refresh token, or authorization code.
- The raw response body of a failed token-endpoint request &mdash;
  Intuit can echo back fragments that resemble client identifiers,
  so `QBOAuthError` only carries the status code + `intuit_tid`.

`tests/smoke_intuit_tid.py` exercises the capture path with mocked
responses and asserts no marker secrets leak into errors.

**Questionnaire answer.** With this change the answer to *"Does your
app capture intuit_tid from response headers?"* on the Intuit production
questionnaire is **Yes** &mdash; the app captures `intuit_tid` from QBO
v3 entity calls and the OAuth token endpoint, surfaces it to operators
in the audit log and job-detail page, and includes it as a support
reference in user-facing error flashes.

## 6. Production cutover (separate from this doc)

When you're cleared to write to a real production QuickBooks company:

1. Create or convert the Intuit app to a production app, then in the
   Intuit Developer dashboard set:
   - **Host Domain / Launch URL:** `https://www.pclawmigrate.com`
   - **Redirect URI:** `https://www.pclawmigrate.com/oauth/callback`
   - **Disconnect URL:** `https://www.pclawmigrate.com/disconnect`
   - **EULA URL:** `https://www.pclawmigrate.com/terms`
   - **Privacy URL:** `https://www.pclawmigrate.com/privacy`
2. Update Render env (Render &rarr; Service &rarr; Environment):
   - `QBO_CLIENT_ID` &mdash; production client id from Intuit
   - `QBO_CLIENT_SECRET` &mdash; production client secret from Intuit
   - `QBO_REDIRECT_URI=https://www.pclawmigrate.com/oauth/callback`
   - `QBO_ENVIRONMENT=production`
   - `QBO_REAL_IMPORT=1`
   - Confirm `APP_ENV=production` and `SUPPORT_EMAIL` are real values
3. Restart the service. Hit `/healthz` and `/readiness`; every required
   item must read green before connecting any real customer.
4. Open `/quickbooks` (logged in). The page should show a
   "Production Mode active" banner; if it shows blockers, fix them in
   Render and restart.
5. Test against a low-stakes company first (e.g. your own bookkeeping)
   before exposing to a paying customer. The production-mode import flow
   requires you to type `IMPORT` to confirm before any journal entries
   are posted.
