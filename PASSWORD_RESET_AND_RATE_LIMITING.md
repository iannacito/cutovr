# Password Reset & Rate Limiting

This document describes the password-reset and rate-limiting features added
to the app, the environment variables needed to enable transactional email,
and how to verify everything is wired correctly after deployment.

## What this gives you

- A self-service `/forgot-password` page that issues a single-use,
  time-limited reset link.
- A `/reset-password/<token>` page that lets the user choose a new
  password (subject to the same length policy as signup).
- A 12-character minimum password length on both signup and reset.
- SMTP-based email delivery for the reset link (Zoho, Google Workspace,
  AWS SES, etc.).
- Per-IP rate limiting on the login and forgot-password POST routes,
  backed by SQLite so it survives a process restart on a single Render
  instance.
- Safe audit logging on every relevant action — never the token, never
  the email body.

## What this does NOT do (intentionally, for a v1)

- No multi-factor auth.
- No Redis-backed limiter (would be needed for multi-instance deploys).
- No global session revocation on reset (this request's session is
  cleared; other active sessions are not).
- No CAPTCHA. The rate limiter is the only brute-force defense.

These are reasonable next-step additions; they are out of scope here.

## Environment variables

### Required for SMTP email delivery

If any of these are unset, the reset email is NOT sent. The user still
sees the same generic "if an account exists we sent a link" message; in
production an audit row of action `password_reset_smtp_missing` is
written so an operator can notice the misconfiguration.

| Variable        | Example                | Notes                                     |
|-----------------|------------------------|-------------------------------------------|
| `SMTP_HOST`     | `smtp.zoho.com`        | Mail server hostname                      |
| `SMTP_PORT`     | `587`                  | 587 for STARTTLS (default), 465 for SSL   |
| `SMTP_USER`     | `noreply@yourfirm.com` | SMTP auth username (often == `SMTP_FROM`) |
| `SMTP_PASSWORD` | `<app-password>`       | SMTP auth password / app password         |
| `SMTP_FROM`     | `noreply@yourfirm.com` | `From:` address. Must be allowed by host. |

### Zoho Mail setup (placeholder values — replace before saving in Render)

Zoho is the example used here; the same pattern works for any provider.

1. In Zoho Mail Admin Console, create a dedicated mailbox or alias for
   transactional mail (e.g. `noreply@yourfirm.com`). Don't reuse a
   personal mailbox — sending volume from that mailbox will count
   against the same daily quota as your personal email.
2. Generate an **app-specific password** for that mailbox under
   *My Account → Security → App Passwords*. Don't paste the regular
   account password into Render.
3. In Render → your service → *Environment*, add:

   ```
   SMTP_HOST=smtp.zoho.com
   SMTP_PORT=587
   SMTP_USER=noreply@yourfirm.com
   SMTP_PASSWORD=<the app password from step 2>
   SMTP_FROM=noreply@yourfirm.com
   ```

4. Save and trigger a redeploy. The new env vars take effect on the
   next process start.

> Other providers — Google Workspace, AWS SES, Mailgun, Postmark —
> follow the same shape. Use port `587` (STARTTLS) unless your provider
> documents otherwise.

### No additional config for rate limiting

The limiter is on by default and stores its state in the same SQLite
database the rest of the app uses (`APP_DB`, default
`data/app.sqlite3`). No new env vars are needed.

If you scale to **more than one instance**, the per-instance counters
become independent and the effective limit doubles. Either keep it
single-instance (the default Render *Starter* / *Standard* plans), or
migrate the limiter to Redis. The current code keeps the limiter
interface narrow (`record_event`, `count_events`, `purge_old`) so a
swap is mechanical.

## How the reset flow works

1. User submits their email at `/forgot-password`.
2. The route always returns the same generic message regardless of
   whether the email matches a real account, so the page can't be used
   as an account-existence oracle.
3. If the email matches a real user, a 32-byte (`secrets.token_urlsafe`)
   token is generated, **SHA-256 hashed**, and stored in the
   `password_reset_tokens` table together with the user id, an
   expiry timestamp (default 30 minutes), and a NULL `used_at`.
4. The plaintext token is sent in the reset URL via SMTP. **It never
   appears in any HTTP response or audit log.** In non-production it is
   logged via Python's `logging` module so a developer running locally
   can copy it from the console.
5. `/reset-password/<token>` hashes the URL token and looks up the row.
   It rejects the request — and redirects the user back to
   `/forgot-password` with a generic flash — if the row is missing,
   already used, or past its expiry.
6. On a valid token + a 12+ char password, the user's `password_hash`
   is updated, the token is marked `used_at`, and any other
   outstanding reset tokens for that user are also marked used so a
   second emailed link can't be replayed.

## Rate limiting

Two endpoints are protected:

| Endpoint           | Window  | Max attempts | Bucket key(s)                     |
|--------------------|---------|--------------|-----------------------------------|
| `POST /login`      | 5 min   | 10           | `login:ip:<ip>`, `login:email:<e>`|
| `POST /forgot-password` | 15 min | 5      | `forgot:ip:<ip>`                  |

When a bucket is over its budget the response is `429`, the user sees a
generic friendly message, and an audit row is written with the action
`login_rate_limited` or `forgot_password_rate_limited`. The tokens / DB
rows / secrets are never included in either the response or the audit
detail.

## Audit actions added

| Action                                  | When                                                  |
|-----------------------------------------|-------------------------------------------------------|
| `password_reset_requested`              | Reset link issued for a known email                   |
| `password_reset_requested_unknown_email`| Reset request for an email with no account            |
| `password_reset_completed`              | User picked a new password using a valid token        |
| `password_reset_smtp_missing`           | Production reset attempted but SMTP env vars not set  |
| `login_rate_limited`                    | Login bucket exceeded                                 |
| `forgot_password_rate_limited`          | Forgot-password bucket exceeded                       |

Details columns include only the IP and (for reset) whether SMTP
delivery succeeded — never the token, never the URL, never the new
password.

## Verifying after deploy

1. Visit `/login` — there should be a "Forgot your password?" link
   below the form.
2. Click the link, submit a real email. You should:
   - Always get the same generic "if an account exists" message.
   - Receive an email with a `https://<your-domain>/reset-password/...`
     link if SMTP is configured.
3. Click the link, set a 12+ character password, and confirm you can
   log in with the new password. Try clicking the link a second time —
   it should redirect to `/forgot-password` with a "link is invalid or
   expired" flash.
4. From the operator dashboard, confirm the audit log has
   `password_reset_completed` for that user.
5. Try logging in with the wrong password 11 times in a row — the 11th
   attempt should return the friendly rate-limit message and a `429`
   status. Wait 5 minutes and confirm legitimate logins work again.

## Files added/changed by this feature

- `app.py` — forgot/reset routes, rate limiter wiring, password length
  policy, audit hooks.
- `app_db.py` — `password_reset_tokens` and `rate_limit_events` tables;
  helper methods.
- `email_sender.py` — minimal SMTP helper.
- `rate_limit.py` — sliding-window limiter on top of `AppDB`.
- `templates/forgot-password.html`, `templates/reset-password.html` —
  user-facing pages.
- `templates/login.html`, `templates/signup.html` — link to forgot
  flow; updated `minlength` on signup.
- `tests/smoke_password_reset.py` — coverage for the new flow,
  including generic-response, hashed-token-storage, single-use,
  expiry, length policy, and rate-limit assertions.
