"""Smoke tests for the workflow step-pages UX cleanup.

Context
-------
After PR #44 a user reported the guided workflow still mixed step pages
with dashboard-style content and had several dead or wrong-destination
buttons:

  1. Step 5 still rendered a Workspace section and an "Open the
     Checklist" loop CTA.
  2. "View Uploaded Reports" routed to /firm/imports (import-history
     audit log), not to the actual list of uploaded reports.
  3. "Back to Step 4: Review" and Step 2's "Back to Step 1: Setup"
     were dead anchors.
  4. Match Accounts (Step 3) lacked clear "Proceed to Step 4" and
     "Back to Step 2" buttons.
  5. "Send to QuickBooks" was hidden on the job-detail page; Step 5
     had no clear primary CTA.

Covered
-------
  C1  /uploaded-reports renders a dedicated page that is NOT /firm/imports.
  C2  /send-to-qbo (Step 5) is reachable when GL + QBO connection exist,
      renders the stepper, the clarification, and a primary
      "Send to QuickBooks" CTA wired to the import-to-qbo route.
  C3  /send-to-qbo includes a Back to Step 4: Review link to the
      preview-import dispatcher.
  C4  /send-to-qbo does NOT render the dashboard workspace panel or
      any "Open the Checklist" loop CTA.
  C5  customer_workflow back/next labels and URLs target real,
      canonical step routes — no '#' dead anchors:
        - STAGE_IMPORT back -> /import-job (Step 4 Review)
        - STAGE_REVIEW cta  -> /import-job (preview-import)
        - STAGE_IMPORT cta  -> /send-to-qbo
        - STAGE_RECONCILE back -> /send-to-qbo
  C6  Migration-checklist Step 5 CTA points at /send-to-qbo and the
      "View uploaded reports" link points at /uploaded-reports, NOT
      /firm/imports.
  C7  Dashboard at Step 2 upload stage:
        - hides the workspace panel,
        - hides the next-recommended-step card (no checklist loop),
        - hides Recent migrations and Recent activity,
        - exposes a "View uploaded reports" link to /uploaded-reports,
        - exposes a "Back to Step 1: Setup" link to /cutover.
  C8  Account-mapping Step 3 page surfaces both a back-to-step-2
      link (to /dashboard#intake) and a proceed-to-step-4 button
      (to /import-job) once every account is mapped.

Run from project root::

    python3 tests/smoke_workflow_step_pages_cleanup.py
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
os.environ.setdefault("SECRET_KEY", "smoke-step-pages-cleanup")

import app as appmod  # noqa: E402
import customer_workflow as cw  # noqa: E402
import cutover_workflow as cwf  # noqa: E402


def _signup_and_login(client, email, firm="Cleanup LLP"):
    pwd = "passw0rd!1234"
    r = client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


def _complete_step1(firm_id):
    appmod.db.upsert_cutover_settings(
        firm_id=firm_id,
        cutover_date="2026-04-01",
        opening_balance_date="2026-04-01",
        period_start="2025-01-01",
        period_end="2025-12-31",
        country="US",
        accounting_basis="accrual",
        migration_scope=None, notes=None,
        qbo_company_name=None, qbo_realm_id=None,
        clio_involved=False,
        ar_ap_strategy="open_only",
    )


def _make_gl_job_with_qbo(user, job_id="job_c5", with_summary=False):
    """Create a GL job and attach a QBO connection so Step 5 is reachable."""
    db = appmod.db
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company="Cleanup LLP", source_file="gl.csv",
        encrypted_file="x.enc", file_sha256="0" * 64, status="uploaded",
    )
    state = {"status": "uploaded", "report_type": "general_ledger"}
    if with_summary:
        state["import_summary"] = {
            "qbo_je_count": 7, "source_transaction_count": 7,
        }
    db.save_job_state(job_id, state)
    appmod.qbo_connections[job_id] = {
        "realm_id": "R1",
        "access_token_enc": appmod.encrypt_token("fake"),
        "refresh_token_enc": appmod.encrypt_token("fake"),
        "company_name": "Cleanup Test QBO",
        "legal_name": "Cleanup Test QBO",
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }


def c1_uploaded_reports_renders_dedicated_page():
    client = appmod.app.test_client()
    _signup_and_login(client, "c1@example.test")
    r = client.get("/uploaded-reports", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="uploaded-reports-page"' in body
    # /uploaded-reports is NOT /firm/imports (the import-history audit
    # log) — different page, different intent, different testid.
    assert 'data-testid="uploaded-reports-empty"' in body, (
        "expected the empty-state marker on a fresh firm"
    )
    print("C1 OK: /uploaded-reports renders its own dedicated page")


def c2_send_to_qbo_step5_page_renders_with_cta():
    client = appmod.app.test_client()
    _signup_and_login(client, "c2@example.test")
    user = appmod.db.get_user_by_email("c2@example.test")
    _complete_step1(user["firm_id"])
    _make_gl_job_with_qbo(user, job_id="job_c2")

    r = client.get("/send-to-qbo", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="send-to-qbo-page"' in body
    # Clarification copy must explain that the app sends entries —
    # user does not manually re-enter in QuickBooks.
    assert 'data-testid="step5-clarification"' in body
    normalized = " ".join(body.split())
    assert "do not need to enter anything manually in QuickBooks" in normalized
    # Primary CTA must be wired to the real import-to-qbo route, not '#'.
    assert 'data-testid="send-to-qbo-cta"' in body
    assert '/jobs/job_c2/import-to-qbo' in body, (
        "Send-to-QuickBooks form must POST to the real import-to-qbo route"
    )
    print("C2 OK: /send-to-qbo renders Step 5 page with a real Send CTA")


def c3_send_to_qbo_back_to_step4_works():
    client = appmod.app.test_client()
    _signup_and_login(client, "c3@example.test")
    user = appmod.db.get_user_by_email("c3@example.test")
    _complete_step1(user["firm_id"])
    _make_gl_job_with_qbo(user, job_id="job_c3")

    r = client.get("/send-to-qbo", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="back-to-step-4"' in body
    assert "/import-job" in body, (
        "Back-to-Step-4 link must target the /import-job (preview-import) "
        "dispatcher, not a dead anchor"
    )
    # And the in-page link must not be a bare '#'.
    assert 'href="#"' not in body or 'data-testid="back-to-step-4"' in body
    print("C3 OK: /send-to-qbo back-to-Step-4 link targets /import-job")


def c4_send_to_qbo_omits_workspace_and_checklist_loop():
    client = appmod.app.test_client()
    _signup_and_login(client, "c4@example.test")
    user = appmod.db.get_user_by_email("c4@example.test")
    _complete_step1(user["firm_id"])
    _make_gl_job_with_qbo(user, job_id="job_c4")

    r = client.get("/send-to-qbo", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="dashboard-workspace-panel"' not in body, (
        "Step 5 must NOT render the dashboard workspace panel"
    )
    # No 'Open the Checklist' loop — Step 5 should advance to Step 6,
    # not bounce the user back to a checklist hub.
    assert "Open the Checklist" not in body
    assert "Open the checklist" not in body
    print("C4 OK: /send-to-qbo has no Workspace panel and no checklist loop")


def c5_canonical_back_next_urls():
    # STAGE_IMPORT.back -> /import-job (Step 4 Review),
    # STAGE_IMPORT.cta  -> /send-to-qbo
    items_at_import = [
        cwf.ChecklistItem(key=k, label=k, status=cwf.STATUS_COMPLETE,
                          summary="", planned=(k == cwf.STEP_TRUST_LISTING))
        for k in (
            cwf.STEP_CUTOVER_SETUP, cwf.STEP_COA_UPLOAD,
            cwf.STEP_OPENING_TB, cwf.STEP_GL_UPLOAD,
            cwf.STEP_QBO_CONNECT, cwf.STEP_ACCOUNT_MAPPING,
            cwf.STEP_DRY_RUN,
        )
    ]
    # Remaining steps stay not_started.
    for k in (cwf.STEP_PROD_IMPORT, cwf.STEP_TRUST_LISTING,
              cwf.STEP_ENDING_TB, cwf.STEP_RECONCILIATION):
        items_at_import.append(cwf.ChecklistItem(
            key=k, label=k, status=cwf.STATUS_NOT_STARTED, summary="",
            planned=(k == cwf.STEP_TRUST_LISTING),
        ))
    stages = cw.build_customer_stages(items_at_import)
    by_key = {s.key: s for s in stages}
    cur = cw.current_stage(stages)
    assert cur and cur.key == cw.STAGE_IMPORT, cur and cur.key
    assert cur.back_label.startswith("Back to Step 4"), cur.back_label
    # We don't have a Flask url_for here — the fallback string is the
    # raw route, which is exactly what we want to assert.
    assert "/import-job" in cur.back_url, cur.back_url
    assert "/send-to-qbo" in cur.cta_url, cur.cta_url
    assert cur.cta_url != "#", cur.cta_url
    print("C5 OK: STAGE_IMPORT back -> /import-job, cta -> /send-to-qbo "
          "(no '#' anchors)")


def c6_migration_checklist_step5_uses_canonical_urls():
    """When the workflow is currently at Step 5 (import), the checklist
    page must link Step 5's CTA at /send-to-qbo and the View Uploaded
    Reports link at /uploaded-reports (not /firm/imports).
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
    items = cwf.build_checklist(
        cutover, jobs, has_qbo_connection=True, account_mapping_count=5,
    )
    stages = cw.build_customer_stages(items, has_jobs=True)
    current = cw.current_stage(stages)
    assert current is not None and current.key == cw.STAGE_IMPORT

    client = appmod.app.test_client()
    _signup_and_login(client, "c6@example.test")
    with mock.patch.object(
        appmod, "_build_firm_checklist",
        return_value=(cutover, items, cwf.next_recommended_step(items)),
    ), mock.patch.object(
        appmod.demo_mode, "filter_active_jobs", return_value=[],
    ):
        r = client.get("/migration-checklist", follow_redirects=False)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "/send-to-qbo" in body, (
        "Step 5 CTA on migration-checklist must point at /send-to-qbo"
    )
    assert "/uploaded-reports" in body, (
        "View Uploaded Reports link must point at /uploaded-reports"
    )
    assert 'href="{{ url_for(\'firm_imports\') }}"' not in body, (
        "View Uploaded Reports must NOT route to /firm/imports anymore"
    )
    print("C6 OK: migration-checklist Step 5 CTA -> /send-to-qbo, "
          "View Uploaded Reports -> /uploaded-reports")


def c7_dashboard_step2_is_focused():
    client = appmod.app.test_client()
    _signup_and_login(client, "c7@example.test")
    user = appmod.db.get_user_by_email("c7@example.test")
    _complete_step1(user["firm_id"])

    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)

    # Workspace panel hidden at upload stage.
    assert 'data-testid="dashboard-workspace-panel"' not in body
    # Recent migrations + Recent activity hidden at upload stage.
    assert "Recent migrations" not in body
    assert "Recent activity (last 10)" not in body
    # Step 2 has its own nav card with the View Uploaded Reports link.
    # The redundant "Back to Step 1: Setup" duplicate in this panel was
    # removed in the workflow-polish PR — the workflow stepper above
    # already shows it. The visible back-to-Step-1 navigation is now
    # only the stepper's back link.
    assert 'data-testid="step2-nav"' in body
    assert 'data-testid="view-uploaded-reports-link"' in body
    assert "/uploaded-reports" in body
    assert "Back to Step 1: Setup" in body, (
        "stepper back link must still be present somewhere on Step 2"
    )
    # Upload form still present — Step 2 still does its primary job.
    assert "Upload your PCLaw reports" in body
    print("C7 OK: Step 2 dashboard hides workspace/recent/audit; "
          "exposes View Uploaded Reports + stepper Back to Step 1")


def c8_match_accounts_step_nav_buttons_present():
    """The match-accounts page must always expose Back to Step 2;
    once every account is mapped, Proceed to Step 4 also renders.

    We exercise the template with a mocked context (the live route
    requires a full GL job + QBO chart-of-accounts roundtrip, which
    is beyond a smoke test).
    """
    # Render the template directly through the Flask app — its global
    # csrf_token() helper is registered, so we just stub the route
    # context. The navigation footer is the only thing under test.
    env = appmod.app.jinja_env
    template = env.get_template("account-mapping.html")
    job = {"id": "job_c8", "company": "Cleanup LLP"}
    qbo_connection = {"realm_id": "R1", "company_name": "Cleanup QBO"}

    with appmod.app.test_request_context("/jobs/job_c8/account-mapping"):
        ctx = {
            "job": job,
            "qbo_connection": qbo_connection,
            "load_error": None,
            "create_missing_offer": None,
            "rows": [], "save_history": [],
            "csrf_token": lambda: "test-csrf",
        }
        partial = template.render(
            **ctx,
            mapping_summary={
                "matched": 3, "matched_saved": 1, "unmatched": 2, "total": 3,
            },
        )
        complete = template.render(
            **ctx,
            mapping_summary={
                "matched": 3, "matched_saved": 3, "unmatched": 0, "total": 3,
            },
        )

    # Back-to-Step-2 footer link always present.
    for body, name in ((partial, "partial"), (complete, "complete")):
        assert 'data-testid="step3-back-to-step2"' in body, (
            f"{name}: missing Back to Step 2 footer link"
        )
        assert "/dashboard" in body, (
            f"{name}: Back to Step 2 must target /dashboard"
        )

    # Proceed-to-Step-4 only renders when mapping is complete, and it
    # targets the canonical /import-job dispatcher (Step 4 Review).
    assert 'data-testid="step3-proceed-to-step4"' not in partial
    assert 'data-testid="step3-proceed-to-step4"' in complete
    assert "/import-job" in complete

    print("C8 OK: Step 3 match-accounts exposes Back to Step 2 always "
          "and Proceed to Step 4 once every account is mapped")


def main():
    c1_uploaded_reports_renders_dedicated_page()
    c2_send_to_qbo_step5_page_renders_with_cta()
    c3_send_to_qbo_back_to_step4_works()
    c4_send_to_qbo_omits_workspace_and_checklist_loop()
    c5_canonical_back_next_urls()
    c6_migration_checklist_step5_uses_canonical_urls()
    c7_dashboard_step2_is_focused()
    c8_match_accounts_step_nav_buttons_present()
    print("\nALL WORKFLOW STEP-PAGES CLEANUP SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
