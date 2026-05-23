"""Smoke tests for workflow progress + per-step guidance + trust-liability auto-match.

Background
----------
After the demo-reset fixes, walking through the customer-facing migration
flow surfaced four UX problems:

  1. The progress stepper didn't show clear progression after Step 3
     (Match accounts) was saved.
  2. The match-accounts page had no completion / next-step card after
     all accounts were saved, leaving the user without a clear next CTA.
  3. Steps 4 (Review), 5 (Import), 6 (Reconcile) lacked stage-aware
     "Step N complete: Next, ..." guidance copy in the migration
     checklist.
  4. PCLaw "Client Trust Liability" demo account didn't auto-match
     against a QBO sandbox account named "Trust Liability" because the
     normalized-name lookup only matched exact strings.

This module verifies the fixes for all four.

Covered
-------
  W1  ``/jobs/<id>/account-mapping`` shows the Step 3 complete card with
      the "Next: Review import" CTA once every PCLaw account has a
      saved mapping.
  W2  ``/migration-checklist`` next-step card shows the Step 3 -> Step 4
      "Review what will be sent to QuickBooks" guidance when the
      customer-workflow current stage is "review".
  W3  Same card shows the Step 4 -> Step 5 "Send to QuickBooks"
      guidance when the current stage is "import".
  W4  Same card shows the Step 5 -> Step 6 "Reconcile balances"
      guidance when the current stage is "reconcile"; once everything
      is done, the page renders the "Step 6 complete" migration-
      complete card instead.
  W5  ``_build_account_mapping_rows`` matches PCLaw "Client Trust
      Liability" to QBO "Trust Liability" via the deterministic alias
      table, even though the normalized names differ.
  W6  ``map_pclaw_account_to_qbo_type`` does not block a
      "Client Trust Liability" row even when only the account name is
      available — the create-missing flow can proceed for the demo
      dataset.

Run from project root:

    python3 tests/smoke_workflow_progress_and_guidance.py
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
os.environ.setdefault("SECRET_KEY", "smoke-workflow-progress-and-guidance")

import app as appmod  # noqa: E402
import customer_workflow  # noqa: E402
import cutover_workflow  # noqa: E402
from coa_apply import map_pclaw_account_to_qbo_type  # noqa: E402


def _signup_and_login(client, email, firm):
    pwd = "passw0rd!1234"
    r = client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


def _make_gl_job(client, email, firm):
    """Sign up a firm, create a GL job, and persist a PCLaw account
    snapshot via save_job_state so /account-mapping renders without
    hitting the missing_source path."""
    _signup_and_login(client, email, firm)
    db = appmod.db
    user = db.get_user_by_email(email)
    job_id = f"job_{firm.replace(' ', '_').lower()}"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company=firm, source_file="demo.csv",
        encrypted_file="never_used.enc", file_sha256="0" * 64,
        status="uploaded",
    )
    db.save_job_state(job_id, {
        "status": "uploaded",
        "report_type": "general_ledger",
        "pclaw_accounts": [
            {"number": "2100", "name": "Client Trust Liability"},
            {"number": "1000", "name": "Operating Bank"},
        ],
    })
    appmod.qbo_connections[job_id] = {
        "realm_id": f"R-{firm}",
        "access_token_enc": appmod.encrypt_token("fake-access"),
        "refresh_token_enc": appmod.encrypt_token("fake-refresh"),
        "company_name": "Test QBO Co",
        "legal_name": "Test QBO Co",
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }
    appmod.jobs.pop(job_id, None)
    return job_id, user


class _FakeQBOWithTrust:
    """QBO accounts response with both a Trust Liability and an
    Operating Account so alias auto-match has targets."""

    def get_accounts(self):
        return {"QueryResponse": {"Account": [
            {"Id": "10", "Name": "Operating Account", "AcctNum": "",
             "AccountType": "Bank", "AccountSubType": "Checking"},
            {"Id": "20", "Name": "Trust Liability", "AcctNum": "",
             "AccountType": "Other Current Liability",
             "AccountSubType": "TrustAccounts-Liabilities"},
        ]}}


def w1_step3_complete_card_renders_when_all_saved():
    client = appmod.app.test_client()
    job_id, user = _make_gl_job(client, "w1@example.test", "W1 LLP")
    db = appmod.db
    realm_id = f"R-W1 LLP"
    db.save_account_mapping(
        firm_id=user["firm_id"], realm_id=realm_id,
        pclaw_account_number="1000", pclaw_account_name="Operating Bank",
        qbo_account_id="10", qbo_account_name="Operating Account",
        qbo_account_type="Bank",
    )
    db.save_account_mapping(
        firm_id=user["firm_id"], realm_id=realm_id,
        pclaw_account_number="2100", pclaw_account_name="Client Trust Liability",
        qbo_account_id="20", qbo_account_name="Trust Liability",
        qbo_account_type="Other Current Liability",
    )
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(_FakeQBOWithTrust(), appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping",
                       follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="step3-complete-card"' in body, \
        "expected Step 3 complete card when all accounts saved"
    assert "Step 3 complete: Accounts matched" in body
    assert 'data-testid="step3-next-cta"' in body
    assert "Next: Review import" in body
    print("W1 OK: Step 3 complete card + Next: Review import CTA rendered")


def w2_review_stage_guidance_renders():
    """When the customer-workflow projection has 'review' as the
    current stage, the migration-checklist next-step card must show
    the Step 3 complete + Next CTA copy."""
    # Build checklist state where stages 1-3 are done.
    cutover = {
        "cutover_date": "2026-04-01", "country": "US",
        "accounting_basis": "accrual",
    }
    jobs = [
        {"id": "j", "report_type": "general_ledger", "status": "uploaded"},
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
    assert current is not None
    assert current.key == customer_workflow.STAGE_REVIEW, \
        f"expected review stage current, got {current.key}"

    # Render the template via Flask's test environment using a stub for
    # _build_firm_checklist so we don't need to wire the whole DB.
    client = appmod.app.test_client()
    _signup_and_login(client, "w2@example.test", "W2 LLP")
    with mock.patch.object(
        appmod, "_build_firm_checklist",
        return_value=(cutover, items, cutover_workflow.next_recommended_step(items)),
    ), mock.patch.object(
        appmod.demo_mode, "filter_active_jobs", return_value=[],
    ):
        r = client.get("/migration-checklist", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-stage-key="review"' in body, \
        "expected next-step card to advertise the review stage"
    assert "Step 3 complete: Accounts matched" in body
    assert "review what will be sent to QuickBooks" in body
    assert "Next: Review import" in body
    print("W2 OK: Review-stage guidance shows Step 3 complete + Next CTA")


def w3_import_stage_guidance_renders():
    """At the import stage, next-step card shows Step 4 -> Step 5 guidance."""
    cutover = {
        "cutover_date": "2026-04-01", "country": "US",
        "accounting_basis": "accrual",
    }
    jobs = [
        {"id": "j", "report_type": "general_ledger", "status": "uploaded",
         "preflight": {"ok": True}},
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
    assert current is not None
    assert current.key == customer_workflow.STAGE_IMPORT, \
        f"expected import current, got {current.key}"

    client = appmod.app.test_client()
    _signup_and_login(client, "w3@example.test", "W3 LLP")
    with mock.patch.object(
        appmod, "_build_firm_checklist",
        return_value=(cutover, items, cutover_workflow.next_recommended_step(items)),
    ), mock.patch.object(
        appmod.demo_mode, "filter_active_jobs", return_value=[],
    ):
        r = client.get("/migration-checklist", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-stage-key="import"' in body
    assert "Step 4 complete" in body
    assert "Send to QuickBooks" in body or "send to QuickBooks" in body
    print("W3 OK: Import-stage guidance shows Step 4 complete + Send CTA")


def w4_reconcile_and_complete_states():
    cutover = {
        "cutover_date": "2026-04-01", "country": "US",
        "accounting_basis": "accrual",
    }
    # Reconcile in progress.
    jobs_recon = [
        {"id": "j", "report_type": "general_ledger", "status": "imported",
         "preflight": {"ok": True}},
        {"id": "c", "report_type": "chart_of_accounts",
         "coa_create_history": [{"created_count": 5}]},
        {"id": "tb", "report_type": "trial_balance",
         "opening_balance_history": [{"qbo_je_id": "JE-1"}]},
    ]
    items_recon = cutover_workflow.build_checklist(
        cutover, jobs_recon, has_qbo_connection=True, account_mapping_count=5,
    )
    stages_recon = customer_workflow.build_customer_stages(
        items_recon, has_jobs=True,
    )
    current_recon = customer_workflow.current_stage(stages_recon)
    assert current_recon is not None
    assert current_recon.key == customer_workflow.STAGE_RECONCILE, \
        f"expected reconcile current, got {current_recon.key}"

    client = appmod.app.test_client()
    _signup_and_login(client, "w4@example.test", "W4 LLP")
    with mock.patch.object(
        appmod, "_build_firm_checklist",
        return_value=(cutover, items_recon,
                      cutover_workflow.next_recommended_step(items_recon)),
    ), mock.patch.object(
        appmod.demo_mode, "filter_active_jobs", return_value=[],
    ):
        r = client.get("/migration-checklist", follow_redirects=False)
    body = r.get_data(as_text=True)
    assert 'data-stage-key="reconcile"' in body
    assert "Step 5 complete" in body
    assert "Reconcile balances" in body or "reconcile balances" in body

    # Everything done.
    jobs_done = [
        {"id": "j", "report_type": "general_ledger", "status": "imported",
         "preflight": {"ok": True},
         "verification": {"status": "ok"}},
        {"id": "c", "report_type": "chart_of_accounts",
         "coa_create_history": [{"created_count": 5}]},
        {"id": "tb", "report_type": "trial_balance",
         "opening_balance_history": [{"qbo_je_id": "JE-1"}],
         "ending_tb_reconciliation": {"ok": True}},
    ]
    items_done = cutover_workflow.build_checklist(
        cutover, jobs_done, has_qbo_connection=True, account_mapping_count=5,
    )
    stages_done = customer_workflow.build_customer_stages(
        items_done, has_jobs=True,
    )
    assert customer_workflow.current_stage(stages_done) is None, \
        "all stages should be complete when everything is done"
    assert all(s.status == "complete" for s in stages_done)

    with mock.patch.object(
        appmod, "_build_firm_checklist",
        return_value=(cutover, items_done,
                      cutover_workflow.next_recommended_step(items_done)),
    ), mock.patch.object(
        appmod.demo_mode, "filter_active_jobs", return_value=[],
    ):
        r2 = client.get("/migration-checklist", follow_redirects=False)
    body2 = r2.get_data(as_text=True)
    assert 'data-testid="migration-complete-card"' in body2
    assert "Step 6 complete" in body2
    print("W4 OK: Reconcile stage guidance + Step 6 complete card rendered")


def w5_client_trust_liability_alias_automatch():
    qbo_accounts = [
        {"Id": "20", "Name": "Trust Liability",
         "AccountType": "Other Current Liability"},
    ]
    pclaw = [{"number": "2100", "name": "Client Trust Liability"}]
    rows, summary = appmod._build_account_mapping_rows(
        pclaw_accounts=pclaw, qbo_accounts=qbo_accounts, saved_by_key={},
    )
    assert summary["unmatched"] == 0, f"expected zero unmatched, got {summary}"
    assert rows[0]["match_basis"] == "Alias", \
        f"expected alias match, got {rows[0]['match_basis']}"
    assert rows[0]["current_qbo_id"] == "20"

    # Reverse direction: QBO uses long form, PCLaw uses short.
    qbo_accounts2 = [
        {"Id": "21", "Name": "Trust Accounts - Liabilities",
         "AccountType": "Other Current Liability"},
    ]
    pclaw2 = [{"number": "2100", "name": "Trust Liability"}]
    rows2, summary2 = appmod._build_account_mapping_rows(
        pclaw_accounts=pclaw2, qbo_accounts=qbo_accounts2, saved_by_key={},
    )
    assert summary2["unmatched"] == 0, \
        f"expected reverse alias to match, got {summary2}"
    assert rows2[0]["current_qbo_id"] == "21"

    # Operating Bank <-> Operating Account.
    qbo_accounts3 = [
        {"Id": "30", "Name": "Operating Account", "AccountType": "Bank"},
    ]
    pclaw3 = [{"number": "1000", "name": "Operating Bank"}]
    rows3, summary3 = appmod._build_account_mapping_rows(
        pclaw_accounts=pclaw3, qbo_accounts=qbo_accounts3, saved_by_key={},
    )
    assert summary3["unmatched"] == 0
    assert rows3[0]["current_qbo_id"] == "30"

    # Sanity: a completely unrelated PCLaw account should NOT be
    # falsely matched by the alias table.
    qbo_accounts4 = [
        {"Id": "40", "Name": "Office Supplies", "AccountType": "Expense"},
    ]
    pclaw4 = [{"number": "5100", "name": "Client Disbursements"}]
    rows4, summary4 = appmod._build_account_mapping_rows(
        pclaw_accounts=pclaw4, qbo_accounts=qbo_accounts4, saved_by_key={},
    )
    assert summary4["unmatched"] == 1, \
        f"unrelated names should not falsely match, got {summary4}"
    print("W5 OK: Trust-liability + Operating-bank aliases match without false positives")


def w6_trust_liability_create_missing_is_unblocked():
    row = {"account_name": "Client Trust Liability"}
    result = map_pclaw_account_to_qbo_type(row)
    assert result["decision"] != "blocked", \
        f"trust liability should not be blocked, got: {result}"
    assert result["account_type"] == "Other Current Liability"
    assert result["detail_type"] == "TrustAccounts-Liabilities"
    assert any("trust" in w.lower() for w in result["warnings"])

    # Same name with the demo COA's qbo_suggested_detail_type column.
    row2 = {
        "account_name": "Client Trust Liability",
        "account_type": "Liability",
        "detail_type": "Trust Accounts - Liabilities",
    }
    result2 = map_pclaw_account_to_qbo_type(row2)
    assert result2["decision"] != "blocked"
    assert result2["account_type"] == "Other Current Liability"
    assert result2["detail_type"] == "TrustAccounts-Liabilities"
    print("W6 OK: Client Trust Liability resolves to "
          "Other Current Liability / TrustAccounts-Liabilities (warning, not blocked)")


def main():
    w1_step3_complete_card_renders_when_all_saved()
    w2_review_stage_guidance_renders()
    w3_import_stage_guidance_renders()
    w4_reconcile_and_complete_states()
    w5_client_trust_liability_alias_automatch()
    w6_trust_liability_create_missing_is_unblocked()
    print("\nALL WORKFLOW PROGRESS + GUIDANCE SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
