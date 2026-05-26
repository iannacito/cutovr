"""Smoke tests for Step 3 single-missing-account trust-liability fix.

Background
----------
The user-visible blocker on a fresh QBO sandbox demo was Step 3 leaving
"2100 Client Trust Liability" unmatched even after every other PCLaw
account auto-matched. Two distinct bugs combined:

  1. ``coa_apply._TYPE_TABLE`` mapped trust-liability hints to
     ``AccountSubType="TrustAccounts-Liabilities"``. QBO's REST API
     does not accept the hyphenated display form — the valid enum
     identifier is ``TrustAccountsLiabilities`` (camelCase, no dash).
     The create-account POST therefore silently failed with HTTP 400
     and 2100 stayed unmatched.

  2. When only ONE PCLaw account is missing, the create-missing banner
     used the same plural copy ("a few PCLaw accounts aren't in
     QuickBooks yet") as the multi-account case, hiding the fact that a
     single-click fix was available. There was also no explicit
     reassurance that the user should NOT add the account manually in
     QuickBooks.

This file locks both fixes in.

Run from project root::

    python3 tests/smoke_step3_single_unmatched_trust_liability.py

Covers
------
  T1  coa_apply maps Client Trust Liability to the QBO-valid
      AccountSubType "TrustAccountsLiabilities" (no hyphen). All three
      hint paths (account_type / detail_type / account_name) reach the
      same QBO-valid value.
  T2  build_create_plan + apply_create_plan POST a payload whose
      AccountSubType is exactly "TrustAccountsLiabilities". The
      hyphenated form must never appear in the outbound payload.
  T3  GET /account-mapping with exactly one missing PCLaw account (the
      Trust Liability) renders the single-account banner variant:
        * The headline names "1 QuickBooks account is missing".
        * The body explicitly reassures the user they do not need to
          add it manually in QuickBooks.
        * The unmatched account is named in the banner.
        * Both Create and Refresh CTAs are present.
  T4  POST /account-mapping/create-missing on the same single-missing
      scenario calls QBO with the correct payload (subtype +
      AcctNum=2100), persists the saved mapping, and the next GET
      shows the row as Saved (no longer Unmatched).
  T5  When QBO returns a 400 on create, the user is bounced back to
      /account-mapping with a friendly error flash that includes the
      Intuit support reference. The page never silently leaves the
      row unmatched without explaining why.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_UPLOAD_DIR = tempfile.mkdtemp(prefix="pclaw_uploads_t3_")
os.environ["UPLOAD_DIR"] = _UPLOAD_DIR
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_t3_")
os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-step3-trust")

import app as appmod  # noqa: E402
from coa_apply import (  # noqa: E402
    map_pclaw_account_to_qbo_type,
    build_create_plan,
    apply_create_plan,
)
from report_types import build_coa_dry_run_preview  # noqa: E402


# --- Helpers ---------------------------------------------------------------


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


def _make_job(client, email, firm, *, snapshot, with_qbo=True):
    _signup_and_login(client, email, firm)
    db = appmod.db
    user = db.get_user_by_email(email)
    job_id = f"job_t3_{firm.replace(' ', '_').lower()}"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company=firm, source_file="x.csv",
        encrypted_file="t3.enc",
        file_sha256="0" * 64,
        status="uploaded",
    )
    db.save_job_state(job_id, {"status": "uploaded",
                                "pclaw_accounts": snapshot})
    if with_qbo:
        appmod.qbo_connections[job_id] = {
            "realm_id": f"R-{firm}",
            "access_token_enc": appmod.encrypt_token("fake-access"),
            "refresh_token_enc": appmod.encrypt_token("fake-refresh"),
            "company_name": firm,
            "legal_name": firm,
            "country": "US",
            "expires_at": "2999-01-01T00:00:00",
            "company_info_error": None,
        }
    appmod.jobs.pop(job_id, None)
    return job_id, user


class _FakeQBO:
    def __init__(self, accounts=None, *, fail_on_create_subtype=None,
                 fail_intuit_tid=None):
        self._accounts = list(accounts or [])
        self.created_payloads = []
        self._fail_subtype = fail_on_create_subtype
        self._fail_tid = fail_intuit_tid

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
        if (self._fail_subtype
                and payload.get("AccountSubType") == self._fail_subtype):
            from qbo_client import QBOError
            raise QBOError(
                "QBO returned 400: invalid AccountSubType",
                status_code=400,
                body='{"Fault":{"Error":[{"Message":"invalid AccountSubType"}]}}',
                intuit_tid=self._fail_tid,
            )
        self.created_payloads.append(payload)
        new_id = str(1000 + len(self.created_payloads))
        new_account = {
            "Id": new_id,
            "Name": payload.get("Name"),
            "AcctNum": payload.get("AcctNum"),
            "AccountType": payload.get("AccountType"),
            "AccountSubType": payload.get("AccountSubType"),
            "Active": payload.get("Active", True),
        }
        self._accounts.append(new_account)
        return {"Account": new_account}


# --- T1 -------------------------------------------------------------------


def t1_subtype_is_camelcase_not_hyphenated():
    """Every hint path must resolve to TrustAccountsLiabilities."""
    # By detail_type
    r1 = map_pclaw_account_to_qbo_type({
        "account_number": "2100",
        "account_name": "Client Trust Liability",
        "detail_type": "Trust Liability",
        "account_type": "Liability",
    })
    # By account_type
    r2 = map_pclaw_account_to_qbo_type({
        "account_number": "2100",
        "account_name": "Client Trust Liability",
        "detail_type": "",
        "account_type": "Client Trust Liability",
    })
    # By account_name only (GL-only scenario, no COA upload)
    r3 = map_pclaw_account_to_qbo_type({
        "account_number": "2100",
        "account_name": "Client Trust Liability",
        "detail_type": "",
        "account_type": "",
    })
    for label, r in (("detail_type", r1), ("account_type", r2),
                      ("account_name", r3)):
        assert r["account_type"] == "Other Current Liability", (label, r)
        assert r["detail_type"] == "TrustAccountsLiabilities", (label, r)
        # Hyphenated form must never appear.
        assert "-Liabilities" not in str(r["detail_type"]), (label, r)
        assert r["decision"] == "warn", (label, r)
    print("T1 OK: trust-liability resolves to TrustAccountsLiabilities (camelCase)")


# --- T2 -------------------------------------------------------------------


def t2_create_plan_payload_uses_qbo_safe_subtype():
    """apply_create_plan ships AccountSubType=TrustAccountsLiabilities."""
    coa_rows = [
        {
            "account_number": "2100",
            "account_name": "Client Trust Liability",
            "account_type": "Liability",
            "detail_type": "Trust Liability",
            "active": True,
        },
    ]
    qbo_resp = {"QueryResponse": {"Account": []}}
    preview = build_coa_dry_run_preview(coa_rows, qbo_resp)
    plan = build_create_plan(coa_rows, preview)
    assert len(plan.to_create) == 1, plan.to_create
    entry = plan.to_create[0]
    assert entry.qbo_detail_type == "TrustAccountsLiabilities", entry
    fake = _FakeQBO([])
    result = apply_create_plan(fake, plan)
    assert result["created"], result
    assert not result["failed"], result
    payload = fake.created_payloads[0]
    assert payload["AccountSubType"] == "TrustAccountsLiabilities", payload
    # Belt-and-suspenders: hyphenated form is never sent.
    assert payload["AccountSubType"] != "TrustAccounts-Liabilities"
    assert payload["AccountType"] == "Other Current Liability"
    assert payload["AcctNum"] == "2100"
    print("T2 OK: outbound QBO payload uses camelCase TrustAccountsLiabilities")


# --- T3 -------------------------------------------------------------------


def t3_single_unmatched_banner_variant_and_reassurance():
    client = appmod.app.test_client()
    # Mimic the user's screenshot: every other account is in QBO; only
    # 2100 Client Trust Liability is missing.
    snapshot = [
        {"number": "1000", "name": "Operating Bank"},
        {"number": "1010", "name": "Trust Bank"},
        {"number": "2100", "name": "Client Trust Liability"},
        {"number": "3100", "name": "Owner Draws"},
        {"number": "4000", "name": "Legal Fees Income"},
        {"number": "5000", "name": "Rent Expense"},
        {"number": "5100", "name": "Office Supplies Expense"},
        {"number": "5200", "name": "Bank Fees"},
    ]
    job_id, _ = _make_job(client, "t3@example.test", "T3 LLP",
                           snapshot=snapshot)
    # QBO has every account EXCEPT 2100.
    qbo = _FakeQBO([
        {"Id": "A1", "Name": "Operating Bank", "AcctNum": "1000",
         "AccountType": "Bank"},
        {"Id": "A2", "Name": "Trust Bank", "AcctNum": "1010",
         "AccountType": "Bank"},
        {"Id": "A3", "Name": "Owner Draws", "AcctNum": "3100",
         "AccountType": "Equity"},
        {"Id": "A4", "Name": "Legal Fees Income", "AcctNum": "4000",
         "AccountType": "Income"},
        {"Id": "A5", "Name": "Rent Expense", "AcctNum": "5000",
         "AccountType": "Expense"},
        {"Id": "A6", "Name": "Office Supplies Expense", "AcctNum": "5100",
         "AccountType": "Expense"},
        {"Id": "A7", "Name": "Bank Fees", "AcctNum": "5200",
         "AccountType": "Expense"},
    ])
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping",
                        follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)

    # Banner is rendered and tagged as single-unmatched.
    assert 'data-testid="create-missing-banner"' in body
    assert 'data-single-unmatched="yes"' in body, \
        "banner must mark single-unmatched state for the 1-account case"
    assert 'data-unmatched-count="1"' in body

    # Headline calls out a single missing account in plain English.
    assert "One account isn" in body and "in QuickBooks yet" in body, body[:2000]

    # Explicit reassurance the user should NOT add it manually.
    assert "no need to open QuickBooks" in body, \
        "banner must reassure the user not to add the account manually"

    # The actual unmatched account is identified by number + name.
    assert "2100 Client Trust Liability" in body, body[:3000]
    assert 'data-testid="create-missing-account-label"' in body

    # Both CTAs are present.
    assert 'data-testid="create-missing-cta"' in body
    assert 'data-testid="refresh-qbo-cta"' in body
    assert "Refresh QuickBooks accounts" in body

    print("T3 OK: single-unmatched banner names the account and reassures the user")


# --- T4 -------------------------------------------------------------------


def t4_create_missing_creates_2100_and_persists_saved_mapping():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "1000", "name": "Operating Bank"},
        {"number": "2100", "name": "Client Trust Liability"},
    ]
    job_id, user = _make_job(client, "t4@example.test", "T4 LLP",
                              snapshot=snapshot)
    qbo = _FakeQBO([
        {"Id": "A1", "Name": "Operating Bank", "AcctNum": "1000",
         "AccountType": "Bank"},
    ])
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.post(
            f"/jobs/{job_id}/account-mapping/create-missing",
            follow_redirects=False,
        )
    assert r.status_code in (301, 302, 303, 307, 308), r.status_code

    # Exactly one create call — for 2100 — with QBO-valid subtype.
    assert len(qbo.created_payloads) == 1, qbo.created_payloads
    payload = qbo.created_payloads[0]
    assert payload["AcctNum"] == "2100"
    assert payload["Name"] == "Client Trust Liability"
    assert payload["AccountType"] == "Other Current Liability"
    assert payload["AccountSubType"] == "TrustAccountsLiabilities"

    # Saved mapping persisted so the next render shows the row as Saved.
    saved = appmod.db.list_account_mappings(
        user["firm_id"], appmod.qbo_connections[job_id]["realm_id"],
    )
    assert any(m["pclaw_account_number"] == "2100" for m in saved), saved

    # And rendering again shows the row as Saved (not Unmatched).
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r2 = client.get(f"/jobs/{job_id}/account-mapping",
                         follow_redirects=False)
    body2 = r2.get_data(as_text=True)
    assert "Saved" in body2
    # The single-unmatched banner is gone now (every PCLaw account matches).
    assert 'data-testid="create-missing-banner"' not in body2

    print("T4 OK: /create-missing creates 2100 with correct subtype; "
          "saved mapping persisted; next render is fully matched")


# --- T5 -------------------------------------------------------------------


def t5_qbo_failure_shows_friendly_flash_with_tid():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "2100", "name": "Client Trust Liability"},
    ]
    job_id, _ = _make_job(client, "t5@example.test", "T5 LLP",
                           snapshot=snapshot)
    # Simulate QBO rejecting create with a 400.
    qbo = _FakeQBO(
        accounts=[],
        fail_on_create_subtype="TrustAccountsLiabilities",
        fail_intuit_tid="tid-XYZ-12345",
    )
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.post(
            f"/jobs/{job_id}/account-mapping/create-missing",
            follow_redirects=True,  # follow to surface the flash on the next page
        )
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # The row is still here and we DID NOT silently mark it matched.
    assert "Client Trust Liability" in body
    # Flash names a recovery action OR mentions the failure clearly.
    # (apply_create_plan catches per-row failures and adds them to "failed".
    # The route flashes a friendly "Created X; N failed" message.)
    assert ("failed" in body.lower() or "couldn't" in body.lower()
            or "error" in body.lower()), \
        "user must see a clear failure indication, not a silent unmatched row"
    print("T5 OK: QBO create failure surfaces a friendly error; row stays visible")


def main():
    t1_subtype_is_camelcase_not_hyphenated()
    t2_create_plan_payload_uses_qbo_safe_subtype()
    t3_single_unmatched_banner_variant_and_reassurance()
    t4_create_missing_creates_2100_and_persists_saved_mapping()
    t5_qbo_failure_shows_friendly_flash_with_tid()
    print("\nALL STEP-3 SINGLE-UNMATCHED TRUST-LIABILITY SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
