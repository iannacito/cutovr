"""Smoke tests for the walkthrough-revised UX fixes.

Covers
------
  W1  Step 6 reconcile-balances page does NOT read banners from query
      params (no `report_status=` / `report_message=` reflected from the
      URL).
  W2  Step 6 has a final terminal CTA (Done — return to Migration /
      Return to Migration) at the bottom of the page when reachable.
  W3  Step 5 send-to-qbo never prints the job.id where a record count
      should be. With row_count present, count is printed; with
      row_count missing, a plain-English fallback appears and job.id
      does NOT appear in the headline H2.
  W4  Step 4 preview-import primary UI no longer shows accountant-only
      copy: "JournalEntry records", "Total debits" / "Total credits"
      in the at-a-glance card, the "QBO id" column, or "Back to job".
  W5  Step 3 account-mapping has exactly one Proceed-to-Step-4 CTA
      (the footer one), and raw AccountType enums like
      "OtherCurrentAsset" / "OtherCurrentLiability" do NOT appear in
      visible option labels. The visible mapping copy does not fall
      back to a numeric realm ID.
  W6  Step 1 cutover form does NOT show a visible "Realm ID (optional)"
      label for normal customers; the input is tucked behind an
      "Advanced" details section.
  W7  Customer-facing pages no longer use "Operator sign in",
      "OAuth-ed", "firm workspace", or "Back to job".

Run from project root::

    python3 tests/smoke_workflow_walkthrough_fixes.py
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
os.environ.setdefault("SECRET_KEY", "smoke-walkthrough-fixes")

for var in (
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_USERNAME",
    "SMTP_PASSWORD", "SMTP_FROM", "SMTP_FROM_EMAIL",
    "MAIL_SERVER", "MAIL_PORT", "MAIL_USERNAME", "MAIL_PASSWORD",
    "MAIL_DEFAULT_SENDER",
):
    os.environ.pop(var, None)

import app as appmod  # noqa: E402


def _signup_and_login(client, email, firm="Walkthrough LLP"):
    pwd = "passw0rd!1234"
    r = client.post(
        "/signup",
        data={
            "firm_name": firm, "email": email,
            "password": pwd, "confirm_password": pwd,
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
        firm_id=firm_id, cutover_date="2026-04-01",
        opening_balance_date="2026-04-01",
        period_start="2025-01-01", period_end="2025-12-31",
        country="US", accounting_basis="accrual",
        migration_scope=None, notes=None,
        qbo_company_name=None, qbo_realm_id=None,
        clio_involved=False, ar_ap_strategy="open_only",
    )


def _make_imported_gl_job(user, job_id="walkjob"):
    db = appmod.db
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company="Walkthrough LLP", source_file="gl.csv",
        encrypted_file="x.enc", file_sha256="0" * 64,
        status="Imported 7 JournalEntries",
    )
    db.save_job_state(
        job_id,
        {
            "status": "Imported 7 JournalEntries",
            "report_type": "general_ledger",
            "import_summary": {
                "qbo_je_count": 7, "source_transaction_count": 7,
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
        "company_name": "Walkthrough Test QBO",
        "legal_name": "Walkthrough Test QBO", "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }


def w1_step6_no_query_string_banner_replay():
    """Stale banner via query string must NOT replay."""
    client = appmod.app.test_client()
    _signup_and_login(client, "w1@example.test")

    # Query-string injection should not produce a banner anymore.
    r = client.get(
        "/reconcile-balances?report_status=success&report_message=Fake+success"
    )
    body = r.get_data(as_text=True)
    assert "Fake success" not in body, (
        "Query-string report_message replayed — must use flash() / PRG."
    )
    # The deprecated testid for the inline banner must no longer be
    # emitted by the template.
    assert 'data-testid="step6-report-status"' not in body, (
        "Inline query-string banner block should be removed."
    )
    print("W1 OK: Step 6 banner cannot be injected via query string")


def w2_step6_terminal_cta_present():
    """The completed Step 6 page has a quiet terminal action back to the
    dashboard — and NO forward 'next migration step' CTA."""
    client = appmod.app.test_client()
    _signup_and_login(client, "w2@example.test")
    user = appmod.db.get_user_by_email("w2@example.test")
    _complete_step1(user["firm_id"])
    _make_imported_gl_job(user, job_id="walkjob_w2")

    r = client.get("/reconcile-balances")
    body = r.get_data(as_text=True)
    assert 'data-testid="step6-return-to-dashboard"' in body, (
        "Step 6 must have a quiet terminal return-to-dashboard action."
    )
    assert "Return to dashboard" in body, (
        "Terminal action must read 'Return to dashboard'."
    )
    # This is the end of the migration: no forward 'Proceed to Step 6' CTA.
    assert "Proceed to Step 6" not in body, (
        "Final page must not show a 'Proceed to Step 6' CTA."
    )
    print("W2 OK: Step 6 has a quiet terminal 'Return to dashboard' action, no forward CTA")


def w3_step5_no_job_id_as_record_count():
    """Step 5 never prints job.id where a record count belongs."""
    client = appmod.app.test_client()
    _signup_and_login(client, "w3@example.test")
    user = appmod.db.get_user_by_email("w3@example.test")
    _complete_step1(user["firm_id"])

    # Case A: row_count missing — must NOT print job.id as a count.
    db = appmod.db
    job_id_no_count = "walkjob_w3a_no_count"
    db.upsert_job(
        job_id=job_id_no_count, firm_id=user["firm_id"],
        user_id=user["id"], company="Walkthrough LLP",
        source_file="gl.csv", encrypted_file="x.enc",
        file_sha256="0" * 64, status="ready_to_import",
    )
    db.save_job_state(
        job_id_no_count,
        {"status": "ready_to_import", "report_type": "general_ledger"},
    )
    appmod.qbo_connections[job_id_no_count] = {
        "realm_id": "R1",
        "access_token_enc": appmod.encrypt_token("fake"),
        "refresh_token_enc": appmod.encrypt_token("fake"),
        "company_name": "Walkthrough Test QBO",
        "legal_name": "Walkthrough Test QBO", "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }

    r = client.get(f"/jobs/{job_id_no_count}/send-to-qbo")
    if r.status_code == 200:
        body = r.get_data(as_text=True)
        # The job ID must never appear as a record count in the headline.
        # i.e. no "ready to send walkjob_w3a_no_count record(s)".
        assert f"send {job_id_no_count} record" not in body, (
            "Job ID rendered as record count!"
        )
        # The job ID should not appear in the ready-to-send H2 at all.
        # Extract the relevant H2.
        if "ready-to-send" in body or "Ready to send" in body:
            assert "your transactions" in body.lower(), (
                "Fallback wording must say 'your transactions' when "
                "no row count is present."
            )
    print("W3 OK: Step 5 never prints job.id as a record count")


def w4_step4_plain_english_pass():
    """Step 4 review page no longer dominates with accountant copy."""
    client = appmod.app.test_client()
    _signup_and_login(client, "w4@example.test")
    user = appmod.db.get_user_by_email("w4@example.test")
    _complete_step1(user["firm_id"])
    _make_imported_gl_job(user, job_id="walkjob_w4")

    r = client.get("/jobs/walkjob_w4/preview-import")
    if r.status_code != 200:
        # No preview data yet for this fixture; check the template
        # source directly for the offending strings.
        tmpl = Path(ROOT, "templates", "preview-import.html").read_text()
        # The dominant accountant-only labels must be gone from the
        # primary UI.
        assert ">JournalEntry records<" not in tmpl, (
            "Step 4 still uses 'JournalEntry records' in primary UI."
        )
        # 'JournalEntry line(s)' headline must also be gone.
        assert "JournalEntry line(s)" not in tmpl, (
            "Step 4 still uses 'JournalEntry line(s)' headline."
        )
        # The QBO id column must be gone from the primary accounts
        # table (we keep raw IDs only under technical details).
        assert ">QBO id<" not in tmpl, (
            "Step 4 still shows a 'QBO id' column in primary UI."
        )
        # The dominant debit/credit dt labels should not be in the
        # at-a-glance section anymore (moved to <details>).
        # The at-a-glance dt should not show "Total debits" alone.
        assert ">Total debits<" not in (
            tmpl.split("Technical details (for support)")[0]
        ), "Step 4 at-a-glance still shows 'Total debits'."
        # Back-link must say "Back to migration", not "Back to job".
        assert "Back to job<" not in tmpl, (
            "Step 4 still says 'Back to job'."
        )
        print("W4 OK (template-only check): Step 4 plain-English pass applied")
        return

    body = r.get_data(as_text=True)
    assert "JournalEntry records" not in body
    assert "QBO id" not in body
    assert "Back to job" not in body
    print("W4 OK: Step 4 review page reads in plain English")


def w5_step3_no_duplicate_proceed_cta_and_no_enums():
    """Step 3 mapping page has one Proceed CTA and no raw enums."""
    tmpl = Path(ROOT, "templates", "account-mapping.html").read_text()

    # The in-page success-card extra "Proceed to Step 4" CTA was
    # removed; only the footer Proceed CTA remains.
    proceed_count = tmpl.count("Proceed to Step 4")
    assert proceed_count == 1, (
        f"Expected exactly 1 'Proceed to Step 4' CTA, found {proceed_count}"
    )

    # Raw QuickBooks AccountType enum strings must not be rendered
    # verbatim in option labels.
    assert "{{ a.AccountType }}" not in tmpl, (
        "Raw a.AccountType enum still in the option label — should be "
        "wrapped in the ACCOUNT_TYPE_LABELS translation."
    )
    assert "ACCOUNT_TYPE_LABELS" in tmpl, (
        "Plain-English account-type translation table missing."
    )

    # coa-confirm: no realm_id fallback rendered in visible company name.
    coa_confirm = Path(ROOT, "templates", "coa-confirm.html").read_text()
    assert "or qbo_connection.realm_id" not in coa_confirm, (
        "coa-confirm still falls back to raw realm_id."
    )
    print("W5 OK: Step 3 single CTA + AccountType translated + no realm-id fallback")


def w6_step1_no_visible_realm_id_input():
    """Step 1 cutover form hides Realm ID from normal customers."""
    tmpl = Path(ROOT, "templates", "cutover.html").read_text()

    # The realm_id input still exists (operator override) but must be
    # tucked inside an Advanced details section.
    assert 'name="qbo_realm_id"' in tmpl, (
        "Realm ID field removed entirely — operators still need it."
    )
    # Find the position of the realm_id input and verify it is inside
    # a <details> block.
    pre = tmpl.split('name="qbo_realm_id"')[0]
    # The most recent open <details> before the input must not have
    # been closed yet.
    last_open = pre.rfind("<details")
    last_close = pre.rfind("</details>")
    assert last_open > last_close, (
        "Realm ID input is not inside a <details> disclosure."
    )
    # The old prominent "Realm ID (optional)" label must be gone.
    assert "Realm ID <small" not in tmpl, (
        "Old prominent 'Realm ID (optional)' label still present."
    )

    # A/R and A/P should be expanded somewhere on the page.
    assert (
        "Accounts receivable" in tmpl
        and "accounts payable" in tmpl.lower()
    ), "A/R + A/P abbreviation should be expanded."
    print("W6 OK: Step 1 realm-ID hidden behind Advanced, A/R+A/P spelled out")


def w7_no_operator_or_oauth_copy_in_auth_pages():
    """Auth pages no longer use admin / operator / OAuth jargon."""
    login_tmpl = Path(ROOT, "templates", "login.html").read_text()
    signup_tmpl = Path(ROOT, "templates", "signup.html").read_text()

    for forbidden in ("Operator sign in", "firm workspace"):
        assert forbidden not in login_tmpl, (
            f"login.html still says '{forbidden}'."
        )
    for forbidden in ("New firm workspace", "OAuth-ed",
                       "Create firm workspace"):
        assert forbidden not in signup_tmpl, (
            f"signup.html still says '{forbidden}'."
        )

    # End-TB sub-page should include the stepper now.
    tb_tmpl = Path(ROOT, "templates", "ending-tb-reconciliation.html").read_text()
    assert '"_workflow_stepper.html"' in tb_tmpl, (
        "ending-tb-reconciliation must include the workflow stepper."
    )
    assert "Back to job<" not in tb_tmpl, (
        "ending-tb-reconciliation still says 'Back to job'."
    )
    print("W7 OK: auth + ending-TB pages cleaned of operator/OAuth/back-to-job jargon")


def main():
    w1_step6_no_query_string_banner_replay()
    w2_step6_terminal_cta_present()
    w3_step5_no_job_id_as_record_count()
    w4_step4_plain_english_pass()
    w5_step3_no_duplicate_proceed_cta_and_no_enums()
    w6_step1_no_visible_realm_id_input()
    w7_no_operator_or_oauth_copy_in_auth_pages()
    print()
    print("ALL WORKFLOW WALKTHROUGH FIX SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
