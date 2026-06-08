"""Smoke tests for the June 5 Cutovr meeting action items 1-5.

These cover the customer-facing fixes agreed in that meeting:

  Item 1/2 — Upload warnings vs blockers
    J1  A clean GL upload with only a non-blocking note reports a
        *success*, not a red "uploaded with warnings" error.
    J2  A GL upload with a genuine blocker (e.g. no transactions /
        missing columns) reports an error whose message names the
        specific problem in plain English, not a vague "review the
        preflight checklist".
    J3  A Chart of Accounts upload with a non-blocking warning is a
        success with a calm note appended — never category=error.

  Item 3 — Back navigation
    J4  The Match-accounts stage back link routes to the upload step
        with the ?step=upload intent so the dashboard renders the clean
        single-action upload view, and the URL still anchors at #intake.
    J5  /dashboard?step=upload renders the upload form WITHOUT the busy
        post-Step-2 workspace panel even after the workflow has advanced.

  Item 4 — Account-mapping rejection clarity
    J6  Every unmatched row carries a plain-English ``unmatched_reason``
        with a concrete next action; matched / saved / system rows do not.

  Item 5 — Review grouping
    J7  build_dry_run_preview returns ``sample_groups`` — one block per
        PCLaw reference, each with its own lines + debit/credit totals +
        balanced flag — so the review screen needs no VLOOKUP.

Run from project root:

    python3 tests/smoke_june5_items.py
"""

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="june5_uploads_")
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="june5_outputs_")
os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-june5-items")

import app as appmod  # noqa: E402
import customer_workflow as cw  # noqa: E402
import migration_quality as mq  # noqa: E402


def _signup_login(client, email):
    pwd = "passw0rd!1234"
    client.post("/signup", data={
        "firm_name": "June5 Firm", "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    client.post("/login", data={"email": email, "password": pwd},
                follow_redirects=False)


def _upload(client, text, filename, report_type="general_ledger"):
    return client.post(
        "/upload",
        data={
            "company_name": "June5 Co",
            "report_type": report_type,
            "ledger_file": (io.BytesIO(text.encode("utf-8")), filename),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )


_CLEAN_GL = (
    "transaction_id,account_number,account_name,date,debit,credit,memo\n"
    "T1,1000,Cash,2024-01-01,100.00,0.00,GB Payroll run\n"
    "T1,4000,Fees,2024-01-01,0.00,100.00,GB Payroll run\n"
    "T2,1000,Cash,2024-01-02,50.00,0.00,Client deposit\n"
    "T2,2000,Trust,2024-01-02,0.00,50.00,Client deposit\n"
)


def _flashes_after(client):
    """Return the list of (category, message) flashes pending in session."""
    with client.session_transaction() as sess:
        return list(sess.get("_flashes") or [])


def j1_clean_gl_is_success():
    client = appmod.app.test_client()
    _signup_login(client, "j1@example.test")
    _upload(client, _CLEAN_GL, "clean_gl.csv", report_type="general_ledger")
    flashes = _flashes_after(client)
    assert flashes, "upload should flash a status"
    cat, msg = flashes[-1]
    assert cat == "success", (cat, msg)
    assert "uploaded with warnings" not in msg.lower(), msg
    print("J1 OK: clean GL upload flashes success, not a warning-error")


def j2_blocker_gl_names_problem():
    client = appmod.app.test_client()
    _signup_login(client, "j2@example.test")
    empty_gl = "transaction_id,account_number,account_name,date,debit,credit,memo\n"
    _upload(client, empty_gl, "empty_gl.csv", report_type="general_ledger")
    flashes = _flashes_after(client)
    assert flashes, "upload should flash a status"
    cat, msg = flashes[-1]
    assert cat == "error", (cat, msg)
    # Plain-English, names the specific problem (no transactions).
    assert "transactions" in msg.lower(), msg
    assert "preflight" not in msg.lower(), msg
    print("J2 OK: GL blocker flashes a specific, plain-English error")


def j3_coa_warning_is_success_with_note():
    client = appmod.app.test_client()
    _signup_login(client, "j3@example.test")
    # COA missing the name column → preflight not ready, but never blocks.
    coa = "account_number\n1000\n4000\n"
    _upload(client, coa, "coa.csv", report_type="chart_of_accounts")
    flashes = _flashes_after(client)
    assert flashes, "upload should flash a status"
    cat, msg = flashes[-1]
    assert cat == "success", (cat, msg)
    assert "Account list uploaded" in msg, msg
    print("J3 OK: COA with a warning flashes success + calm note (not error)")


def j4_match_back_link_targets_clean_upload():
    label, url = cw._stage_back_link(cw.STAGE_MATCH, url_for=None)
    assert label == "Back to Step 2: Upload reports", label
    assert "step=upload" in url, url
    assert url.endswith("#intake"), url
    print("J4 OK: Match back link -> ?step=upload#intake (clean upload view)")


def j5_dashboard_step_upload_hides_workspace():
    client = appmod.app.test_client()
    _signup_login(client, "j5@example.test")
    r = client.get("/dashboard?step=upload")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "Upload your PCLaw reports" in body, "upload form missing"
    assert "dashboard-workspace-panel" not in body, \
        "workspace panel must be hidden in the forced upload view"
    print("J5 OK: ?step=upload renders clean upload view, no workspace panel")


def j6_unmatched_rows_carry_reason():
    pclaw = [
        {"number": "9999", "name": "Mystery Account"},
        {"number": "1000", "name": "Cash"},
    ]
    qbo = [{"Id": "10", "Name": "Cash", "AcctNum": "1000", "AccountType": "Bank"}]
    rows, summary = appmod._build_account_mapping_rows(
        pclaw_accounts=pclaw, qbo_accounts=qbo, saved_by_key={},
    )
    by_name = {r["pclaw_name"]: r for r in rows}
    mystery = by_name["Mystery Account"]
    cash = by_name["Cash"]
    assert mystery["is_suggestion"] is False and not mystery["is_saved"], mystery
    assert mystery["unmatched_reason"], "unmatched row must explain itself"
    assert "QuickBooks" in mystery["unmatched_reason"], mystery["unmatched_reason"]
    # The matched (auto-suggested) Cash row needs no reason.
    assert cash["is_suggestion"] is True, cash
    assert cash["unmatched_reason"] == "", cash
    print("J6 OK: unmatched rows carry a plain-English reason; matched rows don't")


def j7_preview_groups_by_reference():
    qbo = {"QueryResponse": {"Account": [
        {"Id": "10", "Name": "Cash", "AcctNum": "1000", "AccountType": "Bank"},
        {"Id": "40", "Name": "Fees", "AcctNum": "4000", "AccountType": "Income"},
        {"Id": "20", "Name": "Trust", "AcctNum": "2000",
         "AccountType": "Other Current Liability"},
    ]}}
    rows = [
        {"transaction_id": "T1", "account_number": "1000", "account_name": "Cash",
         "date": "2024-01-01", "debit": "100.00", "credit": "0.00",
         "description": "GB Payroll run"},
        {"transaction_id": "T1", "account_number": "4000", "account_name": "Fees",
         "date": "2024-01-01", "debit": "0.00", "credit": "100.00",
         "description": "GB Payroll run"},
        {"transaction_id": "T2", "account_number": "1000", "account_name": "Cash",
         "date": "2024-01-02", "debit": "50.00", "credit": "0.00",
         "description": "Client deposit"},
        {"transaction_id": "T2", "account_number": "2000", "account_name": "Trust",
         "date": "2024-01-02", "debit": "0.00", "credit": "50.00",
         "description": "Client deposit"},
    ]
    preview = mq.build_dry_run_preview(rows, qbo)
    groups = preview["sample_groups"]
    assert len(groups) == 2, groups
    g1 = next(g for g in groups if g["reference"] == "T1")
    assert g1["line_count"] == 2, g1
    assert g1["debits"] == "100.00" and g1["credits"] == "100.00", g1
    assert g1["balanced"] is True, g1
    assert g1["description"] == "GB Payroll run", g1
    # Lines are nested in the group — no VLOOKUP needed.
    accounts = {l["account"] for l in g1["lines"]}
    assert any("Cash" in a for a in accounts), accounts
    assert any("Fees" in a for a in accounts), accounts
    print("J7 OK: preview returns sample_groups (one balanced block per reference)")


def main():
    j1_clean_gl_is_success()
    j2_blocker_gl_names_problem()
    j3_coa_warning_is_success_with_note()
    j4_match_back_link_targets_clean_upload()
    j5_dashboard_step_upload_hides_workspace()
    j6_unmatched_rows_carry_reason()
    j7_preview_groups_by_reference()
    print("\nAll June 5 items 1-5 smoke tests passed.")


if __name__ == "__main__":
    main()
