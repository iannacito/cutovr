"""Smoke tests for the deterministic name-pattern tier added to
``coa_apply.map_pclaw_account_to_qbo_type``.

Background
----------
The Step-3 "Create missing QuickBooks accounts" flow used to block any
GL-extracted account whose PCLaw type column was empty unless the user
also uploaded a full Chart of Accounts CSV with explicit account_type /
detail_type values. For demo runs against a clean QBO company this
turned trivially-recognisable names like "Legal Fees Income",
"Rent Expense", "Bank Fees", "Office Supplies Expense", and "Owner
Draws" into hard blockers — there was no way forward without manually
constructing a COA CSV.

This sprint adds a new tier of *deterministic* compound-name patterns
that resolve unambiguous account names to their canonical QBO
AccountType / AccountSubType. The mapping is the same one a competent
bookkeeper would use; it's only "safe" because every pattern is a
specific compound term (e.g. "rentexpense", "ownerdraw") or a true
suffix on the normalised name (e.g. anything ending in "expense").

Run from project root:

    python3 tests/smoke_create_missing_safe_name_patterns.py

Covers
------
  P1  The five account names from the demo-blocker report now resolve
      to safe QBO types via the new pattern tier — no operator
      intervention required.
  P2  Production-safety guard: ambiguous names without a recognisable
      pattern (e.g. "Misc Account 8123") still surface as blockers.
  P3  Production-safety guard: names with "Payable" / "Receivable"
      anywhere in them are NEVER classified by the generic suffix
      patterns (so "Income Tax Payable" must not become Income).
  P4  Persisted COA metadata still wins over the pattern fallback. If
      the firm uploaded a COA with explicit account_type / detail_type
      for an account, the create-missing step uses those values and
      records the match hint as ``detail_type`` / ``account_type``.
  P5  Existing QBO accounts are not duplicated by the create-missing
      step even when the new pattern resolves the type (regression
      guard for AcctNum dedupe).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_UPLOAD_DIR = tempfile.mkdtemp(prefix="pclaw_uploads_snp_")
os.environ["UPLOAD_DIR"] = _UPLOAD_DIR
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_snp_")
os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-name-patterns")

import app as appmod  # noqa: E402
from coa_apply import map_pclaw_account_to_qbo_type  # noqa: E402


# --- Test helpers ----------------------------------------------------------


def _signup_and_login(client, email, firm):
    pwd = "passw0rd!1234"
    client.post("/logout", follow_redirects=False)
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
            "/login", data={"email": email, "password": pwd},
            follow_redirects=False,
        )


# --- P1: the five demo-blocker account names now resolve safely -----------


def p1_demo_blocker_names_resolve_safely():
    """The exact five accounts from the user's stuck-demo report must all
    resolve to a safe QBO type without manual COA upload."""
    expected = {
        "Legal Fees Income": ("Income", "ServiceFeeIncome"),
        "Rent Expense": ("Expense", "RentOrLeaseOfBuildings"),
        "Bank Fees": ("Expense", "BankCharges"),
        "Office Supplies Expense": (
            "Expense", "OfficeGeneralAdministrativeExpenses",
        ),
        "Owner Draws": ("Equity", "OwnersEquity"),
    }
    for name, (want_type, want_detail) in expected.items():
        result = map_pclaw_account_to_qbo_type({"account_name": name})
        assert result["decision"] in ("ok", "warn"), (
            f"{name!r} unexpectedly blocked: {result}"
        )
        assert result["account_type"] == want_type, (
            f"{name!r} -> {result['account_type']!r}, expected {want_type!r}"
        )
        assert result["detail_type"] == want_detail, (
            f"{name!r} -> detail {result['detail_type']!r}, "
            f"expected {want_detail!r}"
        )
        # Source-of-truth check: these came from the pattern tier, not from
        # uploaded COA columns — the audit hint should reflect that.
        assert result["match_hint"] in (
            "account_name_pattern", "account_name_keyword",
        ), result
    print(
        "P1 OK: demo-blocker account names resolve to deterministic "
        "QBO types (Legal Fees Income, Rent Expense, Bank Fees, Office "
        "Supplies Expense, Owner Draws)"
    )


# --- P2: ambiguous production accounts still block ------------------------


def p2_ambiguous_names_still_block():
    """A production safety guard: names that don't carry a recognisable
    compound term must still surface as blockers. Otherwise we'd be
    silently guessing types for real client data."""
    for name in [
        "Misc Account 8123",      # nothing recognisable
        "Adjustment 42",          # nothing recognisable
        "Suspense",               # single ambiguous word, no pattern
        "Acme Holdings",          # company name, no category signal
    ]:
        result = map_pclaw_account_to_qbo_type({"account_name": name})
        assert result["decision"] == "blocked", (
            f"{name!r} unexpectedly resolved instead of blocking: {result}"
        )
        assert result["account_type"] is None
        assert result["blocked_reason"], result
    print("P2 OK: ambiguous account names still block (no silent guess)")


# --- P3: payable/receivable always blocks the generic suffix patterns -----


def p3_payable_receivable_never_matched_by_generic_pattern():
    """If a name contains 'Payable' or 'Receivable' anywhere, the generic
    suffix patterns must not classify it as Income / Expense / etc.
    'Income Tax Payable' is a liability, not income."""
    for name, want_blocked in [
        ("Income Tax Payable", True),   # liability — must not be Income
        ("Sales Tax Payable", True),    # liability — must not be Expense
        ("Receivable from Affiliate", True),  # must not be Income/Expense
    ]:
        result = map_pclaw_account_to_qbo_type({"account_name": name})
        if want_blocked:
            assert result["decision"] == "blocked", (
                f"{name!r} unexpectedly resolved: {result}"
            )
            assert result["account_type"] is None, result
    print(
        "P3 OK: names containing 'Payable' / 'Receivable' are not "
        "auto-classified by the generic suffix patterns"
    )


# --- P4: uploaded COA metadata still wins over the pattern fallback -------


def p4_uploaded_coa_metadata_wins_over_pattern():
    """When the firm uploaded a COA with explicit account_type, the
    persisted value must take precedence over the new name-pattern
    fallback. This is the production-safety invariant: an explicit
    operator-supplied type always wins."""
    # Account name pattern would resolve "Rent Expense" to
    # Expense/RentOrLeaseOfBuildings — but if the operator told us in the
    # COA upload that it's actually a different sub-type, that wins.
    row = {
        "account_number": "5000",
        "account_name": "Rent Expense",
        # Operator-supplied (e.g. from the COA CSV's account_type column).
        "account_type": "Expense",
        "detail_type": "Utilities",
    }
    result = map_pclaw_account_to_qbo_type(row)
    assert result["decision"] in ("ok", "warn"), result
    # The candidate loop tries detail_type first, so the explicit
    # "Utilities" sub-type from the upload must surface unchanged.
    assert result["account_type"] == "Expense", result
    assert result["detail_type"] == "Utilities", result
    assert result["match_hint"] == "detail_type", result
    print(
        "P4 OK: uploaded COA account_type / detail_type wins over the "
        "name-pattern fallback (operator override always preserved)"
    )


# --- P5: existing QBO accounts not duplicated even after pattern resolves ---


def p5_existing_qbo_accounts_not_duplicated_via_pattern():
    """End-to-end regression: pattern-tier resolutions still flow through
    the create-missing dedupe logic, so an account that already exists in
    QBO is never recreated even when the pattern would have classified it.
    """
    # Local fake QBOClient + helpers cribbed from the existing
    # smoke_account_mapping_create_missing fixture, kept inline to keep
    # this test file self-contained.
    class _FakeQBO:
        def __init__(self, accounts):
            self._accounts = list(accounts)
            self.created_payloads = []

        def get_accounts(self):
            return {"QueryResponse": {"Account": list(self._accounts)}}

        def find_account_by_acctnum(self, num):
            for a in self._accounts:
                if str(a.get("AcctNum") or "") == str(num):
                    return a
            return None

        def find_account_by_name(self, name):
            tgt = (name or "").strip().lower()
            for a in self._accounts:
                if str(a.get("Name") or "").strip().lower() == tgt:
                    return a
            return None

        def create_account(self, payload):
            self.created_payloads.append(payload)
            return {"Account": {
                "Id": f"NEW-{len(self.created_payloads)}",
                "Name": payload.get("Name"),
                "AcctNum": payload.get("AcctNum"),
                "AccountType": payload.get("AccountType"),
                "AccountSubType": payload.get("AccountSubType"),
            }}

    client = appmod.app.test_client()
    _signup_and_login(client, "p5@example.test", "P5 LLP")
    user = appmod.db.get_user_by_email("p5@example.test")
    job_id = "job_p5_dedupe"
    appmod.db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company="P5 LLP", source_file="x.csv",
        encrypted_file="p5_missing.enc",
        file_sha256="0" * 64, status="uploaded",
    )
    snapshot = [
        # Already in QBO by AcctNum — must NOT be recreated.
        {"number": "5000", "name": "Rent Expense"},
        # Genuinely missing — pattern resolves it, so it WILL be created.
        {"number": "5100", "name": "Office Supplies Expense"},
        # Already in QBO by Name (different AcctNum on QBO side) —
        # must NOT be recreated.
        {"number": "4000", "name": "Legal Fees Income"},
    ]
    appmod.db.save_job_state(job_id, {
        "status": "uploaded", "pclaw_accounts": snapshot,
    })
    appmod.qbo_connections[job_id] = {
        "realm_id": "R-P5",
        "access_token_enc": appmod.encrypt_token("fake-access"),
        "refresh_token_enc": appmod.encrypt_token("fake-refresh"),
        "company_name": "P5 LLP", "legal_name": "P5 LLP",
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }
    appmod.jobs.pop(job_id, None)

    qbo = _FakeQBO([
        {"Id": "Q1", "Name": "Rent Expense", "AcctNum": "5000",
         "AccountType": "Expense"},
        # Different AcctNum on the QBO side — name match dedupes.
        {"Id": "Q2", "Name": "Legal Fees Income", "AcctNum": "9999",
         "AccountType": "Income"},
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
    created_acctnums = [p.get("AcctNum") for p in qbo.created_payloads]
    created_names = [p.get("Name") for p in qbo.created_payloads]
    assert "5000" not in created_acctnums, (
        "Rent Expense (AcctNum 5000) already in QBO, must not be recreated: "
        f"{created_acctnums}"
    )
    assert "Legal Fees Income" not in created_names, (
        "Legal Fees Income matched by name in QBO, must not be recreated: "
        f"{created_names}"
    )
    assert "5100" in created_acctnums, (
        f"Office Supplies Expense (5100) is missing and pattern-resolved — "
        f"it should have been created. Created: {created_acctnums}"
    )
    # And the created payload carries the deterministic QBO mapping.
    office = next(
        p for p in qbo.created_payloads if p.get("AcctNum") == "5100"
    )
    assert office["AccountType"] == "Expense", office
    assert office["AccountSubType"] == (
        "OfficeGeneralAdministrativeExpenses"
    ), office
    print(
        "P5 OK: existing QBO accounts not duplicated; pattern-resolved "
        "missing accounts are created with the canonical AccountType / "
        "AccountSubType"
    )


def main():
    p1_demo_blocker_names_resolve_safely()
    p2_ambiguous_names_still_block()
    p3_payable_receivable_never_matched_by_generic_pattern()
    p4_uploaded_coa_metadata_wins_over_pattern()
    p5_existing_qbo_accounts_not_duplicated_via_pattern()
    print(
        "\nALL CREATE-MISSING SAFE-NAME-PATTERN SMOKE TESTS PASSED"
    )


if __name__ == "__main__":
    main()
