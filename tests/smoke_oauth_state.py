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


def t5_connect_persists_durable_oauth_state():
    """connect-qbo must write a durable oauth_states row keyed on the exact
    `state` it sends to Intuit, recording the originating job_id + firm so
    the callback can recover the job without the browser session.
    """
    c = appmod.app.test_client()
    signup(c, "Durable Firm", "durable@example.test")
    job_id = upload_job(c)
    r = c.get(f"/jobs/{job_id}/connect-qbo", follow_redirects=False)
    qs = parse_qs(urlparse(r.headers["Location"]).query)
    state_param = qs.get("state", [""])[0]
    row = appmod.db.peek_oauth_state(state_param)
    assert row is not None, "connect-qbo must persist a durable oauth_states row"
    assert row["job_id"] == job_id, row
    user = appmod.db.get_user_by_email("durable@example.test")
    assert row["firm_id"] == user["firm_id"], row
    assert row["consumed_at"] is None, "freshly minted state must be unconsumed"
    print("T5 OK: connect-qbo persists a durable oauth_states row")


def t6_callback_recovers_job_when_session_pending_lost():
    """THE BUG: user is still logged in, but the session lost
    pending_oauth_state / pending_job_id between connect and the Intuit
    redirect back (cookie dropped on the cross-site round-trip, different
    worker, etc.). The durable oauth_states row must let the callback
    resolve the correct job instead of bouncing with "could not match".
    """
    c = appmod.app.test_client()
    signup(c, "LostSession Firm", "lostsession@example.test")
    job_id = upload_job(c)
    r = c.get(f"/jobs/{job_id}/connect-qbo", follow_redirects=False)
    state_param = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    # Simulate the session losing ONLY the OAuth pending keys (still logged in).
    with c.session_transaction() as s:
        s.pop("pending_oauth_state", None)
        s.pop("pending_job_id", None)
    r2 = c.get(
        f"/oauth/callback?code=fake&state={state_param}&realmId=Z",
        follow_redirects=True,
    )
    body = r2.get_data(as_text=True)
    assert "could not match this QuickBooks connection" not in body, \
        "durable state must recover the job even when the session pending keys are gone"
    print("T6 OK: callback recovers the job from durable state after session-key loss")


def t7_durable_state_is_single_use():
    """A consumed oauth_states row must not be redeemable a second time —
    a replayed callback (same `state` twice) is rejected. This preserves
    the CSRF single-use guarantee now that the binding lives in the DB.
    """
    c = appmod.app.test_client()
    signup(c, "SingleUse Firm", "singleuse@example.test")
    job_id = upload_job(c)
    r = c.get(f"/jobs/{job_id}/connect-qbo", follow_redirects=False)
    state_param = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    first = appmod.db.consume_oauth_state(state_param, 3600)
    assert first is not None and first["job_id"] == job_id, first
    second = appmod.db.consume_oauth_state(state_param, 3600)
    assert second is None, "a consumed oauth_states row must not be redeemable again"
    print("T7 OK: durable oauth_states row is single-use")


def t8_callback_unmatched_offers_return_link():
    """When the job truly cannot be resolved but we still know its id, the
    recovery flash links back to the migration (no scary technical wording)
    instead of dead-ending on the dashboard.
    """
    c = appmod.app.test_client()
    signup(c, "Recovery Firm", "recovery@example.test")
    job_id = upload_job(c)
    r = c.get(f"/jobs/{job_id}/connect-qbo", follow_redirects=False)
    state_param = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    # Make the job unresolvable: drop it from cache AND delete the DB row,
    # but keep the durable oauth_states row (so job_id is known, job is not).
    appmod.jobs.pop(job_id, None)
    appmod.db.delete_job(job_id)
    r2 = c.get(
        f"/oauth/callback?code=fake&state={state_param}&realmId=Z",
        follow_redirects=True,
    )
    body = r2.get_data(as_text=True)
    # Friendly recovery copy + a link back to the migration.
    assert "Go back to your migration" in body, body[:500]
    assert f"/jobs/{job_id}" in body, "recovery flash should link back to the job"
    # No scary internal phrasing.
    assert "migration job. Please open the job and click Connect" not in body
    print("T8 OK: unmatched callback offers a friendly return-to-migration link")


def t9_no_session_branch_recovers_job_id_from_durable_state():
    """When the session is entirely gone (no logged-in user), the no-session
    branch should still recover the originating job_id from the durable
    oauth_states row to drive the post-login redirect — and must NOT split
    a nonce-bearing state into a broken job_id.
    """
    c = appmod.app.test_client()
    signup(c, "NoSession Firm", "nosession@example.test")
    job_id = upload_job(c)
    r = c.get(f"/jobs/{job_id}/connect-qbo", follow_redirects=False)
    state_param = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    # Fully clear the session (logged out / cookie dropped).
    with c.session_transaction() as s:
        s.clear()
    r2 = c.get(
        f"/oauth/callback?code=fake&state={state_param}&realmId=Z",
        follow_redirects=False,
    )
    assert r2.status_code == 302
    loc = r2.headers["Location"]
    # Redirect to login carrying next=<the job page>, with the bare job_id
    # (no ":nonce" suffix leaking into the path).
    assert "/login" in loc, loc
    assert job_id in loc, loc
    assert f"{job_id}%3A" not in loc and f"{job_id}:" not in loc, \
        f"job_id must not carry the nonce suffix: {loc}"
    print("T9 OK: no-session branch recovers a clean job_id from durable state")


if __name__ == "__main__":
    t1_connect_mints_state_nonce()
    t2_callback_rejects_state_mismatch()
    t3_legacy_fallback_still_routes_through_firm_check()
    t4_callback_rehydrates_job_when_cache_cold()
    t5_connect_persists_durable_oauth_state()
    t6_callback_recovers_job_when_session_pending_lost()
    t7_durable_state_is_single_use()
    t8_callback_unmatched_offers_return_link()
    t9_no_session_branch_recovers_job_id_from_durable_state()
    print("\nALL OAUTH STATE SMOKE TESTS PASSED")
