"""Dashboard clarity smoke tests.

Run from project root:

    python3 tests/smoke_dashboard_clarity.py

Pins the QA-reported needs:
  - An obvious "Start a new client migration" button on the dashboard
    that posts to the existing start-fresh endpoint.
  - Many old jobs grouped behind a quiet "Earlier migrations" details
    section so the current migration is the focus.

Checks:
  T1 Dashboard renders the Start-a-new-migration card with a POST form
     pointing at welcome_back_start_fresh.
  T2 With <= 5 jobs the archived section is not rendered.
  T3 With many jobs (~10) the first 5 are in 'Current migration' and
     the rest are inside the 'Earlier migrations' details section.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-dashboard-clarity")

import app as appmod  # noqa: E402

db = appmod.db


def _signup(client, email):
    client.post(
        "/signup",
        data={
            "firm_name": "Clarity Firm",
            "email": email,
            "password": "passw0rd!1234",
            "confirm_password": "passw0rd!1234",
        },
    )


def _add_jobs(firm_id, user_id, n, prefix):
    for i in range(n):
        db.upsert_job(
            job_id=f"job_{prefix}_{i:03d}",
            firm_id=firm_id,
            user_id=user_id,
            company=f"Co {i:02d}",
            source_file=f"file{i}.csv",
            encrypted_file=f"/tmp/file{i}.csv.enc",
            file_sha256=f"sha-{prefix}-{i}",
            status="Imported",
        )


def t1_start_new_migration_button_renders():
    c = appmod.app.test_client()
    _signup(c, "t1-clarity@example.test")
    r = c.get("/dashboard")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Start a new client migration" in body, body[:1000]
    assert "Start a new migration &rarr;" in body or "Start a new migration →" in body
    assert "/welcome-back/start-fresh" in body
    # Must be a POST form (not a GET link) so refresh doesn't trigger.
    assert 'action="/welcome-back/start-fresh" method="post"' in body \
        or "action=\"/welcome-back/start-fresh\" method=\"post\"" in body, body[:800]
    print("T1 OK: dashboard renders Start-a-new-migration POST form")


def t2_few_jobs_no_archive_section():
    c = appmod.app.test_client()
    _signup(c, "t2-clarity@example.test")
    with c.session_transaction() as s:
        firm_id = s["firm_id"]
        user_id = s["user_id"]
    db.upsert_cutover_settings(firm_id, cutover_date="2026-04-01", country="US")
    _add_jobs(firm_id, user_id, 3, prefix="t2")
    body = c.get("/dashboard").get_data(as_text=True)
    assert "Earlier migrations" not in body, "few jobs should not surface archive section"
    print("T2 OK: <= 5 jobs do not surface the archive section")


def t3_many_jobs_splits_current_vs_archive():
    c = appmod.app.test_client()
    _signup(c, "t3-clarity@example.test")
    with c.session_transaction() as s:
        firm_id = s["firm_id"]
        user_id = s["user_id"]
    db.upsert_cutover_settings(firm_id, cutover_date="2026-04-01", country="US")
    _add_jobs(firm_id, user_id, 10, prefix="t3")
    body = c.get("/dashboard").get_data(as_text=True)
    assert "Current migration" in body
    assert "Earlier migrations (" in body, "expected archive details section"
    # The archive section should be inside a <details>.
    archived_idx = body.index("Earlier migrations")
    assert "<details" in body[:archived_idx], "archive heading should be inside <details>"
    print("T3 OK: many jobs split into Current migration + Earlier migrations")


if __name__ == "__main__":
    t1_start_new_migration_button_renders()
    t2_few_jobs_no_archive_section()
    t3_many_jobs_splits_current_vs_archive()
    print("ALL DASHBOARD CLARITY SMOKE TESTS PASSED")
