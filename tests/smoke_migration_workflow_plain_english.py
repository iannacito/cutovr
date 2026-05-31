"""Smoke tests for the plain-English migration workflow copy.

These guard against future regressions to a more verbose / technical
tone in customer-facing pages. Lawyers (the target users) should see
short, friendly copy and clear "what's next" guidance, NOT accounting
jargon or technical noise like job ids and JE counts in headlines.

Covered
-------
  E1  Step 1 (/cutover) opens with a short plain-English summary.
  E2  Step 2 dashboard intake card uses plain language (no
      'PCLaw CSV exports' verbosity in primary copy).
  E3  Step 2 bulk-upload-review page renders the friendly success
      card ("Amazing! Your reports are uploaded.") with a direct
      Proceed-to-Step-3 CTA when nothing is missing.
  E4  Step 3 (account-mapping) page uses the friendly "Amazing!
      We've matched your accounts." completion message and points
      directly at Step 4 — no jargon-heavy banner.
  E5  Step 4 (preview-import) page hero uses a plain-English headline
      ("Review what we'll send to QuickBooks").
  E6  Step 5 (send-to-qbo) page hero is short and points the user at
      the Send CTA.
  E7  Step 5 already-imported success banner is plain-English ("Amazing!
      Your migration is in QuickBooks") and proceeds to Step 6.
  E8  Step 6 (reconcile-balances) page hero is short and the "you're
      done" banner reads in plain English.

Run from project root::

    python3 tests/smoke_migration_workflow_plain_english.py
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
os.environ.setdefault("SECRET_KEY", "smoke-plain-english")

import app as appmod  # noqa: E402


def _signup_and_login(client, email, firm="Plain English LLP"):
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


def _make_gl_job_with_qbo(user, job_id="job_pe"):
    db = appmod.db
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company="Plain English LLP", source_file="gl.csv",
        encrypted_file="x.enc", file_sha256="0" * 64, status="uploaded",
    )
    db.save_job_state(job_id, {
        "status": "uploaded",
        "report_type": "general_ledger",
    })
    appmod.qbo_connections[job_id] = {
        "realm_id": "R1",
        "access_token_enc": appmod.encrypt_token("fake"),
        "refresh_token_enc": appmod.encrypt_token("fake"),
        "company_name": "Plain English QBO",
        "legal_name": "Plain English QBO",
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }


def e1_step1_cutover_intro_is_short():
    c = appmod.app.test_client()
    _signup_and_login(c, "e1@plain.test")
    body = c.get("/cutover").get_data(as_text=True)
    # Plain-English intro replaces the older "Tell us about the cutover"
    # / "a few dates and choices so we can plan the rest of the move"
    # verbiage.
    assert "moving from PCLaw to QuickBooks" in body, (
        "Step 1 intro should mention moving from PCLaw to QuickBooks in plain terms"
    )
    # Should not contain accounting term "Cutover" without the
    # plain-English helper.
    assert "switchover" in body, (
        "Step 1 should pair 'Cutover' with the plain-English 'switchover'"
    )
    print("E1 OK: Step 1 intro is short and plain-English")


def e2_step2_dashboard_copy_is_plain():
    c = appmod.app.test_client()
    _signup_and_login(c, "e2@plain.test")
    body = c.get("/dashboard").get_data(as_text=True)
    # The primary intake card uses plain language.
    assert "We&rsquo;ll figure out which is which" in body \
        or "We'll figure out which is which" in body, (
        "Step 2 intake card should use 'we'll figure out which is which'"
    )
    print("E2 OK: Step 2 intake copy is plain")


def e3_step2_bulk_review_friendly_success_card():
    """When every required report has been uploaded, the bulk-upload
    review page should render the friendly 'Amazing! Your reports are
    uploaded.' card and point directly at Step 3."""
    c = appmod.app.test_client()
    _signup_and_login(c, "e3@plain.test")
    user = appmod.db.get_user_by_email("e3@plain.test")
    _complete_step1(user["firm_id"])

    # Render the template directly with a stub context that simulates
    # the "all required reports uploaded" state.
    env = appmod.app.jinja_env
    template = env.get_template("bulk-upload-review.html")
    bulk = {"id": "bulk_e3", "company": "Plain Co"}
    summary = {"file_count": 3, "categorized": 3, "needs_review": 0}
    results = []
    with appmod.app.test_request_context("/upload/bulk/bulk_e3"):
        body = template.render(
            bulk=bulk, summary=summary, results=results,
            missing_required_labels=[],
            report_types=[], report_label_map={},
            status_labels={},
            csrf_token=lambda: "test-csrf",
        )
    assert 'data-testid="step2-ready-card"' in body, (
        "expected the Step 2 ready-for-Step-3 card"
    )
    assert "Amazing! Your reports are uploaded" in body, (
        "expected the plain-English completion message"
    )
    assert 'data-testid="step2-proceed-to-step3"' in body
    assert "/match-accounts" in body
    print("E3 OK: bulk-upload review renders friendly 'Amazing!' card "
          "with direct Proceed-to-Step-3 CTA")


def e4_step3_complete_card_says_amazing():
    """The Step 3 (account-mapping) page's complete-state card must
    use the friendly 'Amazing! We've matched your accounts.' headline."""
    env = appmod.app.jinja_env
    template = env.get_template("account-mapping.html")
    job = {"id": "job_e4", "company": "Plain Co"}
    qbo_connection = {"realm_id": "R1", "company_name": "Plain QBO"}

    with appmod.app.test_request_context("/jobs/job_e4/account-mapping"):
        body = template.render(
            job=job, qbo_connection=qbo_connection,
            load_error=None, create_missing_offer=None,
            rows=[], save_history=[],
            csrf_token=lambda: "test-csrf",
            mapping_summary={
                "matched": 3, "matched_saved": 3, "unmatched": 0, "total": 3,
            },
        )
    assert 'data-testid="step3-complete-card"' in body
    assert "Amazing! We" in body and "matched your accounts" in body, (
        "expected the plain-English 'Amazing! We've matched your "
        "accounts' completion message"
    )
    # The verbose "Every PCLaw account is paired with..." copy is gone.
    assert "Every PCLaw account is paired" not in body, (
        "old verbose completion copy should be replaced"
    )
    print("E4 OK: Step 3 complete card uses friendly 'Amazing!' headline")


def e5_step4_preview_hero_is_plain():
    """The Step 4 (preview-import) page hero must use the plain-English
    'Review what we'll send to QuickBooks.' headline instead of the
    older 'What would be posted to QuickBooks' phrasing."""
    c = appmod.app.test_client()
    _signup_and_login(c, "e5@plain.test")
    user = appmod.db.get_user_by_email("e5@plain.test")
    _complete_step1(user["firm_id"])
    _make_gl_job_with_qbo(user, job_id="job_e5")

    # Hit the preview-import page through the route; if the preview
    # build fails (it depends on a parsed GL), we just inspect the
    # rendered template's hero.
    r = c.get("/jobs/job_e5/preview-import", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "Review what we" in body and "send to QuickBooks" in body, (
        "Step 4 hero should use plain-English Review headline"
    )
    print("E5 OK: Step 4 hero uses plain-English 'Review what we'll send'")


def e6_step5_send_to_qbo_hero_is_short():
    c = appmod.app.test_client()
    _signup_and_login(c, "e6@plain.test")
    user = appmod.db.get_user_by_email("e6@plain.test")
    _complete_step1(user["firm_id"])
    _make_gl_job_with_qbo(user, job_id="job_e6")

    body = c.get("/send-to-qbo").get_data(as_text=True)
    # The hero headline is now the short, direct "Send to QuickBooks."
    # instead of the prior "Send the prepared entries to QuickBooks".
    assert ">Send to QuickBooks.</h1>" in body, (
        "Step 5 hero should be the short 'Send to QuickBooks.' headline"
    )
    # Plain-English clarification still present.
    normalized = " ".join(body.split())
    assert "do not need to enter anything manually in QuickBooks" in normalized
    print("E6 OK: Step 5 hero is short and direct")


def e7_step5_already_imported_says_amazing():
    c = appmod.app.test_client()
    _signup_and_login(c, "e7@plain.test")
    user = appmod.db.get_user_by_email("e7@plain.test")
    _complete_step1(user["firm_id"])
    # Set up a GL job that already has an import_summary so the
    # send-to-qbo page renders the already-imported card.
    db = appmod.db
    job_id = "job_e7"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company="Plain English LLP", source_file="gl.csv",
        encrypted_file="x.enc", file_sha256="0" * 64,
        status="Imported 7 JEs",
    )
    db.save_job_state(job_id, {
        "status": "Imported 7 JEs",
        "report_type": "general_ledger",
        "import_summary": {
            "qbo_je_count": 7, "source_transaction_count": 7,
        },
    })
    appmod.qbo_connections[job_id] = {
        "realm_id": "R1",
        "access_token_enc": appmod.encrypt_token("fake"),
        "refresh_token_enc": appmod.encrypt_token("fake"),
        "company_name": "Plain English QBO",
        "legal_name": "Plain English QBO",
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }

    body = c.get("/send-to-qbo").get_data(as_text=True)
    assert 'data-testid="already-imported"' in body, (
        "expected the already-imported success card"
    )
    assert "Amazing! Your migration is in QuickBooks" in body, (
        "Step 5 should show plain-English success banner once imported"
    )
    # The CTA points at Step 6: Final balance check.
    assert 'data-testid="step5-next-cta"' in body
    assert "/reconcile-balances" in body
    print("E7 OK: Step 5 already-imported card shows 'Amazing!' + "
          "Proceed-to-Step-6 CTA")


def e8_step6_reconcile_banner_is_plain():
    c = appmod.app.test_client()
    _signup_and_login(c, "e8@plain.test")
    user = appmod.db.get_user_by_email("e8@plain.test")
    _complete_step1(user["firm_id"])

    # Render the template directly so we can supply an is_complete summary.
    env = appmod.app.jinja_env
    template = env.get_template("reconcile-balances.html")
    fake_summary = type("S", (), {})()
    fake_summary.is_complete = True
    fake_summary.firm_name = "Plain English LLP"
    fake_summary.cutover_date = "2026-04-01"
    fake_summary.qbo_company_name = "Plain English QBO"
    fake_summary.qbo_realm_id = "R1"
    fake_summary.reports_uploaded = []
    fake_summary.accounts_matched_count = 3
    fake_summary.accounts_created_count = 1
    fake_summary.journal_entries_count = 7
    fake_summary.transactions_imported = 7
    fake_summary.lines = []
    fake_summary.warnings = []

    with appmod.app.test_request_context("/reconcile-balances"):
        body = template.render(
            blocked=False, blocked_reason="",
            summary=fake_summary,
            report_text="",
            report_status=None, report_message=None, report_email="",
            workflow_stages=[], workflow_current=None,
            workflow_progress=100, workflow_completed=6,
            workflow_terms={},
            csrf_token=lambda: "test-csrf",
        )
    assert 'data-testid="step6-complete-banner"' in body, (
        "expected the Step 6 complete banner"
    )
    # The completed page reads as a success / end-of-migration page.
    assert 'data-testid="step6-success-hero"' in body, (
        "expected the Step 6 success hero on a completed migration"
    )
    assert "Your migration is complete" in body, (
        "Step 6 success hero should state the migration is complete"
    )
    assert "All done" in body and "everything matches" in body, (
        "Step 6 complete banner should read in plain English"
    )
    assert "sent to QuickBooks" in body, (
        "Step 6 success copy should confirm data was sent to QuickBooks"
    )
    # This is the end of the migration — no forward step CTA.
    assert "Proceed to Step 6" not in body, (
        "completed Step 6 page must not show a 'Proceed to Step 6' CTA"
    )
    # Older technical phrase should be gone.
    assert "Migration demo complete" not in body, (
        "old technical 'Migration demo complete' banner copy should be removed"
    )
    print("E8 OK: Step 6 complete banner is plain English + reads as success")


def main():
    e1_step1_cutover_intro_is_short()
    e2_step2_dashboard_copy_is_plain()
    e3_step2_bulk_review_friendly_success_card()
    e4_step3_complete_card_says_amazing()
    e5_step4_preview_hero_is_plain()
    e6_step5_send_to_qbo_hero_is_short()
    e7_step5_already_imported_says_amazing()
    e8_step6_reconcile_banner_is_plain()
    print("\nALL MIGRATION-WORKFLOW PLAIN-ENGLISH SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
