"""Smoke tests for the unmapped-account CTA classifier.

Run from project root:

    python3 tests/smoke_unmapped_account_guidance.py

Covers the customer-facing improvement to the GL import block: when QBO
has no match for one or more PCLaw accounts the operator now sees a
plain-English next step that depends on whether the firm has uploaded
their Account List and whether they've already finalized it in
QuickBooks. The classifier is pure so we exercise every branch directly
without spinning up the Flask app.

  U1  No Account List on file → CTA points at upload, secondary at
      manual matching.
  U2  Account List uploaded but not finalized in QBO → CTA points at
      Account List review (COA-first), language matches the helper-email
      spec ("Finish your Account List first, then try again").
  U3  Account List finalized but specific GL accounts are still
      unmatched → CTA points at the matching page, secondary at the
      Account List review.
  U4  Each missing account is listed with both its account number and
      name; accounts that appear on the uploaded COA are flagged so the
      operator knows the row is "almost ready" rather than truly new.
  U5  Environment label respects QBO_ENVIRONMENT: production deploys
      never say "sandbox", sandbox deploys include the sandbox hint.
  U6  Route-level smoke: stash a GL job with a unmapped-block guidance
      payload and assert the job-detail page renders the new banner
      (headline + each missing account + primary CTA) instead of the
      legacy "Cannot import" string.
"""

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-unmapped-cta")
os.environ.setdefault("QBO_CLIENT_ID", "smoke-client-id")
os.environ.setdefault("QBO_CLIENT_SECRET", "smoke-client-secret")
os.environ.setdefault("QBO_REDIRECT_URI", "http://localhost:5000/oauth/callback")

import unmapped_account_guidance as ug  # noqa: E402
import app as appmod  # noqa: E402


SAMPLE_KEYS = [
    "1300 Prepaid Expenses",
    "3100 Owner Draws",
    "4200 Consulting Revenue",
    "5060 Professional Fees",
    "5070 Insurance Expense",
]


def u1_no_account_list():
    g = ug.classify_unmapped_accounts(
        unmapped_keys=SAMPLE_KEYS,
        mapping_mode="number",
        coa_rows=[],
        coa_create_history=[],
        job_id="job-u1",
        company_name="Acme Sandbox Co",
        environment="sandbox",
    )
    assert g.action == ug.ACTION_UPLOAD_COA, g.action
    assert g.primary_cta_endpoint == "dashboard"
    assert g.primary_cta_kwargs.get("_anchor") == "intake"
    assert g.secondary_cta_endpoint == "account_mapping"
    assert g.secondary_cta_kwargs.get("job_id") == "job-u1"
    assert "Upload your Account List" in g.headline
    assert all(not a.in_coa for a in g.accounts)
    print("U1 OK: no Account List → upload CTA")


def u2_coa_uploaded_not_finalized():
    coa_rows = [
        {"account_number": "1300", "account_name": "Prepaid Expenses",
         "account_type": "Other Current Asset"},
        {"account_number": "3100", "account_name": "Owner Draws",
         "account_type": "Equity"},
        {"account_number": "4200", "account_name": "Consulting Revenue",
         "account_type": "Income"},
        {"account_number": "5060", "account_name": "Professional Fees",
         "account_type": "Expense"},
        {"account_number": "5070", "account_name": "Insurance Expense",
         "account_type": "Expense"},
    ]
    g = ug.classify_unmapped_accounts(
        unmapped_keys=SAMPLE_KEYS,
        mapping_mode="number",
        coa_rows=coa_rows,
        coa_create_history=[],
        job_id="job-u2",
        company_name="Acme Co",
        environment="production",
    )
    assert g.action == ug.ACTION_FINISH_COA, g.action
    # Helper-email mandated copy: this exact phrasing makes it
    # crystal-clear to non-accountants what to do next.
    assert "Finish your Account List first, then try again." in g.headline
    assert g.primary_cta_endpoint == "migration_checklist"
    assert g.secondary_cta_endpoint == "account_mapping"
    # Production deploy → no "sandbox" leaks.
    assert "sandbox" not in g.body.lower()
    # All accounts are listed on the COA so each row carries the badge.
    assert all(a.in_coa for a in g.accounts), [(a.number, a.in_coa) for a in g.accounts]
    print("U2 OK: COA uploaded but not finalized → review COA CTA")


def u3_coa_finalized_but_still_unmapped():
    # Firm pushed something to QBO already but the specific accounts the
    # GL references aren't on the uploaded COA (e.g. they're new accounts
    # that appeared after the initial COA snapshot).
    coa_rows = [
        {"account_number": "1000", "account_name": "Operating Bank",
         "account_type": "Bank"},
    ]
    create_history = [{"created_count": 7, "created": []}]
    g = ug.classify_unmapped_accounts(
        unmapped_keys=SAMPLE_KEYS,
        mapping_mode="number",
        coa_rows=coa_rows,
        coa_create_history=create_history,
        job_id="job-u3",
        company_name="Acme Co",
        environment="production",
    )
    assert g.action == ug.ACTION_MAP_ACCOUNTS, g.action
    assert g.primary_cta_endpoint == "account_mapping"
    assert g.primary_cta_kwargs.get("job_id") == "job-u3"
    assert g.secondary_cta_endpoint == "migration_checklist"
    # None of the missing accounts are on the uploaded COA in this scenario.
    assert all(not a.in_coa for a in g.accounts)
    print("U3 OK: COA finalized + still unmapped → match accounts CTA")


def u4_account_listing_includes_numbers_and_names():
    g = ug.classify_unmapped_accounts(
        unmapped_keys=SAMPLE_KEYS,
        mapping_mode="number",
        coa_rows=[
            {"account_number": "1300", "account_name": "Prepaid Expenses",
             "account_type": "Other Current Asset"},
        ],
        coa_create_history=[],
        job_id="job-u4",
        company_name="Acme Co",
        environment="sandbox",
    )
    # Sorted by key (which starts with the number).
    numbers = [a.number for a in g.accounts]
    assert numbers == ["1300", "3100", "4200", "5060", "5070"], numbers
    names = {a.name for a in g.accounts}
    assert {"Prepaid Expenses", "Owner Draws", "Consulting Revenue",
            "Professional Fees", "Insurance Expense"} == names, names
    # 1300 is on the uploaded COA, the rest aren't.
    in_coa = {a.number: a.in_coa for a in g.accounts}
    assert in_coa["1300"] is True
    assert in_coa["3100"] is False
    # Pretty rendering used by the flash + banner.
    payload = g.to_dict()
    displays = {a["display"] for a in payload["accounts"]}
    assert "1300 Prepaid Expenses" in displays
    assert "5070 Insurance Expense" in displays
    print("U4 OK: missing-account list carries numbers + names + in_coa badge")


def u5_environment_label_drops_sandbox_in_production():
    # Production with company name.
    g = ug.classify_unmapped_accounts(
        unmapped_keys=["1300 Prepaid Expenses"],
        mapping_mode="number",
        coa_rows=[],
        coa_create_history=[],
        job_id="job-u5a",
        company_name="Real Customer LLC",
        environment="production",
    )
    assert "sandbox" not in g.company_label.lower(), g.company_label
    assert "Real Customer LLC" in g.company_label

    # Sandbox without company name → still says "QuickBooks", flags sandbox.
    g2 = ug.classify_unmapped_accounts(
        unmapped_keys=["1300 Prepaid Expenses"],
        mapping_mode="number",
        coa_rows=[],
        coa_create_history=[],
        job_id="job-u5b",
        company_name=None,
        environment="sandbox",
    )
    assert "sandbox" in g2.company_label.lower(), g2.company_label

    # Empty environment string falls back to neutral language.
    g3 = ug.classify_unmapped_accounts(
        unmapped_keys=["1300 Prepaid Expenses"],
        mapping_mode="number",
        coa_rows=[],
        coa_create_history=[],
        job_id="job-u5c",
        company_name=None,
        environment="",
    )
    assert "sandbox" not in g3.company_label.lower()
    assert "QuickBooks" in g3.company_label
    print("U5 OK: environment label respects sandbox vs production")


# --- Route-level smoke ----------------------------------------------------


def _signup_and_login(client, email="unmapped@test.example",
                     firm="Unmapped LLP",
                     pwd="correct-horse-battery-staple-2"):
    client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=True)
    client.post("/login", data={"email": email, "password": pwd},
                follow_redirects=True)


def u6_job_detail_renders_structured_cta():
    c = appmod.app.test_client()
    _signup_and_login(c, email="u6@test.example", firm="U6 LLP")
    # Upload a tiny GL so we have a job we can stamp the guidance onto.
    gl = (
        b"transaction_id,transaction_date,account_number,account_name,"
        b"debit,credit\n"
        b"T1,2026-01-01,1300,Prepaid Expenses,100.00,0.00\n"
        b"T1,2026-01-01,1000,Operating Bank,0.00,100.00\n"
    )
    r = c.post("/upload", data={
        "company_name": "U6 Firm",
        "email": "ops@u6.example",
        "report_type": "general_ledger",
        "ledger_file": (io.BytesIO(gl), "gl.csv"),
    }, content_type="multipart/form-data", follow_redirects=False)
    assert r.status_code == 302, r.status_code
    job_id = r.headers["Location"].rsplit("/", 1)[-1]

    # Synthesize the guidance the import route would have written.
    guidance = ug.classify_unmapped_accounts(
        unmapped_keys=SAMPLE_KEYS,
        mapping_mode="number",
        coa_rows=[],
        coa_create_history=[],
        job_id=job_id,
        company_name="U6 Co",
        environment="production",
    )
    appmod.jobs[job_id]["unmapped_accounts"] = sorted(SAMPLE_KEYS)
    appmod.jobs[job_id]["unmapped_account_guidance"] = guidance.to_dict()

    page = c.get(f"/jobs/{job_id}")
    assert page.status_code == 200, page.status_code
    body = page.get_data(as_text=True)

    # Banner reflects the structured CTA — not the legacy "Cannot import" line.
    assert "These accounts are not in QuickBooks yet" in body, body[-2000:]
    assert "Upload Account List" in body
    # No legacy/confusing phrasing leaks through.
    assert "Cannot import" not in body
    # Production deploy → no "sandbox" in the company label.
    assert "your connected QuickBooks company (U6 Co)" in body
    # Each missing account shows up with number AND name.
    for num, name in [
        ("1300", "Prepaid Expenses"),
        ("3100", "Owner Draws"),
        ("4200", "Consulting Revenue"),
        ("5060", "Professional Fees"),
        ("5070", "Insurance Expense"),
    ]:
        assert num in body and name in body, (num, name)
    print("U6 OK: job-detail renders structured CTA banner")


if __name__ == "__main__":
    u1_no_account_list()
    u2_coa_uploaded_not_finalized()
    u3_coa_finalized_but_still_unmapped()
    u4_account_listing_includes_numbers_and_names()
    u5_environment_label_drops_sandbox_in_production()
    u6_job_detail_renders_structured_cta()
    print("\nALL UNMAPPED ACCOUNT GUIDANCE SMOKE TESTS PASSED")
