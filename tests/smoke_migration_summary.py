"""Smoke tests for the customer-facing migration summary page.

Run from project root:

    python3 tests/smoke_migration_summary.py

Covers:
  S1  build_migration_summary returns a complete dataclass for an
      empty firm (no jobs, no imports) — overall state is Not started,
      every file section is not_started, every balance check is not_run.
  S2  build_migration_summary recognises an imported GL job and an
      opening-balance JE: overall state becomes Imported (when no
      attention items exist), files reflect the activity, the opening
      balance check shows Looks good.
  S3  Attention items surface unmatched accounts, last_error blobs,
      and bulk-upload unknown/duplicate classifications. When any
      attention item is present, overall_state is Needs attention.
  S4  CSV export contains every section heading, sanitises a
      formula-looking firm name with a leading tick, and never echoes
      raw tokens / file paths / intuit_tid.
  S5  /migration-summary renders with auth: status pill, hero copy,
      Files received table, Balance checks list, CSV download link,
      and the workflow stepper.
  S6  /migration-summary.csv returns CSV with the right
      Content-Disposition header and a sanitised filename.
  S7  /migration-summary requires auth — anonymous request redirects
      to /login.
"""

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
os.environ.setdefault("SECRET_KEY", "smoke-secret-migration-summary")

import migration_summary as ms  # noqa: E402
import cutover_workflow as cw  # noqa: E402
import app as appmod  # noqa: E402


def _checklist(**overrides):
    """Build a full checklist with everything not_started, then override."""
    defaults = {
        cw.STEP_CUTOVER_SETUP: cw.STATUS_NOT_STARTED,
        cw.STEP_COA_UPLOAD: cw.STATUS_NOT_STARTED,
        cw.STEP_OPENING_TB: cw.STATUS_NOT_STARTED,
        cw.STEP_GL_UPLOAD: cw.STATUS_NOT_STARTED,
        cw.STEP_ENDING_TB: cw.STATUS_NOT_STARTED,
        cw.STEP_TRUST_LISTING: cw.STATUS_NOT_STARTED,
        cw.STEP_QBO_CONNECT: cw.STATUS_NOT_STARTED,
        cw.STEP_ACCOUNT_MAPPING: cw.STATUS_NOT_STARTED,
        cw.STEP_DRY_RUN: cw.STATUS_NOT_STARTED,
        cw.STEP_PROD_IMPORT: cw.STATUS_NOT_STARTED,
        cw.STEP_RECONCILIATION: cw.STATUS_NOT_STARTED,
    }
    defaults.update(overrides)
    return [
        cw.ChecklistItem(key=k, label=k, status=s, summary="")
        for k, s in defaults.items()
    ]


def s1_empty_firm():
    summary = ms.build_migration_summary(
        firm={"name": "Smith & Hart LLP"},
        cutover=None,
        jobs=[],
        imports=[],
        bulks=[],
        qbo_connections=[],
        checklist=_checklist(),
    )
    assert summary.firm_name == "Smith & Hart LLP"
    assert summary.overall_state == ms.STATE_NOT_STARTED, summary.overall_state
    assert summary.has_jobs is False
    # 4 file sections, all not started.
    assert len(summary.files) == 4
    assert all(f.state == ms.STATE_NOT_STARTED for f in summary.files), [
        (f.report_type, f.state) for f in summary.files
    ]
    # No QBO writes, no attention, balance checks all not run.
    assert not summary.qbo.any_activity
    assert summary.attention == []
    assert all(b.state == ms.STATE_NOT_RUN for b in summary.balance_checks), [
        (b.key, b.state) for b in summary.balance_checks
    ]
    # Next step nudges towards upload.
    assert summary.next_step_label
    print("OK  S1  empty firm: Not started, 4 files not_started, no attention")


def s2_imported_gl_and_opening_balance():
    jobs = [
        {
            "id": "job-gl-1",
            "company": "Smith & Hart LLP",
            "report_type": "general_ledger",
            "status": "Imported",
            "created_at": "2026-05-01T10:00:00",
        },
        {
            "id": "job-tb-1",
            "company": "Smith & Hart LLP",
            "report_type": "trial_balance",
            "status": "Parsed",
            "created_at": "2026-05-02T10:00:00",
            "opening_balance_history": [
                {"qbo_je_id": "QBO-OB-001", "demo_mode": False,
                 "posted_at": "2026-05-02T10:30:00"}
            ],
        },
        {
            "id": "job-coa-1",
            "company": "Smith & Hart LLP",
            "report_type": "chart_of_accounts",
            "status": "Parsed",
            "created_at": "2026-05-03T10:00:00",
            "coa_create_history": [{"created_count": 12}],
        },
    ]
    imports = [
        {
            "id": 1,
            "job_id": "job-gl-1",
            "status": "success",
            "transaction_count": 47,
            "company_name": "Smith & Hart LLP",
            "created_at": "2026-05-01T11:00:00",
            "reversal": None,
        }
    ]
    checklist = _checklist(**{
        cw.STEP_CUTOVER_SETUP: cw.STATUS_COMPLETE,
        cw.STEP_COA_UPLOAD: cw.STATUS_COMPLETE,
        cw.STEP_OPENING_TB: cw.STATUS_COMPLETE,
        cw.STEP_GL_UPLOAD: cw.STATUS_COMPLETE,
        cw.STEP_QBO_CONNECT: cw.STATUS_COMPLETE,
        cw.STEP_ACCOUNT_MAPPING: cw.STATUS_COMPLETE,
        cw.STEP_DRY_RUN: cw.STATUS_COMPLETE,
        cw.STEP_PROD_IMPORT: cw.STATUS_COMPLETE,
    })
    summary = ms.build_migration_summary(
        firm={"name": "Smith & Hart LLP"},
        cutover={"cutover_date": "2026-04-01"},
        jobs=jobs,
        imports=imports,
        bulks=[],
        qbo_connections=[{"realm_id": "9876543210", "company_name": "Smith QBO"}],
        checklist=checklist,
    )
    assert summary.overall_state == ms.STATE_IMPORTED, summary.overall_state
    # 47 GL transactions + 1 opening balance JE.
    assert summary.qbo.journal_entries_created == 48, summary.qbo.journal_entries_created
    assert summary.qbo.accounts_created == 12
    assert summary.qbo_company_name == "Smith QBO"
    assert summary.cutover_date == "2026-04-01"
    # Opening balance check passed; ending balance check is needs_attention
    # because there's only one TB on file and no ending TB recon built.
    opening = next(b for b in summary.balance_checks if b.key == "opening_balance")
    assert opening.state == ms.STATE_PASS, opening.state
    # GL file section reports Imported.
    gl_section = next(f for f in summary.files if f.report_type == "general_ledger")
    assert gl_section.state == ms.STATE_IMPORTED
    assert gl_section.count == 1
    print("OK  S2  imported GL + opening JE: Imported, balances pass, counts roll up")


def s3_attention_items():
    jobs = [
        {
            "id": "job-gl-error",
            "company": "Test Firm",
            "report_type": "general_ledger",
            "status": "Failed",
            "created_at": "2026-05-01T10:00:00",
            "unmapped_accounts": [
                {"pclaw": "1000", "name": "Cash"},
                {"pclaw": "2000", "name": "AP"},
            ],
            "last_error": {"message": "QBO 400: AccountRef not found"},
        },
    ]
    bulks = [
        {
            "firm_id": 1,
            "results": [
                {"status": "needs_review", "filename": "mystery.csv"},
                {"status": "duplicate", "filename": "tb2.csv"},
                {"status": "categorized", "filename": "gl.csv"},
            ],
        }
    ]
    summary = ms.build_migration_summary(
        firm={"name": "Test Firm"},
        cutover=None,
        jobs=jobs,
        imports=[],
        bulks=bulks,
        qbo_connections=[],
        checklist=_checklist(),
        checklist_url="/migration-checklist",
        imports_url="/firm/imports",
        dashboard_url="/dashboard",
    )
    assert summary.overall_state == ms.STATE_NEEDS_ATTENTION
    keys = {a.key for a in summary.attention}
    assert "unmatched_accounts" in keys, keys
    assert "import_errors" in keys, keys
    assert "unknown_files" in keys, keys
    assert "duplicate_files" in keys, keys
    # Missing files surfaces because no COA/TB/GL uploaded — the GL row above
    # is the parent of unmapped_accounts but its report_type IS general_ledger,
    # which means STEP_GL_UPLOAD is still "not_started" in the test checklist
    # we passed (we don't change it). The missing-files attention item should
    # therefore still appear.
    assert "missing_files" in keys, keys
    # Each attention item should have a non-empty label.
    for a in summary.attention:
        assert a.label, a.key
    print("OK  S3  attention items surface unmatched accounts, errors, unknowns, dupes")


def s4_csv_export_safety():
    # Firm name starts with a `=` to test CSV-injection sanitization.
    summary = ms.build_migration_summary(
        firm={"name": "=Evil Firm"},
        cutover={"cutover_date": "2026-04-01"},
        jobs=[],
        imports=[],
        bulks=[],
        qbo_connections=[],
        checklist=_checklist(),
    )
    body = ms.summary_to_csv(summary)
    # Required section headings.
    for section in (
        "Migration summary",
        "Files received",
        "QuickBooks activity",
        "Balance checks",
        "Recommended next step",
    ):
        assert section in body, f"CSV missing section {section!r}"
    # CSV injection mitigation: any cell starting with =, +, -, @ gets a
    # leading tick. Our firm row should therefore contain `'=Evil Firm`.
    assert "'=Evil Firm" in body, "CSV did not sanitize formula-leading firm name"
    # No raw tokens or intuit_tid fragments.
    for forbidden in ("access_token", "refresh_token", "intuit_tid", "BEGIN PGP"):
        assert forbidden not in body, f"CSV leaks {forbidden!r}"
    print("OK  S4  CSV export sanitizes formula chars and contains every section")


def _signup_login(client, email):
    """Sign up a fresh firm + log in for template tests."""
    client.post("/signup", data={
        "firm_name": "Summary Test Firm",
        "email": email,
        "password": "passw0rd!1234",
        "confirm_password": "passw0rd!1234",
    }, follow_redirects=False)
    # If user already exists from a stale run, fall back to login.
    client.post("/login", data={
        "email": email, "password": "passw0rd!1234",
    }, follow_redirects=False)


def s5_summary_page_renders():
    client = appmod.app.test_client()
    _signup_login(client, "summary@stepper.test")
    r = client.get("/migration-summary")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    must_contain = [
        # Stepper still rendered on the page.
        'workflow-stepper',
        # Hero copy.
        'Migration summary',
        # Section headings.
        'Files received',
        'QuickBooks activity',
        'Balance checks',
        # Friendly file labels (plain English, accounting term secondary).
        'Account list',
        'Transaction history',
        'Client trust balances',
        # CSV download link.
        '/migration-summary.csv',
        # Status pill class.
        'summary-state',
    ]
    for token in must_contain:
        assert token in body, f"summary page missing {token!r}"
    print("OK  S5  /migration-summary renders hero, sections, CSV link, stepper")


def s6_summary_csv_download():
    client = appmod.app.test_client()
    _signup_login(client, "summary-csv@stepper.test")
    r = client.get("/migration-summary.csv")
    assert r.status_code == 200, r.status_code
    ctype = r.headers.get("Content-Type", "")
    assert ctype.startswith("text/csv"), ctype
    disp = r.headers.get("Content-Disposition", "")
    assert "attachment" in disp and "migration-summary-" in disp, disp
    body = r.get_data(as_text=True)
    assert "PCLaw Migrate" in body and "Files received" in body
    # Defense: never include the literal env var or token names.
    for forbidden in ("access_token", "refresh_token", "intuit_tid",
                      "SECRET_KEY", "ENCRYPTION_KEY"):
        assert forbidden not in body, f"CSV leaks {forbidden!r}"
    print("OK  S6  /migration-summary.csv downloads as CSV with sanitized filename")


def s7_summary_requires_auth():
    client = appmod.app.test_client()
    # Fresh client, no login.
    r = client.get("/migration-summary", follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    assert "/login" in r.headers.get("Location", ""), r.headers.get("Location")
    r2 = client.get("/migration-summary.csv", follow_redirects=False)
    assert r2.status_code in (302, 303), r2.status_code
    assert "/login" in r2.headers.get("Location", ""), r2.headers.get("Location")
    print("OK  S7  /migration-summary requires auth")


def main():
    s1_empty_firm()
    s2_imported_gl_and_opening_balance()
    s3_attention_items()
    s4_csv_export_safety()
    s5_summary_page_renders()
    s6_summary_csv_download()
    s7_summary_requires_auth()
    print("\nAll migration-summary smoke tests passed.")


if __name__ == "__main__":
    main()
