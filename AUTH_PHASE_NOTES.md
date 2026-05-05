# Auth Phase — what's in, how to use it, what's still missing

## What's in this build

- **`app_db.py`** — separate SQLite DB for firms / users / jobs / qbo_connections /
  audit_logs (kept distinct from `import_history.py` so each module owns its
  schema). Both DBs live under `data/` by default.
- **Password hashing** with `werkzeug.security` (PBKDF2 + salt). No
  plaintext is stored or logged.
- **Routes**: `GET/POST /signup`, `GET/POST /login`, `POST /logout`,
  `GET /dashboard`. Public: `/login`, `/signup`, `/static/...`, plus
  `/oauth/callback` (which still requires a logged-in user).
- **`login_required` decorator** on every privileged route: `/upload`,
  `/jobs/<id>` (and all sub-routes), `/api/jobs/<id>`.
- **Firm-scoped job access** via `_job_or_403(job_id)`: returns 404 (not
  403, to avoid leaking existence) if the job's `firm_id` differs from the
  current user's. Every privileged route uses this helper.
- **Audit log** at: signup, login, login_failed, logout, upload,
  qbo_connected, qbo_disconnected, import_demo, import_blocked,
  import_success, import_failed, verify, delete_job,
  oauth_callback_firm_mismatch.
- **Job metadata mirrored to `app_db.jobs`** on upload and on every status
  change so the dashboard renders even after a restart. Job *details* like
  decrypted CSV rows, qbo_results, and verification still live in the
  in-memory `jobs` dict (that part still resets on restart — see
  limitations below).
- **`templates/signup.html`, `templates/login.html`, `templates/dashboard.html`**.
  `templates/job-detail.html` got a small login-aware header with a
  log-out button.
- **`/oauth/callback` defenses**: requires a logged-in user, requires the
  job's firm_id to match the user's; logs a `oauth_callback_firm_mismatch`
  audit event if it doesn't.

## How to run locally

```bash
cd ~/Desktop/pclaw-qbo-v2
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# minimal env to start the app — Intuit creds optional for the auth flow
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

# Optional: enable real QBO writes (still defaults to demo)
export QBO_CLIENT_ID="<sandbox client id>"
export QBO_CLIENT_SECRET="<sandbox client secret>"
export QBO_REDIRECT_URI="http://localhost:5000/oauth/callback"
export QBO_REAL_IMPORT=1

python3 app.py
# -> http://localhost:5000  (redirects to /login)
```

## Creating your first account

1. Open <http://localhost:5000>. You'll be redirected to `/login`.
2. Click **Create one** → fill in firm name, email, password (≥ 8 chars).
3. You're logged in as the firm admin and dropped on `/dashboard`.
4. Upload a PCLaw GL CSV. Click into the job; the rest of the flow
   (Connect to QuickBooks, Import, Verify) works exactly as before but
   every action is now audit-logged and firm-scoped.

## Run the smoke tests

```bash
python3 tests/smoke_auth.py     # auth + scoping
python3 tests/smoke_phase2.py   # the existing import flow, with auth integrated
```

Both should print `ALL ... PASSED`.

## Known limitations (next sprint)

These are *intentionally* unchanged in this sprint to keep the patch
small. Each is a tracked next step:

1. **In-memory job state still resets on restart.** The `jobs` dict and
   the in-memory `qbo_connections` (which holds Fernet-encrypted access
   and refresh tokens) are not yet persisted. The `app_db.jobs` table
   has the metadata so the dashboard list survives, but the actual
   decrypted CSV / running session does not. **Fix:** move `qbo_connections`
   into the DB (the schema is already there, just needs the encrypted
   token columns added) and rehydrate `jobs` on startup.
2. **No password reset / no account recovery.** Forgot-password is not
   implemented. Workaround for now: open the SQLite DB and update the
   user's `password_hash` directly.
3. **No invites or multi-user firms.** Each signup creates a new firm
   with one admin. To add a second user you'd need a CRUD UI for
   `users` (one-line SQL today).
4. **No CSRF protection.** Flask doesn't include CSRF by default. Add
   Flask-WTF or a manual same-site/double-submit token before exposing
   the app to the open internet.
5. **Sessions are signed cookies, not server-side.** Logout invalidates
   the session for the user but a stolen cookie is valid until expiry.
   Add a server-side session store (Redis or DB) for hard-logout.
6. **Single SQLite file.** Fine for one Render instance + the persistent
   disk. Move both `app.sqlite3` and `import_history.sqlite3` to
   Postgres before scaling beyond one instance — the SQL is portable.
7. **No tenant data export / deletion UI.** Required for GDPR/CCPA.
8. **Email field is the user identity.** Case-folded on save and lookup,
   but not validated for deliverability (no confirmation email).

## File-level diff vs Phase 2

| File | Change |
| --- | --- |
| `app_db.py` | **new** — auth + tenancy DB |
| `app.py` | imports, AppDB init, auth decorator + helpers, signup/login/logout/dashboard routes, every privileged route uses `_job_or_403`, audit calls, job metadata mirrored to DB, `/` redirects |
| `templates/login.html` | **new** |
| `templates/signup.html` | **new** |
| `templates/dashboard.html` | **new** (replaces the old `/` view) |
| `templates/job-detail.html` | login-aware header strip + log-out form; back link points to `/dashboard` |
| `tests/smoke_auth.py` | **new** — auth + scoping tests |
| `tests/smoke_phase2.py` | unchanged surface; updated to sign up + log in inline so the existing import-flow assertions still hold under the new auth gates |

## What was deliberately not changed

- Encrypted file-on-disk storage for uploads. Still in `uploads/` and
  `processed/` exactly as before.
- The `import_history.py` module and its database. Phase 2 duplicate
  prevention and verification still work unchanged.
- The QBO OAuth + import flow's behavior. The same routes do the same
  things, they just refuse to act for someone who isn't logged in or who
  doesn't own the job's firm.
- `QBO_REAL_IMPORT` safety gate.
- Encryption keys / Fernet behavior.
