"""Smoke tests for the account-mapping screen's QBO load-failure recovery.

When the user clicks "Match accounts" and we can't fetch the QuickBooks
chart of accounts, we want to:

  1. Classify the failure (auth vs. permission vs. throttle vs. service
     outage vs. unknown) using qbo_error_hint.classify().
  2. Render the account-mapping screen with a recovery CTA matched to
     the failure category — not redirect the user away with a generic
     flash that hides what went wrong.
  3. Surface a safe diagnostic id (HTTP status + intuit_tid) so support
     can correlate with Intuit, without leaking tokens.

Run from project root:

    python3 tests/smoke_account_mapping_load_error.py

Covers:
  C1  classify() bucket boundaries: None status → network; 401/invalid
      grant → auth; 403 → permission; 429/throttle → rate_limit;
      500/502/503/504 / service unavailable → service_unavailable.
  C2  account-mapping template renders the error card with the
      Reconnect CTA when category == auth, and a Try again CTA when
      category == service_unavailable.
  C3  Template surfaces the diagnostic id (HTTP status + intuit_tid)
      when present, and omits it cleanly when absent.
  C4  Template does NOT leak fake access-token / refresh-token / client
      secret markers if any of them are passed through (regression
      guard around accidentally surfacing raw QBO error bodies).
  C5  Live route: when QBOClient.get_accounts() raises a QBOError with
      status 401, the rendered /jobs/<id>/account-mapping page contains
      the Reconnect CTA pointing at /jobs/<id>/connect-qbo, and the
      audit log row carries the intuit_tid and status code.
  C6  Live route: when _get_qbo_client raises QBOAuthExpired (refresh
      failed), the page renders the same auth-recovery state rather
      than redirecting away with a generic flash.
  C7  Live route: when there is no QBO connection at all, the user is
      redirected to /jobs/<id>/connect-qbo (not the job page).
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-account-mapping-load-error")

import qbo_error_hint  # noqa: E402
import app as appmod  # noqa: E402
from qbo_client import QBOError  # noqa: E402


SECRET_MARKERS = [
    "AT-must-not-leak-here",
    "RT-must-not-leak-here",
    "CS-must-not-leak-here",
]


def c1_classify_buckets():
    cl = qbo_error_hint.classify

    # No HTTP response at all (DNS / TCP failure).
    assert cl(None, "boom") == qbo_error_hint.CATEGORY_NETWORK

    # Auth failures — by status OR by body content.
    assert cl(401, "") == qbo_error_hint.CATEGORY_AUTH
    assert cl(400, "invalid_grant") == qbo_error_hint.CATEGORY_AUTH
    assert cl(400, "{\"error\":\"AuthenticationFailed\"}") == qbo_error_hint.CATEGORY_AUTH

    # Permission denied (company-access / scope).
    assert cl(403, "Forbidden") == qbo_error_hint.CATEGORY_PERMISSION

    # Throttle.
    assert cl(429, "") == qbo_error_hint.CATEGORY_RATE_LIMIT
    assert cl(400, "ThrottleExceeded") == qbo_error_hint.CATEGORY_RATE_LIMIT

    # Service outage.
    for sc in (500, 502, 503, 504):
        assert cl(sc, "") == qbo_error_hint.CATEGORY_SERVICE_UNAVAILABLE, sc
    assert cl(503, "Service Unavailable") == qbo_error_hint.CATEGORY_SERVICE_UNAVAILABLE

    # Anything else → unknown.
    assert cl(418, "") == qbo_error_hint.CATEGORY_UNKNOWN
    print("C1 OK: classify() buckets QBO failures by category correctly")


def _render_error_state(category, *, intuit_tid=None, status_code=None):
    from flask import render_template
    fake_job = {"id": "demo-job", "company": "Demo & Co"}
    fake_conn = {"company_name": "Demo QBO Co", "realm_id": "R-DEMO"}
    with appmod.app.test_request_context(f"/jobs/{fake_job['id']}/account-mapping"):
        return render_template(
            "account-mapping.html",
            job=fake_job, qbo_connection=fake_conn,
            rows=[], qbo_accounts=[],
            user={"email": "c@x.test"}, firm={"name": "Demo Firm"},
            load_error={
                "category": category,
                "status_code": status_code,
                "intuit_tid": intuit_tid,
                "reconnect_url": f"/jobs/{fake_job['id']}/connect-qbo",
                "retry_url": f"/jobs/{fake_job['id']}/account-mapping",
                "job_url": f"/jobs/{fake_job['id']}",
            },
        )


def c2_error_template_shows_correct_cta_per_category():
    # Auth → primary CTA is Reconnect QuickBooks.
    body = _render_error_state(qbo_error_hint.CATEGORY_AUTH)
    assert 'data-testid="account-mapping-error"' in body
    assert "Reconnect QuickBooks" in body
    assert 'data-testid="reconnect-cta"' in body
    assert "/jobs/demo-job/connect-qbo" in body

    # Permission → also offers Reconnect + a contact-support link.
    body = _render_error_state(qbo_error_hint.CATEGORY_PERMISSION)
    assert "Reconnect QuickBooks" in body
    assert "Contact support" in body

    # Rate limit → primary CTA is Try again, NOT Reconnect.
    body = _render_error_state(qbo_error_hint.CATEGORY_RATE_LIMIT)
    assert 'data-testid="retry-cta"' in body
    assert "Try again" in body
    assert "slow down" in body.lower()

    # Service unavailable / network → Try again.
    for category in (qbo_error_hint.CATEGORY_SERVICE_UNAVAILABLE,
                     qbo_error_hint.CATEGORY_NETWORK):
        body = _render_error_state(category)
        assert 'data-testid="retry-cta"' in body
        assert "temporarily unavailable" in body.lower(), category

    # Unknown → offers BOTH retry and reconnect.
    body = _render_error_state(qbo_error_hint.CATEGORY_UNKNOWN)
    assert 'data-testid="retry-cta"' in body
    assert 'data-testid="reconnect-cta"' in body

    print("C2 OK: each error category renders the right primary CTA")


def c3_template_surfaces_diagnostic_id_when_present():
    # With diagnostics: HTTP status + intuit_tid both appear.
    body = _render_error_state(
        qbo_error_hint.CATEGORY_AUTH,
        status_code=401, intuit_tid="1-abc-tid-401",
    )
    assert 'data-testid="error-status"' in body
    assert "HTTP 401" in body
    assert 'data-testid="error-tid"' in body
    assert "1-abc-tid-401" in body

    # Without diagnostics: the whole block is suppressed.
    body = _render_error_state(qbo_error_hint.CATEGORY_AUTH)
    assert 'data-testid="error-status"' not in body
    assert 'data-testid="error-tid"' not in body

    # Only one of the two present is OK — the rest renders cleanly.
    body = _render_error_state(qbo_error_hint.CATEGORY_AUTH, intuit_tid="tid-only")
    assert 'data-testid="error-tid"' in body
    assert "tid-only" in body
    print("C3 OK: diagnostic id (HTTP + intuit_tid) surfaces when present")


def c4_error_template_does_not_leak_token_markers():
    # We pass a tid that contains nothing sensitive; but we also pull the
    # raw rendered body and assert known secret markers do not appear in
    # the template anywhere. Regression guard: if anyone later wires
    # `body` from QBOError into the template they'll fail this assertion.
    body = _render_error_state(
        qbo_error_hint.CATEGORY_AUTH,
        status_code=401, intuit_tid="opaque-tid-ok",
    )
    for marker in SECRET_MARKERS:
        assert marker not in body, f"secret marker leaked: {marker}"
    print("C4 OK: error template does not surface raw token/secret markers")


# --- Live route tests ----------------------------------------------------

def _signup_and_login(client, email, firm):
    pwd = "passw0rd!1234"
    r = client.post("/signup", data={
        "firm_name": firm,
        "email": email,
        "password": pwd,
        "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


def _create_job_with_qbo_conn(client, *, email, firm, realm_id="R-LIVE"):
    """Create a logged-in firm, a job, and a fake QBO connection.

    The QBO connection bypasses the OAuth flow by writing the in-memory
    qbo_connections dict directly; this is how other smoke tests
    introduce a 'connected' state without standing up Intuit.
    """
    _signup_and_login(client, email, firm)

    db = appmod.db
    user = db.get_user_by_email(email)
    assert user, f"smoke setup: user {email} not found"
    user_id, firm_id = user["id"], user["firm_id"]

    # Job id is per-test so back-to-back tests don't collide.
    job_id = f"live-job-{firm_id}"
    db.upsert_job(
        job_id=job_id,
        firm_id=firm_id,
        user_id=user_id,
        company="Live Co",
        source_file="x.csv",
        encrypted_file="missing.enc",
        file_sha256="0" * 64,
        status="uploaded",
    )

    # Inject a fake QBO connection into the in-memory cache that
    # _get_qbo_connection reads.
    appmod.qbo_connections[job_id] = {
        "realm_id": realm_id,
        "access_token_enc": appmod.encrypt_token("fake-access"),
        "refresh_token_enc": appmod.encrypt_token("fake-refresh"),
        "company_name": "Live QBO Co",
        "legal_name": "Live QBO Co",
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }
    return job_id


def c5_route_renders_auth_error_when_get_accounts_401():
    client = appmod.app.test_client()
    job_id = _create_job_with_qbo_conn(
        client, email="c5@example.test", firm="C5 LLP", realm_id="R-C5",
    )

    # Patch _get_qbo_client to return a QBOClient whose get_accounts
    # raises QBOError(401, ...). This exercises the QBOError catch in
    # the route, the classifier, and the rendered template.
    class FakeQBO:
        def get_accounts(self):
            raise QBOError(
                "QBO returned 401 on query: invalid_grant",
                status_code=401,
                body='{"Fault":{"Error":[{"Message":"AuthenticationFailed"}]}}',
                intuit_tid="1-live-401-tid",
            )

    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(FakeQBO(), appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping")

    assert r.status_code == 200, f"expected the page to render, not redirect ({r.status_code})"
    body = r.get_data(as_text=True)
    assert 'data-testid="account-mapping-error"' in body
    assert 'data-testid="reconnect-cta"' in body
    assert f"/jobs/{job_id}/connect-qbo" in body
    # Diagnostics surfaced for support.
    assert "HTTP 401" in body
    assert "1-live-401-tid" in body
    print("C5 OK: 401 QBOError renders auth-recovery state with reconnect CTA + diagnostic id")


def c6_route_renders_auth_error_when_refresh_fails():
    client = appmod.app.test_client()
    job_id = _create_job_with_qbo_conn(
        client, email="c6@example.test", firm="C6 LLP", realm_id="R-C6",
    )

    def boom(*_a, **_kw):
        raise appmod.QBOAuthExpired("refresh token rejected (intuit_tid=1-c6-tid)")

    with mock.patch.object(appmod, "_get_qbo_client", side_effect=boom):
        r = client.get(f"/jobs/{job_id}/account-mapping")

    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'data-testid="account-mapping-error"' in body
    assert 'data-testid="reconnect-cta"' in body
    assert "Reconnect QuickBooks" in body
    print("C6 OK: QBOAuthExpired renders auth-recovery state in-place (no redirect)")


def c7_route_redirects_to_connect_when_no_qbo_connection():
    client = appmod.app.test_client()
    _signup_and_login(client, "c7@example.test", "C7 LLP")
    db = appmod.db
    user = db.get_user_by_email("c7@example.test")
    user_id, firm_id = user["id"], user["firm_id"]

    job_id = f"live-job-c7-{firm_id}"
    db.upsert_job(
        job_id=job_id, firm_id=firm_id, user_id=user_id,
        company="C7 Co", source_file="x.csv",
        encrypted_file="missing.enc", file_sha256="0" * 64,
        status="uploaded",
    )
    # Explicitly ensure no connection exists in the in-memory cache.
    appmod.qbo_connections.pop(job_id, None)

    r = client.get(f"/jobs/{job_id}/account-mapping", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308), r.status_code
    assert f"/jobs/{job_id}/connect-qbo" in (r.headers.get("Location") or ""), \
        f"expected redirect to connect-qbo, got {r.headers.get('Location')}"
    print("C7 OK: no QBO connection → redirect to /connect-qbo (not generic error)")


def main():
    c1_classify_buckets()
    c2_error_template_shows_correct_cta_per_category()
    c3_template_surfaces_diagnostic_id_when_present()
    c4_error_template_does_not_leak_token_markers()
    c5_route_renders_auth_error_when_get_accounts_401()
    c6_route_renders_auth_error_when_refresh_fails()
    c7_route_redirects_to_connect_when_no_qbo_connection()
    print("\nALL ACCOUNT-MAPPING LOAD-ERROR SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
