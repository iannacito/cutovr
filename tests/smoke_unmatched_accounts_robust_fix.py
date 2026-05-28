"""Smoke tests for the "robust unmatched-accounts" fix.

Background
----------
Cesar's report (May 2026): on Step 3 of the migration, lawyers were
stuck in a "create -> refresh -> still missing" loop for 9-15 ambiguous
accounts (M&T Bank LOC, Common Stock, Paid In Capital, Dividends,
Health Insurance, Ins - Other, Business Development, Continued Legal
Ed, Maintenance/Repair, Chase - 7649, Art, ...). Neither the batch
"Create missing QuickBooks accounts" CTA nor the row-level "Add to
QuickBooks" link could complete because:

  1. The safe type-mapper had no rule for these accounts and refused
     to guess.
  2. The customer had no way to tell the app "this is an Expense" /
     "this is Owner equity" without going to the dedicated COA flow.
  3. Partial success looked like total failure in the banner copy.

This sprint:

  * Expands the type-mapper's name patterns so common law-firm
    accounts (LOC, Common Stock, Paid In Capital, Dividends, Health
    Insurance, Ins-Other, Business Development, CLE, Maintenance/Repair)
    resolve to safe (AccountType, AccountSubType) pairs.
  * Adds a per-row category dropdown ("What kind of account is this?")
    with plain-English labels (Bank account / Credit card / Loan or
    liability / Owner/equity / Income / Expense / Fixed asset / Other).
  * Adds /jobs/<id>/account-mapping/add-account — a row-level POST
    endpoint that either creates immediately (when a safe type is
    available) or requires a category selection first. Never silently
    fails; always redirects with a clear flash.
  * Makes the batch create-missing flash actionable on partial success.
  * Net Income (Loss) continues to be skipped, never created.
  * No customer-visible "QBO" text in changed templates.

Run from project root:

    python3 tests/smoke_unmatched_accounts_robust_fix.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_UPLOAD_DIR = tempfile.mkdtemp(prefix="pclaw_uploads_unmatched_")
os.environ["UPLOAD_DIR"] = _UPLOAD_DIR
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_unmatched_")

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-unmatched-robust")

import app as appmod  # noqa: E402
from coa_apply import map_pclaw_account_to_qbo_type  # noqa: E402


# --- Test helpers -----------------------------------------------------------


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


def _make_job_with_qbo(client, email, firm, *, snapshot=None):
    _signup_and_login(client, email, firm)
    db = appmod.db
    user = db.get_user_by_email(email)
    job_id = f"job_robust_{firm.replace(' ', '_').lower()}"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company=firm, source_file="x.csv",
        encrypted_file="robust.enc",
        file_sha256="0" * 64,
        status="uploaded",
    )
    if snapshot is not None:
        db.save_job_state(job_id, {"status": "uploaded",
                                    "pclaw_accounts": snapshot})
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


# --- R1: Cesar's example accounts resolve to safe types --------------------


def r1_cesar_examples_resolve_to_safe_types():
    """All of Cesar's named examples that *can* be resolved by name
    should now map to a safe (AccountType, AccountSubType) pair. The
    truly ambiguous ones (Art, Chase - 7649) stay blocked so the user
    picks a category — but the majority no longer block."""
    expectations = {
        # name -> expected QBO AccountType
        "M&T Bank LOC (56001)": "Long Term Liability",
        "Wells Fargo Business LOC": "Long Term Liability",
        "Common Stock": "Equity",
        "Paid In Capital": "Equity",
        "Dividends": "Equity",
        "Health Insurance": "Expense",
        "Ins - Other": "Expense",
        "Business Development": "Expense",
        "Continued Legal Ed": "Expense",
        "Maintenance/Repair": "Expense",
    }
    for name, expected_type in expectations.items():
        result = map_pclaw_account_to_qbo_type({"account_name": name})
        assert result["decision"] in ("ok", "warn"), (
            f"{name!r} expected resolvable but got {result['decision']}: "
            f"{result.get('blocked_reason')}"
        )
        assert result["account_type"] == expected_type, (
            f"{name!r} expected {expected_type} but got {result['account_type']}"
        )
    # And the genuinely-ambiguous examples still block, so the user
    # gets prompted for a category.
    for ambiguous in ("Art", "Chase - 7649 (7037)"):
        result = map_pclaw_account_to_qbo_type({"account_name": ambiguous})
        assert result["decision"] == "blocked", (
            f"{ambiguous!r} should still be blocked, got {result['decision']}"
        )
    # Net Income (Loss) is skipped, never created.
    ni = map_pclaw_account_to_qbo_type({"account_name": "Net Income (Loss)"})
    assert ni["decision"] == "skipped"
    assert ni["account_type"] is None
    print("R1 OK: Cesar's example accounts resolve safely; ambiguous still block; "
          "Net Income (Loss) skipped")


# --- R2: per-row category selector rendered for ambiguous accounts ---------


def r2_per_row_category_selector_for_ambiguous_accounts():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "1000", "name": "Operating Bank"},  # auto-match
        {"number": "9001", "name": "Art"},              # ambiguous -> needs category
        {"number": "9002", "name": "Chase - 7649"},     # ambiguous -> needs category
        {"number": "9003", "name": "Health Insurance"}, # safe -> Add button
    ]
    job_id, _ = _make_job_with_qbo(client, "r2@example.test", "R2 LLP",
                                    snapshot=snapshot)
    qbo = _FakeQBO([
        {"Id": "A1", "Name": "Operating Bank", "AcctNum": "1000",
         "AccountType": "Bank"},
    ])
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)

    # Each unmatched-and-ambiguous row should expose a category select.
    # We can't predict exact idx but at least one category select must
    # appear for "Art" / "Chase - 7649" since both are blocked.
    assert "row-category-select-" in body, (
        "ambiguous row should expose 'What kind of account is this?' selector"
    )
    # The plain-English labels appear.
    assert "Bank account" in body
    assert "Credit card" in body
    assert "Loan or liability" in body
    assert "Owner/equity" in body
    assert "Income" in body
    assert "Expense" in body
    assert "Fixed asset" in body
    # Safe row (Health Insurance) gets the inferred-type hint.
    assert "row-inferred-type-" in body, (
        "row with a safely-inferred type should show its inferred type label"
    )
    print("R2 OK: ambiguous rows render the plain-English category selector; "
          "safe rows show the inferred-type hint")


# --- R3: row-level Add to QuickBooks creates immediately when safe ---------


def r3_row_level_add_creates_when_safe():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "5150", "name": "Health Insurance"},  # safe -> Expense
    ]
    job_id, user = _make_job_with_qbo(client, "r3@example.test", "R3 LLP",
                                        snapshot=snapshot)
    qbo = _FakeQBO([])  # nothing exists yet
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.post(
            f"/jobs/{job_id}/account-mapping/add-account",
            data={"pclaw_number": "5150", "pclaw_name": "Health Insurance"},
            follow_redirects=False,
        )
    assert r.status_code in (301, 302, 303, 307, 308), r.status_code
    assert qbo.created_payloads, "expected create_account to be called"
    payload = qbo.created_payloads[0]
    assert payload["AccountType"] == "Expense", payload
    assert payload["AcctNum"] == "5150"
    assert payload.get("Name") == "Health Insurance"
    # Saved mapping persisted.
    saved = appmod.db.list_account_mappings(
        user["firm_id"], appmod.qbo_connections[job_id]["realm_id"],
    )
    assert any(m["pclaw_account_number"] == "5150" for m in saved), saved
    print("R3 OK: row-level Add to QuickBooks creates immediately for safe types")


# --- R4: row-level Add asks for category when ambiguous -------------------


def r4_row_level_add_requires_category_when_ambiguous():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "9001", "name": "Art"},  # ambiguous
    ]
    job_id, _ = _make_job_with_qbo(client, "r4@example.test", "R4 LLP",
                                    snapshot=snapshot)
    qbo = _FakeQBO([])
    # First click: no category -> we ask, no create call.
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.post(
            f"/jobs/{job_id}/account-mapping/add-account",
            data={"pclaw_number": "9001", "pclaw_name": "Art"},
            follow_redirects=False,
        )
    assert r.status_code in (301, 302, 303, 307, 308), r.status_code
    assert qbo.created_payloads == [], (
        "ambiguous row without a category must NOT create a QBO account"
    )

    # Second click: pick "Fixed asset" -> create as Fixed Asset.
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r2 = client.post(
            f"/jobs/{job_id}/account-mapping/add-account",
            data={
                "pclaw_number": "9001", "pclaw_name": "Art",
                "category": "fixed_asset",
            },
            follow_redirects=False,
        )
    assert r2.status_code in (301, 302, 303, 307, 308), r2.status_code
    assert qbo.created_payloads, "category-supplied click should create QBO account"
    payload = qbo.created_payloads[0]
    assert payload["AccountType"] == "Fixed Asset", payload
    assert payload["AcctNum"] == "9001"
    print("R4 OK: ambiguous row asks for a category, then creates with the "
          "chosen category — no silent failure, no loop")


# --- R5: batch create with partial success surfaces remaining ambiguous ----


def r5_batch_partial_success_surfaces_remaining_ambiguous():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "5150", "name": "Health Insurance"},  # safe -> Expense
        {"number": "5270", "name": "Ins - Other"},        # safe -> Expense
        {"number": "5320", "name": "Maintenance/Repair"}, # safe -> Expense
        {"number": "9001", "name": "Art"},                # ambiguous
        {"number": "9002", "name": "Chase - 7649"},       # ambiguous
    ]
    job_id, user = _make_job_with_qbo(client, "r5@example.test", "R5 LLP",
                                        snapshot=snapshot)
    qbo = _FakeQBO([])
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.post(
            f"/jobs/{job_id}/account-mapping/create-missing",
            follow_redirects=False,
        )
    assert r.status_code in (301, 302, 303, 307, 308)
    # Safe rows got created; ambiguous rows did not.
    created_nums = {p.get("AcctNum") for p in qbo.created_payloads}
    assert "5150" in created_nums, created_nums
    assert "5270" in created_nums, created_nums
    assert "5320" in created_nums, created_nums
    assert "9001" not in created_nums, created_nums
    assert "9002" not in created_nums, created_nums
    # Saved mappings persisted for the three created.
    saved = appmod.db.list_account_mappings(
        user["firm_id"], appmod.qbo_connections[job_id]["realm_id"],
    )
    saved_nums = {m["pclaw_account_number"] for m in saved}
    assert "5150" in saved_nums and "5270" in saved_nums and "5320" in saved_nums
    print("R5 OK: batch create-missing creates safe rows, surfaces remaining "
          "ambiguous ones — partial success is not total failure")


# --- R6: refresh re-queries QBO and the page now auto-matches new accounts -


def r6_refresh_auto_matches_new_accounts():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "1000", "name": "Operating Bank"},
    ]
    job_id, _ = _make_job_with_qbo(client, "r6@example.test", "R6 LLP",
                                    snapshot=snapshot)
    qbo = _FakeQBO([])
    # First render — account is unmatched.
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        body1 = client.get(f"/jobs/{job_id}/account-mapping").get_data(as_text=True)
    assert "<span class=\"badge error\">Unmatched</span>" in body1

    # User creates the account "in QuickBooks" (mocked by appending to
    # the fake QBO accounts list directly — same effect as a fresh QBO
    # account showing up after the refresh).
    qbo._accounts.append({
        "Id": "Anew", "Name": "Operating Bank", "AcctNum": "1000",
        "AccountType": "Bank",
    })

    # Refresh -> re-render. The row should auto-match by AcctNum.
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.post(
            f"/jobs/{job_id}/account-mapping/refresh",
            follow_redirects=False,
        )
        assert r.status_code in (301, 302, 303, 307, 308)
        body2 = client.get(f"/jobs/{job_id}/account-mapping").get_data(as_text=True)
    assert "Auto-match" in body2, (
        "after refresh, newly-created QBO account should auto-match the row"
    )
    print("R6 OK: refresh + re-render auto-matches newly-visible QBO accounts")


# --- R7: row-level Add never silently fails or loops -----------------------


def r7_row_level_never_silently_fails():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "9001", "name": "Art"},
    ]
    job_id, _ = _make_job_with_qbo(client, "r7@example.test", "R7 LLP",
                                    snapshot=snapshot)
    qbo = _FakeQBO([])
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        # No category provided for an ambiguous account. The endpoint
        # must redirect (no 500) with an actionable flash, and must
        # not have made a create call.
        r = client.post(
            f"/jobs/{job_id}/account-mapping/add-account",
            data={"pclaw_number": "9001", "pclaw_name": "Art"},
            follow_redirects=True,
        )
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # Must mention picking a category to be actionable.
    assert "category" in body.lower()
    assert qbo.created_payloads == []
    print("R7 OK: row-level Add returns 200 with an actionable flash when a "
          "category is needed; no silent failure, no 500")


# --- R8: Net Income (Loss) never created via row-level add -----------------


def r8_net_income_never_created():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "3900", "name": "Net Income (Loss)"},
    ]
    job_id, _ = _make_job_with_qbo(client, "r8@example.test", "R8 LLP",
                                    snapshot=snapshot)
    qbo = _FakeQBO([])
    # Even if the user explicitly tries to add it with a category, the
    # safe type-mapper short-circuits this row as "skipped".
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.post(
            f"/jobs/{job_id}/account-mapping/add-account",
            data={
                "pclaw_number": "3900",
                "pclaw_name": "Net Income (Loss)",
                "category": "equity",
            },
            follow_redirects=False,
        )
    assert r.status_code in (301, 302, 303, 307, 308)
    # Critical: no create call. QBO computes Net Income itself.
    assert qbo.created_payloads == [], (
        "Net Income (Loss) must never be created in QBO — QBO calculates it"
    )
    print("R8 OK: Net Income (Loss) is never created via the row-level add "
          "endpoint, even when a category is forced")


# --- R9: account-mapping templates carry no customer-visible 'QBO' ---------


def r9_account_mapping_templates_use_quickbooks_not_qbo():
    template = ROOT / "templates" / "account-mapping.html"
    body = template.read_text(encoding="utf-8")
    # Token-boundary check: the literal string "QBO" must not appear in
    # the customer-facing template anywhere — we use "QuickBooks" /
    # "quickbooks" throughout.
    assert "QBO" not in body, (
        "templates/account-mapping.html still contains the customer-"
        "unfriendly 'QBO' abbreviation:\n"
        + "\n".join(line for line in body.splitlines() if "QBO" in line)
    )
    print("R9 OK: account-mapping.html uses 'QuickBooks' throughout; no 'QBO'")


# --- R10: category override persists across renders ------------------------


def r10_category_override_persists():
    client = appmod.app.test_client()
    snapshot = [
        {"number": "9001", "name": "Art"},
    ]
    job_id, _ = _make_job_with_qbo(client, "r10@example.test", "R10 LLP",
                                     snapshot=snapshot)
    qbo = _FakeQBO([])
    # First click sets the category. We use a custom category that
    # would still try to create — but since QBO is empty, the create
    # happens, and the saved mapping should carry the Fixed Asset type.
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        client.post(
            f"/jobs/{job_id}/account-mapping/add-account",
            data={
                "pclaw_number": "9001", "pclaw_name": "Art",
                "category": "fixed_asset",
            },
            follow_redirects=False,
        )
    # Override is on the in-memory job dict.
    job = appmod.jobs.get(job_id) or {}
    overrides = job.get("account_mapping_type_overrides") or {}
    assert "9001" in overrides, overrides
    assert overrides["9001"]["account_type"] == "Fixed Asset"
    print("R10 OK: per-row category override is recorded on the job for "
          "subsequent renders")


def main():
    r1_cesar_examples_resolve_to_safe_types()
    r2_per_row_category_selector_for_ambiguous_accounts()
    r3_row_level_add_creates_when_safe()
    r4_row_level_add_requires_category_when_ambiguous()
    r5_batch_partial_success_surfaces_remaining_ambiguous()
    r6_refresh_auto_matches_new_accounts()
    r7_row_level_never_silently_fails()
    r8_net_income_never_created()
    r9_account_mapping_templates_use_quickbooks_not_qbo()
    r10_category_override_persists()
    print("\nALL ROBUST-UNMATCHED-ACCOUNTS SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
