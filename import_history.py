"""
Persistent import history with duplicate-import prevention.

Backed by SQLite (stdlib, no extra dependency). Two tables:

  imports
    id INTEGER PRIMARY KEY,
    job_id TEXT,
    realm_id TEXT,
    file_sha256 TEXT,
    company_name TEXT,
    transaction_count INTEGER,
    debit_total TEXT,        -- stored as string to keep Decimal precision
    credit_total TEXT,
    created_at TEXT,
    status TEXT,
    notes TEXT

  imported_transactions
    import_id INTEGER REFERENCES imports(id) ON DELETE CASCADE,
    transaction_id TEXT,     -- PCLaw transaction_id (e.g. JE-0003)
    qbo_je_id TEXT,
    doc_number TEXT,
    txn_date TEXT,
    PRIMARY KEY (import_id, transaction_id)

  imported_entities
    import_id INTEGER REFERENCES imports(id) ON DELETE CASCADE,
    kind TEXT,               -- 'Customer' or 'Vendor'
    name TEXT,
    qbo_id TEXT

Why SQLite + plain SQL:
  - Zero install for the user (Python stdlib).
  - The same schema migrates cleanly to Postgres: the only Postgres-specific
    change later will be replacing AUTOINCREMENT with SERIAL/IDENTITY and
    swapping the connection layer. SQL queries do not need to change.
  - Foreign-key cascades are enabled so deleting a job cleans up children.

Duplicate-import prevention:
  has_completed_import(file_sha256, realm_id) returns True if any prior
  status='success' import exists for that exact file content into that
  exact realm. The route blocks the import before posting any JE.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    realm_id        TEXT NOT NULL,
    file_sha256     TEXT NOT NULL,
    company_name    TEXT,
    transaction_count INTEGER,
    debit_total     TEXT,
    credit_total    TEXT,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_imports_dedupe
    ON imports(file_sha256, realm_id, status);
CREATE INDEX IF NOT EXISTS idx_imports_job ON imports(job_id);

CREATE TABLE IF NOT EXISTS imported_transactions (
    import_id       INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    transaction_id  TEXT NOT NULL,
    qbo_je_id       TEXT,
    doc_number      TEXT,
    txn_date        TEXT,
    PRIMARY KEY (import_id, transaction_id)
);

CREATE TABLE IF NOT EXISTS imported_entities (
    import_id       INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL,
    qbo_id          TEXT
);

CREATE TABLE IF NOT EXISTS import_reversals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id           INTEGER NOT NULL UNIQUE REFERENCES imports(id) ON DELETE CASCADE,
    job_id              TEXT NOT NULL,
    firm_id             INTEGER,
    realm_id            TEXT NOT NULL,
    status              TEXT NOT NULL,    -- 'success' | 'failed'
    reversed_at         TEXT NOT NULL,
    created_by_user_id  INTEGER,
    error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_reversals_job ON import_reversals(job_id);

CREATE TABLE IF NOT EXISTS reversed_transactions (
    reversal_id      INTEGER NOT NULL REFERENCES import_reversals(id) ON DELETE CASCADE,
    transaction_id   TEXT NOT NULL,
    original_qbo_je_id TEXT,
    reversal_qbo_je_id TEXT,
    reversal_doc_number TEXT,
    reversal_txn_date  TEXT,
    PRIMARY KEY (reversal_id, transaction_id)
);
"""


def sha256_of_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ImportHistory:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

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

    # --- duplicate prevention ----------------------------------------------

    def has_completed_import(self, file_sha256: str, realm_id: str) -> Optional[dict]:
        """Return the prior successful import row, if any, else None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM imports "
                "WHERE file_sha256 = ? AND realm_id = ? AND status = 'success' "
                "ORDER BY id DESC LIMIT 1",
                (file_sha256, realm_id),
            ).fetchone()
            return dict(row) if row else None

    def has_completed_transactions(
        self, transaction_ids: Iterable[str], realm_id: str
    ) -> set:
        """Return the subset of transaction_ids already imported into this realm."""
        ids = list(transaction_ids)
        if not ids:
            return set()
        placeholders = ",".join("?" * len(ids))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT DISTINCT t.transaction_id "
                f"FROM imported_transactions t "
                f"JOIN imports i ON i.id = t.import_id "
                f"WHERE i.realm_id = ? AND i.status = 'success' "
                f"  AND t.transaction_id IN ({placeholders})",
                [realm_id, *ids],
            ).fetchall()
            return {r["transaction_id"] for r in rows}

    # --- recording ---------------------------------------------------------

    def record_import(
        self,
        job_id: str,
        realm_id: str,
        file_sha256: str,
        company_name: Optional[str],
        transaction_count: int,
        debit_total: str,
        credit_total: str,
        status: str,
        notes: Optional[str] = None,
        created_transactions: Optional[Iterable[dict]] = None,
        created_entities: Optional[Iterable[tuple]] = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO imports "
                "(job_id, realm_id, file_sha256, company_name, transaction_count, "
                " debit_total, credit_total, created_at, status, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    realm_id,
                    file_sha256,
                    company_name,
                    transaction_count,
                    str(debit_total),
                    str(credit_total),
                    datetime.utcnow().isoformat(),
                    status,
                    notes,
                ),
            )
            import_id = cur.lastrowid

            for tx in created_transactions or []:
                c.execute(
                    "INSERT OR REPLACE INTO imported_transactions "
                    "(import_id, transaction_id, qbo_je_id, doc_number, txn_date) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        import_id,
                        tx["transaction_id"],
                        tx.get("qbo_je_id"),
                        tx.get("doc_number"),
                        tx.get("txn_date"),
                    ),
                )

            for kind, name, qbo_id in created_entities or []:
                c.execute(
                    "INSERT INTO imported_entities (import_id, kind, name, qbo_id) "
                    "VALUES (?, ?, ?, ?)",
                    (import_id, kind, name, qbo_id),
                )

            return import_id

    # --- read-back ---------------------------------------------------------

    # --- reversals ---------------------------------------------------------

    def get_reversal_for_import(self, import_id: int) -> Optional[dict]:
        """Return the reversal row for this import, if any.

        We model 1 import → at most 1 reversal (UNIQUE constraint on
        import_id). A second reversal attempt is an idempotency error.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM import_reversals WHERE import_id = ?", (import_id,)
            ).fetchone()
            if not row:
                return None
            out = dict(row)
            out["transactions"] = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM reversed_transactions WHERE reversal_id = ? "
                    "ORDER BY transaction_id",
                    (out["id"],),
                ).fetchall()
            ]
            return out

    def get_latest_completed_import_for_job(self, job_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM imports WHERE job_id = ? AND status = 'success' "
                "ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            imp = dict(row)
            imp["transactions"] = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM imported_transactions WHERE import_id = ? "
                    "ORDER BY transaction_id",
                    (imp["id"],),
                ).fetchall()
            ]
            return imp

    def record_reversal(
        self,
        import_id: int,
        job_id: str,
        firm_id: Optional[int],
        realm_id: str,
        status: str,
        created_by_user_id: Optional[int],
        reversed_transactions: Optional[Iterable[dict]] = None,
        error: Optional[str] = None,
    ) -> int:
        """Insert a reversal row + per-transaction rows.

        Raises ValueError if a reversal already exists for this import_id;
        callers should treat that as the idempotency block.
        """
        with self._conn() as c:
            existing = c.execute(
                "SELECT id FROM import_reversals WHERE import_id = ?", (import_id,)
            ).fetchone()
            if existing:
                raise ValueError(f"Import {import_id} already has a reversal (id={existing['id']}).")
            cur = c.execute(
                "INSERT INTO import_reversals "
                "(import_id, job_id, firm_id, realm_id, status, reversed_at, "
                " created_by_user_id, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    import_id, job_id, firm_id, realm_id, status,
                    datetime.utcnow().isoformat(), created_by_user_id, error,
                ),
            )
            reversal_id = cur.lastrowid
            for tx in reversed_transactions or []:
                c.execute(
                    "INSERT INTO reversed_transactions "
                    "(reversal_id, transaction_id, original_qbo_je_id, "
                    " reversal_qbo_je_id, reversal_doc_number, reversal_txn_date) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        reversal_id, tx["transaction_id"],
                        tx.get("original_qbo_je_id"),
                        tx.get("reversal_qbo_je_id"),
                        tx.get("reversal_doc_number"),
                        tx.get("reversal_txn_date"),
                    ),
                )
            return reversal_id

    def get_history_for_jobs(self, job_ids: Iterable[str]) -> list:
        """Return all import rows whose job_id is in the given set, with
        their reversal (if any). Ordered newest-first.

        Used by the per-firm import summary view: the caller has already
        confirmed which job_ids belong to the firm, so we don't enforce
        that constraint here.
        """
        ids = list(job_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        with self._conn() as c:
            rows = [
                dict(r)
                for r in c.execute(
                    f"SELECT * FROM imports WHERE job_id IN ({placeholders}) "
                    f"ORDER BY id DESC",
                    ids,
                ).fetchall()
            ]
            for imp in rows:
                rev = c.execute(
                    "SELECT * FROM import_reversals WHERE import_id = ?", (imp["id"],),
                ).fetchone()
                imp["reversal"] = dict(rev) if rev else None
        return rows

    def get_history_for_job(self, job_id: str) -> list:
        with self._conn() as c:
            imports = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM imports WHERE job_id = ? ORDER BY id DESC",
                    (job_id,),
                ).fetchall()
            ]
            for imp in imports:
                imp["transactions"] = [
                    dict(r)
                    for r in c.execute(
                        "SELECT transaction_id, qbo_je_id, doc_number, txn_date "
                        "FROM imported_transactions WHERE import_id = ? "
                        "ORDER BY transaction_id",
                        (imp["id"],),
                    ).fetchall()
                ]
                imp["entities"] = [
                    dict(r)
                    for r in c.execute(
                        "SELECT kind, name, qbo_id FROM imported_entities "
                        "WHERE import_id = ?",
                        (imp["id"],),
                    ).fetchall()
                ]
                rev = c.execute(
                    "SELECT * FROM import_reversals WHERE import_id = ?", (imp["id"],),
                ).fetchone()
                imp["reversal"] = dict(rev) if rev else None
        return imports
