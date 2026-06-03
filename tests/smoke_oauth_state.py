"""OAuth state-nonce smoke test.

Run from project root:

    python3 tests/smoke_oauth_state.py

Verifies the per-attempt OAuth state nonce defense added by the security
review:

  T1 /jobs/<id>/connect-qbo mints `pending_oauth_state` on the session and
     uses it as the `state` query parameter on the Intuit auth redirect.
  T2 /oauth/callback with a state value that does not match the session's
     pending_oauth_state is rejected (no token exchange, no connection)
     and writes an `oauth_callback_state_mismatch` audit row.
  T3 The legacy fallback (no pending_oauth_state on the session, only
     pending_job_id) still routes through the firm-mismatch check so an
     in-flight upgrade does not break a connect that started on the
     previous build.
"""

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
os.environ.setdefault("SECRET_KEY", "smoke-secret")
# Pretend QBO is wired up so connect-qbo redirects to Intuit instead of
# bouncing back with the "OAuth not configured" flash.
os.environ.setdefault("QBO_CLIENT_ID", "smoke-client-id")
os.environ.setdefault("QBO_CLIENT_SECRET", "smoke-client-secret")
os.environ.setdefault("QBO_REDIRECT_URI", "http://localhost:5000/oauth/callback")

import app as appmod  # noqa: E402


GL = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()


def signup(client, firm, email, password="passw0rd!1234"):
    return client.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
        follow_redirects=False,
    )


def upload_job(client, company="Acme"):
    import io
    r = client.post(
        "/upload",
        data={
            "company_name": company,
            "email": "owner@example.test",
            "ledger_file": (io.BytesIO(GL), "ledger.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.status_code
    location = r.headers["Location"]
    return location.rsplit("/", 1)[-1]


def t1_connect_mints_state_nonce():
    c = appmod.app.test_client()
    signup(c, "State Smoke Firm", "state-smoke@example.test")
    job_id = upload_job(c)
    r = c.get(f"/jobs/{job_id}/connect-qbo", follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["Location"]
    assert location.startswith("https://appcenter.intuit.com/connect/oauth2"), location
    qs = parse_qs(urlparse(location).query)
    state_param = qs.get("state", [""])[0]
    assert state_param.startswith(f"{job_id}:"), state_param
    nonce = state_param.split(":", 1)[1]
    # Strong randomness check: token_urlsafe(32) is at least ~43 chars.
    assert len(nonce) >= 32, len(nonce)
    with c.session_transaction() as s:
        assert s.get("pending_oauth_state") == state_param, s.get("pending_oauth_state")
        assert s.get("pending_job_id") == job_id, s.get("pending_job_id")
    print("T1 OK: connect-qbo mints a session-bound state nonce")


def t2_callback_rejects_state_mismatch():
    c = appmod.app.test_client()
    signup(c, "Mismatch Firm", "mismatch@example.test")
    job_id = upload_job(c)
    # Simulate the redirect to Intuit (mints session state).
    c.get(f"/jobs/{job_id}/connect-qbo", follow_redirects=False)
    # Now simulate Intuit redirecting back with the WRONG state.
    bogus = f"{job_id}:not-the-real-nonce"
    r = c.get(
        f"/oauth/callback?code=x&state={bogus}&realmId=Z",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/dashboard"), r.headers["Location"]
    # No connection was created for the job.
    assert job_id not in appmod.qbo_connections
    # Audit log captured the mismatch action under the user's firm.
    user = appmod.db.get_user_by_email("mismatch@example.test")
    audit = appmod.db.recent_audit_for_firm(user["firm_id"], 50)
    assert any(a["action"] == "oauth_callback_state_mismatch" for a in audit), \
        [a["action"] for a in audit]
    print("T2 OK: callback rejects state mismatch and audits it")


def t3_legacy_fallback_still_routes_through_firm_check():
    """If a session is missing pending_oauth_state (e.g. upgraded mid-flight),
    the callback should fall back to using state as the bare job_id and let
    the existing firm-mismatch check fire.
    """
    c = appmod.app.test_client()
    signup(c, "Legacy Firm", "legacy@example.test")
    job_id = upload_job(c)
    # Simulate an in-flight connect from the previous build by setting
    # only pending_job_id (no pending_oauth_state).
    with c.session_transaction() as s:
        s["pending_job_id"] = job_id
        s.pop("pending_oauth_state", None)
    # Hit the callback with state=<bare job_id>.
    r = c.get(
        f"/oauth/callback?code=x&state={job_id}&realmId=Z",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.status_code
    # We're using a fake code so token exchange will fail; the test only
    # cares that we did NOT 400/500 on the state check.
    assert "/dashboard" in r.headers["Location"] or f"/jobs/{job_id}" in r.headers["Location"]
    print("T3 OK: legacy fallback still routes through firm-mismatch check")


def t4_callback_rehydrates_job_when_cache_cold():
    """Cesar QA item 5: a worker restart between minting the OAuth state
    and Intuit's redirect back wipes the in-memory ``jobs`` cache. The
    callback must rehydrate the job from the DB instead of bouncing the
    user with "could not match this connection" (which read like lost
    progress / a logout). We use a fake ``code`` so the token exchange
    still fails, but the job must be *found* — so we must NOT land on the
    dashboard with the "could not match" flash.
    """
    c = appmod.app.test_client()
    signup(c, "ColdCache Firm", "coldcache@example.test")
    job_id = upload_job(c)
    # Mint a real session-bound state.
    c.get(f"/jobs/{job_id}/connect-qbo", follow_redirects=False)
    with c.session_transaction() as s:
        state_param = s.get("pending_oauth_state")
    assert state_param, "connect-qbo must have set pending_oauth_state"
    # Simulate the worker restart: drop the in-memory job cache entirely.
    appmod.jobs.pop(job_id, None)
    assert job_id not in appmod.jobs
    r = c.get(
        f"/oauth/callback?code=fake&state={state_param}&realmId=Z",
        follow_redirects=True,
    )
    body = r.get_data(as_text=True)
    # The job was found (rehydrated): we must NOT see the "could not match
    # this QuickBooks connection back to a migration job" failure.
    assert "could not match this QuickBooks connection" not in body, \
        "cold-cache callback must rehydrate the job, not bounce as unmatched"
    # And the job is back in the cache as a side effect of _get_job.
    assert job_id in appmod.jobs, "callback should have rehydrated the job"
    print("T4 OK: callback rehydrates the job from DB when the cache is cold")


if __name__ == "__main__":
    t1_connect_mints_state_nonce()
    t2_callback_rejects_state_mismatch()
    t3_legacy_fallback_still_routes_through_firm_check()
    t4_callback_rehydrates_job_when_cache_cold()
    print("\nALL OAUTH STATE SMOKE TESTS PASSED")
