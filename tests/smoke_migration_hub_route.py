"""Smoke tests: /migration-hub route rendering (Roadblock 5).

Verifies the operator/workflow board renders end-to-end through Flask: a
firm with several general ledgers gets one card per ledger, a blocked
ledger and a ready ledger coexist, and the per-GL "open" action links to
the existing job-detail page (where retry/post lives).

Run from project root::

    python3 tests/smoke_migration_hub_route.py
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
os.environ.setdefault("SECRET_KEY", "smoke-migration-hub-route")

import app as appmod  # noqa: E402

db = appmod.db


def _signup(client, email):
    client.post(
        "/signup",
        data={
            "firm_name": "Hub Firm",
            "email": email,
            "password": "passw0rd!1234",
            "confirm_password": "passw0rd!1234",
        },
    )


def _add_gl(firm_id, user_id, job_id, company, **state):
    status = state.pop("status", "Uploaded")
    db.upsert_job(
        job_id=job_id,
        firm_id=firm_id,
        user_id=user_id,
        company=company,
        source_file=f"{job_id}.csv",
        encrypted_file=f"/tmp/{job_id}.csv.enc",
        file_sha256=f"sha-{job_id}",
        status=status,
    )
    # Persist hydrated state (report_type, preflight, unmapped_accounts,
    # import_summary, entity_name_blockers) through save_job_state so the
    # hub projection — which reads hydrate_job — can see it after reload.
    snapshot = {"status": status, "report_type": "general_ledger"}
    snapshot.update(state)
    db.save_job_state(job_id, snapshot)


def t1_empty_hub_renders():
    c = appmod.app.test_client()
    _signup(c, "t1-hub@example.test")
    r = c.get("/migration-hub")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "Migration" in body and "Hub" in body, body[:500]
    assert "No general ledgers yet" in body, "empty state should render"
    # The firm-level setup cards render even for a brand-new firm so the
    # operator sees what's required (COA, Vendors & Clients, Opening Balances).
    assert 'data-testid="hub-setup"' in body, "setup section should render"
    assert "Chart of Accounts" in body
    assert "Vendors &amp; Clients" in body or "Vendors & Clients" in body
    assert "Opening Balances" in body
    print("T1 OK: /migration-hub renders the empty state + setup cards for a new firm")


def t2_multiple_gls_each_get_a_card():
    c = appmod.app.test_client()
    _signup(c, "t2-hub@example.test")
    with c.session_transaction() as s:
        firm_id = s["firm_id"]
        user_id = s["user_id"]
    # A blocked GL (unmapped accounts) and a different GL with no blockers.
    _add_gl(firm_id, user_id, "gl_blocked", "January GL",
            preflight={"line_count": 10}, unmapped_accounts=["1200 Trust"])
    _add_gl(firm_id, user_id, "gl_clean", "February GL",
            preflight={"line_count": 8})
    body = c.get("/migration-hub").get_data(as_text=True)
    # Both ledgers are visible — one stuck file does not hide the other.
    assert "January GL" in body, "blocked GL card missing"
    assert "February GL" in body, "clean GL card missing"
    assert "Needs attention" in body, "blocked status label missing"
    # Each card links to its own job-detail page for per-GL action.
    assert "/jobs/gl_blocked" in body
    assert "/jobs/gl_clean" in body
    print("T2 OK: each GL gets its own card; one blocked ledger does not hide others")


def t3_imported_gl_does_not_block_pending():
    c = appmod.app.test_client()
    _signup(c, "t3-hub@example.test")
    with c.session_transaction() as s:
        firm_id = s["firm_id"]
        user_id = s["user_id"]
    _add_gl(firm_id, user_id, "gl_done", "Posted GL",
            status="Imported", import_summary={"qbo_je_count": 12},
            preflight={"line_count": 5})
    _add_gl(firm_id, user_id, "gl_todo", "Pending GL",
            preflight={"line_count": 6})
    body = c.get("/migration-hub").get_data(as_text=True)
    assert "Sent to QuickBooks" in body, "imported label missing"
    assert "Posted GL" in body and "Pending GL" in body
    print("T3 OK: a posted GL and a pending GL render independently")


def t4_dashboard_links_to_hub():
    c = appmod.app.test_client()
    _signup(c, "t4-hub@example.test")
    with c.session_transaction() as s:
        firm_id = s["firm_id"]
        user_id = s["user_id"]
    # Move the firm past the upload stage so the post-Step-2 dashboard
    # (with the Workspace card) renders, then confirm it links to the hub.
    db.upsert_cutover_settings(firm_id, cutover_date="2026-04-01", country="US")
    _add_gl(firm_id, user_id, "gl_dash", "Dash GL", preflight={"line_count": 3})
    body = c.get("/dashboard").get_data(as_text=True)
    assert "/migration-hub" in body, "dashboard should link to the Migration Hub"
    print("T4 OK: the dashboard Workspace card links to /migration-hub")


def main():
    t1_empty_hub_renders()
    t2_multiple_gls_each_get_a_card()
    t3_imported_gl_does_not_block_pending()
    t4_dashboard_links_to_hub()
    print("\nAll /migration-hub route smoke tests passed.")


if __name__ == "__main__":
    main()
