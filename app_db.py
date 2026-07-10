"""
Application database for auth + tenancy.

This is intentionally separate from `import_history.py` so each module owns
its tables and migrations. Both use the same SQLite file by default, which
keeps the deploy footprint small. Postgres migration is a swap of the
connection layer; the SQL is ANSI-compatible.

Tables:

  firms
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL

  users
    id INTEGER PRIMARY KEY,
    firm_id INTEGER NOT NULL REFERENCES firms(id),
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',
    created_at TEXT NOT NULL

  jobs
    id TEXT PRIMARY KEY,                 -- matches the in-memory job id
    firm_id INTEGER NOT NULL REFERENCES firms(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    company TEXT,
    source_file TEXT,
    encrypted_file TEXT,
    file_sha256 TEXT,
    status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL

  qbo_connections
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    firm_id INTEGER NOT NULL REFERENCES firms(id),
    realm_id TEXT NOT NULL,
    company_name TEXT,
    connected_at TEXT NOT NULL
    -- NOTE: encrypted access/refresh tokens still live in the in-memory
    -- qbo_connections dict for now. Persisting them to this table
    -- (still Fernet-encrypted) is a tracked next step.

  audit_logs
    id INTEGER PRIMARY KEY,
    firm_id INTEGER,
    user_id INTEGER,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    details TEXT,
    created_at TEXT NOT NULL

Password storage: werkzeug's pbkdf2 with salt (the default of
`generate_password_hash`). Never persist plaintext.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from werkzeug.security import generate_password_hash, check_password_hash


# Werkzeug's default in recent versions is scrypt, which depends on
# hashlib.scrypt. On some Pythons (notably Apple's Xcode-bundled python3 on
# macOS, where Python is built against an OpenSSL that omits scrypt) that
# attribute is missing and signup blows up with:
#     AttributeError: module 'hashlib' has no attribute 'scrypt'
# Force PBKDF2/SHA-256 explicitly. It's available on every Python build,
# is still considered acceptable for password storage, and stays compatible
# with check_password_hash because the chosen method is encoded into the
# stored hash string itself.
PASSWORD_HASH_METHOD = "pbkdf2:sha256:600000"
PASSWORD_SALT_LENGTH = 16


def hash_password(password: str) -> str:
    """Return a salted PBKDF2/SHA-256 hash for `password`.

    Centralized so every place that creates a user uses the same algorithm.
    `check_password_hash` reads the algorithm from the stored hash, so this
    is forward-compatible with any future change to PASSWORD_HASH_METHOD.
    """
    return generate_password_hash(
        password,
        method=PASSWORD_HASH_METHOD,
        salt_length=PASSWORD_SALT_LENGTH,
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS firms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id         INTEGER NOT NULL REFERENCES firms(id),
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'admin',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_firm ON users(firm_id);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    firm_id         INTEGER NOT NULL REFERENCES firms(id),
    user_id         INTEGER NOT NULL REFERENCES users(id),
    company         TEXT,
    source_file     TEXT,
    encrypted_file  TEXT,
    encrypted_output TEXT,
    output_file     TEXT,
    file_sha256     TEXT,
    summary_json    TEXT,
    qbo_connected   INTEGER NOT NULL DEFAULT 0,
    qbo_results_json TEXT,
    import_summary_json TEXT,
    verification_json TEXT,
    last_import_id  INTEGER,
    status          TEXT,
    checkpoint      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_firm ON jobs(firm_id);

CREATE TABLE IF NOT EXISTS qbo_connections (
    job_id              TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    firm_id             INTEGER NOT NULL REFERENCES firms(id),
    realm_id            TEXT NOT NULL,
    company_name        TEXT,
    legal_name          TEXT,
    country             TEXT,
    access_token_enc    TEXT NOT NULL,
    refresh_token_enc   TEXT NOT NULL,
    expires_at          TEXT,
    company_info_error  TEXT,
    connected_at        TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_mappings (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id               INTEGER NOT NULL REFERENCES firms(id),
    realm_id              TEXT NOT NULL,
    pclaw_account_number  TEXT,
    pclaw_account_name    TEXT,
    qbo_account_id        TEXT NOT NULL,
    qbo_account_name      TEXT,
    qbo_account_type      TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (firm_id, realm_id, pclaw_account_number, pclaw_account_name)
);
CREATE INDEX IF NOT EXISTS idx_acctmap_firm_realm
    ON account_mappings(firm_id, realm_id);

CREATE TABLE IF NOT EXISTS entity_map (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id           INTEGER NOT NULL REFERENCES firms(id),
    realm_id          TEXT NOT NULL,
    kind              TEXT NOT NULL,
    normalized_name   TEXT NOT NULL,
    qbo_entity_id     TEXT NOT NULL,
    display_name      TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (firm_id, realm_id, kind, normalized_name)
);
CREATE INDEX IF NOT EXISTS idx_entitymap_firm_realm
    ON entity_map(firm_id, realm_id, kind);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id     INTEGER,
    user_id     INTEGER,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    details     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_firm ON audit_logs(firm_id, created_at);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pwreset_user ON password_reset_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_pwreset_hash ON password_reset_tokens(token_hash);

CREATE TABLE IF NOT EXISTS rate_limit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket_key  TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ratelimit_bucket
    ON rate_limit_events(bucket_key, created_at);

CREATE TABLE IF NOT EXISTS oauth_states (
    state        TEXT PRIMARY KEY,
    job_id       TEXT NOT NULL,
    firm_id      INTEGER NOT NULL REFERENCES firms(id),
    user_id      INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    consumed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_oauth_states_created ON oauth_states(created_at);

CREATE TABLE IF NOT EXISTS intake_submissions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    reference           TEXT NOT NULL UNIQUE,
    firm_id             INTEGER REFERENCES firms(id),
    user_id            INTEGER REFERENCES users(id),
    firm_name           TEXT NOT NULL,
    first_name          TEXT NOT NULL,
    last_name           TEXT NOT NULL,
    position            TEXT,
    phone               TEXT,
    email               TEXT NOT NULL,
    plan                TEXT,
    clio_migration_date TEXT,
    uploads_json        TEXT,
    job_id              TEXT,
    email_status        TEXT,
    payment_status      TEXT NOT NULL DEFAULT 'pending',
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intake_firm ON intake_submissions(firm_id);
CREATE INDEX IF NOT EXISTS idx_intake_created ON intake_submissions(created_at);

CREATE TABLE IF NOT EXISTS calendly_leads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    invitee_uri         TEXT NOT NULL UNIQUE,
    invitee_uuid        TEXT,
    event_uri           TEXT,
    event_type_uri      TEXT,
    event_name          TEXT,
    name                TEXT,
    email               TEXT,
    phone               TEXT,
    firm_name           TEXT,
    role                TEXT,
    migration_date      TEXT,
    years_history       TEXT,
    volume              TEXT,
    notes               TEXT,
    clio_rep_name       TEXT,
    clio_rep_email      TEXT,
    meeting_start       TEXT,
    meeting_end         TEXT,
    timezone            TEXT,
    status              TEXT NOT NULL DEFAULT 'scheduled',
    cancel_reason       TEXT,
    canceled_by         TEXT,
    rescheduled         INTEGER NOT NULL DEFAULT 0,
    questions_json      TEXT,
    enrichment_status   TEXT,
    raw_payload_json    TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calendly_leads_start
    ON calendly_leads(meeting_start);
CREATE INDEX IF NOT EXISTS idx_calendly_leads_created
    ON calendly_leads(created_at);

CREATE TABLE IF NOT EXISTS cutover_settings (
    firm_id              INTEGER PRIMARY KEY REFERENCES firms(id) ON DELETE CASCADE,
    cutover_date         TEXT,
    opening_balance_date TEXT,
    period_start         TEXT,
    period_end           TEXT,
    country              TEXT,
    accounting_basis     TEXT,
    migration_scope      TEXT,
    notes                TEXT,
    source_system        TEXT NOT NULL DEFAULT 'PCLaw',
    target_system        TEXT NOT NULL DEFAULT 'QBO',
    clio_involved        INTEGER NOT NULL DEFAULT 0,
    qbo_company_name     TEXT,
    qbo_realm_id         TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


class AppDB:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    def _migrate(self, c):
        """Add columns introduced after the initial schema. Idempotent.

        We never drop or rename columns here; that keeps existing rows
        readable. ALTER TABLE ... ADD COLUMN raises sqlite3.OperationalError
        when the column already exists, which we swallow.
        """
        def add_col(table, col_def):
            col_name = col_def.split()[0]
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

        # jobs table additions for full restart-safe state
        add_col("jobs", "encrypted_output TEXT")
        add_col("jobs", "output_file TEXT")
        add_col("jobs", "summary_json TEXT")
        add_col("jobs", "qbo_connected INTEGER NOT NULL DEFAULT 0")
        add_col("jobs", "qbo_results_json TEXT")
        add_col("jobs", "import_summary_json TEXT")
        add_col("jobs", "verification_json TEXT")
        add_col("jobs", "last_import_id INTEGER")
        add_col("jobs", "unmapped_accounts_json TEXT")
        add_col("jobs", "last_error_json TEXT")
        # Multi-report support: which PCLaw report this job represents
        # (general_ledger | chart_of_accounts | trial_balance | trust_listing).
        # Older rows default to general_ledger so backward compatibility
        # is preserved at read time.
        add_col("jobs", "report_type TEXT")
        # The job-detail preflight panel reads a per-report preflight dict
        # whose shape varies by report_type. Stored as JSON TEXT so the
        # rest of the schema stays flat.
        add_col("jobs", "preflight_json TEXT")
        # Persisted unique (account_number, account_name) list extracted at
        # upload time. Lets the Match-accounts screen survive loss of the
        # encrypted source CSV (e.g. ephemeral storage on a redeployed Render
        # instance) — the file isn't required to enumerate accounts again.
        add_col("jobs", "pclaw_accounts_json TEXT")
        # Full parsed GL rows captured at upload time. Lets the Send-to-QBO
        # import survive loss of the encrypted source CSV the same way the
        # account-mapping snapshot does. Without this, a redeployed Render
        # instance whose uploads/ tree was wiped would 500 on the
        # decrypt_file call at the top of /jobs/<id>/import-to-qbo.
        add_col("jobs", "gl_rows_json TEXT")
        # COA create history: the per-run record of QuickBooks accounts
        # created from a Chart-of-Accounts job (counts, intuit_tids, the
        # created/failed account lists). Without persistence the dashboard
        # checklist could never show "Account list created in QuickBooks"
        # after a reload, and replacing a COA upload wiped the memory of
        # what had already been created. Stored as JSON TEXT.
        add_col("jobs", "coa_create_history_json TEXT")
        # Operator type corrections for COA accounts (account_number/name ->
        # corrected QBO AccountType). Persisted alongside create history so
        # the corrections survive a reload too.
        add_col("jobs", "coa_type_overrides_json TEXT")
        # Entity-name blockers recorded when a GL import is refused because
        # A/R or A/P rows resolve to a blank customer/vendor name. Persisted
        # so the Migration Hub and job-detail page can keep showing which
        # ledger needs names after a reload/redeploy (the in-memory dict is
        # lost on restart). Shape: {"kind": "Customer"|"Vendor", "offenders": [...]}.
        add_col("jobs", "entity_name_blockers_json TEXT")
        # Opening-balance posting attempts (Trial Balance -> opening JE).
        # Each entry records success or a retryable failure (status, error,
        # qbo_je_id). Persisted so a failed attempt stays visible and
        # retryable after a reload instead of silently vanishing.
        add_col("jobs", "opening_balance_history_json TEXT")
        # Auto-balance synthetic entries for GL files with unmatched debit/credit
        # pairs. The preview page creates these on "Auto-fix" click; they are
        # merged into the GL at import time so the file posts balanced.
        # Persisted to DB so the Step 5 balance gate can skip the error block
        # if these are already in place (file WILL be balanced after merge).
        add_col("jobs", "auto_balance_rows_json TEXT")
        # Canonical migration checkpoint (durable, resumable foundation).
        # One of: uploaded | parsed | matched | reviewed | importing |
        # completed | needs_attention. Distinct from the free-text status
        # string, which is customer-facing prose; checkpoint is a stable
        # machine value used to resume a job at the correct step after a
        # refresh / re-login and to drive the operator per-job summary.
        add_col("jobs", "checkpoint TEXT")
        # Parsed rows for Trial Balance and Trust Listing uploads. Without
        # persistence the in-memory list lives only on the Gunicorn worker
        # that handled the upload; a subsequent request routed to the other
        # worker falls back to _reparse_report_rows(), which silently returns
        # [] on any exception. Storing them in the DB eliminates the
        # worker-switch failure mode, mirroring how gl_rows_json works.
        add_col("jobs", "parsed_trial_balance_json TEXT")
        add_col("jobs", "parsed_trust_listing_json TEXT")

        # cutover_settings: AR/AP migration strategy (Task 4 in the
        # migration-workflow completion PR). Default empty so existing
        # firms without a strategy continue to behave as "undecided",
        # which the guidance helper handles cleanly.
        add_col("cutover_settings", "ar_ap_strategy TEXT")

        # qbo_connections additions for encrypted token persistence
        add_col("qbo_connections", "legal_name TEXT")
        add_col("qbo_connections", "country TEXT")
        # Tokens: NOT NULL would break old rows, so add as nullable. The
        # write path always supplies them.
        add_col("qbo_connections", "access_token_enc TEXT")
        add_col("qbo_connections", "refresh_token_enc TEXT")
        add_col("qbo_connections", "expires_at TEXT")
        add_col("qbo_connections", "company_info_error TEXT")
        add_col("qbo_connections", "updated_at TEXT")

        # Post-purchase intake: payment status. We are Stripe-ready but do not
        # collect cards at intake time, so existing/new rows default to
        # 'pending' and are only flipped to 'paid' by a real Stripe path.
        add_col("intake_submissions", "payment_status TEXT NOT NULL DEFAULT 'pending'")

        # Package-first onboarding + Stripe Checkout linkage. These let a
        # Stripe success redirect / webhook find the originating onboarding
        # record and mark it paid. payment_amount_cents stores the charged
        # amount for the internal email + receipts; the Stripe IDs are the
        # durable keys the webhook keys off. All nullable so existing intake
        # rows (and quote-plan rows that never touch Stripe) stay valid.
        add_col("intake_submissions", "payment_amount_cents INTEGER")
        add_col("intake_submissions", "currency TEXT")
        add_col("intake_submissions", "stripe_session_id TEXT")
        add_col("intake_submissions", "stripe_payment_intent_id TEXT")
        add_col("intake_submissions", "username TEXT")
        add_col("intake_submissions", "employees TEXT")
        # Which migration service lane this intake is for (see service_lanes).
        # Nullable: existing rows without it default to the PCLaw -> QuickBooks
        # flow at read time, preserving original behavior.
        add_col("intake_submissions", "service_lane TEXT")
        # When this record was last touched by a Stripe event, so a replayed
        # webhook can be recognised as already-applied (idempotency).
        add_col("intake_submissions", "paid_at TEXT")

        # account_mappings: scope by source type (gl vs tb).
        # GL routes store mappings as source_type='gl'; TB routes as 'tb'.
        # Existing rows default to 'gl' so their behavior is unchanged.
        add_col("account_mappings", "source_type TEXT NOT NULL DEFAULT 'gl'")

        # Rebuild account_mappings with source_type inside the UNIQUE key.
        # Guard: skip if the sentinel index already exists (idempotent).
        _existing_idx = {
            r[1] for r in c.execute(
                "PRAGMA index_list(account_mappings)"
            ).fetchall()
        }
        if "idx_acctmap_source" not in _existing_idx:
            c.executescript("""
                CREATE TABLE account_mappings_v2 (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    firm_id               INTEGER NOT NULL REFERENCES firms(id),
                    realm_id              TEXT    NOT NULL,
                    pclaw_account_number  TEXT,
                    pclaw_account_name    TEXT,
                    qbo_account_id        TEXT    NOT NULL,
                    qbo_account_name      TEXT,
                    qbo_account_type      TEXT,
                    source_type           TEXT    NOT NULL DEFAULT 'gl',
                    created_at            TEXT    NOT NULL,
                    updated_at            TEXT    NOT NULL,
                    UNIQUE (firm_id, realm_id, pclaw_account_number,
                            pclaw_account_name, source_type)
                );
                INSERT OR IGNORE INTO account_mappings_v2
                    (id, firm_id, realm_id, pclaw_account_number,
                     pclaw_account_name, qbo_account_id, qbo_account_name,
                     qbo_account_type, source_type, created_at, updated_at)
                SELECT id, firm_id, realm_id, pclaw_account_number,
                       pclaw_account_name, qbo_account_id, qbo_account_name,
                       qbo_account_type, COALESCE(source_type, 'gl'),
                       created_at, updated_at
                FROM account_mappings;
                DROP TABLE account_mappings;
                ALTER TABLE account_mappings_v2 RENAME TO account_mappings;
                CREATE INDEX IF NOT EXISTS idx_acctmap_firm_realm
                    ON account_mappings(firm_id, realm_id);
                CREATE INDEX IF NOT EXISTS idx_acctmap_source
                    ON account_mappings(firm_id, realm_id, source_type);
            """)

        # Calendly lead: first-class columns derived from the discovery-call
        # form answers, added after the initial calendly_leads schema. All
        # nullable so existing lead rows stay valid; the webhook/sync write
        # path fills them from questions_and_answers when present.
        add_col("calendly_leads", "role TEXT")
        add_col("calendly_leads", "migration_date TEXT")
        add_col("calendly_leads", "years_history TEXT")
        add_col("calendly_leads", "volume TEXT")
        add_col("calendly_leads", "notes TEXT")
        # Which migration service lane the lead is interested in (see
        # service_lanes). Nullable; filled from the Calendly form answers /
        # event name when they clearly name a lane.
        add_col("calendly_leads", "service_lane TEXT")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- firms / users -----------------------------------------------------

    def create_firm_and_admin(self, firm_name: str, email: str, password: str) -> tuple[int, int]:
        """Create a firm + its first admin user atomically. Returns (firm_id, user_id)."""
        email = email.strip().lower()
        with self._conn() as c:
            existing = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                raise ValueError("An account with that email already exists.")
            cur = c.execute(
                "INSERT INTO firms (name, created_at) VALUES (?, ?)",
                (firm_name.strip(), _now()),
            )
            firm_id = cur.lastrowid
            cur = c.execute(
                "INSERT INTO users (firm_id, email, password_hash, role, created_at) "
                "VALUES (?, ?, ?, 'admin', ?)",
                (firm_id, email, hash_password(password), _now()),
            )
            user_id = cur.lastrowid
            return firm_id, user_id

    def authenticate(self, email: str, password: str) -> Optional[dict]:
        """Return the user dict on success, None on bad email/password."""
        email = email.strip().lower()
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()
            if not row:
                return None
            if not check_password_hash(row["password_hash"], password):
                return None
            return dict(row)

    def get_user_by_email(self, email: str) -> Optional[dict]:
        email = email.strip().lower()
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()
            return dict(row) if row else None

    def update_user_password(self, user_id: int, new_password: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(new_password), user_id),
            )

    # --- password reset tokens --------------------------------------------

    def create_password_reset_token(
        self, user_id: int, token_hash: str, expires_at: str
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO password_reset_tokens "
                "(user_id, token_hash, expires_at, used_at, created_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                (user_id, token_hash, expires_at, _now()),
            )
            return cur.lastrowid

    def get_password_reset_token(self, token_hash: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM password_reset_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            return dict(row) if row else None

    def mark_password_reset_used(self, token_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE password_reset_tokens SET used_at = ? WHERE id = ?",
                (_now(), token_id),
            )

    def invalidate_user_reset_tokens(self, user_id: int) -> None:
        """Mark all outstanding (unused) reset tokens for a user as used.

        Called after a successful reset so any other emailed token for the
        same account can no longer be redeemed.
        """
        with self._conn() as c:
            c.execute(
                "UPDATE password_reset_tokens SET used_at = ? "
                "WHERE user_id = ? AND used_at IS NULL",
                (_now(), user_id),
            )

    # --- oauth states (durable, single-use CSRF + job binding) ------------

    def create_oauth_state(
        self, state: str, job_id: str, firm_id: int, user_id: int
    ) -> None:
        """Persist an outbound OAuth state so the callback can recover the
        originating migration job even if the browser session is lost on
        the Intuit round-trip.

        ``state`` is the exact value sent as the ``state`` query parameter
        to Intuit (``"<job_id>:<nonce>"``); Intuit echoes it back verbatim,
        which makes it a durable, server-side key into this row. The row is
        the source of truth for which job/firm an inbound callback belongs
        to — independent of the session cookie.
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO oauth_states (state, job_id, firm_id, user_id, "
                "                          created_at, consumed_at) "
                "VALUES (?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(state) DO UPDATE SET "
                "    job_id=excluded.job_id, firm_id=excluded.firm_id, "
                "    user_id=excluded.user_id, created_at=excluded.created_at, "
                "    consumed_at=NULL",
                (state, job_id, firm_id, user_id, _now()),
            )

    def consume_oauth_state(self, state: str, max_age_seconds: int) -> Optional[dict]:
        """Atomically mark an OAuth state row as consumed and return it.

        Single-use: the UPDATE only matches a row that has not already been
        consumed, so a replayed callback (same ``state`` twice) returns
        None on the second attempt. Time-limited: rows older than
        ``max_age_seconds`` are treated as expired and rejected.

        Returns the row dict (job_id, firm_id, user_id, ...) on success, or
        None if the state is unknown, already consumed, or expired.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=max(1, max_age_seconds))).isoformat()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE oauth_states SET consumed_at = ? "
                "WHERE state = ? AND consumed_at IS NULL AND created_at >= ?",
                (_now(), state, cutoff),
            )
            if not cur.rowcount:
                return None
            row = c.execute(
                "SELECT * FROM oauth_states WHERE state = ?", (state,)
            ).fetchone()
            return dict(row) if row else None

    def peek_oauth_state(self, state: str) -> Optional[dict]:
        """Return an OAuth state row without consuming it.

        Used only to recover the originating job_id for a friendly
        post-failure recovery link when the normal consume path could not
        complete (e.g. the user was bounced to login). Never grants a
        connection by itself.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM oauth_states WHERE state = ?", (state,)
            ).fetchone()
            return dict(row) if row else None

    def purge_expired_oauth_states(self, max_age_seconds: int) -> int:
        """Delete consumed or expired OAuth state rows. Returns count.

        These are short-lived, single-use CSRF tokens; once consumed or
        past their window they have no value and should not linger on disk.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=max(1, max_age_seconds))).isoformat()
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM oauth_states "
                "WHERE consumed_at IS NOT NULL OR created_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0

    # --- rate limiting ----------------------------------------------------

    def record_rate_limit_event(self, bucket_key: str, ts: float) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO rate_limit_events (bucket_key, created_at) VALUES (?, ?)",
                (bucket_key, ts),
            )

    def count_rate_limit_events(self, bucket_key: str, since_ts: float) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM rate_limit_events "
                "WHERE bucket_key = ? AND created_at >= ?",
                (bucket_key, since_ts),
            ).fetchone()
            return int(row["n"]) if row else 0

    def purge_old_rate_limit_events(self, before_ts: float) -> int:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM rate_limit_events WHERE created_at < ?", (before_ts,)
            )
            return cur.rowcount or 0

    # --- data-retention cleanup -------------------------------------------

    def purge_expired_reset_tokens(self) -> int:
        """Delete password-reset tokens that are already used OR past their
        expiry. Single-use, time-limited tokens have no value once spent or
        stale; keeping them only grows the table and leaves hashed secrets
        on disk longer than necessary. Returns the number of rows removed.
        """
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM password_reset_tokens "
                "WHERE used_at IS NOT NULL OR expires_at < ?",
                (_now(),),
            )
            return cur.rowcount or 0

    def list_archived_jobs_before(self, before_iso: str) -> list:
        """Return archived jobs whose row was last updated before
        ``before_iso`` (an ISO-8601 timestamp). Used by retention cleanup
        to find stale demo/abandoned jobs whose encrypted files can be
        purged. Only ever returns jobs whose status marks them archived,
        so an active in-progress migration is never selected.

        Returns the operator-safe columns the cleanup routine needs —
        never token blobs.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, firm_id, company, encrypted_file, encrypted_output, "
                "       status, updated_at "
                "FROM jobs "
                "WHERE status LIKE 'Archived%' AND updated_at < ? "
                "ORDER BY updated_at ASC",
                (before_iso,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def get_firm(self, firm_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM firms WHERE id = ?", (firm_id,)).fetchone()
            return dict(row) if row else None

    # --- jobs --------------------------------------------------------------

    def upsert_job(
        self,
        job_id: str,
        firm_id: int,
        user_id: int,
        company: str,
        source_file: str,
        encrypted_file: str,
        file_sha256: str,
        status: str,
    ) -> None:
        """Initial insert at upload time. Use save_job_state() for updates."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO jobs (id, firm_id, user_id, company, source_file, encrypted_file, "
                "                  file_sha256, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
                (
                    job_id, firm_id, user_id, company, source_file, encrypted_file,
                    file_sha256, status, _now(), _now(),
                ),
            )

    def save_job_state(self, job_id: str, job_dict: dict) -> None:
        """Persist a snapshot of the in-memory job dict.

        Required keys: status. Optional keys are written when present so the
        caller doesn't need to construct a complete row. JSON-serializable
        sub-objects (summary, qbo_results, import_summary, verification)
        are stored as TEXT to keep the schema flat.
        """
        import json
        fields = ["status", "updated_at"]
        values: list = [job_dict.get("status"), _now()]

        for key, db_col in [
            ("output_file", "output_file"),
            ("encrypted_output", "encrypted_output"),
            ("last_import_id", "last_import_id"),
            ("checkpoint", "checkpoint"),
        ]:
            if key in job_dict:
                fields.append(db_col)
                values.append(job_dict[key])

        if "summary" in job_dict:
            fields.append("summary_json")
            values.append(json.dumps(job_dict["summary"]) if job_dict["summary"] is not None else None)
        if "qbo_results" in job_dict:
            fields.append("qbo_results_json")
            values.append(json.dumps(job_dict["qbo_results"]) if job_dict["qbo_results"] is not None else None)
        if "import_summary" in job_dict:
            fields.append("import_summary_json")
            values.append(json.dumps(job_dict["import_summary"]) if job_dict["import_summary"] is not None else None)
        if "verification" in job_dict:
            fields.append("verification_json")
            values.append(json.dumps(job_dict["verification"]) if job_dict["verification"] is not None else None)
        if "unmapped_accounts" in job_dict:
            fields.append("unmapped_accounts_json")
            values.append(json.dumps(job_dict["unmapped_accounts"]) if job_dict["unmapped_accounts"] else None)
        if "last_error" in job_dict:
            fields.append("last_error_json")
            values.append(json.dumps(job_dict["last_error"]) if job_dict["last_error"] else None)
        if "qbo_connected" in job_dict:
            fields.append("qbo_connected")
            values.append(1 if job_dict["qbo_connected"] else 0)
        if "report_type" in job_dict:
            fields.append("report_type")
            values.append(job_dict["report_type"])
        if "preflight" in job_dict:
            fields.append("preflight_json")
            values.append(
                json.dumps(job_dict["preflight"]) if job_dict["preflight"] is not None else None
            )
        if "pclaw_accounts" in job_dict:
            fields.append("pclaw_accounts_json")
            values.append(
                json.dumps(job_dict["pclaw_accounts"])
                if job_dict["pclaw_accounts"] is not None
                else None
            )
        if "gl_rows" in job_dict:
            fields.append("gl_rows_json")
            values.append(
                json.dumps(job_dict["gl_rows"])
                if job_dict["gl_rows"] is not None
                else None
            )
        if "coa_create_history" in job_dict:
            fields.append("coa_create_history_json")
            values.append(
                json.dumps(job_dict["coa_create_history"])
                if job_dict["coa_create_history"]
                else None
            )
        if "coa_type_overrides" in job_dict:
            fields.append("coa_type_overrides_json")
            values.append(
                json.dumps(job_dict["coa_type_overrides"])
                if job_dict["coa_type_overrides"]
                else None
            )
        if "entity_name_blockers" in job_dict:
            fields.append("entity_name_blockers_json")
            values.append(
                json.dumps(job_dict["entity_name_blockers"])
                if job_dict["entity_name_blockers"]
                else None
            )
        if "opening_balance_history" in job_dict:
            fields.append("opening_balance_history_json")
            values.append(
                json.dumps(job_dict["opening_balance_history"])
                if job_dict["opening_balance_history"]
                else None
            )
        if "auto_balance_rows" in job_dict:
            fields.append("auto_balance_rows_json")
            values.append(
                json.dumps(job_dict["auto_balance_rows"])
                if job_dict["auto_balance_rows"]
                else None
            )
        if "parsed_trial_balance" in job_dict:
            fields.append("parsed_trial_balance_json")
            values.append(
                json.dumps(job_dict["parsed_trial_balance"])
                if job_dict["parsed_trial_balance"]
                else None
            )
        if "parsed_trust_listing" in job_dict:
            fields.append("parsed_trust_listing_json")
            values.append(
                json.dumps(job_dict["parsed_trust_listing"])
                if job_dict["parsed_trust_listing"]
                else None
            )

        set_clause = ", ".join(f"{f} = ?" for f in fields)
        values.append(job_id)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", values)

    def update_job_status(self, job_id: str, status: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), job_id),
            )

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def hydrate_job(self, job_id: str) -> Optional[dict]:
        """Return a job in the same shape the in-memory cache uses.

        JSON columns are decoded back into Python objects so the rest of
        the app can read job["summary"], job["qbo_results"], etc.
        """
        import json
        row = self.get_job(job_id)
        if not row:
            return None
        out = {
            "id": row["id"],
            "firm_id": row["firm_id"],
            "user_id": row["user_id"],
            "company": row["company"],
            "email": "",  # not stored; UI uses user.email anyway
            "source_file": row["source_file"],
            "encrypted_file": row["encrypted_file"],
            "encrypted_output": row.get("encrypted_output") if isinstance(row, dict) else row["encrypted_output"],
            "output_file": row["output_file"] if "output_file" in row.keys() else None,
            "file_sha256": row["file_sha256"],
            "status": row["status"],
            "created_at": row["created_at"],
            "qbo_connected": bool(row["qbo_connected"]) if row["qbo_connected"] is not None else False,
        }
        # report_type defaults to general_ledger for legacy rows.
        rt = row["report_type"] if "report_type" in row.keys() else None
        out["report_type"] = rt or "general_ledger"
        # Canonical resumable checkpoint (may be None for legacy rows).
        out["checkpoint"] = row["checkpoint"] if "checkpoint" in row.keys() else None
        # decode JSON sub-objects
        for src, dst in [
            ("summary_json", "summary"),
            ("qbo_results_json", "qbo_results"),
            ("import_summary_json", "import_summary"),
            ("verification_json", "verification"),
            ("unmapped_accounts_json", "unmapped_accounts"),
            ("last_error_json", "last_error"),
            ("preflight_json", "preflight"),
            ("pclaw_accounts_json", "pclaw_accounts"),
            ("gl_rows_json", "gl_rows"),
            ("coa_create_history_json", "coa_create_history"),
            ("coa_type_overrides_json", "coa_type_overrides"),
            ("entity_name_blockers_json", "entity_name_blockers"),
            ("opening_balance_history_json", "opening_balance_history"),
            ("auto_balance_rows_json", "auto_balance_rows"),
            ("parsed_trial_balance_json", "parsed_trial_balance"),
            ("parsed_trust_listing_json", "parsed_trust_listing"),
        ]:
            v = row[src] if src in row.keys() else None
            if v:
                try:
                    out[dst] = json.loads(v)
                except (ValueError, TypeError):
                    out[dst] = None
        if row["last_import_id"]:
            out["last_import_id"] = row["last_import_id"]
        return out

    def list_jobs_for_firm(self, firm_id: int, limit: int = 50) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE firm_id = ? ORDER BY created_at DESC LIMIT ?",
                (firm_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_job(self, job_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM qbo_connections WHERE job_id = ?", (job_id,))
            c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def clear_job_file_pointers(self, job_id: str) -> None:
        """Null out the encrypted-file pointers for a job whose on-disk
        blobs were removed by retention cleanup. Keeps the row (audit
        history) but prevents later reads from chasing a missing file.
        """
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET encrypted_file = NULL, encrypted_output = NULL, "
                "updated_at = ? WHERE id = ?",
                (_now(), job_id),
            )

    def all_job_file_pointers(self) -> list:
        """Return {id, encrypted_file, encrypted_output} for every job row.

        Used by retention cleanup to know which encrypted blobs on disk are
        still referenced by a live job. Returns no token columns.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, encrypted_file, encrypted_output FROM jobs"
            ).fetchall()
            return [dict(r) for r in rows]

    # --- qbo connections (metadata only; tokens still in-memory) ----------

    def upsert_qbo_connection(
        self,
        job_id: str,
        firm_id: int,
        realm_id: str,
        access_token_enc: str,
        refresh_token_enc: str,
        company_name: Optional[str] = None,
        legal_name: Optional[str] = None,
        country: Optional[str] = None,
        expires_at: Optional[str] = None,
        company_info_error: Optional[str] = None,
    ) -> None:
        """Insert or update the encrypted QBO connection record for a job.

        Tokens MUST be encrypted before being passed in. This module never
        sees plaintext tokens.
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO qbo_connections "
                "(job_id, firm_id, realm_id, company_name, legal_name, country, "
                " access_token_enc, refresh_token_enc, expires_at, company_info_error, "
                " connected_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET "
                "    realm_id=excluded.realm_id, company_name=excluded.company_name, "
                "    legal_name=excluded.legal_name, country=excluded.country, "
                "    access_token_enc=excluded.access_token_enc, "
                "    refresh_token_enc=excluded.refresh_token_enc, "
                "    expires_at=excluded.expires_at, "
                "    company_info_error=excluded.company_info_error, "
                "    updated_at=excluded.updated_at",
                (
                    job_id, firm_id, realm_id, company_name, legal_name, country,
                    access_token_enc, refresh_token_enc, expires_at, company_info_error,
                    _now(), _now(),
                ),
            )

    def get_qbo_connection(self, job_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM qbo_connections WHERE job_id = ?", (job_id,)
            ).fetchone()
            return dict(row) if row else None

    def delete_qbo_connection(self, job_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM qbo_connections WHERE job_id = ?", (job_id,))

    def list_qbo_connections_for_firm(self, firm_id: int) -> list:
        """Return every QBO connection (one per job) for the given firm.

        Used by the /quickbooks management page so the user can see what
        QuickBooks companies they currently have connected and disconnect
        any of them. Tokens stay encrypted; the page never decrypts them.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT job_id, firm_id, realm_id, company_name, legal_name, "
                "country, expires_at, company_info_error, connected_at, "
                "updated_at FROM qbo_connections WHERE firm_id = ? "
                "ORDER BY connected_at DESC",
                (firm_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_qbo_connections_for_firm(self, firm_id: int) -> int:
        """Delete every QBO connection row for a firm. Returns count.

        Used by the public /disconnect flow when a logged-in user requests
        a "disconnect everything" so we drop encrypted tokens for every job
        owned by their firm.
        """
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM qbo_connections WHERE firm_id = ?", (firm_id,)
            )
            return cur.rowcount or 0

    # --- account mappings --------------------------------------------------

    def list_account_mappings(
        self, firm_id: int, realm_id: str, source_type: str | None = "gl"
    ) -> list:
        with self._conn() as c:
            if source_type is None:
                rows = c.execute(
                    "SELECT * FROM account_mappings WHERE firm_id = ? AND realm_id = ? "
                    "ORDER BY pclaw_account_number, pclaw_account_name",
                    (firm_id, realm_id),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM account_mappings WHERE firm_id = ? AND realm_id = ? "
                    "AND source_type = ? "
                    "ORDER BY pclaw_account_number, pclaw_account_name",
                    (firm_id, realm_id, source_type),
                ).fetchall()
            return [dict(r) for r in rows]

    def save_account_mapping(
        self,
        firm_id: int,
        realm_id: str,
        pclaw_account_number: Optional[str],
        pclaw_account_name: Optional[str],
        qbo_account_id: str,
        qbo_account_name: Optional[str] = None,
        qbo_account_type: Optional[str] = None,
        source_type: str = "gl",
    ) -> None:
        """Insert or update one mapping row.

        The unique key is (firm_id, realm_id, pclaw_account_number,
        pclaw_account_name, source_type). Use source_type='gl' for GL routes
        and source_type='tb' for Trial Balance routes so their mappings never
        collide even when the PCLaw account key is identical.
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO account_mappings "
                "(firm_id, realm_id, pclaw_account_number, pclaw_account_name, "
                " qbo_account_id, qbo_account_name, qbo_account_type, source_type, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(firm_id, realm_id, pclaw_account_number, "
                "            pclaw_account_name, source_type) "
                "DO UPDATE SET qbo_account_id = excluded.qbo_account_id, "
                "              qbo_account_name = excluded.qbo_account_name, "
                "              qbo_account_type = excluded.qbo_account_type, "
                "              updated_at = excluded.updated_at",
                (
                    firm_id, realm_id, pclaw_account_number, pclaw_account_name,
                    qbo_account_id, qbo_account_name, qbo_account_type, source_type,
                    _now(), _now(),
                ),
            )

    def delete_account_mapping(
        self, firm_id: int, realm_id: str,
        pclaw_account_number: Optional[str], pclaw_account_name: Optional[str],
        source_type: str = "gl",
    ) -> None:
        with self._conn() as c:
            c.execute(
                "DELETE FROM account_mappings WHERE firm_id = ? AND realm_id = ? "
                "AND pclaw_account_number IS ? AND pclaw_account_name IS ? "
                "AND source_type = ?",
                (firm_id, realm_id, pclaw_account_number, pclaw_account_name,
                 source_type),
            )

    # --- entity map (resolved QBO customers / vendors) ---------------------

    def list_entity_map(self, firm_id: int, realm_id: str) -> list:
        """Return every resolved Customer/Vendor mapping for a firm+realm.

        Each row records a normalized entity name and the QuickBooks Id it
        resolved to, so a re-run of a GL import reuses the same QuickBooks
        entity instead of re-querying or re-creating it.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT kind, normalized_name, qbo_entity_id, display_name "
                "FROM entity_map WHERE firm_id = ? AND realm_id = ?",
                (firm_id, realm_id),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_entity_map_entry(
        self,
        firm_id: int,
        realm_id: str,
        kind: str,
        normalized_name: str,
        qbo_entity_id: str,
        display_name: Optional[str] = None,
    ) -> None:
        """Persist one resolved entity (Customer/Vendor) by normalized name.

        Keyed on (firm_id, realm_id, kind, normalized_name) so re-resolving
        the same name updates the stored Id rather than inserting a
        duplicate row.
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO entity_map "
                "(firm_id, realm_id, kind, normalized_name, qbo_entity_id, "
                " display_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(firm_id, realm_id, kind, normalized_name) "
                "DO UPDATE SET qbo_entity_id = excluded.qbo_entity_id, "
                "              display_name = excluded.display_name, "
                "              updated_at = excluded.updated_at",
                (
                    firm_id, realm_id, kind, normalized_name, qbo_entity_id,
                    display_name, _now(), _now(),
                ),
            )

    def delete_entity_map_for_firm(self, firm_id: int) -> int:
        """Drop every entity-map row for a firm. Returns the count deleted.

        Used by Start-new-migration so a fresh migration doesn't carry
        stale entity resolutions from the prior client's books.
        """
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM entity_map WHERE firm_id = ?", (firm_id,)
            )
            return cur.rowcount or 0

    # --- cutover settings --------------------------------------------------

    def get_cutover_settings(self, firm_id: int) -> Optional[dict]:
        """Return the cutover settings row for a firm, or None if never saved.

        Callers should treat None as "firm has not completed the cutover
        setup step" and surface the onboarding nudge.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM cutover_settings WHERE firm_id = ?",
                (firm_id,),
            ).fetchone()
            return dict(row) if row else None

    def delete_cutover_settings(self, firm_id: int) -> int:
        """Remove a firm's cutover settings row so the next migration
        starts from a blank Step 1.

        Used by the production "Start a new migration" reset. Returns the
        number of rows deleted (0 or 1). This only clears the firm's typed
        migration configuration — it never touches QuickBooks or job rows.
        """
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM cutover_settings WHERE firm_id = ?",
                (firm_id,),
            )
            return cur.rowcount or 0

    def upsert_cutover_settings(
        self,
        firm_id: int,
        *,
        cutover_date: Optional[str] = None,
        opening_balance_date: Optional[str] = None,
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
        country: Optional[str] = None,
        accounting_basis: Optional[str] = None,
        migration_scope: Optional[str] = None,
        notes: Optional[str] = None,
        source_system: str = "PCLaw",
        target_system: str = "QBO",
        clio_involved: bool = False,
        qbo_company_name: Optional[str] = None,
        qbo_realm_id: Optional[str] = None,
        ar_ap_strategy: Optional[str] = None,
    ) -> None:
        """Insert or update the firm's cutover settings.

        Every field is optional so a firm can save partial progress as
        they figure things out. We never persist secrets here; this is
        configuration the firm admin types in.
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO cutover_settings "
                "(firm_id, cutover_date, opening_balance_date, period_start, "
                " period_end, country, accounting_basis, migration_scope, "
                " notes, source_system, target_system, clio_involved, "
                " qbo_company_name, qbo_realm_id, ar_ap_strategy, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(firm_id) DO UPDATE SET "
                "    cutover_date=excluded.cutover_date, "
                "    opening_balance_date=excluded.opening_balance_date, "
                "    period_start=excluded.period_start, "
                "    period_end=excluded.period_end, "
                "    country=excluded.country, "
                "    accounting_basis=excluded.accounting_basis, "
                "    migration_scope=excluded.migration_scope, "
                "    notes=excluded.notes, "
                "    source_system=excluded.source_system, "
                "    target_system=excluded.target_system, "
                "    clio_involved=excluded.clio_involved, "
                "    qbo_company_name=excluded.qbo_company_name, "
                "    qbo_realm_id=excluded.qbo_realm_id, "
                "    ar_ap_strategy=excluded.ar_ap_strategy, "
                "    updated_at=excluded.updated_at",
                (
                    firm_id, cutover_date, opening_balance_date, period_start,
                    period_end, country, accounting_basis, migration_scope,
                    notes, source_system or "PCLaw", target_system or "QBO",
                    1 if clio_involved else 0, qbo_company_name, qbo_realm_id,
                    ar_ap_strategy,
                    _now(), _now(),
                ),
            )

    # --- audit log ---------------------------------------------------------

    def audit(
        self,
        action: str,
        firm_id: Optional[int] = None,
        user_id: Optional[int] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        details: Optional[str] = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO audit_logs (firm_id, user_id, action, target_type, target_id, "
                "                        details, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (firm_id, user_id, action, target_type, target_id, details, _now()),
            )

    def recent_audit_for_firm(self, firm_id: int, limit: int = 20) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM audit_logs WHERE firm_id = ? ORDER BY id DESC LIMIT ?",
                (firm_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # --- post-purchase intake submissions ---------------------------------

    def create_intake_submission(
        self,
        *,
        reference: str,
        firm_name: str,
        first_name: str,
        last_name: str,
        email: str,
        position: Optional[str] = None,
        phone: Optional[str] = None,
        plan: Optional[str] = None,
        clio_migration_date: Optional[str] = None,
        uploads_json: Optional[str] = None,
        firm_id: Optional[int] = None,
        user_id: Optional[int] = None,
        job_id: Optional[str] = None,
        payment_status: str = "pending",
        payment_amount_cents: Optional[int] = None,
        currency: Optional[str] = None,
        username: Optional[str] = None,
        employees: Optional[str] = None,
        service_lane: Optional[str] = None,
    ) -> int:
        """Persist a post-purchase onboarding intake record. Returns its id."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO intake_submissions ("
                "  reference, firm_id, user_id, firm_name, first_name, last_name, "
                "  position, phone, email, plan, clio_migration_date, uploads_json, "
                "  job_id, email_status, payment_status, payment_amount_cents, "
                "  currency, username, employees, service_lane, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reference, firm_id, user_id, firm_name.strip(),
                    first_name.strip(), last_name.strip(),
                    (position or "").strip() or None,
                    (phone or "").strip() or None,
                    email.strip().lower(),
                    (plan or "").strip() or None,
                    (clio_migration_date or "").strip() or None,
                    uploads_json, job_id,
                    (payment_status or "pending").strip().lower(),
                    payment_amount_cents,
                    (currency or "").strip().lower() or None,
                    (username or "").strip() or None,
                    (employees or "").strip() or None,
                    (service_lane or "").strip() or None,
                    _now(),
                ),
            )
            return cur.lastrowid

    def attach_stripe_session(
        self, intake_id: int, *, session_id: str,
        payment_amount_cents: Optional[int] = None,
        currency: Optional[str] = None,
    ) -> None:
        """Record the Stripe Checkout Session id for an onboarding record.

        Called right after the session is created so the success redirect and
        the webhook can both find the originating record. Amount/currency are
        stored when known (the price the customer is being charged).
        """
        with self._conn() as c:
            c.execute(
                "UPDATE intake_submissions SET stripe_session_id = ?, "
                "  payment_amount_cents = COALESCE(?, payment_amount_cents), "
                "  currency = COALESCE(?, currency) "
                "WHERE id = ?",
                (
                    (session_id or "").strip() or None,
                    payment_amount_cents,
                    (currency or "").strip().lower() or None,
                    intake_id,
                ),
            )

    def get_intake_by_stripe_session(self, session_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM intake_submissions WHERE stripe_session_id = ?",
                ((session_id or "").strip(),),
            ).fetchone()
            return dict(row) if row else None

    def mark_intake_paid(
        self, intake_id: int, *,
        payment_intent_id: Optional[str] = None,
        payment_amount_cents: Optional[int] = None,
        currency: Optional[str] = None,
    ) -> bool:
        """Idempotently mark an onboarding record paid.

        Returns True if this call transitioned the record from not-paid to
        paid (i.e. the caller should run first-time side effects like the
        receipt). Returns False if it was already paid, so a replayed Stripe
        webhook is a safe no-op. Always records the payment-intent id and
        amount when supplied, even on the already-paid path.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT payment_status FROM intake_submissions WHERE id = ?",
                (intake_id,),
            ).fetchone()
            if row is None:
                return False
            already_paid = (row["payment_status"] or "").strip().lower() == "paid"
            c.execute(
                "UPDATE intake_submissions SET payment_status = 'paid', "
                "  stripe_payment_intent_id = COALESCE(?, stripe_payment_intent_id), "
                "  payment_amount_cents = COALESCE(?, payment_amount_cents), "
                "  currency = COALESCE(?, currency), "
                "  paid_at = COALESCE(paid_at, ?) "
                "WHERE id = ?",
                (
                    (payment_intent_id or "").strip() or None,
                    payment_amount_cents,
                    (currency or "").strip().lower() or None,
                    _now(),
                    intake_id,
                ),
            )
            return not already_paid

    def set_intake_uploads(self, intake_id: int, uploads_json: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE intake_submissions SET uploads_json = ? WHERE id = ?",
                (uploads_json, intake_id),
            )

    def set_intake_email_status(self, intake_id: int, status: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE intake_submissions SET email_status = ? WHERE id = ?",
                (status, intake_id),
            )

    def set_intake_payment_status(self, intake_id: int, status: str) -> None:
        """Update the payment status of an intake record.

        Only a genuine Stripe success/webhook path should ever set this to
        'paid'. The intake form itself always stores 'pending'.
        """
        with self._conn() as c:
            c.execute(
                "UPDATE intake_submissions SET payment_status = ? WHERE id = ?",
                ((status or "pending").strip().lower(), intake_id),
            )

    def get_intake_by_reference(self, reference: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM intake_submissions WHERE reference = ?",
                ((reference or "").strip(),),
            ).fetchone()
            return dict(row) if row else None

    def get_intake_submission(self, intake_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM intake_submissions WHERE id = ?", (intake_id,)
            ).fetchone()
            return dict(row) if row else None

    def recent_intake_submissions(self, limit: int = 50) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM intake_submissions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # --- Calendly discovery-call leads ------------------------------------

    def upsert_calendly_lead(self, *, invitee_uri: str, fields: dict) -> int:
        """Insert or update a Calendly lead keyed on its invitee URI.

        ``invitee_uri`` is Calendly's canonical, globally-unique identifier
        for one person on one scheduled event, so it makes the webhook
        idempotent: a duplicate delivery (same invitee) updates the existing
        row instead of creating a second lead. Returns the row id.

        ``fields`` may contain any of the lead columns; only the keys that
        are present are written, so a sparse ``invitee.canceled`` payload can
        flip ``status`` without clobbering the name/email captured at
        ``invitee.created`` time. JSON sub-objects (questions, raw payload)
        must already be serialized to TEXT by the caller.
        """
        invitee_uri = (invitee_uri or "").strip()
        if not invitee_uri:
            raise ValueError("invitee_uri is required")

        allowed = (
            "invitee_uuid", "event_uri", "event_type_uri", "event_name",
            "name", "email", "phone", "firm_name", "role", "migration_date",
            "years_history", "volume", "notes", "clio_rep_name",
            "clio_rep_email", "meeting_start", "meeting_end", "timezone",
            "status", "cancel_reason", "canceled_by", "rescheduled",
            "questions_json", "enrichment_status", "raw_payload_json",
            "service_lane",
        )
        with self._conn() as c:
            existing = c.execute(
                "SELECT id FROM calendly_leads WHERE invitee_uri = ?",
                (invitee_uri,),
            ).fetchone()
            if existing:
                sets = []
                vals: list = []
                for k in allowed:
                    if k in fields and fields[k] is not None:
                        sets.append(f"{k} = ?")
                        vals.append(fields[k])
                sets.append("updated_at = ?")
                vals.append(_now())
                vals.append(invitee_uri)
                c.execute(
                    f"UPDATE calendly_leads SET {', '.join(sets)} "
                    f"WHERE invitee_uri = ?",
                    vals,
                )
                return existing["id"]

            cols = ["invitee_uri"]
            vals = [invitee_uri]
            for k in allowed:
                if k == "status":
                    continue
                if k in fields and fields[k] is not None:
                    cols.append(k)
                    vals.append(fields[k])
            cols += ["status", "created_at", "updated_at"]
            vals += [fields.get("status") or "scheduled", _now(), _now()]
            ph = ",".join("?" * len(vals))
            cur = c.execute(
                f"INSERT INTO calendly_leads ({', '.join(cols)}) VALUES ({ph})",
                vals,
            )
            return cur.lastrowid

    def get_calendly_lead(self, lead_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM calendly_leads WHERE id = ?", (lead_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_calendly_lead_by_invitee(self, invitee_uri: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM calendly_leads WHERE invitee_uri = ?",
                ((invitee_uri or "").strip(),),
            ).fetchone()
            return dict(row) if row else None

    def list_calendly_leads(self, limit: int = 200) -> list:
        """Return leads ordered so upcoming/recent meetings surface first.

        Sorts by meeting_start descending with NULL start times last, then
        by created_at descending as a tiebreaker. This puts the soonest /
        most recently booked calls at the top, which is what the operator
        Leads view wants.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM calendly_leads "
                "ORDER BY (meeting_start IS NULL) ASC, "
                "         meeting_start DESC, created_at DESC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_calendly_leads(self) -> int:
        """Total number of stored Calendly leads (all statuses)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM calendly_leads"
            ).fetchone()
            return int(row["n"]) if row else 0

    def latest_calendly_lead_received_at(self) -> Optional[str]:
        """Timestamp of the most recently received Calendly webhook, if any.

        Uses MAX(created_at, updated_at) so a recent cancel/reschedule (which
        only updates an existing row) still counts as "we heard from Calendly
        recently". Returns None when no lead has ever been captured — the key
        signal that the webhook is not wired up.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(MAX(created_at), MAX(updated_at)) AS last_at "
                "FROM calendly_leads"
            ).fetchone()
            return row["last_at"] if row and row["last_at"] else None
