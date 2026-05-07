"""
Operator / admin panel: gating + aggregation queries.

This is intentionally kept as a small standalone module so the rest of the
app can stay focused on per-firm tenancy. The panel is for internal app
operators (us), not firm admins. Every firm signup currently creates a
user with role='admin' for *their* firm — so we cannot rely on `role` to
gate access. Instead we use an explicit allowlist of operator emails set
in the deployment environment.

Gating model
------------

Two env vars cooperate:

  OPERATOR_EMAILS      Comma-separated allowlist of email addresses that
                       are allowed to see the operator panel. Case- and
                       whitespace-insensitive. If empty/unset, the panel
                       is fully hidden — no nav link, all routes 404.

  SHOW_OPERATOR_TOOLS  Optional, "1" to enable the panel feature at all.
                       Defaults to enabled when OPERATOR_EMAILS is set,
                       so the common case (set the allowlist, get the
                       panel) "just works" without flipping a second
                       toggle. Setting this to "0" force-disables the
                       panel even if OPERATOR_EMAILS is populated, which
                       is useful for incident-response kill-switching
                       without having to clear the allowlist.

Importantly: an empty OPERATOR_EMAILS means *no one* is an operator,
including locally. The panel never falls back to "everyone is an
operator". That avoids the trap where a misconfigured production deploy
would silently expose the panel to any logged-in user.

Read-only by design
-------------------

The first version exposes only read endpoints. Operators cannot trigger
imports, reversals, or disconnects through this UI. They can navigate to
existing per-firm/per-job pages only when the route already enforces
its own access control.

Secrets the panel never renders
-------------------------------

  - SECRET_KEY / APP_SECRET
  - ENCRYPTION_KEY
  - QBO_CLIENT_SECRET
  - QBO access/refresh tokens (encrypted or plaintext)
  - OAuth `code`, `state`, raw Intuit response bodies

The aggregation queries below explicitly avoid selecting columns that
hold encrypted-token blobs.
"""

from __future__ import annotations

import os
from typing import Optional


def _parse_email_list(raw: Optional[str]) -> set:
    if not raw:
        return set()
    out = set()
    for piece in raw.split(","):
        e = piece.strip().lower()
        if e:
            out.add(e)
    return out


def get_operator_emails() -> set:
    """Read OPERATOR_EMAILS at call time so tests can monkey-patch env."""
    return _parse_email_list(os.environ.get("OPERATOR_EMAILS"))


def operator_panel_enabled() -> bool:
    """True iff the panel feature is turned on in this deploy.

    Disabled when:
      - OPERATOR_EMAILS is empty/unset (no one to grant access to), OR
      - SHOW_OPERATOR_TOOLS is explicitly set to a falsy value.
    """
    if not get_operator_emails():
        return False
    raw = os.environ.get("SHOW_OPERATOR_TOOLS")
    if raw is None:
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")


def is_operator_user(user: Optional[dict]) -> bool:
    """True iff this logged-in user's email is in the operator allowlist
    AND the panel is enabled in this deploy.

    Accepts None safely so callers can write `is_operator_user(current_user())`
    without an extra None-check.
    """
    if not user:
        return False
    if not operator_panel_enabled():
        return False
    email = (user.get("email") or "").strip().lower()
    if not email:
        return False
    return email in get_operator_emails()


# ---------------------------------------------------------------------------
# Aggregation queries
#
# These read from both AppDB (firms/users/jobs/qbo_connections/audit_logs)
# and ImportHistory (imports/import_reversals). They never decrypt tokens or
# return token columns; the panel templates never see them either.
# ---------------------------------------------------------------------------


def collect_metrics(db, history) -> dict:
    """Return high-level operator metrics. Never touches token columns.

    Keys:
      total_firms, total_users, total_jobs, total_imports,
      successful_imports, failed_imports, reversed_imports,
      qbo_connections, distinct_realms,
      recent_login_failures, recent_oauth_failures
    """
    out = {}
    with db._conn() as c:
        out["total_firms"] = c.execute("SELECT COUNT(*) AS n FROM firms").fetchone()["n"]
        out["total_users"] = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        out["total_jobs"] = c.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        out["qbo_connections"] = c.execute(
            "SELECT COUNT(*) AS n FROM qbo_connections"
        ).fetchone()["n"]
        out["distinct_realms"] = c.execute(
            "SELECT COUNT(DISTINCT realm_id) AS n FROM qbo_connections"
        ).fetchone()["n"]
        out["recent_login_failures"] = c.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'login_failed' "
            "AND created_at >= datetime('now', '-7 day')"
        ).fetchone()["n"]
        out["recent_oauth_failures"] = c.execute(
            "SELECT COUNT(*) AS n FROM audit_logs "
            "WHERE action IN ('oauth_callback_firm_mismatch', "
            "                 'qbo_token_refresh_failed', 'oauth_failed') "
            "AND created_at >= datetime('now', '-7 day')"
        ).fetchone()["n"]

    with history._conn() as c:
        out["total_imports"] = c.execute("SELECT COUNT(*) AS n FROM imports").fetchone()["n"]
        out["successful_imports"] = c.execute(
            "SELECT COUNT(*) AS n FROM imports WHERE status = 'success'"
        ).fetchone()["n"]
        out["failed_imports"] = c.execute(
            "SELECT COUNT(*) AS n FROM imports WHERE status != 'success'"
        ).fetchone()["n"]
        out["reversed_imports"] = c.execute(
            "SELECT COUNT(*) AS n FROM import_reversals WHERE status = 'success'"
        ).fetchone()["n"]
    return out


def list_firms_overview(db, history) -> list:
    """One row per firm with summary counts.

    Per-firm fields:
      firm_id, firm_name, created_at,
      admin_email (oldest user, conventionally the firm's signup admin),
      user_count, job_count, last_job_at, last_job_status,
      qbo_connected (bool), qbo_realms (count),
      total_imports, successful_imports, failed_imports, last_import_at,
      last_import_status, last_error (short).
    """
    with db._conn() as c:
        firm_rows = c.execute(
            "SELECT id, name, created_at FROM firms ORDER BY id ASC"
        ).fetchall()

        # admin email per firm = oldest user in that firm
        admin_emails = {}
        for row in c.execute(
            "SELECT firm_id, email FROM users ORDER BY firm_id ASC, id ASC"
        ).fetchall():
            admin_emails.setdefault(row["firm_id"], row["email"])

        user_counts = {
            r["firm_id"]: r["n"]
            for r in c.execute(
                "SELECT firm_id, COUNT(*) AS n FROM users GROUP BY firm_id"
            ).fetchall()
        }

        job_counts = {}
        last_job = {}  # firm_id -> (created_at, status)
        for r in c.execute(
            "SELECT firm_id, COUNT(*) AS n, MAX(created_at) AS last_at "
            "FROM jobs GROUP BY firm_id"
        ).fetchall():
            job_counts[r["firm_id"]] = r["n"]
            last_job[r["firm_id"]] = (r["last_at"], None)
        for r in c.execute(
            "SELECT firm_id, status, created_at FROM jobs "
            "WHERE (firm_id, created_at) IN ("
            "  SELECT firm_id, MAX(created_at) FROM jobs GROUP BY firm_id"
            ")"
        ).fetchall():
            prev = last_job.get(r["firm_id"], (None, None))
            last_job[r["firm_id"]] = (prev[0] or r["created_at"], r["status"])

        qbo_realm_counts = {
            r["firm_id"]: r["n"]
            for r in c.execute(
                "SELECT firm_id, COUNT(DISTINCT realm_id) AS n "
                "FROM qbo_connections GROUP BY firm_id"
            ).fetchall()
        }

        # Map firm -> list of job_ids so we can ask history for per-firm imports
        firm_jobs = {}
        for r in c.execute("SELECT id, firm_id FROM jobs").fetchall():
            firm_jobs.setdefault(r["firm_id"], []).append(r["id"])

    # Per-firm import aggregation from history DB
    import_stats = {}  # firm_id -> dict
    if firm_jobs:
        with history._conn() as c:
            for firm_id, job_ids in firm_jobs.items():
                if not job_ids:
                    continue
                placeholders = ",".join("?" * len(job_ids))
                row = c.execute(
                    f"SELECT COUNT(*) AS total, "
                    f"  SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok, "
                    f"  SUM(CASE WHEN status!='success' THEN 1 ELSE 0 END) AS bad, "
                    f"  MAX(created_at) AS last_at "
                    f"FROM imports WHERE job_id IN ({placeholders})",
                    job_ids,
                ).fetchone()
                last_status = None
                last_notes = None
                if row and row["last_at"]:
                    detail = c.execute(
                        f"SELECT status, notes FROM imports "
                        f"WHERE job_id IN ({placeholders}) "
                        f"ORDER BY id DESC LIMIT 1",
                        job_ids,
                    ).fetchone()
                    if detail:
                        last_status = detail["status"]
                        last_notes = (detail["notes"] or "")[:200]
                import_stats[firm_id] = {
                    "total": row["total"] or 0,
                    "ok": row["ok"] or 0,
                    "bad": row["bad"] or 0,
                    "last_at": row["last_at"],
                    "last_status": last_status,
                    "last_notes": last_notes,
                }

    out = []
    for f in firm_rows:
        fid = f["id"]
        lj = last_job.get(fid, (None, None))
        s = import_stats.get(fid, {})
        out.append({
            "firm_id": fid,
            "firm_name": f["name"],
            "created_at": f["created_at"],
            "admin_email": admin_emails.get(fid),
            "user_count": user_counts.get(fid, 0),
            "job_count": job_counts.get(fid, 0),
            "last_job_at": lj[0],
            "last_job_status": lj[1],
            "qbo_connected": bool(qbo_realm_counts.get(fid)),
            "qbo_realms": qbo_realm_counts.get(fid, 0),
            "total_imports": s.get("total", 0),
            "successful_imports": s.get("ok", 0),
            "failed_imports": s.get("bad", 0),
            "last_import_at": s.get("last_at"),
            "last_import_status": s.get("last_status"),
            "last_error_note": s.get("last_notes") if s.get("last_status") and s.get("last_status") != "success" else None,
        })
    return out


def recent_imports(history, limit: int = 25) -> list:
    """Return recent imports (success and failure), newest first.

    Selects only operator-safe columns. Notes are truncated to keep any
    accidentally-verbose error string from blowing up the rendered page.
    """
    with history._conn() as c:
        rows = c.execute(
            "SELECT id, job_id, realm_id, company_name, transaction_count, "
            "       status, created_at, notes "
            "FROM imports ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("notes"):
                d["notes"] = d["notes"][:200]
            out.append(d)
        return out


_RECENT_ERROR_ACTIONS = (
    "login_failed",
    "oauth_callback_firm_mismatch",
    "oauth_failed",
    "qbo_token_refresh_failed",
    "import_failed",
    "import_blocked",
    "import_reversal_blocked",
    "delete_job_failed",
)


def recent_errors(db, limit: int = 25) -> list:
    """Recent error/auth-failure audit rows from AppDB.

    Returns a flat list of dicts with: id, firm_id, user_id, action,
    target_type, target_id, details, created_at. The `details` field is
    truncated. We intentionally do not resolve user_id -> email here:
    operators can correlate via the per-firm view, and lookups here would
    require an extra query per row. The view template can still render
    a label like "user #4".
    """
    placeholders = ",".join("?" * len(_RECENT_ERROR_ACTIONS))
    with db._conn() as c:
        rows = c.execute(
            f"SELECT id, firm_id, user_id, action, target_type, target_id, "
            f"       details, created_at "
            f"FROM audit_logs WHERE action IN ({placeholders}) "
            f"ORDER BY id DESC LIMIT ?",
            [*_RECENT_ERROR_ACTIONS, limit],
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("details"):
                d["details"] = d["details"][:200]
            out.append(d)
        return out


def firm_detail(db, history, firm_id: int) -> Optional[dict]:
    """Per-firm operator detail view. Returns None if firm doesn't exist.

    Includes:
      firm row, users list (id, email, role, created_at — never password
      hash), jobs (most recent first, no encrypted blobs), qbo
      connections (no token columns), import history (most recent first),
      recent audit log for this firm.
    """
    firm = db.get_firm(firm_id)
    if not firm:
        return None
    with db._conn() as c:
        users = [
            dict(r)
            for r in c.execute(
                "SELECT id, email, role, created_at FROM users "
                "WHERE firm_id = ? ORDER BY id ASC",
                (firm_id,),
            ).fetchall()
        ]
        jobs = [
            dict(r)
            for r in c.execute(
                "SELECT id, company, source_file, status, qbo_connected, "
                "       created_at, updated_at, last_import_id "
                "FROM jobs WHERE firm_id = ? ORDER BY created_at DESC",
                (firm_id,),
            ).fetchall()
        ]
        qbo_conns = [
            dict(r)
            for r in c.execute(
                "SELECT job_id, realm_id, company_name, legal_name, country, "
                "       expires_at, company_info_error, connected_at, updated_at "
                "FROM qbo_connections WHERE firm_id = ? "
                "ORDER BY connected_at DESC",
                (firm_id,),
            ).fetchall()
        ]
        audit = db.recent_audit_for_firm(firm_id, limit=30)

    job_ids = [j["id"] for j in jobs]
    imports: list = []
    if job_ids:
        with history._conn() as c:
            placeholders = ",".join("?" * len(job_ids))
            imports = [
                dict(r)
                for r in c.execute(
                    f"SELECT id, job_id, realm_id, company_name, "
                    f"       transaction_count, status, created_at, notes "
                    f"FROM imports WHERE job_id IN ({placeholders}) "
                    f"ORDER BY id DESC",
                    job_ids,
                ).fetchall()
            ]
            for imp in imports:
                if imp.get("notes"):
                    imp["notes"] = imp["notes"][:300]
                rev = c.execute(
                    "SELECT id, status, reversed_at, error "
                    "FROM import_reversals WHERE import_id = ?",
                    (imp["id"],),
                ).fetchone()
                imp["reversal"] = dict(rev) if rev else None

    return {
        "firm": firm,
        "users": users,
        "jobs": jobs,
        "qbo_connections": qbo_conns,
        "imports": imports,
        "audit": audit,
    }
