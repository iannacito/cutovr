"""Smoke tests: Step 3 (Match accounts) shows the workflow stepper and a
top-of-page primary action.

Background — Cesar's QA 2026-06-03
----------------------------------
Two items on the Match-accounts (Step 3) screen:

  * Item 13: the migration step rail was missing on this page, so a lawyer
    matching accounts had no sense of where they were in the 6-step flow.
    Every other step page renders ``_workflow_stepper.html``; this one
    didn't.

  * Item 8: after matching a long chart of accounts, the only Save / Proceed
    buttons lived at the very bottom of the page, off-screen. The user asked
    for a primary action near the top.

Covers
------
  T1  GET /account-mapping renders the workflow stepper rail (the shared
      partial) with Step 3 (Match) marked current.
  T2  When not all accounts are matched, a top "Save matches" button is
      present and targets the main mapping form (form= attribute), so one
      click posts the matches even though the button sits above the form.
  T3  When every PCLaw account is already matched + saved, the top action
      surfaces the forward CTA to Step 4 instead of a redundant Save.

Run from project root::

    python3 tests/smoke_step3_stepper_and_top_cta.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="pclaw_uploads_s3stp_")
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_s3stp_")
os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-step3-stepper")

import app as appmod  # noqa: E402


def _signup_and_login(client, email, firm):
    pwd = "passw0rd!1234"
    client.post("/logout", follow_redirects=False)
    r = client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


def _make_job(client, email, firm, *, snapshot):
    _signup_and_login(client, email, firm)
    db = appmod.db
    user = db.get_user_by_email(email)
    job_id = f"job_s3stp_{firm.replace(' ', '_').lower()}"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company=firm, source_file="x.csv", encrypted_file="s3.enc",
        file_sha256="0" * 64, status="uploaded",
    )
    db.save_job_state(job_id, {"status": "uploaded",
                                "pclaw_accounts": snapshot})
    appmod.qbo_connections[job_id] = {
        "realm_id": f"R-{firm}",
        "access_token_enc": appmod.encrypt_token("fake-access"),
        "refresh_token_enc": appmod.encrypt_token("fake-refresh"),
        "company_name": firm, "legal_name": firm, "country": "US",
        "expires_at": "2999-01-01T00:00:00", "company_info_error": None,
    }
    appmod.jobs.pop(job_id, None)
    return job_id, user


class _FakeQBO:
    def __init__(self, accounts):
        self._accounts = list(accounts)

    def get_accounts(self):
        return {"QueryResponse": {"Account": list(self._accounts)}}

    def find_account_by_acctnum(self, num):
        for a in self._accounts:
            if str(a.get("AcctNum") or "") == str(num):
                return a
        return None

    def find_account_by_name(self, name):
        if not name:
            return None
        t = name.strip().lower()
        for a in self._accounts:
            if str(a.get("Name") or "").strip().lower() == t:
                return a
        return None


def _qbo_for(snapshot):
    """Build a QBO account list matching the snapshot 1:1 by AcctNum."""
    accounts = []
    for i, pa in enumerate(snapshot, start=1):
        accounts.append({
            "Id": f"A{i}", "Name": pa["name"], "AcctNum": pa["number"],
            "AccountType": "Expense",
        })
    return _FakeQBO(accounts)


def t1_stepper_rendered_with_match_current():
    client = appmod.app.test_client()
    snapshot = [{"number": "5000", "name": "Rent Expense"}]
    # QBO is missing the account so it stays unmatched (irrelevant to the
    # stepper, but keeps the page in its normal mapping state).
    qbo = _FakeQBO([])
    job_id, _ = _make_job(client, "s3t1@example.test", "S3T1 LLP",
                          snapshot=snapshot)
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping",
                       follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # The shared stepper partial renders its rail.
    assert 'class="workflow-stepper"' in body, \
        "Step 3 must render the workflow stepper rail"
    assert 'data-testid="workflow-step-link"' in body, \
        "stepper rail steps must render"
    # Match (Step 3) is the current step.
    assert 'aria-current="step"' in body, \
        "Step 3 must be marked as the current stepper step"
    print("T1 OK: account-mapping renders the workflow stepper with Step 3 current")


def t2_top_save_button_targets_main_form():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "5000", "name": "Rent Expense"},
        {"number": "5100", "name": "Office Supplies"},
    ]
    qbo = _FakeQBO([])  # nothing matches -> not all saved -> Save variant
    job_id, _ = _make_job(client, "s3t2@example.test", "S3T2 LLP",
                          snapshot=snapshot)
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping",
                       follow_redirects=False)
    body = r.get_data(as_text=True)
    assert 'data-testid="step3-top-actions"' in body, \
        "top action bar must render"
    assert 'data-testid="step3-top-save"' in body, \
        "top Save button must render when not all accounts are saved"
    # The button lives above the form but targets it via form= so a single
    # click still posts the matches.
    assert 'form="account-mapping-form"' in body, \
        "top Save button must target the main mapping form by id"
    assert 'id="account-mapping-form"' in body, \
        "main mapping form must carry the id the top button targets"
    print("T2 OK: top Save button targets the main mapping form")


def t3_top_proceed_when_all_matched():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "5000", "name": "Rent Expense"},
        {"number": "5100", "name": "Office Supplies"},
    ]
    # QBO has every account by AcctNum, and we pre-save the mappings so the
    # page sees a fully matched + saved state.
    qbo = _qbo_for(snapshot)
    job_id, user = _make_job(client, "s3t3@example.test", "S3T3 LLP",
                             snapshot=snapshot)
    realm = appmod.qbo_connections[job_id]["realm_id"]
    for i, pa in enumerate(snapshot, start=1):
        appmod.db.save_account_mapping(
            firm_id=user["firm_id"], realm_id=realm,
            pclaw_account_number=pa["number"],
            pclaw_account_name=pa["name"], qbo_account_id=f"A{i}",
        )
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping",
                       follow_redirects=False)
    body = r.get_data(as_text=True)
    assert 'data-testid="step3-top-actions"' in body
    assert 'data-testid="step3-top-proceed-to-step4"' in body, \
        "when fully matched, the top action must be the Step 4 forward CTA"
    assert 'data-testid="step3-top-save"' not in body, \
        "fully-matched page must not also show a top Save button"
    print("T3 OK: top action becomes the Step 4 CTA once every account is matched")


def main():
    t1_stepper_rendered_with_match_current()
    t2_top_save_button_targets_main_form()
    t3_top_proceed_when_all_matched()
    print("\nALL STEP-3 STEPPER + TOP-CTA SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
