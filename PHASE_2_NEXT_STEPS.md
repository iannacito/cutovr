# Phase 2 — Roadmap

What this sprint shipped, what's still missing, and the order I'd tackle it.

## Already in this build (Phase 2 foundation)

- **Persistent import history** — `import_history.py`, SQLite, three tables
  (`imports`, `imported_transactions`, `imported_entities`).
- **Duplicate-import prevention** — checked twice before any JE is posted:
  by `file_sha256 + realm_id` and by individual `transaction_id`s.
- **Import result panel** — source vs QBO transaction count, debit/credit
  totals, balanced flag.
- **Verification panel** — re-queries each created JE from QBO, sums
  debits/credits, compares to source, flags any not-found Ids.
- **Reconnect / disconnect** flow with company name shown.
- **Deployment files** — `render.yaml`, `DEPLOYMENT.md`, gunicorn in
  `requirements.txt`, persistent disk wired for the SQLite DB.

## Known limitations of this build

- **Single-tenant.** All jobs share one in-memory `jobs` dict. A restart
  drops in-progress jobs. The SQLite history survives, but jobs themselves
  do not. *This is the next thing to fix.*
- **No login.** Anyone who reaches the URL can upload, connect QBO, and
  trigger imports.
- **Encrypted files are local.** `uploads/` and `processed/` are on the
  Render disk. A disk failure is total data loss.
- **Verification is QBO-side, but not cryptographic.** It re-fetches by
  Id and confirms totals; it cannot detect a manual edit that happens to
  preserve totals.
- **No retry / partial-failure recovery.** If JE 3 of 5 fails mid-loop, the
  first 2 are written and recorded as a partial success in QBO but no
  history row is committed. (See "Idempotent batched import" below.)

## What I'd build next, in order

### 1. Persistent jobs (1-2 days)

Move the `jobs` dict into the same SQLite database. Same migration
strategy as `imports` — keep the SQL ANSI-compatible so a later swap to
Postgres is mechanical.

### 2. Auth and multi-tenant isolation (3-5 days)

- Add Flask-Login (or Authlib for SSO) and a `users` table.
- Add `user_id` foreign keys to `jobs` and `qbo_connections`.
- Filter every read by `current_user.id`; never look up a job globally.
- Move OAuth tokens out of the `qbo_connections` in-memory dict and into
  the DB (still Fernet-encrypted at rest).

### 3. Postgres migration (1-2 days)

- Drop SQLAlchemy in (or `psycopg` directly for minimal deps).
- Run the existing schema with `SERIAL PRIMARY KEY` instead of
  `AUTOINCREMENT`. The rest is unchanged.
- Set `DATABASE_URL` on Render and read in `import_history.py`.

### 4. Encrypted object storage (S3 / R2) (1 day)

- Replace the `uploads/` and `processed/` directories with an S3 client.
- Use SSE-KMS so AWS handles key management and audit logging.
- Pre-signed URLs only when downloading.

### 5. Idempotent batched import + retry (2 days)

- Wrap the JE-posting loop in a per-`transaction_id` checkpoint.
- Use a QBO request key (`requestid` query param) so QBO itself rejects
  duplicates server-side.
- On failure mid-batch, record what was written, prompt the user with
  "resume from JE-XXXX" rather than restarting from zero.

### 6. Account mapping UI (2-3 days)

Right now `find_unmapped_accounts` blocks the import with a flash. Build
a small UI panel that lists every PCLaw account and lets the user pick
the matching QBO account from a dropdown. Persist mappings per
`(realm_id, account_number)` so subsequent imports for that company are
zero-click.

### 7. SOC 2 readiness (multi-week)

This is policy and process more than code:

- Centralized audit log of every privileged action (login, OAuth connect,
  JE posted, mapping change). The `imports` table is a starting point;
  expand into a generic `audit_log` table.
- Background-checked vendors only for any service handling tokens.
- Documented data classification, encryption-at-rest evidence, key
  rotation runbook.
- Penetration test before going to production scale.
- 24/7 incident response on-call rotation.

If a customer is asking for SOC 2 specifically, the practical first steps
are: (a) get an auditor scoping call (Vanta / Drata / Secureframe sell
turnkey programs), (b) decide Type I (point-in-time) vs Type II (90-day
window). Type I is achievable in ~6 weeks. Type II is a year-long
commitment.

## Dependencies in plain English

- Get to **#1** before deploying for any real customer — losing in-progress
  jobs on every redeploy is unacceptable.
- Get to **#2** before sharing the URL with anyone outside the team.
- **#3** is only required when you outgrow a single Render disk (probably
  ~10k imports or when you need cross-region reads).
- **#4** is a hard requirement before Intuit production review.
- **#5** is a quality-of-life improvement, not a blocker.
- **#6** is the single change that will make the app feel "real" to a
  bookkeeper.
- **#7** is only worth starting when a customer has it as a
  written contract requirement.
