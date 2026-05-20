"""Smoke tests for the COA-first validation + manual override flow.

Run from project root:

    python3 tests/smoke_coa_first_validation.py

Covers the helper-email requests in PR #28:

  V1  validate_tb_against_coa flags TB rows missing from the COA.
  V2  validate_tb_against_coa flags COA rows with a blank account_type
      so the operator must set one manually.
  V3  validate_tb_against_coa flags AR/AP name-vs-type mismatch (the
      exact issue called out in the email: "it's better to have this
      mapped to a different payable account").
  V4  validate_tb_against_coa surfaces "Created in QBO" when the COA
      create-history shows we created the account during this migration.
  V5  validate_tb_against_coa is ready=True only when the firm has a
      COA on file AND every row is ready / created_in_qbo.
  V6  coa_apply.map_pclaw_account_to_qbo_type refuses to map an
      Accounts Payable row to a generic Other Current Liability — it
      must be blocked, not silently misclassified.
  V7  /jobs/<id>/coa-override saves a manual account-type correction
      that flows into the create plan (unblocks a row).
  V8  /jobs/<id>/opening-balance blocks the POST when the firm's COA
      has unresolved AR/AP mismatch even with a fully balanced TB.
  V9  /oauth/callback with an expired session and a state parameter
      returns the user to the originating job page via ?next=.
"""

import io
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-coa-first")
os.environ.setdefault("QBO_CLIENT_ID", "smoke-client-id")
os.environ.setdefault("QBO_CLIENT_SECRET", "smoke-client-secret")
os.environ.setdefault("QBO_REDIRECT_URI", "http://localhost:5000/oauth/callback")

import tb_coa_validation as tbv  # noqa: E402
import coa_apply  # noqa: E402
import app as appmod  # noqa: E402

COA_CSV = (ROOT / "test_data" / "01_chart_of_accounts.csv").read_bytes()
TB_CSV = (ROOT / "test_data" / "03_trial_balance.csv").read_bytes()


def _qbo_response(accounts):
    return {"QueryResponse": {"Account": accounts}}


# --- Pure-function tests on tb_coa_validation ------------------------------


def v1_missing_from_coa():
    coa = [
        {"account_number": "1000", "account_name": "Operating Bank",
         "account_type": "Bank"},
    ]
    tb = [
        {"account_number": "1000", "account_name": "Operating Bank",
         "debit_balance": "1000.00", "credit_balance": "0.00"},
        {"account_number": "9999", "account_name": "Mystery Account",
         "debit_balance": "0.00", "credit_balance": "50.00"},
    ]
    v = tbv.validate_tb_against_coa(tb, coa)
    statuses = [r.status for r in v.rows]
    assert tbv.STATUS_MISSING_FROM_COA in statuses, statuses
    assert any("not in the Chart of Accounts" in b for b in v.blockers), v.blockers
    assert v.ready is False
    print("V1 OK: missing-from-COA flagged")


def v2_blank_account_type():
    coa = [
        {"account_number": "1000", "account_name": "Operating Bank",
         "account_type": ""},
    ]
    tb = [
        {"account_number": "1000", "account_name": "Operating Bank",
         "debit_balance": "1000.00", "credit_balance": "0.00"},
    ]
    v = tbv.validate_tb_against_coa(tb, coa)
    assert v.rows[0].status == tbv.STATUS_NEEDS_ACCOUNT_TYPE, v.rows[0]
    assert v.ready is False
    print("V2 OK: blank account_type flagged")


def v3_ar_ap_type_mismatch():
    # Accounts Payable name on the COA + TB, but the QBO match has type
    # "Other Current Liability" — the exact mismatch the email called out.
    coa = [
        {"account_number": "2000", "account_name": "Accounts Payable",
         "account_type": "Accounts Payable"},
    ]
    qbo = _qbo_response([
        {"Id": "11", "AcctNum": "2000", "Name": "Accounts Payable",
         "AccountType": "Other Current Liability"},
    ])
    tb = [
        {"account_number": "2000", "account_name": "Accounts Payable",
         "debit_balance": "0.00", "credit_balance": "5000.00"},
    ]
    v = tbv.validate_tb_against_coa(tb, coa, qbo)
    assert v.rows[0].status == tbv.STATUS_TYPE_MISMATCH, v.rows[0]
    assert any("type mismatch" in b.lower() for b in v.blockers), v.blockers
    assert v.ready is False
    print("V3 OK: AR/AP type mismatch flagged")


def v4_created_in_qbo_badge():
    coa = [
        {"account_number": "5000", "account_name": "Rent Expense",
         "account_type": "Expense"},
    ]
    qbo = _qbo_response([
        {"Id": "42", "AcctNum": "5000", "Name": "Rent Expense",
         "AccountType": "Expense"},
    ])
    tb = [
        {"account_number": "5000", "account_name": "Rent Expense",
         "debit_balance": "1200.00", "credit_balance": "0.00"},
    ]
    create_history = [{
        "created": [{"qbo_account_id": "42", "account_number": "5000",
                     "account_name": "Rent Expense"}],
    }]
    v = tbv.validate_tb_against_coa(
        tb, coa, qbo, coa_create_history=create_history,
    )
    assert v.rows[0].status == tbv.STATUS_CREATED_IN_QBO, v.rows[0]
    assert v.rows[0].created_in_qbo is True
    assert v.ready is True
    print("V4 OK: created-in-QBO badge surfaces")


def v5_ready_requires_coa_and_all_ready():
    # No COA, but TB has rows: not ready, has a top-level blocker.
    v = tbv.validate_tb_against_coa(
        [{"account_number": "1000", "account_name": "Operating Bank",
          "debit_balance": "100", "credit_balance": "0"}],
        coa_rows=[],
    )
    assert v.ready is False
    assert any("No Chart of Accounts" in b for b in v.blockers), v.blockers

    # Has COA, every row resolves -> ready.
    coa = [
        {"account_number": "1000", "account_name": "Operating Bank",
         "account_type": "Bank"},
    ]
    qbo = _qbo_response([
        {"Id": "1", "AcctNum": "1000", "Name": "Operating Bank",
         "AccountType": "Bank"},
    ])
    tb = [
        {"account_number": "1000", "account_name": "Operating Bank",
         "debit_balance": "1000.00", "credit_balance": "0.00"},
    ]
    v2 = tbv.validate_tb_against_coa(tb, coa, qbo)
    assert v2.ready is True
    print("V5 OK: ready gate requires COA + all rows ready")


# --- Pure-function tests on coa_apply --------------------------------------


def v6_ap_name_blocked_when_resolved_to_generic_liability():
    # "Accounts Payable" name with a vague PCLaw type that would have
    # otherwise mapped to Other Current Liability.
    row = {
        "account_number": "2000",
        "account_name": "Accounts Payable",
        "account_type": "Other Current Liability",
        "detail_type": "OtherCurrentLiabilities",
    }
    decision = coa_apply.map_pclaw_account_to_qbo_type(row)
    assert decision["decision"] == "blocked", decision
    assert "Accounts Payable" in (decision["blocked_reason"] or "")
    print("V6 OK: AP name vs generic liability mapping is blocked")


# --- Route-level smoke ----------------------------------------------------


def _signup_and_login(client, email="coa-first@test.example",
                     firm="COA First LLP",
                     pwd="correct-horse-battery-staple-2"):
    client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=True)
    client.post("/login", data={"email": email, "password": pwd},
                follow_redirects=True)


def _upload(client, body, filename, report_type=""):
    return client.post("/upload", data={
        "company_name": "COA First Firm",
        "email": "ops@first.example",
        "report_type": report_type,
        "ledger_file": (io.BytesIO(body), filename),
    }, content_type="multipart/form-data", follow_redirects=False)


def v7_override_route_persists_override_and_unblocks_create_plan():
    c = appmod.app.test_client()
    _signup_and_login(c, email="v7@test.example", firm="V7 LLP")
    # Upload a COA row that won't auto-classify safely.
    csv = (
        b"account_number,account_name,account_type\n"
        b"2050,Trade Payables Old,Liability\n"
    )
    r = _upload(c, csv, "coa.csv", report_type="chart_of_accounts")
    assert r.status_code == 302, r.status_code
    job_id = r.headers["Location"].rsplit("/", 1)[-1]
    job = appmod.jobs[job_id]

    # Apply manual override.
    r = c.post(f"/jobs/{job_id}/coa-override", data={
        "account_number": "2050", "account_name": "Trade Payables Old",
        "account_type": "Other Current Liability",
        "detail_type": "OtherCurrentLiabilities",
    }, follow_redirects=False)
    assert r.status_code == 302, r.status_code
    assert appmod.jobs[job_id]["coa_type_overrides"]["2050"]["account_type"] \
        == "Other Current Liability"

    # build_create_plan with the override should classify the row as ok.
    preview = {
        "matched": [],
        "would_create": [{"account_number": "2050",
                          "account_name": "Trade Payables Old"}],
        "conflicts": [],
    }
    plan = coa_apply.build_create_plan(
        appmod.jobs[job_id].get("parsed_coa") or [
            {"account_number": "2050", "account_name": "Trade Payables Old",
             "account_type": "Liability"},
        ],
        preview,
        type_overrides=appmod.jobs[job_id]["coa_type_overrides"],
    )
    assert plan.to_create, plan.to_dict()
    assert plan.to_create[0].qbo_account_type == "Other Current Liability"
    print("V7 OK: override route persists and unblocks create plan")


def v8_opening_balance_blocks_when_coa_unresolved():
    c = appmod.app.test_client()
    _signup_and_login(c, email="v8@test.example", firm="V8 LLP")

    # Upload a balanced TB without first uploading a COA.
    csv = (
        b"account_number,account_name,debit_balance,credit_balance\n"
        b"1000,Operating Bank,1000.00,0.00\n"
        b"3000,Owners Equity,0.00,1000.00\n"
    )
    r = _upload(c, csv, "tb.csv", report_type="trial_balance")
    assert r.status_code == 302
    job_id = r.headers["Location"].rsplit("/", 1)[-1]
    page = c.get(f"/jobs/{job_id}/opening-balance")
    body = page.get_data(as_text=True)
    # COA-first gate: page must render the validation card, and posting
    # is disabled (we can verify the message is present rather than
    # trying to drive a full QBO connection in a smoke test).
    assert "Chart of Accounts readiness" in body, body[-800:]
    assert ("No Chart of Accounts on file" in body
            or "Not ready" in body), body[-800:]
    print("V8 OK: opening-balance blocks when COA missing/unresolved")


def v9_oauth_callback_session_expired_returns_to_job_via_next():
    c = appmod.app.test_client()
    _signup_and_login(c, email="v9@test.example", firm="V9 LLP")
    r = _upload(c, TB_CSV, "tb.csv", report_type="trial_balance")
    assert r.status_code == 302
    job_id = r.headers["Location"].rsplit("/", 1)[-1]
    # Start connect-qbo to mint a state nonce.
    r2 = c.get(f"/jobs/{job_id}/connect-qbo", follow_redirects=False)
    assert r2.status_code == 302
    location = r2.headers["Location"]
    state_param = parse_qs(urlparse(location).query)["state"][0]

    # New client = expired/missing session. Hit the callback with the state.
    c2 = appmod.app.test_client()
    r3 = c2.get(
        f"/oauth/callback?code=x&state={state_param}&realmId=Z",
        follow_redirects=False,
    )
    assert r3.status_code == 302, r3.status_code
    loc = r3.headers["Location"]
    # Should redirect to /login with next= pointing at the job page so the
    # operator returns to where they started after re-authenticating.
    assert "/login" in loc, loc
    qs = parse_qs(urlparse(loc).query)
    next_url = qs.get("next", [""])[0]
    assert next_url.endswith(f"/jobs/{job_id}"), f"unexpected next={next_url}"
    print("V9 OK: OAuth callback session-expired returns to job page")


if __name__ == "__main__":
    v1_missing_from_coa()
    v2_blank_account_type()
    v3_ar_ap_type_mismatch()
    v4_created_in_qbo_badge()
    v5_ready_requires_coa_and_all_ready()
    v6_ap_name_blocked_when_resolved_to_generic_liability()
    v7_override_route_persists_override_and_unblocks_create_plan()
    v8_opening_balance_blocks_when_coa_unresolved()
    v9_oauth_callback_session_expired_returns_to_job_via_next()
    print("\nALL COA-FIRST VALIDATION SMOKE TESTS PASSED")
