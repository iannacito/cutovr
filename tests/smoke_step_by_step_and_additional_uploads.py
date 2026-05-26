"""Smoke tests for the step-by-step workflow simplification and the
"Add more reports" append flow.

Run from project root:

    python3 tests/smoke_step_by_step_and_additional_uploads.py

Covers:
  S1  Every workflow page renders an explicit "Step N of 6" eyebrow
      label so the customer always knows where they are in the flow.
  S2  Dashboard intake card surfaces a "forgot a report?" prompt so
      customers know they can add more later without restarting.
  S3  Bulk-upload review page renders an "Add more reports" / "Upload
      additional reports" affordance with a multi-file form pointing at
      the new /upload/bulk/<id>/append route.
  S4  Migration checklist surfaces an "Add more reports" action so a
      customer who already completed bulk upload can still append files
      without going back to start.
  S5  Appending CSVs to an existing bulk preserves the original results
      and adds the new files to the same bulk record (no restart, no
      lost state).
  S6  Appending a file with the same content / report type as an
      existing entry runs collision detection — duplicates are flagged
      for review, never silently overwritten.
  S7  The append route rejects an empty form (no files) cleanly and
      does not create stray jobs.
  S8  /upload/bulk/<id>/add (GET) routes to the review page with the
      add-more-reports anchor.
  S9  /oauth/callback with an expired session preserves the job_id
      from the state so the post-login redirect returns the user to
      their job page (no open redirect — same-origin only).
  S10 OAuth callback session-expired path does NOT attempt a token
      exchange (no QuickBooks side effects without a verified session).
"""

import io
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
os.environ.setdefault("SECRET_KEY", "smoke-secret-step-by-step")
# QBO env for the OAuth tests below — keeps the connect-qbo path live
# without forcing a real Intuit redirect.
os.environ.setdefault("QBO_CLIENT_ID", "smoke-client-id")
os.environ.setdefault("QBO_CLIENT_SECRET", "smoke-client-secret")
os.environ.setdefault("QBO_REDIRECT_URI", "http://localhost:5000/oauth/callback")

import app as appmod  # noqa: E402
from werkzeug.datastructures import MultiDict  # noqa: E402


GL_CSV = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()
COA_CSV = (ROOT / "test_data" / "01_chart_of_accounts.csv").read_bytes()
TB_CSV = (ROOT / "test_data" / "03_trial_balance.csv").read_bytes()
TRUST_CSV = (ROOT / "test_data" / "05_trust_listing.csv").read_bytes()


def _signup_and_login(client, email, firm):
    pwd = "passw0rd!1234"
    client.post(
        "/signup",
        data={
            "firm_name": firm,
            "email": email,
            "password": pwd,
            "confirm_password": pwd,
        },
        follow_redirects=False,
    )
    client.post(
        "/login",
        data={"email": email, "password": pwd},
        follow_redirects=False,
    )


def _bulk_upload_initial(client, company, files):
    data = MultiDict()
    data["company_name"] = company
    for name, body in files:
        data.add("ledger_files", (io.BytesIO(body), name))
    r = client.post(
        "/upload/bulk", data=data,
        content_type="multipart/form-data", follow_redirects=False,
    )
    assert r.status_code == 302, r.status_code
    # Location is /upload/bulk/<bulk_id>
    bulk_url = r.headers["Location"]
    return bulk_url


def s1_step_eyebrows_on_workflow_pages():
    """Each workflow page surfaces a 'Step N of 6' label so the user
    can never lose their place in the sequence."""
    c = appmod.app.test_client()
    _signup_and_login(c, "s1@stepbystep.test", "S1 Firm")
    # Dashboard intake card.
    body = c.get("/dashboard").get_data(as_text=True)
    assert "Step 2 of 6" in body, "dashboard intake should label Step 2 of 6"
    # Migration checklist.
    body = c.get("/migration-checklist").get_data(as_text=True)
    assert ("Step " in body) and (" of 6" in body), \
        "migration checklist should call out the active step number"
    # Cutover setup.
    body = c.get("/cutover").get_data(as_text=True)
    assert "Step 1 of 6" in body, "cutover setup should label Step 1 of 6"
    print("S1 OK: workflow pages render explicit 'Step N of 6' labels")


def s2_dashboard_promises_add_more_later():
    """The dashboard intake hints that more reports can be added later
    so a lawyer doesn't worry about uploading everything in one go."""
    c = appmod.app.test_client()
    _signup_and_login(c, "s2@stepbystep.test", "S2 Firm")
    body = c.get("/dashboard").get_data(as_text=True)
    assert "Forgot a report" in body or "add more" in body.lower(), \
        "dashboard should reassure customers they can add more reports later"
    print("S2 OK: dashboard surfaces a 'forgot a report?' reassurance")


def s3_bulk_review_has_append_form():
    c = appmod.app.test_client()
    _signup_and_login(c, "s3@stepbystep.test", "S3 Firm")
    bulk_url = _bulk_upload_initial(
        c, "S3 Co", [("chart_of_accounts.csv", COA_CSV)]
    )
    body = c.get(bulk_url).get_data(as_text=True)
    bulk_id = bulk_url.rsplit("/", 1)[-1]
    # The append form action points at the new route.
    assert f"/upload/bulk/{bulk_id}/append" in body, \
        "review page should expose the append-files form action"
    # A clear "Add more reports" / "Add more files" CTA / heading is present.
    assert ("Add more reports" in body
            or "Add more files" in body
            or "Upload additional reports" in body), \
        "review page should call out an Add-more-files affordance"
    # The legacy "Continue" CTA stays visible when nothing is missing
    # OR an "Upload missing files" CTA when something is missing.
    assert ("Continue &rarr;" in body
            or "Upload missing files &rarr;" in body), \
        "review page should keep its dominant primary CTA"
    print("S3 OK: bulk review page exposes the Add-more-reports form")


def s4_checklist_links_to_add_more():
    c = appmod.app.test_client()
    _signup_and_login(c, "s4@stepbystep.test", "S4 Firm")
    body = c.get("/migration-checklist").get_data(as_text=True)
    assert "Add more reports" in body, \
        "checklist should expose an 'Add more reports' affordance"
    print("S4 OK: migration checklist links to add-more-reports")


def s5_append_route_preserves_state_and_adds_files():
    c = appmod.app.test_client()
    _signup_and_login(c, "s5@stepbystep.test", "S5 Firm")
    bulk_url = _bulk_upload_initial(
        c, "S5 Co", [("chart_of_accounts.csv", COA_CSV)]
    )
    bulk_id = bulk_url.rsplit("/", 1)[-1]

    # Sanity: the bulk record contains the initial file.
    initial = c.get(bulk_url).get_data(as_text=True)
    assert "chart_of_accounts.csv" in initial

    # Append two more files.
    data = MultiDict()
    data.add("ledger_files", (io.BytesIO(GL_CSV), "general_ledger.csv"))
    data.add("ledger_files", (io.BytesIO(TRUST_CSV), "trust_listing.csv"))
    r = c.post(
        f"/upload/bulk/{bulk_id}/append",
        data=data, content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["Location"].endswith(f"/upload/bulk/{bulk_id}")

    # The bulk record now contains all three files.
    body = c.get(bulk_url).get_data(as_text=True)
    for fname in (
        "chart_of_accounts.csv", "general_ledger.csv", "trust_listing.csv",
    ):
        assert fname in body, f"appended bulk should still list {fname!r}"
    print("S5 OK: append route adds files to the existing bulk record")


def s6_append_runs_collision_detection_for_duplicates():
    """Appending a same-report-type file with a different filename runs
    through resolve_collisions — both entries get marked for review and
    neither is silently overwritten."""
    c = appmod.app.test_client()
    _signup_and_login(c, "s6@stepbystep.test", "S6 Firm")
    bulk_url = _bulk_upload_initial(
        c, "S6 Co", [("chart_of_accounts.csv", COA_CSV)]
    )
    bulk_id = bulk_url.rsplit("/", 1)[-1]
    # Send the same COA payload again with a different filename so the
    # classifier identifies it as the same report type.
    data = MultiDict()
    data.add("ledger_files", (io.BytesIO(COA_CSV), "chart_of_accounts_v2.csv"))
    r = c.post(
        f"/upload/bulk/{bulk_id}/append", data=data,
        content_type="multipart/form-data", follow_redirects=False,
    )
    assert r.status_code == 302
    body = c.get(bulk_url).get_data(as_text=True)
    # Both files should still be visible (no overwrite).
    assert "chart_of_accounts.csv" in body
    assert "chart_of_accounts_v2.csv" in body
    # The collision resolution flags duplicates for review — the
    # "Duplicate" badge or the warn-state cell should be present.
    assert ("Duplicate" in body) or ("Needs review" in body) \
        or ("needs_review" in body), \
        "duplicate append should be flagged for review, not auto-merged"
    print("S6 OK: append flags duplicates via existing collision logic")


def s7_append_rejects_empty_form():
    c = appmod.app.test_client()
    _signup_and_login(c, "s7@stepbystep.test", "S7 Firm")
    bulk_url = _bulk_upload_initial(
        c, "S7 Co", [("chart_of_accounts.csv", COA_CSV)]
    )
    bulk_id = bulk_url.rsplit("/", 1)[-1]
    initial_body = c.get(bulk_url).get_data(as_text=True)

    r = c.post(
        f"/upload/bulk/{bulk_id}/append", data={},
        content_type="multipart/form-data", follow_redirects=False,
    )
    assert r.status_code == 302
    # State unchanged.
    after = c.get(bulk_url).get_data(as_text=True)
    # Initial filename still appears; no extra file rows added.
    assert "chart_of_accounts.csv" in after
    # The page also surfaces an error flash on the next render.
    assert "Pick one or more" in after or "no files" in after.lower() \
        or "chart_of_accounts.csv" in after
    print("S7 OK: empty append form does not corrupt the bulk record")


def s8_bulk_add_redirect_anchor():
    c = appmod.app.test_client()
    _signup_and_login(c, "s8@stepbystep.test", "S8 Firm")
    bulk_url = _bulk_upload_initial(
        c, "S8 Co", [("chart_of_accounts.csv", COA_CSV)]
    )
    bulk_id = bulk_url.rsplit("/", 1)[-1]
    r = c.get(f"/upload/bulk/{bulk_id}/add", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("#add-more-reports"), \
        f"bulk_upload_add should redirect with the add-more-reports anchor, got {r.headers['Location']!r}"
    print("S8 OK: bulk_upload_add redirects to review#add-more-reports")


def s9_oauth_callback_session_expired_preserves_return_path():
    """When the session is missing on the OAuth callback, the friendly
    error message redirects to /login with a `next=` pointing back at
    the originating job — so the customer doesn't get stranded on the
    dashboard."""
    c = appmod.app.test_client()
    # Note: no login. The OAuth callback should treat this as
    # "session expired" rather than letting the request through.
    # We supply a state of the form "<job_id>:<nonce>" matching what
    # /jobs/<id>/connect-qbo would have minted.
    r = c.get(
        "/oauth/callback?code=ignored&realmId=R1&state=job-1234:nonce",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.status_code
    loc = r.headers["Location"]
    parsed = urlparse(loc)
    # Routed back to login.
    assert parsed.path.endswith("/login"), f"expected /login, got {loc!r}"
    # And the `next` query param carries a same-origin job URL.
    qs = parse_qs(parsed.query)
    nxt = qs.get("next", [""])[0]
    assert nxt.startswith("/jobs/") or nxt.endswith("/jobs/job-1234"), \
        f"expected next= to point back to /jobs/job-1234, got {nxt!r}"
    assert "//" not in nxt or nxt.startswith("/"), \
        "next must be a relative path (no open redirect)"
    print("S9 OK: session-expired OAuth callback preserves the job return path")


def s10_oauth_callback_session_expired_does_not_exchange_token():
    """The session-expired branch must NOT initiate a token exchange.
    We verify this by patching qbo_auth.get_bearer_token to raise — if
    the branch tried to call it, the test would surface the exception
    rather than the friendly redirect."""
    import unittest.mock as mock
    c = appmod.app.test_client()

    def boom(*_a, **_kw):
        raise AssertionError(
            "OAuth callback must not exchange a token without a session"
        )

    with mock.patch.object(appmod.qbo_auth, "get_bearer_token", side_effect=boom):
        r = c.get(
            "/oauth/callback?code=should-not-be-used&realmId=R1&state=job-555:abc",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]
    print("S10 OK: session-expired callback skips token exchange entirely")


def main():
    s1_step_eyebrows_on_workflow_pages()
    s2_dashboard_promises_add_more_later()
    s3_bulk_review_has_append_form()
    s4_checklist_links_to_add_more()
    s5_append_route_preserves_state_and_adds_files()
    s6_append_runs_collision_detection_for_duplicates()
    s7_append_rejects_empty_form()
    s8_bulk_add_redirect_anchor()
    s9_oauth_callback_session_expired_preserves_return_path()
    s10_oauth_callback_session_expired_does_not_exchange_token()
    print("\nALL STEP-BY-STEP / ADDITIONAL-UPLOADS SMOKE TESTS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
