# Production Readiness Checklist

This is your pre-flight checklist before letting real customers (real law firms,
real QuickBooks Online production companies) use the app. Work through it top to
bottom. Don't skip the "Rotate exposed keys" step.

## 1. Render environment variables

Set these in **Render → Service → Environment**. Never paste them into source
code, commit them to git, or share them in screenshots.

| Variable | Required | Notes |
|---|---|---|
| `APP_ENV` | yes | Set to `production`. This flips on HTTPS-only cookies and strict env validation. |
| `SECRET_KEY` | yes | At least 32 chars. Generate with: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ENCRYPTION_KEY` | yes | Fernet key. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` Losing this means you can't decrypt stored QBO tokens or uploaded files. Save it in a password manager. |
| `QBO_CLIENT_ID` | yes | From Intuit Developer dashboard. |
| `QBO_CLIENT_SECRET` | yes | From Intuit Developer dashboard. |
| `QBO_REDIRECT_URI` | yes | Must be HTTPS, must exactly match the value in the Intuit dashboard (e.g. `https://pclaw-qbo-v2.onrender.com/oauth/callback`). |
| `QBO_ENVIRONMENT` | yes | `sandbox` while you're still testing, `production` only when you're cleared to write to real customer books. |
| `QBO_REAL_IMPORT` | optional | Set to `1` to enable real journal-entry writes. Leave unset to keep demo mode. |
| `IMPORT_HISTORY_DB` | optional | Path to SQLite file on the Render persistent disk, e.g. `/var/data/import_history.sqlite3`. |
| `APP_DB` | optional | Path to the auth/jobs SQLite file on the persistent disk, e.g. `/var/data/app.sqlite3`. |
| `CSRF_DISABLE` | **never** | Tests only. The app refuses to start if this is on with `APP_ENV=production`. |
| `APP_NAME` | optional | Display name shown in the page title and brand mark. Defaults to `Cutovr`. |
| `COMPANY_NAME` | optional | Operating-company name shown in the footer copyright. Defaults to `Cutovr`. |
| `SUPPORT_EMAIL` | optional but recommended | Address surfaced on `/support` for customer issues. Defaults to `support@your-domain.example` &mdash; set this before customer testing. |
| `SECURITY_EMAIL` | optional but recommended | Address surfaced on `/support` for vulnerability reports. Defaults to `security@your-domain.example` &mdash; set this before customer testing. |
| `PRIVACY_CONTACT_EMAIL` | optional | Address shown on `/privacy` for data-deletion requests. Falls back to `SUPPORT_EMAIL`. |

The app calls `_validate_production_env()` on startup. If anything is missing
or malformed, the deploy will fail loudly with a list of what's wrong. It will
**not** print the offending values.

You can confirm the deploy is healthy by visiting `/healthz`. It returns JSON
with which keys are set (true/false), but never the secret values themselves.
The probe also reports `branding_support_email_set` and
`branding_security_email_set` &mdash; both are `false` while the
`@your-domain.example` placeholders are still in use, so you can spot from
one curl whether the customer-facing addresses still need replacement.

## 2. Intuit Developer setup

- App is registered at https://developer.intuit.com.
- Redirect URI in the Intuit dashboard matches `QBO_REDIRECT_URI` **exactly**
  (trailing slash matters, http vs https matters).
- Use the **sandbox** company for testing. Switch to a production company only
  when you've completed Intuit's app review (if going public) or are running
  it as an internal tool against your own books.
- Scopes requested: at minimum `com.intuit.quickbooks.accounting`.

## 3. Sandbox vs production: the rule

Until you've completed at least one full happy path against a sandbox company
(upload → map → import → verify → reverse), do **not** point the app at a real
production QuickBooks company. Once one customer's data is wrong in real
QuickBooks, undoing it is painful.

When you flip to production:
1. Create a fresh Intuit production app (or convert the sandbox app).
2. Update `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_REDIRECT_URI`, and
   `QBO_ENVIRONMENT=production` on Render.
3. Test against a low-stakes company first (e.g. your own bookkeeping).

## 4. Rotate exposed keys before real production

If any secret was ever in a chat, screenshot, screen share, public repo, or
shared doc, **rotate it** before going live:

- `SECRET_KEY`: generate a new one and update Render. Existing user sessions
  will be invalidated, which is what you want.
- `ENCRYPTION_KEY`: rotation is harder — anything previously encrypted (QBO
  tokens, uploaded files) cannot be decrypted with the new key. Plan for this:
  in a fresh deploy, you'll re-OAuth into QBO and re-upload any files you care
  about. If you must rotate after data exists, you'll need a one-time
  re-encryption migration.
- Intuit `QBO_CLIENT_SECRET`: rotate from the Intuit dashboard, then update
  Render. Connected customers must re-authorize.

## 5. Backups

The two SQLite files (`app.sqlite3`, `import_history.sqlite3`) on the Render
persistent disk are your source of truth. Set up either:
- Render's automated disk snapshots (paid feature), **or**
- A scheduled job that copies both files to S3/Backblaze/etc. nightly.

Test the restore path at least once. A backup you've never restored is a hope,
not a backup.

## 6. Logs

- Render keeps recent stdout/stderr logs in the service dashboard.
- The app does not currently ship logs to a third-party aggregator. For real
  customer use, plan to add Sentry (errors) or Logtail/Better Stack (full
  stdout). That gives you alerting and history beyond Render's retention
  window.
- Confirm the app does not log raw QBO tokens, raw `SECRET_KEY`, or
  `ENCRYPTION_KEY`. The startup error path is already designed not to.
- The app does capture and log `intuit_tid` (an opaque Intuit request
  id from response headers) on every QBO API call &mdash; see
  [INTUIT_PRODUCTION_REVIEW.md §5.1](INTUIT_PRODUCTION_REVIEW.md). When
  a customer reports a failed import, the Intuit support reference id
  shown in the flash message and the job-detail error panel is what
  Intuit support will ask for; it is also embedded in the
  `import_failed` / `oauth_token_exchange_failed` audit rows.

## 7. Support process

Before any customer touches the app, decide:
- Where do users report bugs? (email address, form, etc.)
- Who is on call? What's the response SLA?
- How do you communicate downtime? (status page, email blast)
- For data-impacting bugs (e.g. an import that posted wrong amounts), the
  reversal workflow is the recovery tool — make sure you've practiced using
  it before you need it.

## 8. SOC 2 roadmap (note, not a blocker)

If you plan to sell to mid-market or enterprise law firms, they will ask about
SOC 2. You don't need to be SOC 2 compliant on day one, but start building the
artifacts now:

- Written security policies (access control, incident response, vendor mgmt).
- A central place tracking which production secrets exist, who has access,
  and when they were last rotated.
- MFA on every admin account: Render, Intuit Developer, GitHub, domain
  registrar, email.
- Audit log of admin actions (the app already writes one for user actions).
- Vendor list (Render, Intuit, any analytics/monitoring).

Vanta or Drata can automate most of the evidence collection once you're
ready. Until then, a single shared doc tracking the above is enough.

## 8.4. Data retention &amp; deletion

Each migration job stores three things on the server:

1. The encrypted PCLaw CSV (`uploads/<timestamp>_*.csv.enc`).
2. The encrypted QBO OAuth tokens (`qbo_connections` rows in `app.sqlite3`).
3. A row in `import_history.sqlite3` for every successful import, used by the
   duplicate-import guard.

Users can purge (1) and (2) at any time from the job page using the
**Delete local job data** button in the danger zone. The action requires the
user to type `DELETE` to confirm and is recorded in the per-firm audit log.

What **Delete local job data** does NOT do, by design:

- It does **not** delete or void any JournalEntry records already posted to
  QuickBooks. To remove those, the user must run **Reverse this import**
  *first* (which posts offsetting entries via Intuit's API), or void/delete
  them by hand in QuickBooks. The job-detail UI explains this distinction.
- It does **not** remove the row in `import_history.sqlite3`. Keeping the
  row is what protects the customer from accidentally re-importing the
  same file content into the same QuickBooks company. If a customer
  legitimately needs the import-history row gone (e.g. mistaken upload
  with sensitive content), an operator can delete it directly from the
  SQLite file out-of-band.
- It does **not** remove audit-log rows, which are kept for compliance.

Operators have a read-only view of every import this firm has ever
attempted at `/firm/imports`, scoped to the logged-in firm. Use it to
spot which job a failed import came from, which QBO company received
which entries, and whether anything was reversed.

## 8.5. Public Privacy / Terms / Support pages

The app now ships public pages at `/privacy`, `/terms`, and `/support`,
linked from the footer of every page (including login and signup). These
URLs are what Intuit's production app submission asks for. The starter
copy is intentionally minimal &mdash; see [INTUIT_PRODUCTION_REVIEW.md](INTUIT_PRODUCTION_REVIEW.md)
for the legal-review caveat and the placeholders to replace before pointing
Intuit at the production URLs.

## 9. Pre-launch smoke checklist

Run through this list against the live URL before announcing to a customer:

- [ ] `GET /healthz` returns `status: ok` and the env booleans you expect.
- [ ] Sign up a new firm + user. Log out. Log back in.
- [ ] Upload a sample GL CSV. Verify the job appears on the dashboard.
- [ ] Connect QBO (sandbox), complete account mapping.
- [ ] Run an import in `QBO_REAL_IMPORT=1` mode against the sandbox.
- [ ] Verify the journal entries appear in the QBO sandbox UI.
- [ ] Reverse the import. Confirm the entries are gone in QBO.
- [ ] Confirm the encrypted `*.enc` upload files exist in `uploads/` and the
      plaintext originals do not.
- [ ] Confirm `qbo_connections.access_token` in the SQLite DB is not
      plaintext.
- [ ] Run all smoke tests locally: see `tests/` directory.
