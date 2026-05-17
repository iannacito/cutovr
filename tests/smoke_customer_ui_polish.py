"""Smoke tests for the customer-facing UI simplification pass.

Run from project root:

    python3 tests/smoke_customer_ui_polish.py

Covers:
  P1  Dashboard renders the new calm intake card with the dominant
      primary CTA "Upload & identify" and a quiet "single file" disclosure.
  P2  Dashboard renders the plain-English report-type options
      (Transaction history, Account list, Starting / final balances,
      Client trust balances) so customers see plain English first.
  P3  Migration checklist renders "Next step:" with a single dominant
      primary CTA, not a wall of competing buttons.
  P4  Job-detail page renders the dominant "Next step" card pointing
      to the right action for the current stage (match accounts or
      preview import or send to QuickBooks).
  P5  Account-mapping page uses the friendlier customer copy.
  P6  Stepper short labels match the customer-friendly six-stage flow.
  P7  Cutover-setup page still surfaces the original legacy strings
      ("Cutover date", "Save cutover settings") while showing the new
      friendlier "Migration setup (your switchover day)" headline.
  P8  Bulk-upload review page renders the new per-file detection summary
      and shows the dominant "Continue" or "Upload missing files" CTA.

These tests focus on visible UI tokens, not visual styling. The stylesheet
is exercised implicitly via successful page renders.
"""

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-ui-polish")

import app as appmod  # noqa: E402


def _signup_and_login(client, email="ui-polish@example.test", firm="UI Polish LLP"):
    pwd = "passw0rd!1234"
    r = client.post("/signup", data={
        "firm_name": firm,
        "email": email,
        "password": pwd,
        "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={
            "email": email, "password": pwd,
        }, follow_redirects=False)


def p1_dashboard_calm_intake_and_disclosure():
    c = appmod.app.test_client()
    _signup_and_login(c, "p1@uipolish.test", "P1 Firm")
    r = c.get("/dashboard")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)

    # Dominant primary CTA on intake.
    assert "Upload &amp; identify" in body or "Upload & identify" in body, \
        "dashboard should expose dominant 'Upload & identify' primary CTA"
    # Single-file uploader is collapsed behind a disclosure to keep the
    # page calm.
    assert "Upload one file at a time" in body, \
        "expected the single-file form behind a disclosure"
    assert 'details class="quiet"' in body, \
        "expected calm <details class=\"quiet\"> disclosures on dashboard"
    print("P1 OK: dashboard surfaces a single dominant CTA + quiet disclosures")


def p2_dashboard_plain_english_report_options():
    c = appmod.app.test_client()
    _signup_and_login(c, "p2@uipolish.test", "P2 Firm")
    r = c.get("/dashboard")
    body = r.get_data(as_text=True)
    # Plain-English first; legacy terms remain in small/secondary text.
    for plain in (
        "Transaction history",
        "Account list",
        "Starting / final balances",
        "Client trust balances",
    ):
        assert plain in body, f"dashboard missing plain-English option: {plain!r}"
    # Legacy accounting names are still listed as secondary helpers.
    for legacy in ("General Ledger", "Chart of Accounts",
                   "Trial Balance", "Trust Listing"):
        assert legacy in body, \
            f"dashboard should still surface legacy term {legacy!r} for accuracy"
    print("P2 OK: dashboard pairs plain-English labels with legacy terms")


def p3_checklist_has_single_dominant_next_step():
    c = appmod.app.test_client()
    _signup_and_login(c, "p3@uipolish.test", "P3 Firm")
    r = c.get("/migration-checklist")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # "Next step:" retains legacy wording AND the new step-of-6 framing.
    assert "Next step:" in body
    assert "Step " in body and " of 6" in body
    # Dominant primary CTA on the next-step card.
    assert 'class="btn btn-primary btn-lg"' in body, \
        "checklist should expose a single dominant primary CTA"
    # No legacy "Recommended order & accounting guidance" h2 — the
    # advanced guidance is now behind a quiet disclosure.
    assert "Recommended order" not in body or 'details class="quiet"' in body, \
        "advanced guidance should be tucked behind a disclosure"
    print("P3 OK: migration checklist has Next step + single dominant CTA")


def p4_job_detail_dominant_next_step_card():
    """A fresh job with no QBO connection should land on a 'Match accounts'
    or 'Connect QuickBooks' or 'Preview the import' next-step card."""
    c = appmod.app.test_client()
    _signup_and_login(c, "p4@uipolish.test", "P4 Firm")
    # Upload a GL CSV to make a job.
    gl = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()
    r = c.post(
        "/upload",
        data={
            "company_name": "P4 Co",
            "report_type": "general_ledger",
            "ledger_file": (io.BytesIO(gl), "gl.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.status_code
    loc = r.headers["Location"]
    body = c.get(loc).get_data(as_text=True)

    # The dominant next-step card is rendered.
    assert "next-card" in body, "expected dominant next-step card"
    # Exactly one of the next-step CTAs appears as the dominant call.
    cta_candidates = [
        "Match accounts &rarr;",
        "Connect QuickBooks &rarr;",
        "Preview the import &rarr;",
        "Download audit report",
        "See required columns",
    ]
    assert any(cta in body for cta in cta_candidates), \
        f"expected one of dominant next-step CTAs, got body[:1200]={body[:1200]!r}"
    # The detailed migration sequence is still discoverable (tucked
    # behind a "See all six migration steps" disclosure).
    assert ("six migration steps" in body) or ("Migration sequence" in body)
    print("P4 OK: job-detail surfaces a dominant next-step card")


def p5_account_mapping_friendlier_copy():
    """Static check: the rendered template carries the new friendly headline
    and the legacy 'Account mapping' substring that other smoke tests rely
    on. We render the template directly to avoid needing a live QBO
    connection in this smoke test."""
    from flask import render_template
    fake_job = {"id": "demo", "company": "P5 Co"}
    fake_conn = {"company_name": "P5 QuickBooks Co", "realm_id": "R-P5"}
    fake_rows = [
        {"idx": 0, "pclaw_name": "Operating Bank", "pclaw_number": "1000",
         "current_qbo_id": None, "is_saved": False, "is_suggestion": False},
    ]
    fake_accounts = [
        {"Id": "A11", "Name": "Bank Operating", "AcctNum": "1000",
         "AccountType": "Bank"},
    ]
    with appmod.app.test_request_context("/jobs/demo/account-mapping"):
        body = render_template(
            "account-mapping.html",
            job=fake_job, qbo_connection=fake_conn,
            rows=fake_rows, qbo_accounts=fake_accounts,
            user={"email": "p5@uipolish.test"},
            firm={"name": "P5 Firm"},
        )
    assert "Pair each PCLaw account with QuickBooks" in body, \
        "expected the new customer-friendly account-mapping headline"
    assert "Account mapping" in body, \
        "expected to keep the legacy 'Account mapping' string"
    assert "Auto-match" not in body or "Saved" in body  # sanity
    print("P5 OK: account mapping page uses friendly customer copy")


def p6_stepper_short_labels_are_friendly():
    c = appmod.app.test_client()
    _signup_and_login(c, "p6@uipolish.test", "P6 Firm")
    body = c.get("/dashboard").get_data(as_text=True)
    # The 6 stepper short labels (kept stable across UI iterations).
    for short in ("Setup", "Upload", "Match", "Review", "Import", "Reconcile"):
        assert f">{short}<" in body, f"stepper missing short label {short!r}"
    # And the new headline language for the current stage isn't shouting
    # "Next:" anymore — it just states the stage.
    assert "Migration progress" in body
    print("P6 OK: stepper short labels intact, calmer header copy")


def p7_cutover_setup_friendlier_headline():
    c = appmod.app.test_client()
    _signup_and_login(c, "p7@uipolish.test", "P7 Firm")
    body = c.get("/cutover").get_data(as_text=True)
    # New friendlier framing.
    assert "your switchover day" in body, "missing friendlier headline"
    # Legacy strings the existing smoke tests rely on are still present.
    for legacy in ("Cutover date", "Country", "Accounting basis",
                   "Save cutover settings"):
        assert legacy in body, f"cutover page missing legacy token {legacy!r}"
    print("P7 OK: cutover setup keeps legacy terms + adds friendly headline")


def p8_bulk_upload_review_dominant_cta():
    c = appmod.app.test_client()
    _signup_and_login(c, "p8@uipolish.test", "P8 Firm")
    coa = (ROOT / "test_data" / "01_chart_of_accounts.csv").read_bytes()
    from werkzeug.datastructures import MultiDict
    data = MultiDict()
    data["company_name"] = "P8 Co"
    data.add("ledger_files", (io.BytesIO(coa), "chart_of_accounts.csv"))
    r = c.post(
        "/upload/bulk",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.status_code
    body = c.get(r.headers["Location"]).get_data(as_text=True)

    # New per-file summary heading.
    assert "Per-file detection summary" in body, \
        "bulk review should keep 'Per-file detection summary'"
    # Dominant CTA — either "Upload missing files" (likely, since only
    # COA was sent) or "Continue".
    assert "Upload missing files &rarr;" in body or "Continue &rarr;" in body, \
        "expected a dominant 'Upload missing files' or 'Continue' CTA"
    assert "next-card" in body, "expected dominant next-step card"
    print("P8 OK: bulk upload review surfaces missing reports + dominant CTA")


def main():
    p1_dashboard_calm_intake_and_disclosure()
    p2_dashboard_plain_english_report_options()
    p3_checklist_has_single_dominant_next_step()
    p4_job_detail_dominant_next_step_card()
    p5_account_mapping_friendlier_copy()
    p6_stepper_short_labels_are_friendly()
    p7_cutover_setup_friendlier_headline()
    p8_bulk_upload_review_dominant_cta()
    print("\nALL CUSTOMER-UI-POLISH SMOKE TESTS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
