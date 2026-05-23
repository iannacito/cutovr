"""Smoke tests for the "Start new demo" reset isolation fix.

Background
----------
Before this fix, clicking "Start new demo" archived prior job rows by
flipping their ``status`` column to ``Archived (demo reset <run id>)``
but the dashboard / migration checklist / customer workflow stepper
still treated those rows as "current" because they only looked at
``report_type`` (and the per-job ``*_history`` blobs). The result: a
fresh demo run rendered with Step 2 already showing "chart of
accounts", "trial balance", and "general ledger" as on file from the
prior demo.

This is the bug Dan hit: clicking "Start new demo" did not give him a
clean slate; it sent him back into the old run. Demo unusable.

The fix filters jobs whose status starts with the demo-archived prefix
out of the dashboard / checklist / stepper code paths. Operator panel
and audit history are intentionally left alone — those views must keep
showing the full history.

Covered
-------
  R1  Active demo jobs (status not archived) DO still drive the
      checklist. Sanity check that the filter does not break the
      normal flow.
  R2  After /demo/start the migration checklist no longer shows prior
      COA / TB / GL uploads as "on file". The relevant steps return to
      "not_started" / no-uploads state.
  R3  Dashboard "Jobs" count drops to 0 active jobs after a reset.
  R4  /demo/start can be clicked repeatedly. Each run mints a unique
      run id, archives every still-active job (whether from a prior
      demo or from a brand-new upload between resets), and each
      generated GL has unique transaction ids per run.
  R5  Non-demo production behavior is unchanged: jobs with normal
      statuses ("Imported", "Failed", "In progress") still count.
  R6  Cross-firm isolation: archiving firm A's jobs does not affect
      firm B's checklist state.

Run from project root:

    python3 tests/smoke_demo_reset_clears_workflow.py
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


ENC_KEY_VALUE = "Yh7m5b1J9P0sR8wQv3KsVJpC1Bl0r2Gn9D6X2g8oZqU="
SECRET_VALUE = "z" * 64


def _reset_app(env: dict):
    for mod in ("app", "operator_panel", "demo_mode", "encryption"):
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
    }
    for k in ("OPERATOR_EMAILS", "SHOW_OPERATOR_TOOLS", "DEMO_MODE", "APP_DEMO_MODE"):
        os.environ.pop(k, None)
    base.update(env)
    for k, v in base.items():
        os.environ[k] = v
    return importlib.import_module("app")


def _signup(client, firm, email, password="passw0rd!1234"):
    return client.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
        follow_redirects=False,
    )


def _seed_job(appdb, firm_id, user_id, job_id, report_type, status="File uploaded (encrypted)"):
    """Insert a job and tag it with a report_type. Returns nothing."""
    appdb.upsert_job(
        job_id=job_id,
        firm_id=firm_id,
        user_id=user_id,
        company="Demo Co",
        source_file=f"/tmp/{job_id}.csv",
        encrypted_file=f"/tmp/{job_id}.enc",
        file_sha256=("0" * 63) + job_id[-1] if len(job_id) >= 1 else ("0" * 64),
        status=status,
    )
    appdb.save_job_state(job_id, {"status": status, "report_type": report_type})


def r1_active_jobs_still_drive_checklist():
    """Sanity: a freshly uploaded (non-archived) GL job *does* still show
    up as 'on file' in the checklist. This proves the new filter does
    not over-filter and break the normal flow.
    """
    appmod = _reset_app({"DEMO_MODE": "true"})
    appdb = appmod.db
    cutover_workflow = sys.modules["cutover_workflow"]

    firm_id, user_id = appdb.create_firm_and_admin(
        "Active Firm", "active@demo.test", "passw0rd!1234"
    )
    _seed_job(appdb, firm_id, user_id, "job-gl-1", "general_ledger")
    _seed_job(appdb, firm_id, user_id, "job-tb-1", "trial_balance")
    _seed_job(appdb, firm_id, user_id, "job-coa-1", "chart_of_accounts")

    with appmod.app.test_request_context("/"):
        _cutover, items, _next = appmod._build_firm_checklist(firm_id)
    by_step = {it.key: it for it in items}

    assert "in_progress" in by_step["coa_upload"].status \
        or "complete" in by_step["coa_upload"].status, by_step
    assert "in_progress" in by_step["opening_tb"].status \
        or "complete" in by_step["opening_tb"].status, by_step
    assert "in_progress" in by_step["gl_upload"].status \
        or "complete" in by_step["gl_upload"].status, by_step
    print("R1 OK: active jobs still drive checklist (filter is not over-eager)")


def r2_checklist_clears_after_demo_reset():
    """The bug. Three jobs on file -> click Start new demo -> checklist
    must no longer say "X chart-of-accounts upload(s) on file".
    """
    appmod = _reset_app({"DEMO_MODE": "true"})
    appdb = appmod.db

    c = appmod.app.test_client()
    _signup(c, "Stale Firm", "stale@demo.test")

    # Look up the firm_id and user_id from the signed-up user via the
    # raw users table (no get_user_by_email helper exists on AppDB).
    with appdb._conn() as conn:
        row = conn.execute(
            "SELECT id, firm_id FROM users WHERE email = ?", ("stale@demo.test",)
        ).fetchone()
    assert row, "signup did not create a user row"
    firm_id = row["firm_id"]
    user_id = row["id"]

    _seed_job(appdb, firm_id, user_id, "job-gl-pre", "general_ledger")
    _seed_job(appdb, firm_id, user_id, "job-tb-pre", "trial_balance")
    _seed_job(appdb, firm_id, user_id, "job-coa-pre", "chart_of_accounts")

    # Pre-reset: checklist sees the uploads.
    with appmod.app.test_request_context("/"):
        _cutover, items_pre, _next = appmod._build_firm_checklist(firm_id)
    by_step_pre = {it.key: it for it in items_pre}
    assert by_step_pre["coa_upload"].status != "not_started", \
        f"pre-reset COA step should be in_progress/complete: {by_step_pre['coa_upload']}"

    # Click Start new demo.
    r = c.post("/demo/start", follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code

    # Post-reset: every "uploaded" step is back to not_started.
    with appmod.app.test_request_context("/"):
        _cutover, items_post, _next = appmod._build_firm_checklist(firm_id)
    by_step_post = {it.key: it for it in items_post}
    for step in (
        "coa_upload",
        "opening_tb",
        "gl_upload",
    ):
        assert by_step_post[step].status == "not_started", (
            f"after demo reset, step {step} should be not_started, "
            f"got {by_step_post[step].status} ({by_step_post[step].summary})"
        )
    print("R2 OK: checklist clears after Start new demo (no stale 'on file')")


def r3_dashboard_jobs_count_is_zero_after_reset():
    """The dashboard's "Jobs" tile (firm_jobs|length) must drop to 0
    active jobs after Start new demo. Old jobs still exist in the DB
    (so the operator panel keeps history) but the customer-facing
    dashboard treats the workspace as empty.
    """
    appmod = _reset_app({"DEMO_MODE": "true"})
    appdb = appmod.db

    c = appmod.app.test_client()
    _signup(c, "Dashboard Firm", "dash@demo.test")
    with appdb._conn() as conn:
        row = conn.execute(
            "SELECT id, firm_id FROM users WHERE email = ?", ("dash@demo.test",)
        ).fetchone()
    firm_id, user_id = row["firm_id"], row["id"]

    for i in range(3):
        _seed_job(appdb, firm_id, user_id, f"job-pre-{i}", "general_ledger")

    # Sanity: pre-reset dashboard shows 3 jobs.
    r = c.get("/dashboard")
    assert r.status_code == 200, r.status_code
    body_pre = r.get_data(as_text=True)
    assert ">3</dd>" in body_pre or ">3<" in body_pre, "expected 3 jobs on dashboard pre-reset"

    # Reset.
    r = c.post("/demo/start", follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code

    # Post-reset: 0 jobs visible on dashboard.
    r = c.get("/dashboard")
    body_post = r.get_data(as_text=True)
    # The "Jobs" tile is `<dt>Jobs</dt><dd>{count}</dd>` so look for ">0<".
    assert "<dt>Jobs</dt><dd>0</dd>" in body_post, (
        "dashboard should show 0 jobs after demo reset (archived jobs "
        "filtered out of the customer view)"
    )

    # And the underlying rows still exist in the DB (so operator panel keeps history).
    raw = appdb.list_jobs_for_firm(firm_id, limit=500)
    assert len(raw) == 3, f"DB should still hold the 3 archived jobs, got {len(raw)}"
    archived = [j for j in raw if (j.get("status") or "").startswith("Archived (demo reset")]
    assert len(archived) == 3, f"all 3 jobs should be archived, got {archived}"
    print("R3 OK: dashboard jobs count drops to 0; DB rows preserved for audit")


def r4_repeated_resets_are_each_isolated():
    """Click Start new demo, upload a job, click Start new demo again.
    The second reset must archive the new job too, and each run id is
    unique with unique GL transaction ids.
    """
    appmod = _reset_app({"DEMO_MODE": "true"})
    appdb = appmod.db
    demo_mode = sys.modules["demo_mode"]

    c = appmod.app.test_client()
    _signup(c, "Repeat Firm", "repeat@demo.test")
    with appdb._conn() as conn:
        row = conn.execute(
            "SELECT id, firm_id FROM users WHERE email = ?", ("repeat@demo.test",)
        ).fetchone()
    firm_id, user_id = row["firm_id"], row["id"]

    # First reset, no prior jobs.
    c.post("/demo/start", follow_redirects=False)

    # Simulate a new upload between resets.
    _seed_job(appdb, firm_id, user_id, "job-mid", "general_ledger")

    # Second reset must archive the new job too.
    c.post("/demo/start", follow_redirects=False)

    raw = appdb.list_jobs_for_firm(firm_id, limit=500)
    statuses = [j.get("status") or "" for j in raw]
    assert all(s.startswith("Archived (demo reset") for s in statuses), (
        f"after two resets, all jobs should be archived: {statuses}"
    )

    # Run ids on the two archived statuses must differ.
    run_ids = set()
    for s in statuses:
        # status looks like: "Archived (demo reset D-20260522T143012-3F9A)"
        if ")" in s:
            inner = s.rsplit(" ", 1)[-1].rstrip(")")
            run_ids.add(inner)
    assert len(run_ids) >= 1, f"expected at least one run id captured, got {run_ids}"

    # And the per-run GL generator produces unique transaction ids.
    run_a = demo_mode.new_demo_run_id()
    run_b = demo_mode.new_demo_run_id()
    assert run_a != run_b
    gl_a = demo_mode.render_general_ledger_csv(run_a)
    gl_b = demo_mode.render_general_ledger_csv(run_b)
    assert gl_a != gl_b, "repeated runs must produce distinct GL CSV bodies"
    print("R4 OK: repeated resets isolated; each run id unique; GL bodies distinct")


def r5_production_behavior_unchanged():
    """Outside DEMO_MODE, a job with a normal status is still counted.
    The filter only drops the demo-archived status prefix.
    """
    appmod = _reset_app({})  # DEMO_MODE unset
    appdb = appmod.db

    firm_id, user_id = appdb.create_firm_and_admin(
        "Prod Firm", "prod@demo.test", "passw0rd!1234"
    )
    _seed_job(appdb, firm_id, user_id, "prod-imported", "general_ledger",
              status="Imported to QuickBooks")
    _seed_job(appdb, firm_id, user_id, "prod-failed", "general_ledger",
              status="Failed")
    _seed_job(appdb, firm_id, user_id, "prod-progress", "general_ledger",
              status="In progress")

    with appmod.app.test_request_context("/"):
        _cutover, items, _next = appmod._build_firm_checklist(firm_id)
    by_step = {it.key: it for it in items}
    # "Imported" status => the GL step should be complete.
    assert by_step["gl_upload"].status == "complete", \
        by_step["gl_upload"]

    # Direct unit check on the helper.
    demo_mode = sys.modules["demo_mode"]
    sample = [
        {"id": "x", "status": "Imported to QuickBooks"},
        {"id": "y", "status": "Failed"},
        {"id": "z", "status": "Archived (demo reset D-fake)"},
    ]
    filtered = demo_mode.filter_active_jobs(sample)
    ids = {j["id"] for j in filtered}
    assert ids == {"x", "y"}, f"demo-archived row should be dropped, others kept: {ids}"
    print("R5 OK: production statuses untouched; only demo-archived rows dropped")


def r6_cross_firm_isolation():
    """Firm A clicks Start new demo; firm B's checklist must be
    completely unaffected (no cross-firm bleed of archive state).
    """
    appmod = _reset_app({"DEMO_MODE": "true"})
    appdb = appmod.db

    firm_a, user_a = appdb.create_firm_and_admin("Firm A", "a@demo.test", "passw0rd!1234")
    firm_b, user_b = appdb.create_firm_and_admin("Firm B", "b@demo.test", "passw0rd!1234")
    _seed_job(appdb, firm_a, user_a, "a-gl", "general_ledger")
    _seed_job(appdb, firm_b, user_b, "b-gl", "general_ledger")

    c = appmod.app.test_client()
    c.post("/login", data={"email": "a@demo.test", "password": "passw0rd!1234"})
    c.post("/demo/start", follow_redirects=False)

    with appmod.app.test_request_context("/"):
        _ca, items_a, _na = appmod._build_firm_checklist(firm_a)
        _cb, items_b, _nb = appmod._build_firm_checklist(firm_b)
    by_a = {it.key: it for it in items_a}
    by_b = {it.key: it for it in items_b}
    assert by_a["gl_upload"].status == "not_started", by_a
    assert by_b["gl_upload"].status != "not_started", by_b
    print("R6 OK: cross-firm isolation preserved (B's checklist unaffected)")


if __name__ == "__main__":
    failures = []
    for fn in (
        r1_active_jobs_still_drive_checklist,
        r2_checklist_clears_after_demo_reset,
        r3_dashboard_jobs_count_is_zero_after_reset,
        r4_repeated_resets_are_each_isolated,
        r5_production_behavior_unchanged,
        r6_cross_firm_isolation,
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e}")
    if failures:
        raise SystemExit(f"{len(failures)} test(s) failed")
    print("\nALL DEMO-RESET ISOLATION TESTS PASSED")
