"""Smoke tests for two UX fixes:

Run from project root:

    python3 tests/smoke_demo_default_and_step2_cta.py

Covers:
  D1  On a demo deploy (``DEMO_MODE=true``), the cutover setup page
      pre-selects the "Skip AR/AP migration entirely" option when the
      firm has no stored choice yet. The pre-select is shown as the
      selected ``<option>`` and a small note explains it is a demo
      default.
  D2  On a non-demo deploy, the same page renders no AR/AP default —
      "Not decided yet" stays selected.
  D3  A firm that already saved a non-skip strategy keeps that choice
      even when DEMO_MODE is on (we never overwrite an explicit pick).
  S1  ``customer_workflow.upload_stage_ready_to_advance`` returns False
      when only some of the required uploads are present, True once the
      account list + starting balances + transaction history are all on
      file (even before QBO posting).
  S2  When the upload stage is current AND the required uploads are
      present, ``build_customer_stages`` switches the upload CTA from
      "Upload another report" to "Next: Match accounts" and the URL
      points at the migration checklist instead of the upload intake.
  S3  Migration-checklist HTML for that ready state contains a primary
      "Next: Match accounts" CTA and the new "Continue to Step 3"
      messaging — i.e. users are not trapped on Step 2 after a
      successful upload.
  S4  When required uploads are still missing, the migration-checklist
      page lists what's still needed and keeps the "Add more reports"
      affordance visible.
"""

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


def _reset_app(env):
    for mod in ("app", "operator_panel", "demo_mode", "encryption",
                "customer_workflow", "cutover_workflow"):
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
    for k in ("OPERATOR_EMAILS", "SHOW_OPERATOR_TOOLS",
              "DEMO_MODE", "APP_DEMO_MODE"):
        os.environ.pop(k, None)
    base.update(env)
    for k, v in base.items():
        os.environ[k] = v
    return importlib.import_module("app")


def _signup_and_login(client, firm, email, password="passw0rd!1234"):
    client.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
        follow_redirects=False,
    )


def d1_demo_default_preselects_skip():
    appmod = _reset_app({"DEMO_MODE": "true"})
    c = appmod.app.test_client()
    _signup_and_login(c, "Demo Firm", "demo1@demo.test")

    r = c.get("/cutover")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert '<option value="skip" selected>' in body, \
        "skip option should be pre-selected on a demo deploy"
    assert "Pre-selected for the demo" in body, \
        "Demo default helper text should be visible"
    print("D1 OK: demo deploy pre-selects skip AR/AP")


def d2_non_demo_has_no_ar_ap_default():
    appmod = _reset_app({})
    c = appmod.app.test_client()
    _signup_and_login(c, "Regular Firm", "user2@regular.test")

    r = c.get("/cutover")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert '<option value="skip" selected>' not in body, \
        "skip option must NOT be pre-selected on a production deploy"
    assert '<option value="" selected>Not decided yet</option>' in body, \
        '"Not decided yet" should be the default on prod'
    assert "Pre-selected for the demo" not in body, \
        "Demo helper text should not appear on prod"
    print("D2 OK: prod deploy renders no AR/AP default")


def d3_explicit_choice_preserved_in_demo_mode():
    appmod = _reset_app({"DEMO_MODE": "true"})
    c = appmod.app.test_client()
    _signup_and_login(c, "Demo Firm", "demo3@demo.test")

    # Save an explicit choice the user picked themselves.
    r = c.post(
        "/cutover",
        data={
            "cutover_date": "2026-04-01",
            "country": "US",
            "accounting_basis": "accrual",
            "ar_ap_strategy": "summary_je",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.status_code

    r = c.get("/cutover")
    body = r.get_data(as_text=True)
    assert '<option value="summary_je" selected>' in body, \
        "explicit user choice must be preserved even with DEMO_MODE on"
    assert '<option value="skip" selected>' not in body, \
        "demo default must not override a saved explicit choice"
    print("D3 OK: explicit AR/AP choice preserved in demo mode")


def s1_upload_stage_ready_flag():
    appmod = _reset_app({})  # imports modules fresh
    cutover_workflow = importlib.import_module("cutover_workflow")
    customer_workflow = importlib.import_module("customer_workflow")

    def item(key, status, planned=False):
        return cutover_workflow.ChecklistItem(
            key=key, label=key, status=status, summary="", planned=planned,
        )

    items_none = [
        item(cutover_workflow.STEP_COA_UPLOAD, cutover_workflow.STATUS_NOT_STARTED),
        item(cutover_workflow.STEP_OPENING_TB, cutover_workflow.STATUS_NOT_STARTED),
        item(cutover_workflow.STEP_GL_UPLOAD, cutover_workflow.STATUS_NOT_STARTED),
    ]
    assert customer_workflow.upload_stage_ready_to_advance(items_none) is False

    items_partial = [
        item(cutover_workflow.STEP_COA_UPLOAD, cutover_workflow.STATUS_IN_PROGRESS),
        item(cutover_workflow.STEP_OPENING_TB, cutover_workflow.STATUS_NOT_STARTED),
        item(cutover_workflow.STEP_GL_UPLOAD, cutover_workflow.STATUS_IN_PROGRESS),
    ]
    assert customer_workflow.upload_stage_ready_to_advance(items_partial) is False
    missing = customer_workflow.upload_stage_missing_reports(items_partial)
    assert any("Starting balances" in m for m in missing), missing

    items_all = [
        item(cutover_workflow.STEP_COA_UPLOAD, cutover_workflow.STATUS_IN_PROGRESS),
        item(cutover_workflow.STEP_OPENING_TB, cutover_workflow.STATUS_IN_PROGRESS),
        item(cutover_workflow.STEP_GL_UPLOAD, cutover_workflow.STATUS_IN_PROGRESS),
    ]
    assert customer_workflow.upload_stage_ready_to_advance(items_all) is True
    assert customer_workflow.upload_stage_missing_reports(items_all) == []
    print("S1 OK: upload-stage ready flag + missing list behave correctly")


def s2_upload_cta_switches_to_match_accounts_when_ready():
    appmod = _reset_app({})
    cutover_workflow = importlib.import_module("cutover_workflow")
    customer_workflow = importlib.import_module("customer_workflow")

    Item = cutover_workflow.ChecklistItem
    items = [
        Item(cutover_workflow.STEP_CUTOVER_SETUP, "Setup",
             cutover_workflow.STATUS_COMPLETE),
        Item(cutover_workflow.STEP_COA_UPLOAD, "COA",
             cutover_workflow.STATUS_IN_PROGRESS),
        Item(cutover_workflow.STEP_OPENING_TB, "TB",
             cutover_workflow.STATUS_IN_PROGRESS),
        Item(cutover_workflow.STEP_GL_UPLOAD, "GL",
             cutover_workflow.STATUS_IN_PROGRESS),
        Item(cutover_workflow.STEP_ENDING_TB, "Ending TB",
             cutover_workflow.STATUS_NOT_STARTED),
        Item(cutover_workflow.STEP_TRUST_LISTING, "Trust",
             cutover_workflow.STATUS_NOT_STARTED, planned=True),
        Item(cutover_workflow.STEP_QBO_CONNECT, "QBO",
             cutover_workflow.STATUS_NOT_STARTED),
        Item(cutover_workflow.STEP_ACCOUNT_MAPPING, "Map",
             cutover_workflow.STATUS_NOT_STARTED),
        Item(cutover_workflow.STEP_DRY_RUN, "Dry",
             cutover_workflow.STATUS_NOT_STARTED),
        Item(cutover_workflow.STEP_PROD_IMPORT, "Import",
             cutover_workflow.STATUS_NOT_STARTED),
        Item(cutover_workflow.STEP_RECONCILIATION, "Recon",
             cutover_workflow.STATUS_NOT_STARTED),
    ]
    stages = customer_workflow.build_customer_stages(items, has_jobs=True)
    current = customer_workflow.current_stage(stages)
    assert current is not None, "expected a current stage"
    assert current.key == customer_workflow.STAGE_UPLOAD, current.key
    assert "Match accounts" in current.cta_label, current.cta_label
    assert "match-accounts" in current.cta_url, current.cta_url
    # The CTA must not point back at the checklist or at a dead anchor —
    # both regressions Dan saw on the first pass.
    assert "migration-checklist" not in current.cta_url, current.cta_url
    assert "#" not in current.cta_url, current.cta_url
    print("S2 OK: upload stage CTA flips to '/match-accounts' when ready")


def _seed_job(appmod, firm_id, user_id, job_id, report_type):
    appmod.db.upsert_job(
        job_id=job_id, firm_id=firm_id, user_id=user_id,
        company="Step2 Firm", source_file=f"{report_type}.csv",
        encrypted_file=f"enc_{job_id}", file_sha256=("x" * 64),
        status="Uploaded",
    )
    appmod.db.save_job_state(job_id, {
        "status": "Uploaded", "report_type": report_type,
    })


def s3_checklist_page_shows_continue_cta():
    appmod = _reset_app({"DEMO_MODE": "true"})  # demo deploy is fine for this
    c = appmod.app.test_client()
    _signup_and_login(c, "Step2 Firm", "step2@step2.test")
    # Mark cutover setup minimally complete so the upload stage becomes the
    # 'current' one in the stepper.
    c.post(
        "/cutover",
        data={
            "cutover_date": "2026-04-01",
            "country": "US",
            "accounting_basis": "accrual",
        },
        follow_redirects=False,
    )

    # Stub the firm's jobs so the checklist treats COA + TB + GL as uploaded.
    user = appmod.db.get_user_by_email("step2@step2.test")
    fid, uid = user["firm_id"], user["id"]
    _seed_job(appmod, fid, uid, "job-coa", "chart_of_accounts")
    _seed_job(appmod, fid, uid, "job-tb", "trial_balance")
    _seed_job(appmod, fid, uid, "job-gl", "general_ledger")

    r = c.get("/migration-checklist")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "Start Step 3: Match accounts" in body, \
        "ready-to-advance card heading or CTA label missing"
    # Both CTAs on the page (the header one from the stepper and the
    # next-step card one) must point at the real /match-accounts entry,
    # not at the checklist or a dead #anchor.
    assert 'href="/match-accounts"' in body, \
        "primary CTA must link to /match-accounts"
    assert 'href="/migration-checklist"' not in body or body.count(
        'href="/match-accounts"') >= 2, \
        "Both stepper + next-card CTAs should point at /match-accounts"
    # No stale 'Open the checklist' / 'Open checklist' / 'Go to upload'
    # primary CTA should be the headline action when reports are ready.
    assert "Open the checklist" not in body
    assert "Open checklist" not in body
    print("S3 OK: migration-checklist routes both CTAs to /match-accounts")


def s5_match_accounts_route_dispatches():
    """Step 3 entry route lands the user somewhere real, not a no-op.

    With no GL job on file, the route flashes a missing-prereq message
    and redirects back to the checklist.

    With a GL job but no QBO connection, the route redirects to the GL
    job's connect-qbo flow (the real prerequisite).
    """
    appmod = _reset_app({})
    c = appmod.app.test_client()
    _signup_and_login(c, "Step3 Firm", "step3@step3.test")
    user = appmod.db.get_user_by_email("step3@step3.test")
    fid, uid = user["firm_id"], user["id"]

    # No GL job yet: should redirect to /migration-checklist with a flash.
    r = c.get("/match-accounts", follow_redirects=False)
    assert r.status_code == 302, r.status_code
    assert "/migration-checklist" in r.headers["Location"], r.headers
    follow = c.get(r.headers["Location"])
    assert b"general ledger" in follow.data or b"transaction history" in follow.data

    # Now seed a GL job. Connect-qbo should be the redirect target.
    _seed_job(appmod, fid, uid, "gl-job-1", "general_ledger")
    r = c.get("/match-accounts", follow_redirects=False)
    assert r.status_code == 302, r.status_code
    loc = r.headers["Location"]
    assert "/jobs/gl-job-1/connect-qbo" in loc, loc
    print("S5 OK: /match-accounts dispatches to connect-qbo with no QBO, "
          "and back to checklist with no GL job")


def s6_match_accounts_dispatches_to_mapping_when_connected():
    """When a GL job has a QBO connection, /match-accounts goes
    straight to that job's account-mapping page."""
    appmod = _reset_app({})
    c = appmod.app.test_client()
    _signup_and_login(c, "Step3 Conn Firm", "step3conn@step3.test")
    user = appmod.db.get_user_by_email("step3conn@step3.test")
    fid, uid = user["firm_id"], user["id"]
    _seed_job(appmod, fid, uid, "gl-job-2", "general_ledger")

    # Simulate a stored QBO connection for this job — encrypted token
    # blobs are opaque to the dispatch logic, which only checks presence.
    appmod.db.upsert_qbo_connection(
        job_id="gl-job-2",
        firm_id=fid,
        realm_id="9999",
        access_token_enc="enc-access",
        refresh_token_enc="enc-refresh",
        expires_at="2099-01-01T00:00:00",
        company_name="Demo QBO Co.",
        legal_name="Demo QBO Co.",
        country="US",
    )

    r = c.get("/match-accounts", follow_redirects=False)
    assert r.status_code == 302, r.status_code
    loc = r.headers["Location"]
    assert "/jobs/gl-job-2/account-mapping" in loc, loc
    print("S6 OK: /match-accounts dispatches to account-mapping when "
          "a QBO connection exists")


def s4_missing_reports_keep_add_more_visible():
    appmod = _reset_app({})
    c = appmod.app.test_client()
    _signup_and_login(c, "Step2b Firm", "step2b@step2b.test")
    c.post(
        "/cutover",
        data={
            "cutover_date": "2026-04-01",
            "country": "US",
            "accounting_basis": "accrual",
        },
        follow_redirects=False,
    )
    user = appmod.db.get_user_by_email("step2b@step2b.test")
    fid, uid = user["firm_id"], user["id"]
    # Only upload the COA — TB and GL are missing.
    _seed_job(appmod, fid, uid, "job-coa-only", "chart_of_accounts")

    r = c.get("/migration-checklist")
    body = r.get_data(as_text=True)
    assert "Upload these to keep going" in body, \
        "missing-reports next-card heading missing"
    assert "Starting balances" in body
    assert "Transaction history" in body
    assert "Add more reports" in body, \
        "'Add more reports' affordance should remain visible"
    print("S4 OK: missing reports listed + 'Add more reports' still visible")


def main():
    d1_demo_default_preselects_skip()
    d2_non_demo_has_no_ar_ap_default()
    d3_explicit_choice_preserved_in_demo_mode()
    s1_upload_stage_ready_flag()
    s2_upload_cta_switches_to_match_accounts_when_ready()
    s3_checklist_page_shows_continue_cta()
    s4_missing_reports_keep_add_more_visible()
    s5_match_accounts_route_dispatches()
    s6_match_accounts_dispatches_to_mapping_when_connected()
    print("\nALL OK")


if __name__ == "__main__":
    main()
