# Security Posture (current)

A snapshot of what controls are in place today and what is still missing.
The honest version, written so a customer or auditor can read it.

## Controls in place

| Area | Control |
| --- | --- |
| Auth | Email + password (PBKDF2/SHA-256, 600 000 iterations, 16-byte salt). No anonymous access; every privileged route is `@login_required`. |
| Tenancy | Every job is firm-scoped via `_job_or_403(job_id)`. Cross-firm access returns 404, not 403, so existence is not leaked. |
| CSRF | Per-session token in `session["_csrf_token"]`, validated on every state-changing request (POST/PUT/PATCH/DELETE). Tokens injected into every form via `<input name="csrf_token">`. Mismatch → friendly flash + redirect, no 500. |
| Session cookies | `HttpOnly=True`, `SameSite=Lax`, and `Secure=True` when `APP_ENV != local`. |
| Encryption at rest | Uploaded CSVs and the QBO export CSV are AES-256 (Fernet) encrypted on disk. QBO access and refresh tokens are Fernet-encrypted in SQLite. |
| Encryption keys | `ENCRYPTION_KEY` is required env. `SECRET_KEY` is required when `APP_ENV != local`; the app refuses to start without it. |
| Auth audit | Signup, login, login_failed, logout, upload, qbo_connected, qbo_disconnected, qbo_token_refreshed, qbo_token_refresh_failed, import_demo, import_blocked, import_success, import_failed, verify, account_mapping_saved, delete_job, oauth_callback_firm_mismatch are all written to `audit_logs`. |
| QBO scope | One scope only: `com.intuit.quickbooks.accounting`. |
| QBO duplicate guard | `(file_sha256, realm_id)` and `(transaction_id, realm_id)` are checked against `import_history.imports` before any JournalEntry is posted. |
| QBO verification | After every successful import, each created JE is re-queried by Id and totals are compared to the source CSV. |
| Token refresh | Performed lazily, just-in-time, on every QBO call. Refresh failures surface a friendly "please reconnect" message and an audit row. |
| Database | SQLite via stdlib. ANSI-portable SQL so a Postgres swap is mechanical. |
| Defaults | `QBO_REAL_IMPORT=0` — demo mode is the default. |

## What is NOT in place yet

In rough priority order:

1. **No password reset.** Forgot-password is unimplemented. Workaround: an
   admin can manually update `users.password_hash` in SQLite using
   `app_db.hash_password(...)`. **Required before opening signup beyond a
   small beta.**
2. **No invitations / multi-user firms.** Each signup creates a new firm
   with one admin. Adding a second user means inserting a row in
   `users` directly.
3. **No 2FA / MFA.** TOTP via `pyotp` is the simplest add and would be
   the next thing for any customer with security requirements.
4. **No CSP / clickjacking protection** beyond the defaults Flask sets.
   `Content-Security-Policy` and `X-Frame-Options: DENY` should be added
   before opening the URL beyond a small beta.
5. **Single-tenant SQLite.** Fine for one Render web instance + the
   persistent disk. Move both `data/app.sqlite3` and
   `data/import_history.sqlite3` to managed Postgres before scaling
   beyond one instance or going to multiple regions.
6. **Encrypted CSVs on local disk.** `uploads/*.enc` and `processed/*.enc`
   live on the Render persistent disk. A disk failure or accidental
   redeploy without the disk attached loses those files. S3 + SSE-KMS is
   the recommended next step before non-trivial customer load.
7. **Sessions are signed cookies, not server-side.** A stolen cookie is
   valid until it expires. There is no "force logout one user" lever.
   Add a server-side session store (Redis or DB) and a `revoked_sessions`
   table.
8. **No SOC 2.** Required only if a customer asks for it in writing.
   Realistic timelines: SOC 2 Type I in ~6 weeks via Vanta/Drata; Type II
   is a year-long commitment.
9. **No data export / data deletion UI.** Required for GDPR/CCPA.
10. **Rate limiting.** None. Login + signup endpoints can be brute-forced.
    Add `flask-limiter` or front the deploy with Cloudflare Turnstile.
11. **`QBO_ENVIRONMENT=production`** still requires Intuit's separate app
    review. Stay on `sandbox` until that's complete.

## Threats addressed and not addressed

| Threat | Addressed? | How |
| --- | --- | --- |
| CSRF forgery from another origin | ✅ | per-session token + `before_request` check |
| Session cookie theft over HTTP | ✅ in production | `Secure` cookie when `APP_ENV != local` |
| XSS injecting into rendered pages | partial | Jinja autoescaping, no user-controlled HTML; no CSP yet |
| Cross-firm read of jobs | ✅ | `_job_or_403` 404s on mismatch |
| Cross-firm OAuth callback hijack | ✅ | callback verifies the job's firm_id matches the session user's |
| Brute-force password guessing | ❌ | no rate limiting yet |
| Stolen DB exfiltration | partial | passwords hashed; QBO tokens Fernet-encrypted; firm/user/audit data is plaintext (the last is by design — auditors need to read it) |
| Concurrent sign-in from multiple devices | n/a | not a threat we currently protect against |

## Encryption key rotation

- **`SECRET_KEY` rotation** invalidates every signed session cookie on the
  next request, which is the right behavior for incident response. No
  "force logout one user" yet — see #7 above.
- **`ENCRYPTION_KEY` rotation** is destructive: every Fernet ciphertext
  on disk and in the DB becomes unreadable. The app does not have a
  re-encrypt-with-new-key migration step yet. Plan accordingly.

## Where the secrets live

| Secret | Stored | How |
| --- | --- | --- |
| User passwords | `app.sqlite3` `users.password_hash` | PBKDF2/SHA-256, 600 000 iterations, 16-byte salt |
| QBO access + refresh tokens | `app.sqlite3` `qbo_connections.{access,refresh}_token_enc` | Fernet (AES-128-CBC + HMAC-SHA-256) |
| Encrypted CSVs | `uploads/*.enc`, `processed/*.enc` | Fernet |
| `ENCRYPTION_KEY` | Render env var, marked secret | Render manages |
| `SECRET_KEY` | Render env var, marked secret | Render manages |
| Intuit `QBO_CLIENT_SECRET` | Render env var, marked secret | Render manages |

No secrets in source control.
