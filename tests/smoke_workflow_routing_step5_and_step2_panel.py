"""Smoke tests for the workflow-routing fixes after PR #43.

Background
----------
After PR #43 stabilized the customer-facing 6-step migration flow, four
new user-reported bugs surfaced:

  1. ``Start a new demo`` on /demo did not navigate the user to Step 1
     (the setup page). The button reset state but left the user on the
     demo control panel, which felt broken.
  2. Once a user had completed a prior demo, the stepper would skip
     past Step 3 (Match) and Step 4 (Review) on the *next* demo and
     land them on Step 5 (Import) — because preflight on any uploaded
     file flipped the dry-run checklist step to "complete" and prior
     QBO/mapping state persisted across demo resets. The user wanted
     strict in-order progression for every demo run.
  3. The Step 2 upload screen (the dashboard) rendered a "Workspace"
     panel beside the upload card, which made the page feel like a
     dashboard instead of a guided step.
  4. Step 5's "Open the import job" CTA pointed at /firm/imports, a
     read-only list page — clicking it did nothing actionable.
  5. Step 5 did not clearly tell the user that the app sends entries
     for them; users were left wondering whether they had to manually
     re-enter data inside QuickBooks Online.

Covered
-------
  R1  POST /demo/start redirects to /cutover (Step 1 setup), not /demo.
  R2  After uploading reports on a fresh active demo run, the current
      stage is "match" (Step 3) — *not* "import" — even when prior demo
      state (QBO connection, account mappings, COA preflight) survives
      the reset.
  R3  Sequential gating in build_customer_stages: a later stage cannot
      be marked complete if any earlier stage is upcoming.
  R4  The dashboard at the upload stage hides the workspace panel.
  R5  /import-job (import_job_entry) redirects to a real preview-import
      page when an active GL job + QBO connection exist; redirects to
      migration-checklist with a clear flash when no GL job exists.
  R6  Migration-checklist Step 5 CTA points at /import-job and renders
      the new "you don't need to enter anything in QuickBooks" copy.
  R7  has_dry_run is driven only by GL job preflight — uploading a
      chart-of-accounts CSV (which sets its own preflight) does NOT
      mark Step 4 Review complete.

Run from project root::

    python3 tests/smoke_workflow_routing_step5_and_step2_panel.py
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="pclaw_uploads_")
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_")
os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("DEMO_MODE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-workflow-routing-step5")

import app as appmod  # noqa: E402
import customer_workflow  # noqa: E402
import cutover_workflow  # noqa: E402


def _signup_and_login(client, email, firm):
    pwd = "passw0rd!1234"
    r = client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


def r1_demo_start_redirects_to_step1():
    client = appmod.app.test_client()
    _signup_and_login(client, "r1@example.test", "R1 LLP")
    r = client.post("/demo/start", follow_redirects=False)
    assert r.status_code in (301, 302), r.status_code
    location = r.headers.get("Location", "")
    # cutover_setup is registered on both /cutover and /migration-setup;
    # Flask's url_for picks whichever it likes, so accept either.
    assert "/cutover" in location or "/migration-setup" in location, (
        f"expected /demo/start to redirect to Step 1 setup; "
        f"got {location!r}"
    )
    print(f"R1 OK: /demo/start redirects to Step 1 setup ({location})")


def r2_skip_to_step5_is_blocked():
    """Reproduce the reported bug: after Start-new-demo + fresh
    uploads, the workflow must land on Step 3 (Match), not Step 5
    (Import).

    The fix has two parts:

      * demo_mode.reset_demo_workspace now clears saved account
        mappings, so account_mapping_count drops back to 0 for the
        fresh run. (Mappings are cheap to re-confirm via the alias
        auto-matcher in Step 3.)
      * customer_workflow.build_customer_stages enforces strict
        sequential gating, so any leakage from prior runs still cannot
        leapfrog the user past an incomplete predecessor.

    Scenario: cutover saved (Step 1), uploads in progress (Step 2),
    QBO connection persisted from a prior demo (preserved — same demo
    QBO company is intentionally reused), mappings cleared by the
    reset (account_mapping_count=0).
    """
    cutover = {
        "cutover_date": "2026-04-01", "country": "US",
        "accounting_basis": "accrual",
    }
    jobs = [
        {"id": "c", "report_type": "chart_of_accounts",
         "status": "uploaded", "preflight": {"ready": True}},
        {"id": "tb", "report_type": "trial_balance",
         "status": "uploaded", "preflight": {"ready": True}},
        {"id": "g", "report_type": "general_ledger",
         "status": "uploaded"},
    ]
    items = cutover_workflow.build_checklist(
        cutover, jobs,
        has_qbo_connection=True,   # QBO connection preserved
        account_mapping_count=0,   # mappings cleared on demo reset
    )
    stages = customer_workflow.build_customer_stages(items, has_jobs=True)
    current = customer_workflow.current_stage(stages)
    assert current is not None, "expected a current stage, got none"
    assert current.key == customer_workflow.STAGE_MATCH, (
        f"expected match current after fresh upload, "
        f"got {current.key!r}"
    )
    by_key = {s.key: s for s in stages}
    assert by_key[customer_workflow.STAGE_IMPORT].status == "upcoming", (
        f"import stage should be upcoming, got "
        f"{by_key[customer_workflow.STAGE_IMPORT].status}"
    )
    assert by_key[customer_workflow.STAGE_REVIEW].status == "upcoming"
    print("R2 OK: fresh upload after demo reset lands on Step 3 Match, "
          "not Step 5 Import")


def r2b_reset_clears_account_mappings():
    """The Start-new-demo button must clear saved account mappings so
    the next demo walks through Step 3 again."""
    import demo_mode

    class _StubDB:
        def __init__(self):
            self.jobs = [{"id": "j1", "status": "uploaded"}]
            self.conns = [{"realm_id": "R1"}]
            self.mappings = {
                "R1": [
                    {"pclaw_account_number": "1000",
                     "pclaw_account_name": "Operating"},
                    {"pclaw_account_number": "2100",
                     "pclaw_account_name": "Trust"},
                ],
            }
            self.deleted = []
            self.archived = []

        def list_jobs_for_firm(self, firm_id, limit=500):
            return list(self.jobs)

        def update_job_status(self, job_id, status):
            self.archived.append((job_id, status))
            for j in self.jobs:
                if j["id"] == job_id:
                    j["status"] = status

        def list_qbo_connections_for_firm(self, firm_id):
            return list(self.conns)

        def list_account_mappings(self, firm_id, realm_id):
            return list(self.mappings.get(realm_id) or [])

        def delete_account_mapping(self, firm_id, realm_id,
                                   pclaw_account_number,
                                   pclaw_account_name):
            self.deleted.append(
                (realm_id, pclaw_account_number, pclaw_account_name)
            )

    stub = _StubDB()
    result = demo_mode.reset_demo_workspace(stub, firm_id=1, run_id="D-x")
    assert result["archived_jobs"] == 1
    assert result["cleared_mappings"] == 2, result
    assert len(stub.deleted) == 2
    print("R2b OK: reset_demo_workspace clears saved account mappings "
          "across every connected realm")


def r3_sequential_gating_enforced():
    """With strictly sequential gating, no later stage can be complete
    if any earlier stage is not complete, regardless of raw rollup.
    """
    # Cutover NOT done (Setup upcoming), but Match accidentally has
    # support (QBO + mappings). Sequential gating must force Match to
    # upcoming because Setup is upcoming.
    items = cutover_workflow.build_checklist(
        cutover=None,  # Setup not started
        firm_jobs=[],
        has_qbo_connection=True,
        account_mapping_count=5,
    )
    stages = customer_workflow.build_customer_stages(items, has_jobs=False)
    by_key = {s.key: s for s in stages}
    assert by_key[customer_workflow.STAGE_SETUP].status == "current", (
        f"setup should be current, got {by_key[customer_workflow.STAGE_SETUP].status}"
    )
    for k in (customer_workflow.STAGE_UPLOAD,
              customer_workflow.STAGE_MATCH,
              customer_workflow.STAGE_REVIEW,
              customer_workflow.STAGE_IMPORT,
              customer_workflow.STAGE_RECONCILE):
        assert by_key[k].status == "upcoming", (
            f"{k} must be upcoming when setup is not complete, got "
            f"{by_key[k].status}"
        )
    print("R3 OK: sequential gating forces later stages to upcoming "
          "when an earlier stage is incomplete")


def r4_dashboard_hides_workspace_panel_at_upload_stage():
    client = appmod.app.test_client()
    _signup_and_login(client, "r4@example.test", "R4 LLP")
    # Save cutover so Step 1 is complete and the user is at Step 2.
    client.post("/cutover", data={
        "cutover_date": "2026-04-01",
        "country": "US",
        "accounting_basis": "accrual",
    }, follow_redirects=False)
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="dashboard-workspace-panel"' not in body, (
        "workspace panel must not render on the dashboard while the "
        "user is at the Step 2 upload stage"
    )
    # Upload card must still be present.
    assert "Upload your PCLaw reports" in body, (
        "Step 2 upload card must still be present"
    )
    print("R4 OK: dashboard at Step 2 upload stage hides the workspace "
          "panel but keeps the upload form")


def r5_import_job_entry_routes():
    """/import-job dispatches correctly based on firm state."""
    client = appmod.app.test_client()
    _signup_and_login(client, "r5@example.test", "R5 LLP")

    # No GL job yet — redirect to checklist with a clear flash.
    r = client.get("/import-job", follow_redirects=False)
    assert r.status_code in (301, 302), r.status_code
    assert "/migration-checklist" in r.headers.get("Location", ""), (
        f"expected redirect to /migration-checklist, got {r.headers}"
    )

    # Now create a GL job and a QBO connection, then expect a redirect
    # to the preview-import page.
    db = appmod.db
    user = db.get_user_by_email("r5@example.test")
    job_id = "job_r5"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company="R5 LLP", source_file="gl.csv",
        encrypted_file="x.enc", file_sha256="0" * 64, status="uploaded",
    )
    db.save_job_state(job_id, {
        "status": "uploaded", "report_type": "general_ledger",
    })
    appmod.qbo_connections[job_id] = {
        "realm_id": "R5",
        "access_token_enc": appmod.encrypt_token("fake"),
        "refresh_token_enc": appmod.encrypt_token("fake"),
        "company_name": "Test", "legal_name": "Test", "country": "US",
        "expires_at": "2999-01-01T00:00:00", "company_info_error": None,
    }
    r = client.get("/import-job", follow_redirects=False)
    assert r.status_code in (301, 302), r.status_code
    location = r.headers.get("Location", "")
    assert f"/jobs/{job_id}/preview-import" in location, (
        f"expected redirect to the GL job's preview-import page, "
        f"got {location!r}"
    )
    print("R5 OK: /import-job dispatches to preview-import for the GL "
          "job, or back to checklist with a clear blocker")


def r6_checklist_step5_cta_and_copy():
    """When the workflow is currently at Step 5 (import), the migration
    checklist must point its CTA at /import-job and render the
    clarification copy.
    """
    cutover = {
        "cutover_date": "2026-04-01", "country": "US",
        "accounting_basis": "accrual",
    }
    jobs = [
        {"id": "g", "report_type": "general_ledger", "status": "uploaded",
         "preflight": {"ready": True}},
        {"id": "c", "report_type": "chart_of_accounts",
         "coa_create_history": [{"created_count": 5}]},
        {"id": "tb", "report_type": "trial_balance",
         "opening_balance_history": [{"qbo_je_id": "JE-1"}]},
    ]
    items = cutover_workflow.build_checklist(
        cutover, jobs, has_qbo_connection=True, account_mapping_count=5,
    )
    stages = customer_workflow.build_customer_stages(items, has_jobs=True)
    current = customer_workflow.current_stage(stages)
    assert current is not None and current.key == customer_workflow.STAGE_IMPORT

    client = appmod.app.test_client()
    _signup_and_login(client, "r6@example.test", "R6 LLP")
    with mock.patch.object(
        appmod, "_build_firm_checklist",
        return_value=(cutover, items,
                      cutover_workflow.next_recommended_step(items)),
    ), mock.patch.object(
        appmod.demo_mode, "filter_active_jobs", return_value=[],
    ):
        r = client.get("/migration-checklist", follow_redirects=False)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "/import-job" in body, (
        "Step 5 CTA must link to /import-job (the new dispatcher)"
    )
    assert 'data-testid="step5-clarification"' in body, (
        "Step 5 clarification paragraph must render"
    )
    # The clarification paragraph contains a soft-wrap newline in the
    # template; normalise whitespace before checking the phrase.
    normalized = " ".join(body.split())
    assert "do not need to enter anything manually in QuickBooks" in normalized
    # Plain-English clarification (post copy-cleanup): explicitly says we
    # send everything to QuickBooks on the user's behalf.
    assert "send everything to QuickBooks" in normalized
    print("R6 OK: migration-checklist Step 5 CTA points at /import-job "
          "and renders the 'no manual QBO entry' clarification")


def r7_has_dry_run_requires_gl():
    """Uploading a chart-of-accounts CSV must NOT mark Step 4 (Review)
    complete just because the COA upload sets its own preflight."""
    cutover = {
        "cutover_date": "2026-04-01", "country": "US",
        "accounting_basis": "accrual",
    }
    # Only a COA upload with preflight set — no GL job at all.
    jobs = [
        {"id": "c", "report_type": "chart_of_accounts",
         "status": "uploaded", "preflight": {"ready": True}},
    ]
    items = cutover_workflow.build_checklist(
        cutover, jobs, has_qbo_connection=False, account_mapping_count=0,
    )
    dry_run = next(i for i in items if i.key == cutover_workflow.STEP_DRY_RUN)
    assert dry_run.status == cutover_workflow.STATUS_NOT_STARTED, (
        f"dry-run step must stay not_started without a GL preflight, "
        f"got {dry_run.status}"
    )
    # And with a GL preflight, it should flip to complete.
    jobs.append({"id": "g", "report_type": "general_ledger",
                 "status": "uploaded", "preflight": {"ready": True}})
    items2 = cutover_workflow.build_checklist(
        cutover, jobs, has_qbo_connection=False, account_mapping_count=0,
    )
    dry_run2 = next(i for i in items2 if i.key == cutover_workflow.STEP_DRY_RUN)
    assert dry_run2.status == cutover_workflow.STATUS_COMPLETE, (
        f"dry-run step must flip to complete once a GL preflight exists, "
        f"got {dry_run2.status}"
    )
    print("R7 OK: has_dry_run requires a general-ledger preflight, "
          "not just any uploaded job's preflight")


def main():
    r1_demo_start_redirects_to_step1()
    r2_skip_to_step5_is_blocked()
    r2b_reset_clears_account_mappings()
    r3_sequential_gating_enforced()
    r4_dashboard_hides_workspace_panel_at_upload_stage()
    r5_import_job_entry_routes()
    r6_checklist_step5_cta_and_copy()
    r7_has_dry_run_requires_gl()
    print("\nALL WORKFLOW ROUTING / STEP 5 / STEP 2 PANEL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
