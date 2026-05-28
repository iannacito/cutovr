"""Smoke tests for Step 6 (Reconcile balances) + final-report email flow.

Covered
-------
  R1  /reconcile-balances renders the Step 6 page when Step 5 has
      completed (GL job carries an import_summary), with a
      reconciliation summary card AND the report preview rendered
      inline so the demo user can see what would be sent.
  R2  /reconcile-balances renders a single, clear blocker pointing back
      to Step 5 when nothing has been imported yet.
  R3  Posting an invalid email to /reconcile-balances/send-report
      surfaces a friendly validation error and does NOT call SMTP.
  R4  Posting a valid email when SMTP is NOT configured renders a
      clear "delivery is not configured" message (we never claim it
      was sent or saved), and the report itself is shown on the page.
  R5  Posting a valid email when SMTP IS configured calls
      email_sender.send_email and renders a "sent to <email>" message.
  R6  customer_workflow.STAGE_RECONCILE CTA points at
      /reconcile-balances (not /migration-checklist), so the stepper
      no longer dead-ends.
  R7  final_report.build_reconciliation_summary classifies the
      reconcile lines correctly for the three demo states:
        - no import yet            -> overall pending
        - imported, no ending TB   -> overall completed (skipped lines
                                       don't block completion)
        - missing-account blocker  -> overall blocked
  R8  Posting a valid email when SMTP IS configured but the transport
      fails renders a customer-friendly error AND keeps the report
      preview visible. We never claim it was sent.
  R9  email_sender.is_smtp_configured() honors the Flask-Mail-style
      aliases (MAIL_SERVER / MAIL_USERNAME / MAIL_PASSWORD /
      MAIL_DEFAULT_SENDER) in addition to the SMTP_* names.

Run from project root::

    python3 tests/smoke_step6_reconcile_balances.py
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
os.environ.setdefault("SECRET_KEY", "smoke-step6-reconcile")

# IMPORTANT: ensure SMTP env vars are unset by default — R4 depends on
# is_smtp_configured() returning False. Tests that need SMTP wired up
# mock email_sender directly.
for var in (
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_USERNAME",
    "SMTP_PASSWORD", "SMTP_FROM", "SMTP_FROM_EMAIL", "SMTP_FROM_NAME",
    "SMTP_USE_TLS",
    "MAIL_SERVER", "MAIL_PORT", "MAIL_USERNAME", "MAIL_PASSWORD",
    "MAIL_DEFAULT_SENDER", "MAIL_FROM_NAME", "MAIL_USE_TLS",
):
    os.environ.pop(var, None)

import app as appmod  # noqa: E402
import customer_workflow as cw  # noqa: E402
import cutover_workflow as cwf  # noqa: E402
import final_report  # noqa: E402


def _signup_and_login(client, email, firm="Reconcile LLP"):
    pwd = "passw0rd!1234"
    r = client.post(
        "/signup",
        data={
            "firm_name": firm,
            "email": email,
            "password": pwd,
            "confirm_password": pwd,
        },
        follow_redirects=False,
    )
    if r.status_code == 200:
        client.post(
            "/login",
            data={"email": email, "password": pwd},
            follow_redirects=False,
        )


def _complete_step1(firm_id):
    appmod.db.upsert_cutover_settings(
        firm_id=firm_id,
        cutover_date="2026-04-01",
        opening_balance_date="2026-04-01",
        period_start="2025-01-01",
        period_end="2025-12-31",
        country="US",
        accounting_basis="accrual",
        migration_scope=None,
        notes=None,
        qbo_company_name=None,
        qbo_realm_id=None,
        clio_involved=False,
        ar_ap_strategy="open_only",
    )


def _make_imported_gl_job(user, job_id="job_r1"):
    """Create a GL job, attach a QBO connection, and mark it imported."""
    db = appmod.db
    db.upsert_job(
        job_id=job_id,
        firm_id=user["firm_id"],
        user_id=user["id"],
        company="Reconcile LLP",
        source_file="gl.csv",
        encrypted_file="x.enc",
        file_sha256="0" * 64,
        status="Imported 7 JournalEntries",
    )
    db.save_job_state(
        job_id,
        {
            "status": "Imported 7 JournalEntries",
            "report_type": "general_ledger",
            "import_summary": {
                "qbo_je_count": 7,
                "source_transaction_count": 7,
                "source_debit_total": "1000.00",
                "source_credit_total": "1000.00",
                "balanced": True,
            },
        },
    )
    appmod.qbo_connections[job_id] = {
        "realm_id": "R1",
        "access_token_enc": appmod.encrypt_token("fake"),
        "refresh_token_enc": appmod.encrypt_token("fake"),
        "company_name": "Reconcile Test QBO",
        "legal_name": "Reconcile Test QBO",
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }


def _make_uploaded_gl_job_no_import(user, job_id="job_r2"):
    """GL job uploaded but never imported (Step 5 not done)."""
    db = appmod.db
    db.upsert_job(
        job_id=job_id,
        firm_id=user["firm_id"],
        user_id=user["id"],
        company="Reconcile LLP",
        source_file="gl.csv",
        encrypted_file="x.enc",
        file_sha256="0" * 64,
        status="uploaded",
    )
    db.save_job_state(
        job_id,
        {"status": "uploaded", "report_type": "general_ledger"},
    )


def r1_step6_renders_when_step5_complete():
    client = appmod.app.test_client()
    _signup_and_login(client, "r1@example.test")
    user = appmod.db.get_user_by_email("r1@example.test")
    _complete_step1(user["firm_id"])
    _make_imported_gl_job(user, job_id="job_r1")

    r = client.get("/reconcile-balances", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="reconcile-balances-page"' in body
    # Reconciliation lines render.
    assert 'data-testid="step6-reconcile-lines"' in body
    assert 'data-testid="step6-line-import"' in body
    assert 'data-testid="step6-line-accounts"' in body
    assert 'data-testid="step6-line-starting_balances"' in body
    assert 'data-testid="step6-line-ending_balance"' in body
    assert 'data-testid="step6-line-client_trust"' in body
    # Final-report form present and validated.
    assert 'data-testid="step6-final-report"' in body
    assert 'data-testid="step6-report-form"' in body
    assert 'data-testid="step6-report-submit"' in body
    # Step 6 completion banner present once import is done.
    assert 'data-testid="step6-complete-banner"' in body
    # And the blocked panel is NOT present.
    assert 'data-testid="step6-blocked"' not in body
    # The report preview is visible inline so the demo user can see
    # exactly what would be sent.
    assert 'data-testid="step6-report-preview"' in body, \
        "Step 6 must show the report inline so demo users can see it"
    assert 'data-testid="step6-report-text"' in body
    assert "PCLaw → QuickBooks migration summary" in body
    print("R1 OK: /reconcile-balances renders summary + report preview inline")


def r2_step6_blocked_before_import():
    client = appmod.app.test_client()
    _signup_and_login(client, "r2@example.test")
    user = appmod.db.get_user_by_email("r2@example.test")
    _complete_step1(user["firm_id"])
    _make_uploaded_gl_job_no_import(user, job_id="job_r2")

    r = client.get("/reconcile-balances", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="step6-blocked"' in body
    # Back-to-Step-5 CTA must point at /send-to-qbo, not '#'.
    assert 'data-testid="step6-back-to-step5"' in body
    assert "/send-to-qbo" in body
    # The reconciliation cards must NOT render in blocked state.
    assert 'data-testid="step6-reconcile-lines"' not in body
    assert 'data-testid="step6-final-report"' not in body
    print("R2 OK: /reconcile-balances blocked before import points back to Step 5")


def r3_invalid_email_validation():
    client = appmod.app.test_client()
    _signup_and_login(client, "r3@example.test")
    user = appmod.db.get_user_by_email("r3@example.test")
    _complete_step1(user["firm_id"])
    _make_imported_gl_job(user, job_id="job_r3")

    sent_calls = []
    with mock.patch.object(appmod.email_sender, "send_email",
                           side_effect=lambda **kw: sent_calls.append(kw) or True):
        # POST must redirect (PRG) so a refresh cannot replay the banner.
        r = client.post(
            "/reconcile-balances/send-report",
            data={"email": "not-an-email"},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.status_code
        assert "/reconcile-balances" in r.headers["Location"]
        # Follow the redirect to confirm the banner shows on the GET.
        r = client.get("/reconcile-balances")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # Flash class "error" appears in base template banner.
    assert 'flash error' in body
    assert "valid email address" in body
    # Crucially: we did not attempt to send.
    assert sent_calls == [], sent_calls
    # Refresh the page — the flash must NOT replay (consumed by Flask).
    r = client.get("/reconcile-balances")
    body2 = r.get_data(as_text=True)
    assert "valid email address" not in body2, "Flash replayed on refresh!"
    print("R3 OK: invalid email surfaces a friendly validation error, no SMTP call")


def r4_submit_when_smtp_unconfigured():
    client = appmod.app.test_client()
    _signup_and_login(client, "r4@example.test")
    user = appmod.db.get_user_by_email("r4@example.test")
    _complete_step1(user["firm_id"])
    _make_imported_gl_job(user, job_id="job_r4")

    # SMTP env vars are unset at module load — is_smtp_configured() is
    # False. We still patch send_email so an accidental call would be
    # visible in `sent_calls`.
    sent_calls = []
    with mock.patch.object(appmod.email_sender, "is_smtp_configured",
                           return_value=False), \
         mock.patch.object(appmod.email_sender, "send_email",
                           side_effect=lambda **kw: sent_calls.append(kw) or True):
        # PRG: POST redirects, banner is shown via flash on the GET.
        r = client.post(
            "/reconcile-balances/send-report",
            data={"email": "lawyer@firm.example"},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.status_code
        r = client.get("/reconcile-balances")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # We use the "info" flash class for the not-configured case so the
    # user sees it's neutral (not an error and not a false success).
    assert 'flash info' in body, body[:2000]
    assert "not configured" in body.lower()
    assert "didn't send" in body.lower() or "didn&#39;t send" in body.lower(), (
        "Must not claim the email was sent when SMTP isn't configured"
    )
    # The report itself must be visible on the page even when SMTP is
    # off — that's the whole point of this fix.
    assert 'data-testid="step6-report-preview"' in body
    assert 'data-testid="step6-report-text"' in body
    assert "PCLaw → QuickBooks migration summary" in body
    # Never expose SMTP config state to the user.
    assert "SMTP_" not in body, "Must not leak SMTP env var names"
    # send_email must NOT be called when SMTP is unconfigured.
    assert sent_calls == [], sent_calls
    # Flash must NOT replay on a second GET.
    body2 = client.get("/reconcile-balances").get_data(as_text=True)
    assert "not configured" not in body2.lower() or "flash info" not in body2, (
        "Flash banner replayed on refresh!"
    )
    print("R4 OK: clear 'not configured' info banner + report shown; no SMTP attempted")


def r5_submit_when_smtp_configured_succeeds():
    client = appmod.app.test_client()
    _signup_and_login(client, "r5@example.test")
    user = appmod.db.get_user_by_email("r5@example.test")
    _complete_step1(user["firm_id"])
    _make_imported_gl_job(user, job_id="job_r5")

    sent = {}
    def _fake_send(**kw):
        sent.update(kw)
        return True
    with mock.patch.object(appmod.email_sender, "is_smtp_configured",
                           return_value=True), \
         mock.patch.object(appmod.email_sender, "send_email",
                           side_effect=_fake_send):
        r = client.post(
            "/reconcile-balances/send-report",
            data={"email": "lawyer@firm.example"},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.status_code
        r = client.get("/reconcile-balances")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'flash success' in body
    assert "lawyer@firm.example" in body
    # send_email got called with the recipient + a plain-text body.
    assert sent.get("to") == "lawyer@firm.example", sent
    assert sent.get("subject", "").startswith(
        "PCLaw → QuickBooks migration summary"
    ), sent.get("subject")
    assert "PCLaw → QuickBooks migration summary" in sent.get("body_text", "")
    # The report body must never include SMTP creds.
    body_text = sent.get("body_text", "")
    for forbidden in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        assert forbidden not in body_text, (
            f"report body leaks {forbidden}"
        )
    print("R5 OK: SMTP-configured submit calls send_email and renders success")


def r6_stage_reconcile_cta_points_at_step6_route():
    # Build a checklist where Step 5 (import) is complete so STAGE_RECONCILE
    # becomes the current stage.
    items = [
        cwf.ChecklistItem(
            key=k, label=k, status=cwf.STATUS_COMPLETE, summary="",
            planned=(k == cwf.STEP_TRUST_LISTING),
        )
        for k in (
            cwf.STEP_CUTOVER_SETUP, cwf.STEP_COA_UPLOAD,
            cwf.STEP_OPENING_TB, cwf.STEP_GL_UPLOAD,
            cwf.STEP_QBO_CONNECT, cwf.STEP_ACCOUNT_MAPPING,
            cwf.STEP_DRY_RUN, cwf.STEP_PROD_IMPORT,
        )
    ]
    # Leave the reconcile / ending TB items not_started so STAGE_RECONCILE
    # is the current stage (not auto-rolled-up).
    for k in (cwf.STEP_TRUST_LISTING, cwf.STEP_ENDING_TB,
              cwf.STEP_RECONCILIATION):
        items.append(cwf.ChecklistItem(
            key=k, label=k, status=cwf.STATUS_NOT_STARTED, summary="",
            planned=(k == cwf.STEP_TRUST_LISTING),
        ))
    stages = cw.build_customer_stages(items)
    current = cw.current_stage(stages)
    assert current is not None and current.key == cw.STAGE_RECONCILE, (
        current and current.key
    )
    assert "/reconcile-balances" in current.cta_url, current.cta_url
    assert current.cta_url != "#", current.cta_url
    # The label should clearly name Step 6.
    assert "Step 6" in current.cta_label, current.cta_label
    print("R6 OK: STAGE_RECONCILE CTA -> /reconcile-balances, no dead anchor")


def r7_reconciliation_summary_classification():
    # No import yet -> overall pending, import line pending.
    summary_a = final_report.build_reconciliation_summary(
        firm_name="A LLP",
        cutover={"cutover_date": "2026-04-01"},
        jobs=[
            {"id": "g", "report_type": "general_ledger", "status": "uploaded"},
        ],
        qbo_connections=[],
        account_mapping_count=0,
    )
    by_key = {ln.key: ln for ln in summary_a.lines}
    assert summary_a.overall_status == final_report.STATUS_PENDING
    assert by_key["import"].status == final_report.STATUS_PENDING
    assert by_key["accounts"].status == final_report.STATUS_PENDING

    # Imported -> overall completed even with skipped optional lines.
    summary_b = final_report.build_reconciliation_summary(
        firm_name="B LLP",
        cutover={"cutover_date": "2026-04-01"},
        jobs=[
            {"id": "g", "report_type": "general_ledger",
             "import_summary": {"qbo_je_count": 7,
                                "source_transaction_count": 7,
                                "balanced": True}},
            {"id": "c", "report_type": "chart_of_accounts",
             "coa_create_history": [{"created_count": 5}]},
        ],
        qbo_connections=[{"company_name": "Demo QBO", "realm_id": "R1"}],
        account_mapping_count=10,
    )
    by_key_b = {ln.key: ln for ln in summary_b.lines}
    assert summary_b.overall_status == final_report.STATUS_COMPLETED
    assert by_key_b["import"].status == final_report.STATUS_COMPLETED
    assert by_key_b["accounts"].status == final_report.STATUS_COMPLETED
    # Optional reports skipped, not blocked.
    assert by_key_b["client_trust"].status == final_report.STATUS_SKIPPED
    assert by_key_b["ending_balance"].status == final_report.STATUS_SKIPPED
    assert summary_b.journal_entries_count == 7
    assert summary_b.transactions_imported == 7
    assert summary_b.accounts_created_count == 5
    assert "Demo QBO" in (summary_b.qbo_company_name or "")

    # Missing-account blocker -> overall blocked.
    summary_c = final_report.build_reconciliation_summary(
        firm_name="C LLP",
        cutover={"cutover_date": "2026-04-01"},
        jobs=[
            {"id": "g", "report_type": "general_ledger",
             "unmapped_accounts": ["1101 Petty Cash"]},
        ],
        qbo_connections=[],
        account_mapping_count=2,
    )
    by_key_c = {ln.key: ln for ln in summary_c.lines}
    assert summary_c.overall_status == final_report.STATUS_BLOCKED
    assert by_key_c["import"].status == final_report.STATUS_BLOCKED
    assert summary_c.warnings, "blocked state must surface a warning"

    # Email validation.
    assert final_report.is_valid_email("a@b.co")
    assert not final_report.is_valid_email("no-at-sign")
    assert not final_report.is_valid_email("")
    assert not final_report.is_valid_email(None)

    # Report body shape.
    body = final_report.build_report_text(summary_b)
    assert "B LLP" in body
    assert "Demo QBO" in body
    assert "Reconciliation" in body
    assert "[COMPLETED] Transaction history imported" in body
    assert "Status: Migration demo complete." in body
    # Must not leak SMTP env material.
    for forbidden in ("SMTP_HOST", "SMTP_PASSWORD"):
        assert forbidden not in body
    print("R7 OK: reconciliation summary classifies pending/completed/blocked")


def r8_submit_when_smtp_configured_but_send_fails():
    client = appmod.app.test_client()
    _signup_and_login(client, "r8@example.test")
    user = appmod.db.get_user_by_email("r8@example.test")
    _complete_step1(user["firm_id"])
    _make_imported_gl_job(user, job_id="job_r8")

    # SMTP "configured" but transport returns False (e.g. auth failure,
    # network unreachable). We must NOT claim it was sent.
    with mock.patch.object(appmod.email_sender, "is_smtp_configured",
                           return_value=True), \
         mock.patch.object(appmod.email_sender, "send_email",
                           return_value=False):
        r = client.post(
            "/reconcile-balances/send-report",
            data={"email": "lawyer@firm.example"},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.status_code
        r = client.get("/reconcile-balances")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'flash error' in body, body[:2000]
    # Customer-friendly wording: no stack traces, no SMTP jargon.
    # Jinja HTML-escapes the apostrophe to &#39;, so match both forms.
    lower = body.lower()
    assert (
        "couldn't send" in lower
        or "couldn&#39;t send" in lower
        or "could not send" in lower
    ), "expected friendly 'couldn't send' wording in body"
    assert "SMTP_" not in body
    # Report preview must still render so the user can copy/retry.
    assert 'data-testid="step6-report-preview"' in body
    print("R8 OK: SMTP-failure path shows friendly error + keeps report visible")


def r9_mail_alias_env_vars_work():
    """The MAIL_*-style env names (a la Flask-Mail / Render Zoho guides)
    should be honored alongside the SMTP_*-style names.
    """
    # Save and clear all known names so we test the MAIL_* path in
    # isolation.
    saved = {}
    for var in (
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_USERNAME",
        "SMTP_PASSWORD", "SMTP_FROM", "SMTP_FROM_EMAIL",
        "MAIL_SERVER", "MAIL_PORT", "MAIL_USERNAME", "MAIL_PASSWORD",
        "MAIL_DEFAULT_SENDER",
    ):
        saved[var] = os.environ.pop(var, None)

    try:
        # With nothing set, not configured.
        assert appmod.email_sender.is_smtp_configured() is False

        # Set only MAIL_*-style vars (no SMTP_* equivalents).
        os.environ["MAIL_SERVER"] = "smtp.zoho.com"
        os.environ["MAIL_PORT"] = "587"
        os.environ["MAIL_USERNAME"] = "noreply@pclawmigrate.com"
        os.environ["MAIL_PASSWORD"] = "fake-app-password"
        os.environ["MAIL_DEFAULT_SENDER"] = "noreply@pclawmigrate.com"

        assert appmod.email_sender.is_smtp_configured() is True, (
            "MAIL_* env vars should fully configure SMTP"
        )
        status = appmod.email_sender.smtp_status()
        assert status["configured"] is True
        assert status["host"] == "smtp.zoho.com"
        assert status["port"] == "587"
        assert status["user_set"] is True
        assert status["from_set"] is True
        # smtp_status must never include the password.
        assert "password" not in {k.lower() for k in status.keys()}
    finally:
        for var, val in saved.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val
        for var in (
            "MAIL_SERVER", "MAIL_PORT", "MAIL_USERNAME",
            "MAIL_PASSWORD", "MAIL_DEFAULT_SENDER",
        ):
            os.environ.pop(var, None)
    print("R9 OK: MAIL_* env aliases configure SMTP (Render/Zoho friendly)")


def main():
    r1_step6_renders_when_step5_complete()
    r2_step6_blocked_before_import()
    r3_invalid_email_validation()
    r4_submit_when_smtp_unconfigured()
    r5_submit_when_smtp_configured_succeeds()
    r6_stage_reconcile_cta_points_at_step6_route()
    r7_reconciliation_summary_classification()
    r8_submit_when_smtp_configured_but_send_fails()
    r9_mail_alias_env_vars_work()
    print("\nALL STEP 6 RECONCILE-BALANCES SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
