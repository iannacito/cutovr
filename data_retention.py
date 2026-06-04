"""Data-retention cleanup for Cutovr.

Law-firm financial data should not linger on disk longer than it is
needed. This module collects the small, *safe* cleanup jobs that keep the
deploy tidy without ever touching an active migration or QuickBooks Online
data:

  1. Expired / used password-reset tokens — single-use, 30-minute secrets
     that have no value once spent or stale.
  2. Stale archived jobs' encrypted files — when a firm clicks "Start a new
     migration" (or a demo is reset), the prior job row is *archived*, not
     deleted, so history is preserved. But the encrypted source/output
     files for those archived jobs are dead weight after a retention
     window. We unlink the encrypted blobs (the row itself stays for audit
     history) once an archived job is older than the configured window.
  3. Orphaned encrypted upload files — encrypted blobs in UPLOAD_DIR /
     OUTPUT_DIR that no live job row references AND are older than the
     window. These accumulate when a process is killed mid-upload.

Design rules (intentional constraints):

  * NEVER delete an active job. Only rows whose status starts with
    "Archived" are eligible, and only after the retention window.
  * NEVER touch QuickBooks Online. This is local-disk + local-DB only.
  * NEVER delete a file newer than the window, even if it looks orphaned —
    that file may belong to an upload still in flight.
  * Be idempotent and safe to run repeatedly (cron, operator button, or
    CLI). Every step swallows per-item errors so one bad file can't abort
    the whole sweep; the per-item failure count is reported back.

The functions return plain dicts of counts so callers (an operator route,
a CLI entrypoint, or a test) can render or assert on them. No secrets,
tokens, or file *contents* are ever returned or logged — only counts and
non-sensitive identifiers.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# Default retention window for archived-job artifacts and orphaned upload
# files. 7 days is long enough that a customer who archived a migration by
# mistake on Friday can still recover context Monday, short enough that we
# aren't sitting on encrypted financial exports indefinitely. Override with
# RETENTION_DAYS in the environment.
DEFAULT_RETENTION_DAYS = 7


def _retention_days() -> int:
    # UPLOAD_RETENTION_DAYS is the documented, customer-facing name for the
    # window ("Files can be automatically deleted after processing");
    # RETENTION_DAYS is the original name and stays supported so existing
    # deploys keep working. UPLOAD_RETENTION_DAYS wins when both are set.
    raw = os.environ.get("UPLOAD_RETENTION_DAYS")
    if raw is None:
        raw = os.environ.get("RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return max(1, n)


def _cutoff_iso(days: int) -> str:
    """ISO-8601 (UTC, no tz suffix) timestamp `days` in the past, matching
    the `_now()` format AppDB writes for `updated_at`.
    """
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def purge_expired_reset_tokens(db) -> int:
    """Delete used/expired password-reset tokens. Returns rows removed."""
    return db.purge_expired_reset_tokens()


def purge_archived_job_files(
    db,
    upload_dir: Path,
    output_dir: Path,
    *,
    days: Optional[int] = None,
) -> dict:
    """Unlink encrypted source/output files for archived jobs older than the
    retention window. The job *row* is preserved (audit history); only the
    on-disk encrypted blobs are removed, and the row's file pointers are
    cleared so the app never tries to read a now-missing file.

    Returns {"jobs_swept", "files_removed", "errors"}.
    """
    days = _retention_days() if days is None else max(1, int(days))
    cutoff = _cutoff_iso(days)
    jobs_swept = 0
    files_removed = 0
    errors = 0

    for job in db.list_archived_jobs_before(cutoff):
        touched = False
        for column, base in (
            ("encrypted_file", upload_dir),
            ("encrypted_output", output_dir),
        ):
            name = job.get(column)
            if not name:
                continue
            try:
                (Path(base) / name).unlink(missing_ok=True)
                files_removed += 1
                touched = True
            except Exception:  # noqa: BLE001 - one bad file must not abort the sweep
                errors += 1
        if touched:
            jobs_swept += 1
            # Clear the now-dangling pointers so later reads don't 500.
            try:
                db.clear_job_file_pointers(job["id"])
            except Exception:  # noqa: BLE001
                errors += 1

    return {"jobs_swept": jobs_swept, "files_removed": files_removed, "errors": errors}


def _referenced_filenames(db) -> set:
    """All encrypted file names still referenced by a live job row."""
    referenced = set()
    try:
        for job in db.all_job_file_pointers():
            for key in ("encrypted_file", "encrypted_output"):
                name = job.get(key)
                if name:
                    referenced.add(name)
    except Exception:  # noqa: BLE001
        # If we cannot enumerate references, be conservative and treat
        # everything as referenced (i.e. delete nothing).
        return None  # type: ignore[return-value]
    return referenced


def purge_orphaned_upload_files(
    db,
    upload_dir: Path,
    output_dir: Path,
    *,
    days: Optional[int] = None,
) -> dict:
    """Remove encrypted blobs on disk that no job row references and that are
    older than the retention window. Files newer than the window are left
    alone (an upload may still be in flight).

    Returns {"files_removed", "skipped_recent", "errors"}.
    """
    days = _retention_days() if days is None else max(1, int(days))
    referenced = _referenced_filenames(db)
    if referenced is None:
        # Could not safely enumerate references — do nothing.
        return {"files_removed": 0, "skipped_recent": 0, "errors": 1}

    cutoff_ts = (datetime.now(timezone.utc).timestamp() - days * 24 * 3600)
    files_removed = 0
    skipped_recent = 0
    errors = 0

    for base in {Path(upload_dir), Path(output_dir)}:
        if not base.exists():
            continue
        for path in base.iterdir():
            if not path.is_file():
                continue
            if path.name in referenced:
                continue
            try:
                if path.stat().st_mtime >= cutoff_ts:
                    skipped_recent += 1
                    continue
                path.unlink(missing_ok=True)
                files_removed += 1
            except Exception:  # noqa: BLE001
                errors += 1

    return {
        "files_removed": files_removed,
        "skipped_recent": skipped_recent,
        "errors": errors,
    }


def run_cleanup(
    db,
    upload_dir: Path,
    output_dir: Path,
    *,
    days: Optional[int] = None,
) -> dict:
    """Run every safe retention step and return a combined, JSON-able report.

    Safe to call from an operator route, a CLI, or a scheduled task. Never
    raises for per-item failures — those are counted in the report.
    """
    days = _retention_days() if days is None else max(1, int(days))
    tokens = purge_expired_reset_tokens(db)
    archived = purge_archived_job_files(db, upload_dir, output_dir, days=days)
    orphans = purge_orphaned_upload_files(db, upload_dir, output_dir, days=days)
    return {
        "retention_days": days,
        "expired_reset_tokens_removed": tokens,
        "archived_job_files": archived,
        "orphaned_upload_files": orphans,
    }


def main(argv=None) -> int:
    """CLI entrypoint for scheduled retention cleanup.

    Resolves the same storage paths and database the web app uses (via the
    UPLOAD_DIR / OUTPUT_DIR / APP_DB environment variables) and runs the
    safe cleanup sweep, printing a JSON report. Intended for cron / a
    scheduled task on the deploy host, e.g.::

        python -m data_retention            # nightly cleanup
        UPLOAD_RETENTION_DAYS=30 python -m data_retention

    Exit code is 0 on a clean sweep and 1 if any per-item errors occurred,
    so a scheduler can alert on failures. Never touches active jobs or any
    QuickBooks Online data.
    """
    import json

    from app_db import AppDB

    base_dir = Path(__file__).resolve().parent
    upload_dir = Path(os.environ.get("UPLOAD_DIR") or (base_dir / "uploads"))
    output_dir = Path(os.environ.get("OUTPUT_DIR") or (base_dir / "processed"))
    app_db_path = os.environ.get("APP_DB", str(base_dir / "data" / "app.sqlite3"))

    db = AppDB(app_db_path)
    report = run_cleanup(db, upload_dir, output_dir)
    print(json.dumps(report, indent=2))

    errors = (
        report["archived_job_files"]["errors"]
        + report["orphaned_upload_files"]["errors"]
    )
    return 1 if errors else 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
