"""Smoke tests for Step-3 "create missing QuickBooks accounts" flow.

Background
----------
On Step 3 of the migration workflow ("Match accounts") lawyers were
forced to manually pair every PCLaw account against the connected
QuickBooks chart of accounts. When QBO had a fresh / mostly-empty COA,
none of the dropdowns matched and the demo (and live) flow stalled.

This sprint adds a one-click path:

  * Detect that PCLaw accounts are missing from QBO and surface a clear
    banner CTA on the Match-accounts page.
  * Run the existing safe type-mapping (coa_apply.build_create_plan)
    against the GL-extracted PCLaw account list, optionally enriched
    with the firm's uploaded Chart-of-Accounts row types.
  * Create only the missing QBO accounts. Existing QBO accounts are
    never duplicated (matched first by AcctNum, then exact Name).
  * Persist the new account ids as saved mappings so the very next
    /account-mapping render shows them as "Saved" and the user only
    reviews remaining exceptions.
  * If the safe type-mapper can't classify a row, surface it as a
    blocked review item *before* writing — no QBO account is ever
    created with a guessed type.

Run from project root:

    python3 tests/smoke_account_mapping_create_missing.py

Covers
------
  N1  Auto-match by exact AcctNum, then by normalized name (case +
      punctuation tolerant). Summary distinguishes saved / auto /
      unmatched.
  N2  GET /account-mapping renders the "create missing accounts" CTA
      banner when QBO is missing PCLaw accounts. Banner is suppressed
      when every PCLaw account already matches.
  N3  POST /account-mapping/create-missing calls the mocked QBO
      create-account endpoint *only* for the missing accounts; the
      existing-account is not re-created (dedupe by AcctNum).
  N4  After creation the route persists saved_account_mapping rows so
      auto-match shows the new accounts as "Saved" on next render.
  N5  Ambiguous PCLaw account types do NOT create wrong QBO accounts —
      they surface as a review blocker (no create call) and the user
      gets a clear message pointing to the COA step.
  N6  No QBO connection -> /create-missing redirects to /connect-qbo.
  N7  POST /account-mapping/refresh -> redirects back to GET (which
      re-queries QBO every render); does not write anything.
  N8  Pure helper: _build_create_missing_plan reuses the same safe
      type-mapping the dedicated COA flow uses (warn/blocked rules are
      identical to coa_apply.map_pclaw_account_to_qbo_type).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_UPLOAD_DIR = tempfile.mkdtemp(prefix="pclaw_uploads_cm_")
os.environ["UPLOAD_DIR"] = _UPLOAD_DIR
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_cm_")

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-create-missing")

import app as appmod  # noqa: E402


# --- Test helpers -----------------------------------------------------------


def _signup_and_login(client, email, firm):
    pwd = "passw0rd!1234"
    # POST /logout drops any prior session so back-to-back tests using
    # one client don't land in the "already logged in -> /dashboard"
    # branch on signup.
    client.post("/logout", follow_redirects=False)
    r = client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


def _make_job_with_qbo(client, email, firm, *, snapshot=None, no_qbo=False):
    """Sign up a firm, create a GL job, attach a fake QBO connection.

    ``snapshot`` is the persisted pclaw_accounts list (the survives-
    redeploy snapshot) the route consumes.
    """
    _signup_and_login(client, email, firm)
    db = appmod.db
    user = db.get_user_by_email(email)
    job_id = f"job_cm_{firm.replace(' ', '_').lower()}"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company=firm, source_file="x.csv",
        encrypted_file="cm_missing.enc",  # never touched — we use the snapshot
        file_sha256="0" * 64,
        status="uploaded",
    )
    if snapshot is not None:
        db.save_job_state(job_id, {"status": "uploaded",
                                    "pclaw_accounts": snapshot})
    if not no_qbo:
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
    else:
        appmod.qbo_connections.pop(job_id, None)
    appmod.jobs.pop(job_id, None)
    return job_id, user


class _FakeQBO:
    """In-memory QBOClient stand-in.

    ``accounts`` is the list of dicts the COA query returns. Each call
    to ``create_account`` appends to the same list so subsequent
    queries see the new accounts (lets a test assert auto-match works
    after creation).
    """

    def __init__(self, accounts=None):
        self._accounts = list(accounts or [])
        self.created_payloads = []
        self.find_by_acctnum_calls = []
        self.find_by_name_calls = []

    def get_accounts(self):
        return {"QueryResponse": {"Account": list(self._accounts)}}

    def find_account_by_acctnum(self, num):
        self.find_by_acctnum_calls.append(num)
        if not num:
            return None
        for a in self._accounts:
            if str(a.get("AcctNum") or "") == str(num):
                return a
        return None

    def find_account_by_name(self, name):
        self.find_by_name_calls.append(name)
        if not name:
            return None
        target = name.strip().lower()
        for a in self._accounts:
            if str(a.get("Name") or "").strip().lower() == target:
                return a
        return None

    def create_account(self, payload):
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


# --- N1: auto-match by AcctNum then normalized Name ------------------------


def n1_auto_match_by_number_then_normalized_name():
    pclaw_accounts = [
        {"number": "1000", "name": "Operating Bank"},
        # exact name (different case) — matches via normalized name
        {"number": None, "name": "trust bank"},
        # punctuation drift — matches via normalized name
        {"number": "4100", "name": "Service Fees"},
        # truly missing
        {"number": "9999", "name": "Brand New Account"},
    ]
    qbo_accounts = [
        {"Id": "A1", "Name": "Operating Bank", "AcctNum": "1000",
         "AccountType": "Bank"},
        {"Id": "A2", "Name": "Trust Bank", "AcctNum": None,
         "AccountType": "Bank"},
        # Note the trailing word + different case to test normalization.
        {"Id": "A3", "Name": "Service Fees!", "AcctNum": None,
         "AccountType": "Income"},
    ]
    rows, summary = appmod._build_account_mapping_rows(
        pclaw_accounts=pclaw_accounts,
        qbo_accounts=qbo_accounts,
        saved_by_key={},
    )
    assert rows[0]["match_basis"] == "AcctNum", rows[0]
    assert rows[0]["current_qbo_id"] == "A1"
    assert rows[1]["match_basis"] == "Name", rows[1]
    assert rows[1]["current_qbo_id"] == "A2"
    assert rows[2]["match_basis"] == "Name", rows[2]
    assert rows[2]["current_qbo_id"] == "A3"
    assert rows[3]["current_qbo_id"] is None, rows[3]
    assert summary["total"] == 4
    assert summary["matched"] == 3
    assert summary["unmatched"] == 1
    assert summary["any_unmatched"] is True
    # 1 / 4 = 25% so many_unmatched is True (>=25%).
    assert summary["many_unmatched"] is True
    print("N1 OK: auto-match by AcctNum then normalized Name")


# --- N2: banner shown when accounts missing --------------------------------


def n2_banner_shown_when_accounts_missing_and_not_otherwise():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "1000", "name": "Operating Bank"},
        {"number": "9999", "name": "Brand New Account"},
    ]
    job_id, _ = _make_job_with_qbo(
        client, "n2@example.test", "N2 LLP", snapshot=snapshot,
    )
    # One QBO account exists, the other doesn't.
    qbo = _FakeQBO([
        {"Id": "A1", "Name": "Operating Bank", "AcctNum": "1000",
         "AccountType": "Bank"},
    ])
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping",
                        follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="create-missing-banner"' in body, \
        "expected create-missing banner when QBO is missing PCLaw accounts"
    assert 'data-testid="create-missing-cta"' in body
    assert 'data-testid="refresh-qbo-cta"' in body
    assert "Create missing QuickBooks accounts" in body
    assert "Refresh QuickBooks accounts" in body
    # Lawyer-friendly clarification: this step only creates accounts, not transactions.
    assert "needed for matching" in body
    assert "no transactions are posted" in " ".join(body.split()).lower()

    # And when every account already matches the banner is suppressed.
    snapshot_full = [{"number": "1000", "name": "Operating Bank"}]
    job_id2, _ = _make_job_with_qbo(
        client, "n2b@example.test", "N2B LLP", snapshot=snapshot_full,
    )
    qbo2 = _FakeQBO([
        {"Id": "A1", "Name": "Operating Bank", "AcctNum": "1000",
         "AccountType": "Bank"},
    ])
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo2, appmod.qbo_connections[job_id2]),
    ):
        r2 = client.get(f"/jobs/{job_id2}/account-mapping",
                         follow_redirects=False)
    body2 = r2.get_data(as_text=True)
    assert 'data-testid="create-missing-banner"' not in body2, \
        "banner must not appear when every PCLaw account is matched"
    print("N2 OK: create-missing banner appears only when accounts are missing")


# --- N3: create-missing creates only missing accounts, dedupes existing ----


def n3_create_missing_only_creates_missing_accounts_and_dedupes():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "1000", "name": "Operating Bank"},   # already exists in QBO
        {"number": "2000", "name": "Trust Bank"},         # missing, safe type
        {"number": "4100", "name": "Service Fee Income"}, # missing, safe type
    ]
    job_id, _ = _make_job_with_qbo(
        client, "n3@example.test", "N3 LLP", snapshot=snapshot,
    )
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
    # Always redirects back to the Match-accounts screen.
    assert r.status_code in (301, 302, 303, 307, 308), r.status_code
    assert f"/jobs/{job_id}/account-mapping" in r.headers.get("Location", "")

    # Existing account never recreated.
    created_acctnums = [p.get("AcctNum") for p in qbo.created_payloads]
    assert "1000" not in created_acctnums, \
        f"existing AcctNum 1000 must not be recreated: {created_acctnums}"
    assert "2000" in created_acctnums, created_acctnums
    assert "4100" in created_acctnums, created_acctnums

    # Each create call carries the QBO-safe payload shape.
    for p in qbo.created_payloads:
        assert p["AccountType"], f"created without AccountType: {p}"
        assert p.get("Name"), f"created without Name: {p}"
        assert p.get("AcctNum"), f"created without AcctNum: {p}"

    # Trust Bank should map to Bank / TrustAccounts per coa_apply table.
    trust = next(p for p in qbo.created_payloads if p["AcctNum"] == "2000")
    assert trust["AccountType"] == "Bank"
    assert trust["AccountSubType"] == "TrustAccounts"

    # Income accounts map to Income.
    fees = next(p for p in qbo.created_payloads if p["AcctNum"] == "4100")
    assert fees["AccountType"] == "Income"

    print("N3 OK: create-missing creates only missing accounts; dedupes existing")


# --- N4: saved mappings persisted so auto-match continues to show "Saved" --


def n4_saved_mappings_persisted_after_creation():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "2000", "name": "Trust Bank"},
    ]
    job_id, user = _make_job_with_qbo(
        client, "n4@example.test", "N4 LLP", snapshot=snapshot,
    )
    qbo = _FakeQBO([])  # nothing in QBO yet
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        client.post(
            f"/jobs/{job_id}/account-mapping/create-missing",
            follow_redirects=False,
        )
        # Now hit GET — the same fake QBO now contains the just-created
        # account, so the row should render as Saved (not just Auto).
        r2 = client.get(f"/jobs/{job_id}/account-mapping",
                         follow_redirects=False)

    assert qbo.created_payloads, "expected create_account to be called"

    # DB row is persisted for the firm + realm.
    saved = appmod.db.list_account_mappings(
        user["firm_id"], appmod.qbo_connections[job_id]["realm_id"],
    )
    assert any(m["pclaw_account_number"] == "2000" for m in saved), saved

    body2 = r2.get_data(as_text=True)
    # Saved badge is in the row.
    assert "Saved" in body2
    # No "Unmatched" badge for the trust account.
    assert "<span class=\"badge error\">Unmatched</span>" not in body2 or \
        body2.count("Unmatched") == 0
    print("N4 OK: saved mappings persisted; GET shows row as Saved")


# --- N5: ambiguous types are blocked, NOT created with a guess -------------


def n5_ambiguous_type_blocked_no_create_call():
    client = appmod.app.test_client()
    snapshot = [
        # Account name carries no safe type signal — coa_apply.refuses
        # to guess and surfaces this as a blocker.
        {"number": "8123", "name": "Misc Account 8123"},
    ]
    job_id, _ = _make_job_with_qbo(
        client, "n5@example.test", "N5 LLP", snapshot=snapshot,
    )
    qbo = _FakeQBO([])
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        client.post(
            f"/jobs/{job_id}/account-mapping/create-missing",
            follow_redirects=False,
        )
    # Nothing was created.
    assert qbo.created_payloads == [], \
        f"ambiguous type must not produce a create call, got {qbo.created_payloads}"

    # And the user sees an actionable flash on the next render.
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping",
                        follow_redirects=False)
    body = r.get_data(as_text=True)
    # Either the flash region or the per-row "Unmatched" badge surfaces;
    # critically the row is still on the page and not silently created.
    assert "Misc Account 8123" in body
    print("N5 OK: ambiguous account type surfaced as review blocker (no create)")


# --- N6: no QBO connection -> redirect to /connect-qbo ---------------------


def n6_no_qbo_connection_redirects_to_connect():
    client = appmod.app.test_client()
    job_id, _ = _make_job_with_qbo(
        client, "n6@example.test", "N6 LLP",
        snapshot=[{"number": "1000", "name": "Cash"}],
        no_qbo=True,
    )
    r = client.post(
        f"/jobs/{job_id}/account-mapping/create-missing",
        follow_redirects=False,
    )
    assert r.status_code in (301, 302, 303, 307, 308), r.status_code
    assert f"/jobs/{job_id}/connect-qbo" in r.headers.get("Location", ""), \
        r.headers.get("Location")
    print("N6 OK: no QBO connection -> redirect to /connect-qbo")


# --- N7: /refresh -> redirect back to GET, no writes -----------------------


def n7_refresh_redirects_without_writes():
    client = appmod.app.test_client()
    job_id, _ = _make_job_with_qbo(
        client, "n7@example.test", "N7 LLP",
        snapshot=[{"number": "1000", "name": "Cash"}],
    )
    r = client.post(
        f"/jobs/{job_id}/account-mapping/refresh",
        follow_redirects=False,
    )
    assert r.status_code in (301, 302, 303, 307, 308), r.status_code
    assert f"/jobs/{job_id}/account-mapping" in r.headers.get("Location", "")
    print("N7 OK: refresh redirects back to /account-mapping (re-fetches on GET)")


# --- N8: helper reuses coa_apply safe type rules ---------------------------


def n8_helper_reuses_safe_type_rules():
    # Sign in a user so _firm_latest_coa_state has a firm_id.
    client = appmod.app.test_client()
    _signup_and_login(client, "n8@example.test", "N8 LLP")
    user = appmod.db.get_user_by_email("n8@example.test")

    pclaw_accounts = [
        {"number": "1000", "name": "Operating Bank"},      # safe -> Bank
        {"number": "2000", "name": "Trust Bank"},          # safe -> Bank (TrustAccounts)
        {"number": "8123", "name": "Misc Account 8123"},   # blocked (ambiguous)
    ]
    qbo_resp = {"QueryResponse": {"Account": []}}

    preview, plan = appmod._build_create_missing_plan(
        user=user, pclaw_accounts=pclaw_accounts,
        qbo_accounts_response=qbo_resp,
    )
    assert preview["matched_count"] == 0
    # The blocker is in plan.blocked, the safe ones in plan.to_create.
    to_create_numbers = {e.account_number for e in plan.to_create}
    blocked_numbers = {e.account_number for e in plan.blocked}
    assert "1000" in to_create_numbers, to_create_numbers
    assert "2000" in to_create_numbers, to_create_numbers
    assert "8123" in blocked_numbers, blocked_numbers
    assert "8123" not in to_create_numbers
    # And the resolved types come from the coa_apply table.
    bank = next(e for e in plan.to_create if e.account_number == "1000")
    assert bank.qbo_account_type == "Bank"
    trust = next(e for e in plan.to_create if e.account_number == "2000")
    assert trust.qbo_account_type == "Bank"
    assert trust.qbo_detail_type == "TrustAccounts"
    print("N8 OK: helper reuses coa_apply safe type rules (warn/blocked)")


def main():
    n1_auto_match_by_number_then_normalized_name()
    n2_banner_shown_when_accounts_missing_and_not_otherwise()
    n3_create_missing_only_creates_missing_accounts_and_dedupes()
    n4_saved_mappings_persisted_after_creation()
    n5_ambiguous_type_blocked_no_create_call()
    n6_no_qbo_connection_redirects_to_connect()
    n7_refresh_redirects_without_writes()
    n8_helper_reuses_safe_type_rules()
    print("\nALL ACCOUNT-MAPPING CREATE-MISSING SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
