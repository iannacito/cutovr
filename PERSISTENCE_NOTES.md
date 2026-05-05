# Persistence Notes — restart-safe state

This sprint hardens the app so a Render redeploy / process restart no
longer loses signed-in users' work. The QBO OAuth + import + verify flow
is unchanged in behavior; the in-memory dicts are now write-through caches
in front of SQLite, and any cache miss is silently rehydrated.

## What now survives a restart

| State | Where it lives | Survives restart? |
| --- | --- | --- |
| Firms, users, password hashes | `data/app.sqlite3` → `firms`, `users` | ✅ |
| Jobs (firm_id, file_sha256, status, summary, qbo_results, import_summary, verification) | `data/app.sqlite3` → `jobs` | ✅ |
| QBO connections (Fernet-encrypted access + refresh tokens, realm_id, company name) | `data/app.sqlite3` → `qbo_connections` | ✅ |
| Audit log | `data/app.sqlite3` → `audit_logs` | ✅ |
| Import history (incl. duplicate-import dedupe) | `data/import_history.sqlite3` | ✅ (already from Phase 2) |
| Encrypted source CSVs (`uploads/*.enc`) | local disk | ✅ on persistent disk only |
| Encrypted output CSVs (`processed/*.enc`) | local disk | ✅ on persistent disk only |

In-memory `jobs` dict and `qbo_connections` dict are now caches only. On
any read miss (`_get_job`, `_get_qbo_connection`), the row is loaded from
SQLite and the cache is repopulated.

## How the cache + DB pattern works

- `_get_job(job_id)` checks `jobs` dict, falls back to
  `db.hydrate_job(job_id)` which decodes the JSON columns
  (`summary_json`, `qbo_results_json`, `import_summary_json`,
  `verification_json`) back into Python.
- `_get_qbo_connection(job_id)` checks `qbo_connections` dict, falls back
  to `db.get_qbo_connection(job_id)` and copies the encrypted token
  ciphertext into the cache. The cached ciphertext is decrypted lazily
  with `decrypt_token` at the moment we need to call QBO.
- `_save_job(job_id)` is called every time a route mutates the in-memory
  job (status change, OAuth complete, import success, verification
  result). Writes `status`, `output_file`, `encrypted_output`,
  `last_import_id`, and the JSON sub-objects.
- `db.upsert_qbo_connection(...)` is called from the OAuth callback with
  the **already-encrypted** access and refresh tokens. The DB layer
  never sees plaintext tokens.

## What's still local-disk only

- The encrypted PCLaw CSVs themselves (`uploads/*.enc`) and the encrypted
  QBO export CSVs (`processed/*.enc`). These must live on a persistent
  volume that survives restarts. On Render that's the 1 GB disk wired up
  in `render.yaml`. On a fresh Render container without that disk, the
  job *metadata* survives but the file referenced by `encrypted_file`
  won't, and the import will fail at the decrypt step.

  Migrating to S3 (with SSE-KMS) is the recommended next step for any
  multi-instance or cross-region deployment.

## Schema migration safety

`AppDB._migrate(...)` ALTERs each new column with `ADD COLUMN ...` and
swallows the "duplicate column" error. So upgrading an existing
`data/app.sqlite3` (from the auth-only build) does not require a wipe —
old rows continue to work, new columns default to NULL.

## Remaining limitations

- **SQLite is fine for one Render web instance.** Move both
  `data/app.sqlite3` and `data/import_history.sqlite3` to Postgres before
  scaling to multiple instances. The SQL is portable; the connection
  layer in `app_db.py` and `import_history.py` is what changes.
- **Sessions are signed cookies.** A `SECRET_KEY` rotation logs everyone
  out on the next request (which is the right behavior for incident
  response). No "force log out a single user" capability — that needs a
  server-side session store (Redis or DB) and a `revoked_sessions` table.
- **Token refresh is not yet automatic.** When the access token expires
  (60 min by default), the next QBO call returns 401 and the import
  fails with a flash that says to reconnect. We *store* the refresh
  token but don't yet use it. Wiring `qbo_auth.refresh_access_token(...)`
  in front of every QBO call is a small, well-scoped next change.
- **No password reset / no 2FA / no CSRF.** Same caveats as before.

## Run + verify

```bash
cd ~/Desktop/pclaw-qbo-v2
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

# Optional, only for real QBO writes:
export QBO_CLIENT_ID="<sandbox client id>"
export QBO_CLIENT_SECRET="<sandbox client secret>"
export QBO_REDIRECT_URI="http://localhost:5000/oauth/callback"
export QBO_REAL_IMPORT=1

python3 app.py        # http://localhost:5000

# Try the persistence loop manually:
#  1. Sign up, upload a CSV, connect QBO, import.
#  2. Stop the server with Ctrl-C.
#  3. Start it again with `python3 app.py`.
#  4. Log back in. Your firm, dashboard, jobs, and QBO connection should
#     all still be there. Click Re-verify against QuickBooks — it should
#     run successfully without re-doing the OAuth dance.
```

Run the offline test suites:

```bash
python3 tests/smoke_auth.py
python3 tests/smoke_phase2.py
python3 tests/smoke_persistence.py
```

All three should print `ALL ... PASSED`.

## Files changed in this sprint

- `app_db.py` — new columns on `jobs` and `qbo_connections`,
  `_migrate(...)` for in-place schema upgrades, `save_job_state(...)`,
  `hydrate_job(...)`, expanded `upsert_qbo_connection(...)`,
  `get_qbo_connection(...)`.
- `app.py` — new helpers `_get_job`, `_get_qbo_connection`, `_save_job`;
  `_job_or_403` and every consumer of `qbo_connections.get(...)` now go
  through these helpers; OAuth callback writes encrypted tokens to DB;
  every route that mutates job state calls `_save_job(...)`; disconnect
  removes the DB row; delete cascades children.
- `tests/smoke_persistence.py` — **new**, simulates a restart by clearing
  the in-memory caches and verifies dashboard/job-detail/verify still
  work, and that the DB never contains plaintext tokens.
- `PERSISTENCE_NOTES.md` — this file.

## What was deliberately not changed

- `QBO_REAL_IMPORT` safety gate.
- `SECRET_KEY` / `ENCRYPTION_KEY` env contract.
- `import_history.py` schema and behavior.
- The user-visible HTML / templates.
- Local file-based encrypted storage.
