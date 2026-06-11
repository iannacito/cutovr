"""Regression smoke tests for the data-safety bugs found in the end-to-end
walkthrough (2026-06-11). These exercise the real /upload route and the
checklist so a regression in the upload pipeline surfaces immediately.

Run from project root::

    python3 tests/smoke_e2e_data_safety.py

Covers
------
  E1  (bug 1) A header-only CSV (zero data rows) is rejected as an error
       and never supersedes a firm's real, previously-uploaded general
       ledger. The good GL stays the active Step 5 target.
  E2  (bug 2) A failed/garbage upload is stored under the neutral
       "unknown" report type, so it never pools with general-ledger jobs
       and can never become the Step 5 import target.
  E3  (bug 3) Uploading a general ledger while choosing "Account list"
       (chart_of_accounts) is blocked as a mismatch instead of being
       processed as the wrong type. The mismatch message is plain-English
       and names both the chosen and detected types.
  E4  (bug 4) Chart-of-Accounts creation history persists across a
       dashboard reload (hydrate from DB) and survives a later re-upload
       (supersede) of the chart of accounts — the "created in QuickBooks"
       checklist step stays ticked.
  E5  (bug 6) A recognized-but-unsupported report (e.g. a bank
       reconciliation) is gated with a "coming soon" message instead of
       being misprocessed as a general ledger.
"""

from __future__ import annotations

import importlib
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENC_KEY_VALUE = "Yh7m5b1J9P0sR8wQv3KsVJpC1Bl0r2Gn9D6X2g8oZqU="
SECRET_VALUE = "z" * 64

GL_CSV = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()
COA_CSV = (ROOT / "test_data" / "01_chart_of_accounts.csv").read_bytes()
GL_HEADER = GL_CSV.split(b"\n", 1)[0] + b"\n"  # header row, no data rows


def _reset_app(env=None):
    for mod in ("app", "operator_panel", "demo_mode", "encryption",
                "cutover_workflow", "report_types"):
        if mod in sys.modules:
            del sys.modules[mod]
    base = {
        "APP_DB": tempfile.mktemp(suffix=".sqlite3"),
        "IMPORT_HISTORY_DB": tempfile.mktemp(suffix=".sqlite3"),
        "UPLOAD_DIR": tempfile.mkdtemp(prefix="pclaw_uploads_e2e_"),
        "OUTPUT_DIR": tempfile.mkdtemp(prefix="pclaw_outputs_e2e_"),
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


def _signup_login(client, firm, email, password="passw0rd!1234"):
    client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": password, "confirm_password": password,
    }, follow_redirects=False)
    client.post("/login", data={"email": email, "password": password},
                follow_redirects=False)


def _upload(client, body, name, report_type=""):
    return client.post("/upload", data={
        "company_name": "Acme", "email": "e2e@example.test",
        "report_type": report_type,
        "ledger_file": (io.BytesIO(body), name),
    }, content_type="multipart/form-data", follow_redirects=False)


def e1_empty_upload_never_supersedes_real_gl():
    appmod = _reset_app()
    demo_mode = sys.modules["demo_mode"]
    c = appmod.app.test_client()
    _signup_login(c, "E1 Firm", "e2e@example.test")
    user = appmod.db.get_user_by_email("e2e@example.test")

    # A real GL goes in first and becomes the active Step 5 target.
    r = _upload(c, GL_CSV, "real_gl.csv")
    assert r.status_code == 302, r.status_code
    good_id = r.headers["Location"].rsplit("/", 1)[-1]
    active = appmod._firm_latest_jobs_by_type(user["firm_id"], "general_ledger")
    assert [j["id"] for j in active] == [good_id], "real GL should be active"

    # Now a header-only CSV. It must be rejected and must NOT supersede
    # the good GL.
    r = _upload(c, GL_HEADER, "empty_gl.csv")
    assert r.status_code == 302, r.status_code
    empty_id = r.headers["Location"].rsplit("/", 1)[-1]

    by_id = {j["id"]: j for j in appmod.db.list_jobs_for_firm(user["firm_id"], limit=500)}
    assert by_id[empty_id]["status"].startswith("Error:"), \
        f"empty upload must be an error, got {by_id[empty_id]['status']!r}"
    # The good GL is untouched: not superseded, still the active target.
    assert not demo_mode.is_superseded_job(by_id[good_id]), \
        "a header-only upload must never supersede the real general ledger"
    active = appmod._firm_latest_jobs_by_type(user["firm_id"], "general_ledger")
    assert [j["id"] for j in active] == [good_id], \
        f"real GL must remain the sole active Step 5 target, got {[j['id'] for j in active]}"
    print("E1 OK: header-only upload rejected; real GL stays active")


def e2_failed_upload_stored_as_unknown():
    appmod = _reset_app()
    rt = sys.modules["report_types"]
    c = appmod.app.test_client()
    _signup_login(c, "E2 Firm", "e2e@example.test")
    user = appmod.db.get_user_by_email("e2e@example.test")

    # Garbage that the pipeline can't parse as any known report.
    junk = b"col_a,col_b\nfoo,bar\nbaz,qux\n"
    r = _upload(c, junk, "junk.csv")
    assert r.status_code == 302, r.status_code
    junk_id = r.headers["Location"].rsplit("/", 1)[-1]

    by_id = {j["id"]: j for j in appmod.db.list_jobs_for_firm(user["firm_id"], limit=500)}
    assert by_id[junk_id]["status"].startswith("Error:"), \
        f"junk upload must be an error, got {by_id[junk_id]['status']!r}"
    assert (by_id[junk_id].get("report_type") or "") == rt.REPORT_UNKNOWN, \
        f"failed upload must be stored as 'unknown', got {by_id[junk_id].get('report_type')!r}"
    # It must not appear in the GL pool used by Step 5.
    active = appmod._firm_latest_jobs_by_type(user["firm_id"], "general_ledger")
    assert junk_id not in [j["id"] for j in active], \
        "a failed upload must never become a general-ledger Step 5 candidate"
    print("E2 OK: failed upload stored as 'unknown', stays out of GL pool")


def e3_wrong_type_upload_blocked_as_mismatch():
    appmod = _reset_app()
    c = appmod.app.test_client()
    _signup_login(c, "E3 Firm", "e2e@example.test")
    user = appmod.db.get_user_by_email("e2e@example.test")

    # Upload a general ledger but tell the app it's an account list.
    r = _upload(c, GL_CSV, "gl_as_coa.csv", report_type="chart_of_accounts")
    assert r.status_code == 302, r.status_code
    job_id = r.headers["Location"].rsplit("/", 1)[-1]

    # The error status is persisted; the structured validation message lives
    # on the in-memory job (it drives the flash + job-detail page).
    db_job = appmod.db.hydrate_job(job_id)
    assert db_job["status"].startswith("Error:"), \
        f"wrong-type upload must be blocked, got {db_job['status']!r}"
    job = appmod.jobs[job_id]
    err = job.get("last_validation_error") or {}
    blob = (err.get("headline", "") + " " + err.get("action", "")).lower()
    # Plain-English mismatch message names what it looks like vs. what was chosen.
    assert "account list" in blob, f"message should name the chosen type: {blob!r}"
    assert "general ledger" in blob, f"message should name the detected type: {blob!r}"
    # The blocked upload must not pool with real general ledgers.
    active = appmod._firm_latest_jobs_by_type(user["firm_id"], "general_ledger")
    assert job_id not in [j["id"] for j in active], \
        "a mismatched upload must never become a GL Step 5 candidate"
    print("E3 OK: GL-as-account-list blocked with a plain-English mismatch")


def e4_coa_created_state_persists_and_survives_replacement():
    appmod = _reset_app()
    cw = sys.modules["cutover_workflow"]
    c = appmod.app.test_client()
    _signup_login(c, "E4 Firm", "e2e@example.test")
    user = appmod.db.get_user_by_email("e2e@example.test")

    # Upload a chart of accounts and simulate a successful QBO create by
    # recording a coa_create_history entry on the job (the create route is
    # covered end-to-end in smoke_coa_create.py; here we focus on
    # persistence + survival across reload/replacement).
    r = _upload(c, COA_CSV, "coa.csv", report_type="chart_of_accounts")
    assert r.status_code == 302, r.status_code
    coa_id = r.headers["Location"].rsplit("/", 1)[-1]

    job = appmod.jobs[coa_id]
    job["coa_create_history"] = [{
        "created_at": "2026-06-11T00:00:00Z",
        "created_count": 7,
        "failed_count": 0,
    }]
    job["status"] = "Chart of Accounts created in QuickBooks"
    appmod._save_job(coa_id)

    # 1) Survives a "dashboard reload": drop the in-memory cache and read
    # the checklist purely from the DB-hydrated jobs.
    appmod.jobs.pop(coa_id, None)
    _cutover, items, _next = appmod._build_firm_checklist(user["firm_id"])
    coa_item = next(i for i in items if i.key == cw.STEP_COA_UPLOAD)
    assert coa_item.status == cw.STATUS_COMPLETE, \
        f"COA-created step must persist across reload, got {coa_item}"
    assert "created" in coa_item.summary.lower()
    print("E4a OK: COA created state survives a dashboard reload (DB hydrate)")

    # 2) Survives a re-upload (supersede) of the chart of accounts. The new
    # upload has no create history, but accounts were already created in
    # QuickBooks, so the milestone must stay complete.
    r = _upload(c, COA_CSV, "coa_v2.csv", report_type="chart_of_accounts")
    assert r.status_code == 302, r.status_code
    new_coa_id = r.headers["Location"].rsplit("/", 1)[-1]
    assert new_coa_id != coa_id

    _cutover, items, _next = appmod._build_firm_checklist(user["firm_id"])
    coa_item = next(i for i in items if i.key == cw.STEP_COA_UPLOAD)
    assert coa_item.status == cw.STATUS_COMPLETE, \
        f"COA-created step must survive a re-upload, got {coa_item}"
    print("E4b OK: COA created state survives a chart-of-accounts re-upload")


def e5_coming_soon_report_gated_not_processed():
    """(bug 6) A report Cutovr recognizes but does not yet support (e.g. a
    bank reconciliation) is gated with a 'coming soon' message rather than
    misprocessed as a general ledger or accepted silently."""
    appmod = _reset_app()
    c = appmod.app.test_client()
    _signup_login(c, "E5 Firm", "e2e@example.test")
    user = appmod.db.get_user_by_email("e2e@example.test")

    bank = (ROOT / "test_data" / "04_bank_balances.csv").read_bytes()
    r = _upload(c, bank, "bank.csv")
    assert r.status_code == 302, r.status_code
    job_id = r.headers["Location"].rsplit("/", 1)[-1]

    db_job = appmod.db.hydrate_job(job_id)
    assert db_job["status"].startswith("Error:"), \
        f"unsupported report must be gated, got {db_job['status']!r}"
    job = appmod.jobs[job_id]
    err = job.get("last_validation_error") or {}
    blob = (err.get("headline", "") + " " + err.get("action", "")).lower()
    assert "support" in blob or "soon" in blob, \
        f"message should explain the report is not yet supported: {blob!r}"
    # And it never becomes a GL Step 5 candidate.
    active = appmod._firm_latest_jobs_by_type(user["firm_id"], "general_ledger")
    assert job_id not in [j["id"] for j in active], \
        "an unsupported report must never become a GL Step 5 candidate"
    print("E5 OK: unsupported report gated as 'coming soon', kept out of GL pool")


def main():
    failures = []
    for fn in (
        e1_empty_upload_never_supersedes_real_gl,
        e2_failed_upload_stored_as_unknown,
        e3_wrong_type_upload_blocked_as_mismatch,
        e4_coa_created_state_persists_and_survives_replacement,
        e5_coming_soon_report_gated_not_processed,
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e}")
    if failures:
        raise SystemExit(f"{len(failures)} test(s) failed")
    print("\nALL E2E DATA-SAFETY SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
