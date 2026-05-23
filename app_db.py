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
from datetime import datetime
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
    return datetime.utcnow().isoformat()


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

    def list_account_mappings(self, firm_id: int, realm_id: str) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM account_mappings WHERE firm_id = ? AND realm_id = ? "
                "ORDER BY pclaw_account_number, pclaw_account_name",
                (firm_id, realm_id),
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
    ) -> None:
        """Insert or update one mapping row.

        The unique key is (firm_id, realm_id, pclaw_account_number,
        pclaw_account_name). Either of the PCLaw columns can be NULL — most
        sandboxes have account numbers but some don't, so the route should
        pass whichever value the source CSV row supplied.
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO account_mappings "
                "(firm_id, realm_id, pclaw_account_number, pclaw_account_name, "
                " qbo_account_id, qbo_account_name, qbo_account_type, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(firm_id, realm_id, pclaw_account_number, pclaw_account_name) "
                "DO UPDATE SET qbo_account_id = excluded.qbo_account_id, "
                "              qbo_account_name = excluded.qbo_account_name, "
                "              qbo_account_type = excluded.qbo_account_type, "
                "              updated_at = excluded.updated_at",
                (
                    firm_id, realm_id, pclaw_account_number, pclaw_account_name,
                    qbo_account_id, qbo_account_name, qbo_account_type,
                    _now(), _now(),
                ),
            )

    def delete_account_mapping(
        self, firm_id: int, realm_id: str,
        pclaw_account_number: Optional[str], pclaw_account_name: Optional[str],
    ) -> None:
        with self._conn() as c:
            c.execute(
                "DELETE FROM account_mappings WHERE firm_id = ? AND realm_id = ? "
                "AND pclaw_account_number IS ? AND pclaw_account_name IS ?",
                (firm_id, realm_id, pclaw_account_number, pclaw_account_name),
            )

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
