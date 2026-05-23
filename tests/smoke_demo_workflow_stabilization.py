"""Smoke tests for the demo-workflow stabilization sprint.

Background
----------
PR #37 made fresh demo uploads write a (account_number, account_name)
snapshot so the Step 3 Match-accounts screen can render even after the
ephemeral upload disk is wiped on redeploy. But old jobs created
*before* PR #37 don't have that snapshot, so they still hit the
"missing_source" recovery path. For those legacy demo jobs the existing
"Re-upload PCLaw export" CTA is the wrong primary action — the demo
operator usually doesn't have the original file on hand and would
rather start fresh.

This sprint:

  1. When the deploy is in DEMO_MODE (or the user is an operator) and a
     job hits the unrecoverable "missing_source" state on
     ``/jobs/<id>/account-mapping``, the page surfaces a primary
     ``demo-restart-cta`` linking to the demo workspace. The legacy
     re-upload CTA still renders but as a secondary affordance.
  2. The production path is unchanged: no demo CTA, single
     "Re-upload PCLaw export" primary.
  3. ``/demo`` renders a preflight checklist with one row per
     prerequisite (deploy mode, QBO env, redirect URI, QBO connection,
     active demo run) so the demo operator can spot a blocker in one
     glance before walking a customer through the flow.
  4. ``/demo/sample/ending-trial-balance.csv`` is downloadable so the
     customer-facing Step 6 ("Final balance check") has a real source
     report bundled into the demo dataset.

Covered
-------
  D1  In DEMO_MODE, account-mapping missing_source recovery renders the
      ``demo-restart-cta`` primary button alongside (not in place of)
      the legacy re-upload CTA.
  D2  Outside DEMO_MODE and without operator rights, the same page
      renders only the legacy re-upload CTA — no demo-restart link
      leaks into production.
  D3  ``/demo`` renders the preflight checklist with one entry per
      prerequisite.
  D4  ``/demo/sample/ending-trial-balance.csv`` 200s and returns a
      well-formed TB CSV when DEMO_MODE is enabled.

Run from project root:

    python3 tests/smoke_demo_workflow_stabilization.py
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Per-run scratch dirs so we don't pollute the working tree.
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="pclaw_uploads_")
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_")
os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-demo-workflow-stabilization")
# Default deploy is non-demo. Individual tests flip DEMO_MODE on as needed.
os.environ.pop("DEMO_MODE", None)
os.environ.pop("APP_DEMO_MODE", None)

import app as appmod  # noqa: E402
import demo_mode  # noqa: E402


def _signup_and_login(client, email, firm):
    pwd = "passw0rd!1234"
    r = client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


def _make_unrecoverable_job(client, email, firm):
    """Sign up a firm and create a job whose snapshot AND upload file are gone.

    Mirrors the demo screenshot scenario after PR #37: the job row
    survives on the durable disk but neither the encrypted upload nor
    the new pclaw_accounts snapshot was ever persisted.
    """
    _signup_and_login(client, email, firm)
    db = appmod.db
    user = db.get_user_by_email(email)
    job_id = f"job_{firm.replace(' ', '_').lower()}"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company=firm, source_file="lost.csv",
        encrypted_file="never_existed.enc", file_sha256="0" * 64,
        status="uploaded",
    )
    appmod.qbo_connections[job_id] = {
        "realm_id": "R-DEMO",
        "access_token_enc": appmod.encrypt_token("fake-access"),
        "refresh_token_enc": appmod.encrypt_token("fake-refresh"),
        "company_name": "Demo QBO Co",
        "legal_name": "Demo QBO Co",
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }
    appmod.jobs.pop(job_id, None)
    return job_id, user


class _FakeQBO:
    def get_accounts(self):
        return {"QueryResponse": {"Account": [
            {"Id": "10", "Name": "Cash", "AcctNum": "1000", "AccountType": "Bank"},
        ]}}


def d1_demo_mode_shows_demo_restart_cta():
    # Flip on DEMO_MODE for the request — is_demo_mode_enabled() reads
    # the env at call time so a temporary patch is sufficient.
    with mock.patch.dict(os.environ, {"DEMO_MODE": "true"}):
        client = appmod.app.test_client()
        job_id, _user = _make_unrecoverable_job(
            client, "d1@example.test", "D1 LLP",
        )
        with mock.patch.object(
            appmod, "_get_qbo_client",
            return_value=(_FakeQBO(), appmod.qbo_connections[job_id]),
        ):
            r = client.get(f"/jobs/{job_id}/account-mapping",
                           follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="account-mapping-error"' in body
    # New primary CTA points at the demo workspace.
    assert 'data-testid="demo-restart-cta"' in body, \
        "expected demo-restart CTA in demo-mode recovery page"
    assert "Start a fresh demo run" in body
    assert "/demo" in body
    # The "older run" wording should appear (customer-friendly recovery copy).
    assert "older run" in body
    # Re-upload CTA still rendered as a secondary action.
    assert 'data-testid="reupload-cta"' in body
    print("D1 OK: DEMO_MODE recovery surfaces Start-a-fresh-demo primary CTA")


def d2_production_shows_only_reupload_cta():
    # No DEMO_MODE in env, user is not an operator.
    os.environ.pop("DEMO_MODE", None)
    os.environ.pop("APP_DEMO_MODE", None)
    client = appmod.app.test_client()
    job_id, _user = _make_unrecoverable_job(
        client, "d2@example.test", "D2 LLP",
    )
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(_FakeQBO(), appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping",
                       follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="account-mapping-error"' in body
    # Demo CTA must NOT leak into the production-config response.
    assert 'data-testid="demo-restart-cta"' not in body, \
        "demo CTA leaked into production recovery page"
    assert "Start a fresh demo run" not in body
    # The original precise re-upload CTA is still the primary affordance.
    assert 'data-testid="reupload-cta"' in body
    assert "Re-upload PCLaw export" in body
    assert "no longer on file for this job" in body
    print("D2 OK: production recovery still shows the single re-upload CTA")


def d3_demo_preflight_renders():
    """The /demo page renders a preflight checklist with one row per item."""
    with mock.patch.dict(os.environ, {"DEMO_MODE": "true"}):
        client = appmod.app.test_client()
        _signup_and_login(client, "d3@example.test", "D3 LLP")
        r = client.get("/demo", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="demo-preflight"' in body, \
        "preflight card missing from demo workspace"
    # Each preflight item exposes a stable test id so future regressions
    # are easy to catch.
    for key in (
        "deploy", "qbo_environment", "redirect_uri",
        "qbo_connection", "demo_run",
    ):
        assert f'data-testid="preflight-{key}"' in body, \
            f"missing preflight row {key}"
    # Pre-demo state: no QBO connection, no active run id.
    assert 'data-status="missing"' in body, \
        "expected at least one preflight row to be flagged 'missing'"
    print("D3 OK: /demo preflight checklist renders one row per prerequisite")


def d4_ending_trial_balance_sample_downloads():
    with mock.patch.dict(os.environ, {"DEMO_MODE": "true"}):
        client = appmod.app.test_client()
        _signup_and_login(client, "d4@example.test", "D4 LLP")
        r = client.get("/demo/sample/ending-trial-balance.csv",
                       follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # Header line of the rendered CSV must match the TB schema.
    first_line = body.splitlines()[0]
    assert first_line == "account_number,account_name,debit_balance,credit_balance", \
        f"unexpected TB header: {first_line!r}"
    # And the file has at least one row beyond the header.
    assert len(body.splitlines()) > 1
    # render_ending_trial_balance_csv is a pure helper — assert it's
    # exposed for re-use by future sample-data tooling.
    assert hasattr(demo_mode, "render_ending_trial_balance_csv")
    print("D4 OK: /demo/sample/ending-trial-balance.csv 200s with TB schema")


def main():
    d1_demo_mode_shows_demo_restart_cta()
    d2_production_shows_only_reupload_cta()
    d3_demo_preflight_renders()
    d4_ending_trial_balance_sample_downloads()
    print("\nALL DEMO WORKFLOW STABILIZATION SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
