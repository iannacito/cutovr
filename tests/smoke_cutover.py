"""Smoke tests for cutover setup + migration checklist.

Run from project root:

    python3 tests/smoke_cutover.py

Covers:
  T1  build_checklist on a brand-new firm: every step is Not started
      and the next recommended step is cutover_setup.
  T2  build_checklist promotes cutover_setup → Complete once the firm
      has saved cutover_date + country + accounting_basis, and the
      next recommended step shifts to chart_of_accounts.
  T3  /cutover is protected — anonymous users are redirected to /login.
  T4  GET /cutover renders the form for a logged-in user.
  T5  POST /cutover persists a row and audit log entry; round-trip
      shows on the GET response.
  T6  POST /cutover normalizes unknown country / basis to safe values.
  T7  /migration-checklist renders for a fresh firm and shows the
      cutover_setup nudge as the first action.
  T8  /migration-checklist reflects an uploaded COA + GL + import as
      Complete on the right rows.
  T9  Dashboard injects `next_step` for a firm with no cutover row.
  T10 Backward compatibility: firms with no cutover_settings row still
      load dashboard and checklist without 500s.
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
os.environ.setdefault("SECRET_KEY", "smoke-cutover-secret")

import cutover_workflow as cw  # noqa: E402
import app as appmod  # noqa: E402

COA_CSV = (ROOT / "test_data" / "01_chart_of_accounts.csv").read_bytes()
GL_CSV = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()


# --- Unit-level checks (no Flask client) ----------------------------------


def t1_empty_firm_checklist():
    items = cw.build_checklist(
        cutover=None,
        firm_jobs=[],
        has_qbo_connection=False,
        account_mapping_count=0,
    )
    by_key = {i.key: i for i in items}
    # Every step is not_started on an empty firm.
    for key in (
        cw.STEP_CUTOVER_SETUP, cw.STEP_COA_UPLOAD, cw.STEP_OPENING_TB,
        cw.STEP_GL_UPLOAD, cw.STEP_ENDING_TB, cw.STEP_TRUST_LISTING,
        cw.STEP_QBO_CONNECT, cw.STEP_ACCOUNT_MAPPING, cw.STEP_DRY_RUN,
        cw.STEP_PROD_IMPORT, cw.STEP_RECONCILIATION,
    ):
        assert key in by_key, f"missing checklist step {key}"
        assert by_key[key].status == cw.STATUS_NOT_STARTED, (key, by_key[key])

    nxt = cw.next_recommended_step(items)
    assert nxt is not None
    assert nxt.key == cw.STEP_CUTOVER_SETUP, nxt.key
    # Trust posting is the only step still flagged "planned" — opening
    # TB JE posting and ending TB reconciliation shipped in the
    # migration-workflow-completion PR.
    assert by_key[cw.STEP_TRUST_LISTING].planned is True
    print("T1 empty firm checklist: OK")


def t2_cutover_complete_promotes_next_step():
    cutover = {
        "cutover_date": "2026-04-01",
        "country": "CA",
        "accounting_basis": "accrual",
    }
    items = cw.build_checklist(
        cutover=cutover,
        firm_jobs=[],
        has_qbo_connection=False,
        account_mapping_count=0,
    )
    by_key = {i.key: i for i in items}
    assert by_key[cw.STEP_CUTOVER_SETUP].status == cw.STATUS_COMPLETE
    nxt = cw.next_recommended_step(items)
    assert nxt.key == cw.STEP_COA_UPLOAD, nxt.key
    print("T2 cutover complete promotes next step: OK")


def t2b_partial_cutover_is_in_progress():
    cutover = {"cutover_date": "2026-04-01"}  # country/basis missing
    items = cw.build_checklist(cutover=cutover, firm_jobs=[],
                               has_qbo_connection=False)
    by_key = {i.key: i for i in items}
    assert by_key[cw.STEP_CUTOVER_SETUP].status == cw.STATUS_IN_PROGRESS
    print("T2b partial cutover is in_progress: OK")


def t2c_imported_gl_marks_prod_import_complete():
    fake_jobs = [
        {"report_type": "general_ledger", "status": "Imported"},
    ]
    items = cw.build_checklist(cutover=None, firm_jobs=fake_jobs,
                               has_qbo_connection=True,
                               account_mapping_count=3)
    by_key = {i.key: i for i in items}
    assert by_key[cw.STEP_GL_UPLOAD].status == cw.STATUS_COMPLETE
    assert by_key[cw.STEP_PROD_IMPORT].status == cw.STATUS_COMPLETE
    assert by_key[cw.STEP_QBO_CONNECT].status == cw.STATUS_COMPLETE
    assert by_key[cw.STEP_ACCOUNT_MAPPING].status == cw.STATUS_COMPLETE
    print("T2c imported GL drives multiple steps to Complete: OK")


# --- Flask client checks --------------------------------------------------


def _signup_and_login(client, firm="Cutover Test LLP",
                      email="test@cutover.example",
                      password="correct-horse-battery-staple"):
    client.post(
        "/signup",
        data={
            "firm_name": firm, "email": email,
            "password": password, "confirm_password": password,
        },
        follow_redirects=True,
    )
    client.post("/login",
                data={"email": email, "password": password},
                follow_redirects=True)


def _upload(client, body, filename, report_type=""):
    return client.post(
        "/upload",
        data={
            "company_name": "Cutover Smoke",
            "email": "ops@cutover.example",
            "report_type": report_type,
            "ledger_file": (io.BytesIO(body), filename),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def _run_flask_tests():
    appmod.app.config["TESTING"] = True
    appmod.app.config["WTF_CSRF_ENABLED"] = False

    # T3 — auth gate
    with appmod.app.test_client() as anon:
        resp = anon.get("/cutover", follow_redirects=False)
        assert resp.status_code in (301, 302, 303), resp.status_code
        loc = resp.headers.get("Location", "")
        assert "/login" in loc, loc
        print("T3 /cutover requires login: OK")

    with appmod.app.test_client() as client:
        _signup_and_login(client)

        # T4 — GET renders the form
        resp = client.get("/cutover")
        assert resp.status_code == 200, resp.status_code
        body = resp.get_data(as_text=True)
        for needle in (
            "switchover",          # plain-English replacement for "cutover"
            "Cutover date",
            "Opening balance",
            "Country",
            "Accounting basis",
            "QBO company name",
            "Save cutover settings",
        ):
            assert needle in body, f"missing {needle!r}"
        print("T4 GET /cutover renders form: OK")

        # T5 — POST persists + audit + round-trip
        resp = client.post(
            "/cutover",
            data={
                "cutover_date": "2026-04-01",
                "opening_balance_date": "2026-03-31",
                "period_start": "2025-04-01",
                "period_end": "2026-03-31",
                "country": "CA",
                "accounting_basis": "accrual",
                "migration_scope": "FY2026 GL + opening TB + trust listing",
                "notes": "Multi-entity setup; check trust posting strategy.",
                "qbo_company_name": "Cutover Smoke (Operating)",
                "qbo_realm_id": "1234567890",
                "clio_involved": "1",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303), resp.status_code
        assert "/migration-checklist" in resp.headers.get("Location", "")

        # The audit row + DB row should exist for this firm.
        # Locate the firm via the most recent firm in the DB (this test
        # uses a dedicated DB file, so it's the one we just made).
        firm_row = appmod.db.get_firm(1) or appmod.db.get_firm(2)
        assert firm_row is not None
        cut = appmod.db.get_cutover_settings(firm_row["id"])
        assert cut is not None
        assert cut["cutover_date"] == "2026-04-01"
        assert cut["country"] == "CA"
        assert cut["accounting_basis"] == "accrual"
        assert cut["clio_involved"] == 1
        assert cut["source_system"] == "PCLaw"
        assert cut["target_system"] == "QBO"

        audit_rows = appmod.db.recent_audit_for_firm(firm_row["id"], limit=20)
        assert any(a["action"] == "cutover_settings_saved" for a in audit_rows)

        # Round-trip: GET shows values
        resp = client.get("/cutover")
        body = resp.get_data(as_text=True)
        assert "2026-04-01" in body
        assert "Cutover Smoke (Operating)" in body
        print("T5 POST /cutover persists + audits + round-trips: OK")

        # T6 — unknown country / basis normalized
        resp = client.post(
            "/cutover",
            data={
                "cutover_date": "2026-04-01",
                "country": "XX",                       # invalid
                "accounting_basis": "made_up_basis",   # invalid
            },
            follow_redirects=False,
        )
        cut = appmod.db.get_cutover_settings(firm_row["id"])
        assert cut["country"] == "OTHER", cut["country"]
        assert cut["accounting_basis"] == "unknown", cut["accounting_basis"]
        print("T6 POST /cutover normalizes invalid enums: OK")

    # T7-T10 — checklist + dashboard wiring with a second fresh firm
    with appmod.app.test_client() as client:
        _signup_and_login(client, firm="Backward Compat LLP",
                          email="bc@cutover.example")

        # T10 — dashboard + checklist both load with no cutover row
        resp = client.get("/dashboard")
        assert resp.status_code == 200, resp.status_code
        dash = resp.get_data(as_text=True)
        assert "Next recommended step" in dash
        assert "Cutover setup completed" in dash, dash[:1500]
        print("T10 dashboard backward-compatible (no cutover row): OK")

        resp = client.get("/migration-checklist")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Migration checklist" in body
        assert "Next step:" in body
        assert "Cutover setup completed" in body
        # T9 — dashboard's next-step is cutover_setup
        assert "Open cutover setup" in dash
        print("T7 /migration-checklist renders + nudge: OK")
        print("T9 dashboard surfaces next_step=cutover_setup: OK")

        # T8 — upload COA + GL and confirm checklist updates
        resp = _upload(client, COA_CSV, "coa.csv", report_type="chart_of_accounts")
        assert resp.status_code in (302, 303)
        resp = _upload(client, GL_CSV, "gl.csv", report_type="general_ledger")
        assert resp.status_code in (302, 303)

        resp = client.get("/migration-checklist")
        body = resp.get_data(as_text=True)
        # COA upload should now show at least In progress (preview was
        # uploaded but actual QBO creation hasn't happened yet).
        assert "Chart of Accounts uploaded" in body
        coa_idx = body.find("Chart of Accounts uploaded")
        window = body[max(0, coa_idx - 600):coa_idx + 200]
        assert ("In progress" in window or "Complete" in window), window
        # GL row should reflect at least an upload.
        gl_idx = body.find("General Ledger uploaded")
        gl_window = body[max(0, gl_idx - 600):gl_idx + 200]
        assert ("In progress" in gl_window or "Complete" in gl_window), gl_window
        print("T8 uploads reflected in checklist: OK")


def main():
    t1_empty_firm_checklist()
    t2_cutover_complete_promotes_next_step()
    t2b_partial_cutover_is_in_progress()
    t2c_imported_gl_marks_prod_import_complete()
    _run_flask_tests()
    print("\nAll cutover/checklist smoke tests passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
