"""Smoke tests: a replacement upload supersedes the prior report of the
same type, so Step 5 stops importing the *old* general ledger.

Background — Cesar's QA 2026-06-03 (item 4)
-------------------------------------------
Cesar uploaded a corrected general ledger, but the migration kept reading
the *old* GL. Root cause: ``import_job_entry`` (Step 5) lists the firm's
GL jobs newest-first but then prefers any job that already has a
QuickBooks connection. A freshly uploaded replacement has no connection
yet, so it lost to the stale-but-connected prior upload.

Fix: when a new report is successfully ingested, ``_process_uploaded_csv``
calls ``demo_mode.supersede_prior_jobs`` to flip prior active jobs of the
same report type to a "Superseded" status. ``filter_active_jobs`` drops
those, so ``_firm_latest_jobs_by_type`` (and therefore Step 5) only sees
the new upload. The old row stays in the DB for operator/audit history.

Covered
-------
  S1  ``supersede_prior_jobs`` flips only prior active jobs of the *same*
      report type, never the kept job, never a different type, and never a
      job already archived/superseded.
  S2  ``filter_active_jobs`` drops superseded rows (and still drops
      demo-archived rows), keeping everything else.
  S3  After superseding, ``_firm_latest_jobs_by_type`` returns only the
      new GL job — so Step 5 routing can no longer pick the stale upload.

Run from project root::

    python3 tests/smoke_supersede_prior_gl.py
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENC_KEY_VALUE = "Yh7m5b1J9P0sR8wQv3KsVJpC1Bl0r2Gn9D6X2g8oZqU="
SECRET_VALUE = "z" * 64


def _reset_app(env=None):
    for mod in ("app", "operator_panel", "demo_mode", "encryption"):
        if mod in sys.modules:
            del sys.modules[mod]
    base = {
        "APP_DB": tempfile.mktemp(suffix=".sqlite3"),
        "IMPORT_HISTORY_DB": tempfile.mktemp(suffix=".sqlite3"),
        "UPLOAD_DIR": tempfile.mkdtemp(prefix="pclaw_uploads_sup_"),
        "OUTPUT_DIR": tempfile.mkdtemp(prefix="pclaw_outputs_sup_"),
        "CSRF_DISABLE": "1",
        "SECRET_KEY": SECRET_VALUE,
        "APP_ENV": "local",
        "ENCRYPTION_KEY": ENC_KEY_VALUE,
    }
    for k in ("OPERATOR_EMAILS", "SHOW_OPERATOR_TOOLS", "DEMO_MODE", "APP_DEMO_MODE"):
        os.environ.pop(k, None)
    base.update(env or {})
    for k, v in base.items():
        os.environ[k] = v
    return importlib.import_module("app")


def _seed_job(appdb, firm_id, user_id, job_id, report_type,
              status="File uploaded (encrypted)"):
    appdb.upsert_job(
        job_id=job_id, firm_id=firm_id, user_id=user_id, company="Co",
        source_file=f"/tmp/{job_id}.csv", encrypted_file=f"/tmp/{job_id}.enc",
        file_sha256="0" * 64, status=status,
    )
    appdb.save_job_state(job_id, {"status": status, "report_type": report_type})


def s1_supersede_targets_only_same_type_priors():
    appmod = _reset_app()
    appdb = appmod.db
    demo_mode = sys.modules["demo_mode"]
    firm_id, user_id = appdb.create_firm_and_admin(
        "S1 Firm", "s1@example.test", "passw0rd!1234"
    )
    _seed_job(appdb, firm_id, user_id, "gl-old", "general_ledger")
    _seed_job(appdb, firm_id, user_id, "tb-old", "trial_balance")
    _seed_job(appdb, firm_id, user_id, "gl-already-arch", "general_ledger",
              status="Archived (demo reset D-x)")
    _seed_job(appdb, firm_id, user_id, "gl-new", "general_ledger")

    n = demo_mode.supersede_prior_jobs(
        appdb, firm_id, "general_ledger", keep_job_id="gl-new"
    )
    assert n == 1, f"only the one active prior GL should be superseded, got {n}"

    by_id = {j["id"]: j for j in appdb.list_jobs_for_firm(firm_id, limit=500)}
    assert demo_mode.is_superseded_job(by_id["gl-old"]), "old GL must be superseded"
    assert not demo_mode.is_superseded_job(by_id["gl-new"]), "kept job untouched"
    assert not demo_mode.is_superseded_job(by_id["tb-old"]), "other type untouched"
    # Already-archived job keeps its archive status (no churn).
    assert by_id["gl-already-arch"]["status"].startswith("Archived"), \
        "already-archived job must not be re-flipped"
    print("S1 OK: supersede flips only the active same-type prior")


def s2_filter_active_drops_superseded():
    appmod = _reset_app()
    demo_mode = sys.modules["demo_mode"]
    sample = [
        {"id": "keep", "status": "File uploaded (encrypted)"},
        {"id": "imported", "status": "Imported to QuickBooks"},
        {"id": "sup", "status": "Superseded (replaced by newer upload gl-new)"},
        {"id": "arch", "status": "Archived (demo reset D-x)"},
    ]
    ids = {j["id"] for j in demo_mode.filter_active_jobs(sample)}
    assert ids == {"keep", "imported"}, \
        f"superseded + archived must be dropped, kept others: {ids}"
    print("S2 OK: filter_active_jobs drops superseded rows")


def s3_latest_gl_is_only_the_new_upload():
    appmod = _reset_app()
    appdb = appmod.db
    demo_mode = sys.modules["demo_mode"]
    firm_id, user_id = appdb.create_firm_and_admin(
        "S3 Firm", "s3@example.test", "passw0rd!1234"
    )
    _seed_job(appdb, firm_id, user_id, "gl-old", "general_ledger")
    _seed_job(appdb, firm_id, user_id, "gl-new", "general_ledger")
    demo_mode.supersede_prior_jobs(
        appdb, firm_id, "general_ledger", keep_job_id="gl-new"
    )
    latest = appmod._firm_latest_jobs_by_type(firm_id, "general_ledger", limit=500)
    ids = [j["id"] for j in latest]
    assert ids == ["gl-new"], \
        f"only the new GL should be active for Step 5 routing, got {ids}"
    print("S3 OK: _firm_latest_jobs_by_type returns only the new GL")


GL_CSV = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()


def _signup_login(client, firm, email, password="passw0rd!1234"):
    client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": password, "confirm_password": password,
    }, follow_redirects=False)
    client.post("/login", data={"email": email, "password": password},
                follow_redirects=False)


def s4_single_upload_supersedes_prior_gl():
    """The real /upload route: uploading a replacement GL supersedes the
    prior GL so Step 5 stops reading the old one (Cesar QA item 4)."""
    import io
    appmod = _reset_app()
    demo_mode = sys.modules["demo_mode"]
    c = appmod.app.test_client()
    _signup_login(c, "Single Upload Firm", "single@example.test")

    def _upload(name):
        r = c.post("/upload", data={
            "company_name": "Acme", "email": "single@example.test",
            "ledger_file": (io.BytesIO(GL_CSV), name),
        }, content_type="multipart/form-data", follow_redirects=False)
        assert r.status_code == 302, r.status_code
        return r.headers["Location"].rsplit("/", 1)[-1]

    job1 = _upload("gl_january.csv")
    job2 = _upload("gl_february.csv")
    assert job1 != job2

    user = appmod.db.get_user_by_email("single@example.test")
    active = appmod._firm_latest_jobs_by_type(
        user["firm_id"], "general_ledger", limit=500
    )
    active_ids = [j["id"] for j in active]
    assert active_ids == [job2], \
        f"single-upload replacement should leave only the new GL active: {active_ids}"
    # The prior job still exists in the DB, just flagged superseded.
    by_id = {j["id"]: j for j in appmod.db.list_jobs_for_firm(user["firm_id"], limit=500)}
    assert demo_mode.is_superseded_job(by_id[job1]), "old GL must be superseded"
    print("S4 OK: single /upload replacement supersedes the prior GL")


def s5_bulk_multi_gl_keeps_all_active():
    """The bulk route: several monthly GLs in one batch must all stay
    active — the batch must not archive its own earlier files (item 12)."""
    import io
    from werkzeug.datastructures import MultiDict
    appmod = _reset_app()
    c = appmod.app.test_client()
    _signup_login(c, "Bulk GL Firm", "bulkgl@example.test")

    data = MultiDict()
    data["company_name"] = "Acme"
    data["email"] = "bulkgl@example.test"
    data.add("ledger_files", (io.BytesIO(GL_CSV), "gl_jan.csv"))
    data.add("ledger_files", (io.BytesIO(GL_CSV), "gl_feb.csv"))
    data.add("ledger_files", (io.BytesIO(GL_CSV), "gl_mar.csv"))
    r = c.post("/upload/bulk", data=data,
               content_type="multipart/form-data", follow_redirects=False)
    assert r.status_code == 302, r.status_code

    user = appmod.db.get_user_by_email("bulkgl@example.test")
    active = appmod._firm_latest_jobs_by_type(
        user["firm_id"], "general_ledger", limit=500
    )
    assert len(active) == 3, \
        f"all three monthly GLs in one batch must stay active, got {len(active)}"
    print("S5 OK: bulk multi-GL batch keeps every monthly GL active")


def main():
    failures = []
    for fn in (
        s1_supersede_targets_only_same_type_priors,
        s2_filter_active_drops_superseded,
        s3_latest_gl_is_only_the_new_upload,
        s4_single_upload_supersedes_prior_gl,
        s5_bulk_multi_gl_keeps_all_active,
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e}")
    if failures:
        raise SystemExit(f"{len(failures)} test(s) failed")
    print("\nALL SUPERSEDE-PRIOR-GL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
