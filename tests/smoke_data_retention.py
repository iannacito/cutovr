"""Smoke tests for data-retention cleanup (data_retention.py + operator route).

Run from project root:

    python3 tests/smoke_data_retention.py

Covers:
  T1 purge_expired_reset_tokens removes used and expired tokens but keeps
     a fresh, unused token.
  T2 purge_archived_job_files unlinks encrypted files for archived jobs
     older than the window, clears the row's file pointers, and NEVER
     touches an active (non-archived) job's files.
  T3 purge_orphaned_upload_files removes old unreferenced blobs but keeps
     (a) files still referenced by a live job and (b) recently-modified
     files (an upload may be in flight).
  T4 Operator /operator/cleanup route runs the sweep, writes an audit row
     with counts only (no file names / secrets), and is 404 for a
     non-operator (firm-isolation / privilege check).
"""

import importlib
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENC_KEY_VALUE = "Yh7m5b1J9P0sR8wQv3KsVJpC1Bl0r2Gn9D6X2g8oZqU="
SECRET_VALUE = "z" * 64


def _reset_app(env: dict):
    for mod in ("app", "operator_panel", "demo_mode", "encryption",
                "data_retention", "app_db"):
        if mod in sys.modules:
            del sys.modules[mod]
    base = {
        "APP_DB": tempfile.mktemp(suffix=".sqlite3"),
        "IMPORT_HISTORY_DB": tempfile.mktemp(suffix=".sqlite3"),
        "CSRF_DISABLE": "1",
        "SECRET_KEY": SECRET_VALUE,
        "APP_ENV": "local",
        "ENCRYPTION_KEY": ENC_KEY_VALUE,
        "QBO_CLIENT_ID": "test-client-id",
        "QBO_CLIENT_SECRET": "test-client-secret",
        "QBO_REDIRECT_URI": "https://example.com/oauth/callback",
        "UPLOAD_DIR": tempfile.mkdtemp(),
        "OUTPUT_DIR": tempfile.mkdtemp(),
    }
    for k in ("OPERATOR_EMAILS", "SHOW_OPERATOR_TOOLS", "DEMO_MODE"):
        os.environ.pop(k, None)
    base.update(env)
    for k, v in base.items():
        os.environ[k] = v
    return importlib.import_module("app")


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def t1_expired_reset_tokens():
    appmod = _reset_app({})
    db = appmod.db
    fid, uid = db.create_firm_and_admin("Firm", "a@a.test", "passw0rd!1234")

    # Fresh, unused, not-yet-expired token: keep.
    db.create_password_reset_token(uid, "hash-fresh",
                                   _iso(-1))  # expires in the future
    # Expired token: remove.
    db.create_password_reset_token(uid, "hash-expired", _iso(1))
    # Used token: remove.
    tid = db.create_password_reset_token(uid, "hash-used", _iso(-1))
    db.mark_password_reset_used(tid)

    import data_retention
    removed = data_retention.purge_expired_reset_tokens(db)
    assert removed == 2, f"expected 2 removed, got {removed}"
    assert db.get_password_reset_token("hash-fresh") is not None
    assert db.get_password_reset_token("hash-expired") is None
    assert db.get_password_reset_token("hash-used") is None
    print("T1 OK: expired/used reset tokens purged; fresh token kept")


def t2_archived_job_files():
    appmod = _reset_app({})
    db = appmod.db
    import data_retention
    upload_dir = appmod.UPLOAD_DIR
    output_dir = appmod.OUTPUT_DIR

    fid, uid = db.create_firm_and_admin("Firm", "a@a.test", "passw0rd!1234")

    # Archived + old: should be swept.
    old_enc = upload_dir / "old.enc"
    old_out = output_dir / "old_out.enc"
    old_enc.write_text("ciphertext")
    old_out.write_text("ciphertext")
    db.upsert_job(job_id="job-old", firm_id=fid, user_id=uid, company="Old",
                  source_file="/tmp/old.csv", encrypted_file="old.enc",
                  file_sha256="a" * 64, status="Archived (demo reset D-1)")
    db.save_job_state("job-old", {"status": "Archived (demo reset D-1)",
                                  "encrypted_output": "old_out.enc"})
    # Backdate updated_at so it's outside the window.
    with db._conn() as c:
        c.execute("UPDATE jobs SET updated_at = ? WHERE id = ?",
                  (_iso(30), "job-old"))

    # Active job: must NOT be touched even though file is old.
    active_enc = upload_dir / "active.enc"
    active_enc.write_text("ciphertext")
    db.upsert_job(job_id="job-active", firm_id=fid, user_id=uid, company="Active",
                  source_file="/tmp/active.csv", encrypted_file="active.enc",
                  file_sha256="b" * 64, status="In progress")
    with db._conn() as c:
        c.execute("UPDATE jobs SET updated_at = ? WHERE id = ?",
                  (_iso(30), "job-active"))

    result = data_retention.purge_archived_job_files(db, upload_dir, output_dir, days=7)
    assert result["jobs_swept"] == 1, result
    assert result["files_removed"] == 2, result
    assert not old_enc.exists(), "archived source file should be removed"
    assert not old_out.exists(), "archived output file should be removed"
    assert active_enc.exists(), "ACTIVE job file must be preserved"

    # Row preserved, pointers cleared.
    row = db.get_job("job-old")
    assert row is not None, "archived job ROW must be preserved for audit history"
    assert row.get("encrypted_file") in (None, ""), row
    print("T2 OK: archived-job files swept, active job untouched, row preserved")


def t3_orphaned_upload_files():
    appmod = _reset_app({})
    db = appmod.db
    import data_retention
    upload_dir = appmod.UPLOAD_DIR

    fid, uid = db.create_firm_and_admin("Firm", "a@a.test", "passw0rd!1234")
    db.upsert_job(job_id="job-live", firm_id=fid, user_id=uid, company="Live",
                  source_file="/tmp/live.csv", encrypted_file="referenced.enc",
                  file_sha256="c" * 64, status="In progress")

    # Referenced file: keep even if old.
    referenced = upload_dir / "referenced.enc"
    referenced.write_text("ciphertext")
    old_ts = time.time() - 30 * 24 * 3600
    os.utime(referenced, (old_ts, old_ts))

    # Orphan + old: remove.
    orphan_old = upload_dir / "orphan_old.enc"
    orphan_old.write_text("ciphertext")
    os.utime(orphan_old, (old_ts, old_ts))

    # Orphan + recent: keep (may be an in-flight upload).
    orphan_recent = upload_dir / "orphan_recent.enc"
    orphan_recent.write_text("ciphertext")

    result = data_retention.purge_orphaned_upload_files(
        db, upload_dir, appmod.OUTPUT_DIR, days=7)
    assert result["files_removed"] == 1, result
    assert result["skipped_recent"] == 1, result
    assert referenced.exists(), "referenced file must be preserved"
    assert not orphan_old.exists(), "old orphan should be removed"
    assert orphan_recent.exists(), "recent orphan must be preserved"
    print("T3 OK: old orphan removed; referenced + recent files preserved")


def t4_operator_cleanup_route():
    op_email = "ops@anthro.test"
    appmod = _reset_app({"OPERATOR_EMAILS": op_email})
    db = appmod.db

    # Operator firm + a normal firm.
    c_op = appmod.app.test_client()
    c_op.post("/signup", data={"firm_name": "Ops", "email": op_email,
                               "password": "passw0rd!1234",
                               "confirm_password": "passw0rd!1234"})
    c_user = appmod.app.test_client()
    c_user.post("/signup", data={"firm_name": "Normal", "email": "n@n.test",
                                 "password": "passw0rd!1234",
                                 "confirm_password": "passw0rd!1234"})

    # Non-operator must get 404 (privilege isolation).
    r = c_user.post("/operator/cleanup", follow_redirects=False)
    assert r.status_code == 404, f"non-operator should 404, got {r.status_code}"

    # Operator runs cleanup -> redirect.
    r = c_op.post("/operator/cleanup", follow_redirects=False)
    assert r.status_code in (302, 303), f"cleanup should redirect, got {r.status_code}"

    # Audit row written with counts only, no secrets.
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT action, details FROM audit_logs WHERE action = 'data_retention_cleanup'"
        ).fetchall()
    assert rows, "expected a data_retention_cleanup audit row"
    details = rows[-1]["details"]
    assert "window_days=" in details, details
    for forbidden in ("token=ey", "secret", "password=", ".enc", "/tmp/"):
        assert forbidden.lower() not in details.lower(), f"audit leaked: {forbidden} in {details}"
    print("T4 OK: operator cleanup works; non-operator 404; audit has counts only")


if __name__ == "__main__":
    t1_expired_reset_tokens()
    t2_archived_job_files()
    t3_orphaned_upload_files()
    t4_operator_cleanup_route()
    print("\nALL data-retention smoke tests passed.")
