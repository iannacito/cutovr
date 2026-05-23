"""Smoke tests for the Step 5 missing-account gating fix.

Background
----------
A customer hit Step 5 "Send to QuickBooks" and saw a raw error:

    These accounts are not in QuickBooks yet. Finish your Account List
    first, then try again. Accounts missing in your connected
    QuickBooks company (PCLaw Migrate Demo): 2100 Client Trust
    Liability.

The stepper showed Setup, Upload, Match, Review *complete* even though
the connected QuickBooks company was missing an account the
transaction history needed — Step 3 Match cannot be considered
complete in that state, and Step 5 must not be reachable.

This module exercises the gating + UX fixes:

  S1  customer_workflow.build_customer_stages(..., match_blocked=True)
      forces the Match stage back to current, marks Review/Import/
      Reconcile upcoming, and points the Match CTA at the
      create-missing-accounts page when match_blocked_job_id is set.
  S2  STAGE_IMPORT CTA label is "Send to QuickBooks" (not "Proceed to
      Step 5: Send to QuickBooks"). The user is already on Step 5 when
      they see it; the duplicate "Proceed to Step 5" is the bug from
      the screenshot.
  S3  /send-to-qbo redirects to /jobs/<id>/account-mapping with a
      single, lawyer-friendly flash that names the missing account by
      number + name, when the firm's GL job carries unmapped_accounts.
  S4  unmapped_account_guidance now emits the lawyer-friendly headline
      and a primary CTA "Create missing QuickBooks accounts" that
      deep-links to the Match accounts page.
  S5  Client Trust Liability (account number 2100) is created as
      Other Current Liability / TrustAccountsLiabilities and is
      auto-matched after creation; account number 2100 is preserved.
  S6  No duplicate is created when 2100 Client Trust Liability is
      already present in QBO (matched by AcctNum first).

Run from project root::

    python3 tests/smoke_step5_missing_account_gating.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="pclaw_uploads_s5_")
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_s5_")
os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-s5-gating")

import app as appmod  # noqa: E402
import coa_apply  # noqa: E402
import customer_workflow as cw  # noqa: E402
import cutover_workflow as cwf  # noqa: E402
import unmapped_account_guidance as ug  # noqa: E402


def _complete_checklist_through_step5(firm_id):
    """Force the checklist items to "ready for Step 5" so a sequential
    gating check on its own would mark Step 5 as the current stage.
    This is the exact state the original bug surfaced: every prior step
    technically rolls up to complete, so the stepper renders Step 5 as
    current even though the underlying QBO company isn't ready.
    """
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


def _signup_and_login(client, email, firm="S5 LLP"):
    pwd = "passw0rd!1234"
    client.post("/logout", follow_redirects=False)
    r = client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


# --- S1: match_blocked forces Match current, downstream upcoming -----------


def s1_match_blocked_forces_match_current_and_cta():
    # Build a synthetic checklist where every step is already complete
    # so without match_blocked the stepper would advance to STAGE_IMPORT
    # or beyond.
    items = [
        cwf.ChecklistItem(
            key=k, label=k, status=cwf.STATUS_COMPLETE,
            summary="", planned=False,
        )
        for k in (
            cwf.STEP_CUTOVER_SETUP,
            cwf.STEP_COA_UPLOAD,
            cwf.STEP_OPENING_TB,
            cwf.STEP_GL_UPLOAD,
            cwf.STEP_QBO_CONNECT,
            cwf.STEP_ACCOUNT_MAPPING,
            cwf.STEP_DRY_RUN,
            cwf.STEP_PROD_IMPORT,
            cwf.STEP_RECONCILIATION,
            cwf.STEP_ENDING_TB,
            cwf.STEP_TRUST_LISTING,
        )
    ]

    # No blocker — sequence advances normally (everything complete).
    stages_ok = cw.build_customer_stages(items, has_jobs=True)
    assert all(s.status == cw.STAGE_STATUS_COMPLETE for s in stages_ok), \
        [(s.key, s.status) for s in stages_ok]

    # With match_blocked=True the Match stage becomes current again and
    # everything after it gets pushed back to upcoming.
    stages = cw.build_customer_stages(
        items, has_jobs=True,
        match_blocked=True,
        match_blocked_job_id="job-s1",
    )
    by_key = {s.key: s for s in stages}
    assert by_key[cw.STAGE_SETUP].status == cw.STAGE_STATUS_COMPLETE
    assert by_key[cw.STAGE_UPLOAD].status == cw.STAGE_STATUS_COMPLETE
    assert by_key[cw.STAGE_MATCH].status == cw.STAGE_STATUS_CURRENT, \
        by_key[cw.STAGE_MATCH].status
    for k in (cw.STAGE_REVIEW, cw.STAGE_IMPORT, cw.STAGE_RECONCILE):
        assert by_key[k].status == cw.STAGE_STATUS_UPCOMING, \
            f"{k} should be upcoming, got {by_key[k].status}"

    # CTA on Match is now "Create missing QuickBooks accounts" and
    # carries the blocked job id in the URL fragment.
    match_stage = by_key[cw.STAGE_MATCH]
    assert match_stage.cta_label == "Create missing QuickBooks accounts", \
        match_stage.cta_label
    assert "job-s1" in match_stage.cta_url, match_stage.cta_url

    # current_stage() resolves to Match (not Import).
    cur = cw.current_stage(stages)
    assert cur and cur.key == cw.STAGE_MATCH, (cur and cur.key)
    print("S1 OK: match_blocked forces Match current + create-missing CTA")


# --- S2: STAGE_IMPORT CTA is "Send to QuickBooks", not "Proceed to Step 5" ---


def s2_step5_cta_label_is_send_not_proceed():
    # Build a minimal checklist where everything is done EXCEPT the
    # final reconcile so Import is the current stage.
    items = [
        cwf.ChecklistItem(
            key=k, label=k,
            status=(cwf.STATUS_NOT_STARTED if k in (
                cwf.STEP_PROD_IMPORT, cwf.STEP_RECONCILIATION, cwf.STEP_ENDING_TB,
            ) else cwf.STATUS_COMPLETE),
            summary="", planned=False,
        )
        for k in (
            cwf.STEP_CUTOVER_SETUP,
            cwf.STEP_COA_UPLOAD,
            cwf.STEP_OPENING_TB,
            cwf.STEP_GL_UPLOAD,
            cwf.STEP_QBO_CONNECT,
            cwf.STEP_ACCOUNT_MAPPING,
            cwf.STEP_DRY_RUN,
            cwf.STEP_PROD_IMPORT,
            cwf.STEP_RECONCILIATION,
            cwf.STEP_ENDING_TB,
            cwf.STEP_TRUST_LISTING,
        )
    ]
    stages = cw.build_customer_stages(items, has_jobs=True)
    by_key = {s.key: s for s in stages}
    imp = by_key[cw.STAGE_IMPORT]
    assert imp.status == cw.STAGE_STATUS_CURRENT, imp.status
    # The user is *on* Step 5 when they see this CTA — telling them to
    # "Proceed to Step 5" while already there was the screenshot bug.
    assert imp.cta_label == "Send to QuickBooks", imp.cta_label
    assert "Proceed to Step 5" not in imp.cta_label
    # CTA still points at the send-to-qbo page so the back-to-step-4
    # routing assertions in smoke_workflow_step_pages_cleanup keep working.
    assert "/send-to-qbo" in imp.cta_url, imp.cta_url
    print("S2 OK: Step 5 CTA is 'Send to QuickBooks', not 'Proceed to Step 5'")


# --- S3: /send-to-qbo redirects when missing accounts on a GL job ----------


def s3_send_to_qbo_redirects_when_missing_accounts():
    client = appmod.app.test_client()
    _signup_and_login(client, "s3@s5.example", firm="S3 LLP")
    user = appmod.db.get_user_by_email("s3@s5.example")
    firm_id = user["firm_id"]
    _complete_checklist_through_step5(firm_id)

    # Create a GL job + QBO connection and stamp unmapped_accounts onto
    # it — this is exactly the state the GL import route leaves behind
    # when the connected QBO company is missing an account.
    job_id = "job_s3_gl"
    appmod.db.upsert_job(
        job_id=job_id, firm_id=firm_id, user_id=user["id"],
        company="S3 LLP", source_file="gl.csv",
        encrypted_file="gl_s3.enc", file_sha256="0" * 64,
        status="Import blocked: unmapped accounts",
    )
    appmod.db.save_job_state(job_id, {
        "status": "Import blocked: unmapped accounts",
        "report_type": appmod.REPORT_GENERAL_LEDGER,
        "unmapped_accounts": ["2100 Client Trust Liability"],
        "unmapped_account_guidance": {
            "headline": "One QuickBooks account is missing. Create it from "
                        "your PCLaw account list before sending.",
            "accounts": [{
                "key": "2100 Client Trust Liability",
                "number": "2100", "name": "Client Trust Liability",
                "in_coa": False, "display": "2100 Client Trust Liability",
            }],
        },
    })
    appmod.qbo_connections[job_id] = {
        "realm_id": "R-S3",
        "access_token_enc": appmod.encrypt_token("fake"),
        "refresh_token_enc": appmod.encrypt_token("fake"),
        "company_name": "PCLaw Migrate Demo",
        "expires_at": "2999-01-01T00:00:00",
    }
    appmod.jobs.pop(job_id, None)

    # /send-to-qbo must redirect into the account-mapping screen for
    # the blocked job and flash the lawyer-friendly headline.
    r = client.get("/send-to-qbo", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308), r.status_code
    location = r.headers.get("Location", "")
    assert f"/jobs/{job_id}/account-mapping" in location, location

    # Follow the redirect once so the flash lands.
    page = client.get("/send-to-qbo", follow_redirects=True)
    body = page.get_data(as_text=True)
    assert "QuickBooks account is missing" in body or \
        "QuickBooks accounts are missing" in body, body[-2000:]
    assert "PCLaw account list" in body
    assert "2100 Client Trust Liability" in body
    assert "Finish your Account List first" not in body, \
        "old jargon copy must not leak through"
    print("S3 OK: /send-to-qbo redirects to Step 3 with lawyer-friendly flash")


# --- S4: classified guidance has the lawyer-friendly headline + CTA --------


def s4_unmapped_guidance_uses_lawyer_friendly_headline():
    g = ug.classify_unmapped_accounts(
        unmapped_keys=["2100 Client Trust Liability"],
        mapping_mode="number",
        coa_rows=[
            {"account_number": "2100", "account_name": "Client Trust Liability",
             "account_type": "Liability"},
        ],
        coa_create_history=[],
        job_id="job-s4",
        company_name="PCLaw Migrate Demo",
        environment="sandbox",
    )
    assert g.action == ug.ACTION_FINISH_COA, g.action
    assert g.primary_cta_label == "Create missing QuickBooks accounts"
    assert g.primary_cta_endpoint == "account_mapping"
    assert g.primary_cta_kwargs.get("job_id") == "job-s4"
    # Singular phrasing for one missing account.
    assert "One QuickBooks account is missing" in g.headline, g.headline
    assert "PCLaw account list" in g.headline
    # Plural phrasing kicks in for more than one missing account.
    g2 = ug.classify_unmapped_accounts(
        unmapped_keys=["2100 Client Trust Liability",
                       "5060 Professional Fees"],
        mapping_mode="number",
        coa_rows=[],
        coa_create_history=[],
        job_id="job-s4b",
        company_name="PCLaw Migrate Demo",
        environment="sandbox",
    )
    assert "2 QuickBooks accounts are missing" in g2.headline, g2.headline
    print("S4 OK: lawyer-friendly headline + create-missing CTA")


# --- S5: Client Trust Liability creates safely with type + AcctNum --------


class _FakeQBO:
    def __init__(self, accounts=None):
        self._accounts = list(accounts or [])
        self.created_payloads = []

    def get_accounts(self):
        return {"QueryResponse": {"Account": list(self._accounts)}}

    def find_account_by_acctnum(self, num):
        if not num:
            return None
        for a in self._accounts:
            if str(a.get("AcctNum") or "") == str(num):
                return a
        return None

    def find_account_by_name(self, name):
        if not name:
            return None
        target = name.strip().lower()
        for a in self._accounts:
            if str(a.get("Name") or "").strip().lower() == target:
                return a
        return None

    def create_account(self, payload):
        self.created_payloads.append(payload)
        new_id = str(2000 + len(self.created_payloads))
        new_account = {
            "Id": new_id, "Name": payload.get("Name"),
            "AcctNum": payload.get("AcctNum"),
            "AccountType": payload.get("AccountType"),
            "AccountSubType": payload.get("AccountSubType"),
            "Active": True,
        }
        self._accounts.append(new_account)
        return {"Account": new_account}


def s5_client_trust_liability_creates_and_automatches():
    client = appmod.app.test_client()
    _signup_and_login(client, "s5@s5.example", firm="S5 Trust LLP")
    user = appmod.db.get_user_by_email("s5@s5.example")
    firm_id = user["firm_id"]
    job_id = "job_s5_gl"
    appmod.db.upsert_job(
        job_id=job_id, firm_id=firm_id, user_id=user["id"],
        company="S5 LLP", source_file="gl.csv",
        encrypted_file="gl_s5.enc", file_sha256="0" * 64,
        status="uploaded",
    )
    appmod.db.save_job_state(job_id, {
        "status": "uploaded",
        "report_type": appmod.REPORT_GENERAL_LEDGER,
        "pclaw_accounts": [
            {"number": "2100", "name": "Client Trust Liability"},
        ],
    })
    appmod.qbo_connections[job_id] = {
        "realm_id": "R-S5",
        "access_token_enc": appmod.encrypt_token("fake"),
        "refresh_token_enc": appmod.encrypt_token("fake"),
        "company_name": "PCLaw Migrate Demo",
        "expires_at": "2999-01-01T00:00:00",
    }
    appmod.jobs.pop(job_id, None)

    fake = _FakeQBO([])  # QBO is empty — 2100 is not in QBO yet
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(fake, appmod.qbo_connections[job_id]),
    ):
        r = client.post(
            f"/jobs/{job_id}/account-mapping/create-missing",
            follow_redirects=False,
        )
    assert r.status_code in (301, 302, 303, 307, 308), r.status_code

    # The route created Client Trust Liability with the canonical safe
    # mapping (Other Current Liability / TrustAccountsLiabilities).
    assert len(fake.created_payloads) == 1, fake.created_payloads
    payload = fake.created_payloads[0]
    assert payload["Name"] == "Client Trust Liability"
    assert payload["AcctNum"] == "2100"
    assert payload["AccountType"] == "Other Current Liability"
    assert payload["AccountSubType"] == "TrustAccountsLiabilities"

    # Saved mapping was persisted so a subsequent render auto-matches.
    saved = appmod.db.list_account_mappings(firm_id, "R-S5")
    assert any(m["pclaw_account_number"] == "2100" for m in saved), saved
    print("S5 OK: Client Trust Liability created as Other Current Liability / "
          "TrustAccountsLiabilities, AcctNum 2100 preserved, mapping persisted")


# --- S6: existing 2100 in QBO is NOT recreated ----------------------------


def s6_existing_2100_is_not_duplicated():
    client = appmod.app.test_client()
    _signup_and_login(client, "s6@s5.example", firm="S6 Trust LLP")
    user = appmod.db.get_user_by_email("s6@s5.example")
    firm_id = user["firm_id"]
    job_id = "job_s6_gl"
    appmod.db.upsert_job(
        job_id=job_id, firm_id=firm_id, user_id=user["id"],
        company="S6 LLP", source_file="gl.csv",
        encrypted_file="gl_s6.enc", file_sha256="0" * 64,
        status="uploaded",
    )
    appmod.db.save_job_state(job_id, {
        "status": "uploaded",
        "report_type": appmod.REPORT_GENERAL_LEDGER,
        "pclaw_accounts": [
            {"number": "2100", "name": "Client Trust Liability"},
        ],
    })
    appmod.qbo_connections[job_id] = {
        "realm_id": "R-S6",
        "access_token_enc": appmod.encrypt_token("fake"),
        "refresh_token_enc": appmod.encrypt_token("fake"),
        "company_name": "PCLaw Migrate Demo",
        "expires_at": "2999-01-01T00:00:00",
    }
    appmod.jobs.pop(job_id, None)

    # 2100 already exists in QBO.
    fake = _FakeQBO([
        {"Id": "Q1", "Name": "Client Trust Liability", "AcctNum": "2100",
         "AccountType": "Other Current Liability",
         "AccountSubType": "TrustAccountsLiabilities"},
    ])
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(fake, appmod.qbo_connections[job_id]),
    ):
        client.post(
            f"/jobs/{job_id}/account-mapping/create-missing",
            follow_redirects=False,
        )
    assert fake.created_payloads == [], \
        f"existing 2100 must not be recreated, got {fake.created_payloads}"
    print("S6 OK: existing AcctNum 2100 is not duplicated by create-missing")


if __name__ == "__main__":
    s1_match_blocked_forces_match_current_and_cta()
    s2_step5_cta_label_is_send_not_proceed()
    s3_send_to_qbo_redirects_when_missing_accounts()
    s4_unmapped_guidance_uses_lawyer_friendly_headline()
    s5_client_trust_liability_creates_and_automatches()
    s6_existing_2100_is_not_duplicated()
    print("\nALL STEP 5 MISSING-ACCOUNT GATING SMOKE TESTS PASSED")
