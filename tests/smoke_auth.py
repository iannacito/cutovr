"""Auth + tenancy smoke test (no QBO calls).

Run from project root:

    python3 tests/smoke_auth.py

Verifies:
  T1 Anonymous user is redirected from privileged routes to /login.
  T2 Signup creates firm + admin, logs the user in, lands on /dashboard.
  T3 Duplicate email signup is rejected.
  T4 Login + logout work; bad password fails.
  T5 Upload creates a firm-scoped job; only that firm sees it.
  T6 Cross-firm job access returns 404 (does not leak existence).
  T7 OAuth callback rejects firm mismatch.
  T8 Audit log records signup, login, upload, logout.
"""

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Use throwaway DBs so reruns are deterministic.
APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret")

import app as appmod  # noqa: E402

GL = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()


def fresh_client():
    return appmod.app.test_client()


def signup(client, firm, email, password="passw0rd!"):
    return client.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
        follow_redirects=False,
    )


def login(client, email, password="passw0rd!"):
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


def main():
    # T1 anonymous redirects
    c = fresh_client()
    for path in ["/dashboard", "/upload", "/jobs/anything", "/api/jobs/anything"]:
        if path in ("/upload",):
            r = c.post(path)
        else:
            r = c.get(path)
        assert r.status_code in (302, 401, 405), f"{path} returned {r.status_code}"
        if r.status_code == 302:
            assert "/login" in r.headers["Location"], path
    print("T1 OK: privileged routes require login")

    # T2 signup
    c = fresh_client()
    r = signup(c, "Acme Law", "alice@acme.test")
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard"), r.headers
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert b"Acme Law" in r.data and b"alice@acme.test" in r.data
    print("T2 OK: signup logs user in and shows dashboard")

    # T3 duplicate email
    c2 = fresh_client()
    r = signup(c2, "Other Firm", "alice@acme.test")
    assert r.status_code == 200
    assert b"already exists" in r.data
    print("T3 OK: duplicate email rejected")

    # T4 logout, bad password, then login
    r = c.post("/logout", follow_redirects=False)
    assert r.status_code == 302
    r = c.get("/dashboard")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    r = login(c, "alice@acme.test", "wrong-pw")
    assert r.status_code == 200 and b"Invalid email" in r.data
    r = login(c, "alice@acme.test")
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")
    print("T4 OK: logout + bad password + login")

    # T5 firm-scoped upload
    r = c.post(
        "/upload",
        data={"company_name": "Test Co", "ledger_file": (io.BytesIO(GL), "gl.csv")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert r.status_code == 302
    job_id_a = sorted(appmod.jobs.keys())[-1]
    assert appmod.jobs[job_id_a]["firm_id"] is not None
    db_jobs = appmod.db.list_jobs_for_firm(appmod.jobs[job_id_a]["firm_id"])
    assert any(j["id"] == job_id_a for j in db_jobs)
    print("T5 OK: upload creates firm-scoped job (mirrored to DB)")

    # T6 cross-firm access
    c_b = fresh_client()
    signup(c_b, "Bravo Firm", "bob@bravo.test")
    r = c_b.get(f"/jobs/{job_id_a}")
    assert r.status_code == 404, r.status_code
    r = c_b.get(f"/api/jobs/{job_id_a}")
    assert r.status_code == 404
    r = c_b.post(f"/jobs/{job_id_a}/disconnect-qbo")
    assert r.status_code == 404
    r = c_b.post(f"/jobs/{job_id_a}/import-to-qbo")
    assert r.status_code == 404
    r = c_b.post(f"/jobs/{job_id_a}/delete")
    assert r.status_code == 404
    # Owner can still access
    r = c.get(f"/jobs/{job_id_a}")
    assert r.status_code == 200
    print("T6 OK: cross-firm access returns 404; owner unaffected")

    # T7 OAuth callback firm mismatch
    # Set Bob's session pending_job_id to Alice's job, then hit callback —
    # should be rejected without creating a connection.
    with c_b.session_transaction() as s:
        s["pending_job_id"] = job_id_a
    r = c_b.get(f"/oauth/callback?code=x&state={job_id_a}&realmId=Z")
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")
    assert job_id_a not in appmod.qbo_connections
    # Audit log captured the mismatch
    alice = appmod.db.authenticate("alice@acme.test", "passw0rd!")
    bob = appmod.db.authenticate("bob@bravo.test", "passw0rd!")
    bob_audit = appmod.db.recent_audit_for_firm(bob["firm_id"], 50)
    assert any(a["action"] == "oauth_callback_firm_mismatch" for a in bob_audit), [a["action"] for a in bob_audit]
    print("T7 OK: oauth callback rejects firm mismatch and audits it")

    # T8 audit log entries
    actions = [a["action"] for a in appmod.db.recent_audit_for_firm(alice["firm_id"], 50)]
    for needed in ("signup", "login", "logout", "upload"):
        assert needed in actions, (needed, actions)
    print("T8 OK: audit log contains signup/login/logout/upload")

    print("\nALL AUTH SMOKE TESTS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        for p in (APP_DB, HIST_DB):
            try:
                os.unlink(p)
            except OSError:
                pass
