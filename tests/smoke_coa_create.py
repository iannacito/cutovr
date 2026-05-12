"""Smoke tests for Chart of Accounts QBO creation.

Run from project root:

    python3 tests/smoke_coa_create.py

Covers:
  C1  Type-mapping table maps safe PCLaw account types to QBO
      AccountType/AccountSubType pairs.
  C2  Type-mapping refuses to guess ambiguous categories ("Asset" alone,
      garbage values) — returns decision='blocked'.
  C3  Special accounts (AR/AP, Trust, Bank, RetainedEarnings) carry
      warnings even when mappable.
  C4  build_create_plan keeps matched rows out of to_create.
  C5  Existing-account match: account already in QBO triggers no QBO
      create call when apply runs.
  C6  New account create: confirmation phrase triggers create_account.
  C7  No create without confirmation: POST /coa-apply without the
      phrase short-circuits before any QBO write.
  C8  Unknown/ambiguous type blocked: garbage type means apply route
      refuses to write.
  C9  QBO write not called for Trial Balance / Trust Listing (regression
      check on /coa-confirm + /coa-apply).
  C10 Checklist status promotes to "Chart of Accounts created in
      QuickBooks" after a successful apply.
  C11 GL import flow remains unchanged after the new routes land.
"""

import io
import os
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-coa-create")

import coa_apply  # noqa: E402
import report_types as rt  # noqa: E402
import cutover_workflow as cw  # noqa: E402
import app as appmod  # noqa: E402

COA_CSV = (ROOT / "test_data" / "01_chart_of_accounts.csv").read_bytes()
TB_CSV = (ROOT / "test_data" / "03_trial_balance.csv").read_bytes()
TRUST_CSV = (ROOT / "test_data" / "05_trust_listing.csv").read_bytes()


def _signup_and_login(client, email="coa-create@test.example"):
    pwd = "correct-horse-battery-staple"
    client.post(
        "/signup",
        data={
            "firm_name": "COA Create LLP",
            "email": email,
            "password": pwd,
            "confirm_password": pwd,
        },
        follow_redirects=True,
    )
    client.post(
        "/login",
        data={"email": email, "password": pwd},
        follow_redirects=True,
    )


def _upload(client, body, filename, report_type=""):
    return client.post(
        "/upload",
        data={
            "company_name": "Smoke Firm COA",
            "email": "ops@smoke.example",
            "report_type": report_type,
            "ledger_file": (io.BytesIO(body), filename),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def _last_job():
    return max(appmod.jobs.values(), key=lambda j: j.get("created_at", ""))


def _install_fake_qbo(job_id, *, existing_accounts=None):
    """Wire a fake QBO connection so _get_qbo_client returns a usable client.

    Patches the in-memory qbo_connections dict + a stub QBOClient that
    answers query() / create_account() / get_accounts() without HTTP.
    """
    existing_accounts = existing_accounts or []

    class _FakeClient:
        def __init__(self):
            self.created_payloads = []
            self.realm_id = "REALM-TEST"
            self.last_intuit_tid = None

        def get_accounts(self):
            return {"QueryResponse": {"Account": list(existing_accounts)}}

        def query(self, sql):
            return {"QueryResponse": {"Account": list(existing_accounts)}}

        def create_account(self, payload):
            self.created_payloads.append(payload)
            new_id = str(100 + len(self.created_payloads))
            return {"Account": {
                "Id": new_id,
                "Name": payload.get("Name"),
                "AcctNum": payload.get("AcctNum"),
                "AccountType": payload.get("AccountType"),
                "AccountSubType": payload.get("AccountSubType"),
                "Active": payload.get("Active", True),
            }}

    fake = _FakeClient()
    # Inject the in-memory connection that the route layer will find via
    # _get_qbo_connection.
    appmod.qbo_connections[job_id] = {
        "realm_id": "REALM-TEST",
        "access_token_enc": "fake",
        "refresh_token_enc": "fake",
        "expires_at": "2099-01-01T00:00:00",
        "company_name": "Test QBO Co",
        "legal_name": "Test QBO Co",
        "country": "CA",
        "company_info_error": None,
        "connected_at": "2026-01-01T00:00:00",
    }
    # Patch helpers so _get_qbo_client returns our fake without touching
    # encryption / refresh logic.
    return fake


# --- Pure unit tests --------------------------------------------------------


def c1_type_mapping_safe():
    bank = coa_apply.map_pclaw_account_to_qbo_type({
        "account_name": "Operating Bank",
        "account_type": "Asset",
        "detail_type": "Bank",
    })
    assert bank["account_type"] == "Bank", bank
    assert bank["detail_type"] == "Checking", bank
    assert bank["decision"] in ("ok", "warn"), bank

    ar = coa_apply.map_pclaw_account_to_qbo_type({
        "account_name": "Accounts Receivable",
        "account_type": "Receivable",
        "detail_type": "Accounts Receivable",
    })
    assert ar["account_type"] == "Accounts Receivable", ar
    assert ar["detail_type"] == "AccountsReceivable", ar
    assert ar["decision"] == "warn", ar  # auto-provisioned warning
    assert ar["warnings"], ar

    exp = coa_apply.map_pclaw_account_to_qbo_type({
        "account_name": "Rent Expense",
        "account_type": "Expense",
        "detail_type": "Rent or Lease of Buildings",
    })
    assert exp["account_type"] == "Expense", exp
    assert exp["detail_type"] == "RentOrLeaseOfBuildings", exp
    print("C1 type mapping (safe): OK")


def c2_type_mapping_blocks_ambiguous():
    bad = coa_apply.map_pclaw_account_to_qbo_type({
        "account_name": "Mystery Account",
        "account_type": "MysteryCategory",
        "detail_type": "",
    })
    assert bad["decision"] == "blocked", bad
    assert bad["blocked_reason"], bad
    assert bad["account_type"] is None, bad

    # Bare "Asset" alone is also too broad.
    broad = coa_apply.map_pclaw_account_to_qbo_type({
        "account_name": "Some Asset",
        "account_type": "Asset",
        "detail_type": "",
    })
    assert broad["decision"] == "blocked", broad
    assert broad["account_type"] is None, broad
    print("C2 type mapping (ambiguous blocked): OK")


def c3_type_mapping_special_warnings():
    trust_bank = coa_apply.map_pclaw_account_to_qbo_type({
        "account_name": "Trust Bank",
        "account_type": "Trust Bank",
        "detail_type": "Trust Account",
    })
    assert trust_bank["decision"] == "warn", trust_bank
    assert any("trust" in w.lower() for w in trust_bank["warnings"]), trust_bank

    retained = coa_apply.map_pclaw_account_to_qbo_type({
        "account_name": "Retained Earnings",
        "account_type": "Equity",
        "detail_type": "Retained Earnings",
    })
    assert retained["decision"] == "warn", retained
    assert any("retained" in w.lower() for w in retained["warnings"]), retained
    print("C3 type mapping (special warnings): OK")


def c4_build_create_plan_keeps_matched():
    coa_rows = [
        {"account_number": "1000", "account_name": "Operating Bank",
         "account_type": "Asset", "detail_type": "Bank", "active": True},
        {"account_number": "9999", "account_name": "Brand New",
         "account_type": "Expense", "detail_type": "Office", "active": True},
        {"account_number": "ZZZZ", "account_name": "Mystery",
         "account_type": "MysteryCategory", "detail_type": "", "active": True},
    ]
    qbo_accounts = {"QueryResponse": {"Account": [
        {"Id": "1", "Name": "Operating Bank", "AcctNum": "1000", "AccountType": "Bank"},
    ]}}
    preview = rt.build_coa_dry_run_preview(coa_rows, qbo_accounts)
    plan = coa_apply.build_create_plan(coa_rows, preview)
    assert len(plan.matched) == 1
    assert len(plan.to_create) == 1, plan.to_create
    assert plan.to_create[0].account_number == "9999"
    assert len(plan.blocked) == 1, plan.blocked
    assert plan.blocked[0].account_number == "ZZZZ"
    assert plan.has_blockers is True
    print("C4 build_create_plan: OK")


# --- Flask-client tests -----------------------------------------------------


def _run_flask_tests():
    appmod.app.config["TESTING"] = True
    appmod.app.config["WTF_CSRF_ENABLED"] = False
    with appmod.app.test_client() as client:
        _signup_and_login(client)

        # Upload a COA file.
        resp = _upload(client, COA_CSV, "coa.csv", report_type="chart_of_accounts")
        assert resp.status_code in (302, 303)
        coa_job = _last_job()
        assert coa_job["report_type"] == rt.REPORT_CHART_OF_ACCOUNTS
        coa_job_id = coa_job["id"]

        # C5 — Existing-account match: every PCLaw account already exists.
        existing = [
            {"Id": "1", "Name": "Operating Bank", "AcctNum": "1000", "AccountType": "Bank"},
            {"Id": "2", "Name": "Trust Bank", "AcctNum": "1010", "AccountType": "Bank"},
            {"Id": "3", "Name": "Accounts Receivable", "AcctNum": "1100",
             "AccountType": "Accounts Receivable"},
            {"Id": "4", "Name": "Unbilled Disbursements", "AcctNum": "1200",
             "AccountType": "Other Current Asset"},
            {"Id": "5", "Name": "Accounts Payable", "AcctNum": "2000",
             "AccountType": "Accounts Payable"},
            {"Id": "6", "Name": "Client Trust Liability", "AcctNum": "2100",
             "AccountType": "Other Current Liability"},
            {"Id": "7", "Name": "Owner Equity", "AcctNum": "3000",
             "AccountType": "Equity"},
            {"Id": "8", "Name": "Legal Fees Revenue", "AcctNum": "4000",
             "AccountType": "Income"},
            {"Id": "9", "Name": "Disbursement Recovery", "AcctNum": "4100",
             "AccountType": "Income"},
            {"Id": "10", "Name": "Rent Expense", "AcctNum": "5000",
             "AccountType": "Expense"},
            {"Id": "11", "Name": "Office Expense", "AcctNum": "5100",
             "AccountType": "Expense"},
            {"Id": "12", "Name": "Filing Fees Expense", "AcctNum": "5200",
             "AccountType": "Expense"},
        ]
        fake = _install_fake_qbo(coa_job_id, existing_accounts=existing)

        # Patch the token-refresh path so _get_qbo_client returns the fake.
        with mock.patch.object(appmod, "_get_qbo_client",
                                return_value=(fake, appmod.qbo_connections[coa_job_id])):
            # POST with confirmation but everything matches — no creates.
            r = client.post(
                f"/jobs/{coa_job_id}/coa-apply",
                data={"confirm_create": "CREATE ACCOUNTS"},
                follow_redirects=False,
            )
            assert r.status_code in (200, 302, 303), r.status_code
            assert fake.created_payloads == [], (
                "create_account should not be called when every row matches"
            )
        print("C5 existing-account match no create: OK")

        # C6 — New account create.
        # Reset by recreating fake with only some accounts present, so
        # several PCLaw rows are 'would_create'.
        fake2 = _install_fake_qbo(coa_job_id, existing_accounts=[
            {"Id": "1", "Name": "Operating Bank", "AcctNum": "1000", "AccountType": "Bank"},
        ])
        with mock.patch.object(appmod, "_get_qbo_client",
                                return_value=(fake2, appmod.qbo_connections[coa_job_id])):
            r = client.post(
                f"/jobs/{coa_job_id}/coa-apply",
                data={"confirm_create": "CREATE ACCOUNTS"},
                follow_redirects=False,
            )
            assert r.status_code == 200, (r.status_code, r.data[:300])
            assert len(fake2.created_payloads) >= 5, (
                f"expected several creates, got {len(fake2.created_payloads)}"
            )
            # AcctNum + AccountType present on each payload.
            for p in fake2.created_payloads:
                assert "AcctNum" in p, p
                assert p.get("AccountType"), p
        # Job should now have coa_create_history.
        refreshed = appmod.jobs[coa_job_id]
        assert refreshed.get("coa_create_history"), "history not recorded"
        last = refreshed["coa_create_history"][-1]
        assert last["created_count"] == len(fake2.created_payloads)
        assert "COA" in refreshed["status"]
        print("C6 new account create: OK")

        # C7 — No create without confirmation.
        fake3 = _install_fake_qbo(coa_job_id, existing_accounts=[])
        with mock.patch.object(appmod, "_get_qbo_client",
                                return_value=(fake3, appmod.qbo_connections[coa_job_id])):
            r = client.post(
                f"/jobs/{coa_job_id}/coa-apply",
                data={"confirm_create": "nope"},
                follow_redirects=False,
            )
            assert r.status_code in (302, 303)
            assert fake3.created_payloads == [], (
                "create_account must not be called without typed confirmation"
            )
        print("C7 no create without confirmation: OK")

        # C8 — Unknown/ambiguous type blocked. Build a tiny COA CSV with
        # a row that has no recognised type.
        bad_coa = (
            b"account_number,account_name,account_type,qbo_suggested_detail_type\n"
            b"9000,Mystery Garbage,WeirdCategoryXYZ,\n"
            b"9100,Another Mystery,,,\n"
        )
        _upload(client, bad_coa, "bad_coa.csv", report_type="chart_of_accounts")
        bad_job = _last_job()
        bad_id = bad_job["id"]
        fake_bad = _install_fake_qbo(bad_id, existing_accounts=[])
        with mock.patch.object(appmod, "_get_qbo_client",
                                return_value=(fake_bad, appmod.qbo_connections[bad_id])):
            r = client.post(
                f"/jobs/{bad_id}/coa-apply",
                data={"confirm_create": "CREATE ACCOUNTS"},
                follow_redirects=False,
            )
            # Plan has blockers -> route refuses to write, redirects back.
            assert r.status_code in (302, 303)
            assert fake_bad.created_payloads == [], (
                "create_account must not be called when plan has blocked rows"
            )
        print("C8 unknown/ambiguous type blocked: OK")

        # C9 — QBO write not called for Trial Balance / Trust Listing.
        _upload(client, TB_CSV, "tb.csv", report_type="trial_balance")
        tb_job = _last_job()
        tb_id = tb_job["id"]
        fake_tb = _install_fake_qbo(tb_id, existing_accounts=[])
        with mock.patch.object(appmod, "_get_qbo_client",
                                return_value=(fake_tb, appmod.qbo_connections[tb_id])):
            # The route should bounce a TB job out before any plan is built.
            r = client.post(
                f"/jobs/{tb_id}/coa-apply",
                data={"confirm_create": "CREATE ACCOUNTS"},
                follow_redirects=False,
            )
            assert r.status_code in (302, 303), r.status_code
            assert fake_tb.created_payloads == []
            r2 = client.get(f"/jobs/{tb_id}/coa-confirm", follow_redirects=False)
            assert r2.status_code in (302, 303)
            assert fake_tb.created_payloads == []

        _upload(client, TRUST_CSV, "trust.csv", report_type="trust_listing")
        trust_job = _last_job()
        trust_id = trust_job["id"]
        fake_tr = _install_fake_qbo(trust_id, existing_accounts=[])
        with mock.patch.object(appmod, "_get_qbo_client",
                                return_value=(fake_tr, appmod.qbo_connections[trust_id])):
            r = client.post(
                f"/jobs/{trust_id}/coa-apply",
                data={"confirm_create": "CREATE ACCOUNTS"},
                follow_redirects=False,
            )
            assert r.status_code in (302, 303)
            assert fake_tr.created_payloads == []
        print("C9 non-COA reports never trigger create: OK")

        # C10 — Checklist promotes when COA created.
        # Build the checklist from the same firm's jobs.
        items = cw.build_checklist(
            None,
            list(appmod.jobs.values()),
            has_qbo_connection=True,
            account_mapping_count=0,
        )
        coa_item = next(i for i in items if i.key == cw.STEP_COA_UPLOAD)
        assert coa_item.status == cw.STATUS_COMPLETE, coa_item
        assert "created" in coa_item.label.lower() or "created" in coa_item.summary.lower()
        print("C10 checklist promoted to complete: OK")


def c11_gl_flow_unchanged():
    """Regression: import-to-qbo route still refuses non-GL types in the
    same way it did before this PR (i.e. our new routes did not alter
    the existing safety gate)."""
    appmod.app.config["TESTING"] = True
    appmod.app.config["WTF_CSRF_ENABLED"] = False
    with appmod.app.test_client() as client:
        _signup_and_login(client, email="gl-regression@test.example")
        _upload(client, COA_CSV, "coa.csv", report_type="chart_of_accounts")
        coa_job = _last_job()
        # The /import-to-qbo route on a COA job should redirect (block).
        with mock.patch.object(appmod.QBOClient, "create_journal_entry") as m_create:
            r = client.post(
                f"/jobs/{coa_job['id']}/import-to-qbo",
                data={"confirm_import": "IMPORT"},
                follow_redirects=False,
            )
            assert r.status_code in (302, 303)
            assert not m_create.called, "GL safety gate must still block non-GL"
    print("C11 GL import gate unchanged for non-GL: OK")


def main():
    c1_type_mapping_safe()
    c2_type_mapping_blocks_ambiguous()
    c3_type_mapping_special_warnings()
    c4_build_create_plan_keeps_matched()
    _run_flask_tests()
    c11_gl_flow_unchanged()
    print("\nAll COA-create smoke tests passed.")


if __name__ == "__main__":
    main()
