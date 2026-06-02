"""Step-2 manage-reports controls smoke tests (Cesar QA 2026-06-01).

Cesar asked for a clearer report-management page after the initial
firm-name/setup flow, with per-file delete + replace actions (especially
for the General Ledger, which firms re-export after fixing dates) and a
compact "more actions" menu.

Pins
----
  M1 The manage page (/uploaded-reports) renders a per-report "slot"
     with Open, Replace, and a "More actions" menu containing Remove —
     and the clickable workflow rail.
  M2 Removing a report via /uploaded-reports/<id>/remove deletes it and
     it no longer appears on the manage page.
  M3 Replacing a report via /uploaded-reports/<id>/replace swaps the
     file, keeps the report visible, and removes the old job id.
  M4 A replace with no file attached is rejected and leaves the
     original report in place (no data loss on a fumbled click).

Run from project root:

    python3 tests/smoke_uploaded_reports_manage.py
"""

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-manage-reports")

import app as appmod  # noqa: E402

_GL_CSV = (
    "transaction_id,date,account_number,account_name,debit,credit\n"
    "JE1,Jan 4/21,1000,Operating Bank,100.00,0.00\n"
    "JE1,Jan 4/21,3000,Owner Equity,0.00,100.00\n"
)


def _signup_login(client, email):
    client.post("/signup", data={
        "firm_name": "Manage Reports Firm",
        "email": email,
        "password": "passw0rd!1234",
        "confirm_password": "passw0rd!1234",
    }, follow_redirects=True)
    client.post("/login", data={
        "email": email, "password": "passw0rd!1234",
    }, follow_redirects=True)


def _upload_gl(client, name="general_ledger.csv", company="Manage Reports Firm"):
    data = {
        "company_name": company,
        "report_type": "general_ledger",
        "ledger_file": (io.BytesIO(_GL_CSV.encode("utf-8")), name),
    }
    return client.post("/upload", data=data,
                       content_type="multipart/form-data",
                       follow_redirects=True)


def _job_ids(client):
    user = None
    with client.session_transaction() as sess:
        firm_id = sess.get("firm_id")
    rows = appmod.db.list_jobs_for_firm(firm_id, limit=500)
    return [r["id"] for r in rows]


def m1_manage_page_renders_slot_controls():
    client = appmod.app.test_client()
    _signup_login(client, "m1@example.test")
    _upload_gl(client)
    r = client.get("/uploaded-reports")
    body = r.get_data(as_text=True)
    assert r.status_code == 200, r.status_code
    assert 'data-testid="report-slot"' in body, "no report slot rendered"
    assert 'data-testid="report-slot-replace-form"' in body, "no replace control"
    assert 'data-testid="report-slot-more"' in body, "no more-actions menu"
    assert 'data-testid="report-slot-remove-submit"' in body, "no remove button"
    # Clickable rail present (current/complete steps carry links).
    assert 'data-testid="workflow-step-link"' in body, "rail not clickable"
    print("M1 OK: manage page renders slot with Open/Replace/More-actions + clickable rail")


def m2_remove_deletes_report():
    client = appmod.app.test_client()
    _signup_login(client, "m2@example.test")
    _upload_gl(client)
    ids = _job_ids(client)
    assert len(ids) == 1, ids
    job_id = ids[0]
    r = client.post(f"/uploaded-reports/{job_id}/remove", data={},
                    follow_redirects=True)
    assert r.status_code == 200, r.status_code
    assert _job_ids(client) == [], "report was not removed"
    body = r.get_data(as_text=True)
    assert 'data-testid="uploaded-reports-empty"' in body, "empty state not shown"
    print("M2 OK: remove deletes the report and the slot disappears")


def m3_replace_swaps_file_keeps_report():
    client = appmod.app.test_client()
    _signup_login(client, "m3@example.test")
    _upload_gl(client, name="gl_old.csv")
    old_ids = _job_ids(client)
    assert len(old_ids) == 1, old_ids
    old_id = old_ids[0]
    data = {
        "ledger_file": (io.BytesIO(_GL_CSV.encode("utf-8")), "gl_corrected.csv"),
    }
    r = client.post(f"/uploaded-reports/{old_id}/replace", data=data,
                    content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200, r.status_code
    new_ids = _job_ids(client)
    assert len(new_ids) == 1, new_ids
    assert old_id not in new_ids, "old job id should be gone after replace"
    body = r.get_data(as_text=True)
    assert "gl_corrected.csv" in body, "replacement filename not shown"
    print("M3 OK: replace swaps the file, keeps one report, drops the old id")


def m4_replace_without_file_keeps_original():
    client = appmod.app.test_client()
    _signup_login(client, "m4@example.test")
    _upload_gl(client, name="gl_keep.csv")
    ids = _job_ids(client)
    job_id = ids[0]
    r = client.post(f"/uploaded-reports/{job_id}/replace", data={},
                    content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200, r.status_code
    assert _job_ids(client) == ids, "original report should remain on a no-file replace"
    print("M4 OK: replace with no file is rejected and leaves the original intact")


if __name__ == "__main__":
    m1_manage_page_renders_slot_controls()
    m2_remove_deletes_report()
    m3_replace_swaps_file_keeps_report()
    m4_replace_without_file_keeps_original()
    print("\nALL MANAGE-REPORTS SMOKE TESTS PASSED")
