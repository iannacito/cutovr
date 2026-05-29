"""Smoke tests for the demo workspace (/demo) and demo sample fixtures.

Run from project root:

    python3 tests/smoke_demo_mode.py

Covers:
  T1 Demo mode hidden by default.
       - With DEMO_MODE unset and OPERATOR_EMAILS unset:
         * /demo for a logged-in normal user returns 404.
         * Dashboard nav HTML does NOT contain a "Demo" link.
         * Sample CSV downloads also return 404.
  T2 Demo mode visible when DEMO_MODE=true.
       - Logged-in user gets 200 on /demo and a "Demo" nav link.
       - Sample CSV downloads return 200 with text/csv body.
  T3 Demo mode visible for operators even when DEMO_MODE is not set.
       - User in OPERATOR_EMAILS sees /demo without DEMO_MODE being set.
  T4 Start-new-demo POST archives prior jobs for the firm and mints a
     run id, without touching any other firm.
  T5 Generated demo GL CSVs are internally balanced AND embed the
     current demo run id in transaction ids / memos so repeat runs
     against the same QBO company would not collide.
  T6 Duplicate-import protection on the production /upload path is
     unchanged when DEMO_MODE is on. Specifically: the file_sha256
     dedup logic must still apply to the same bytes uploaded twice.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


ENC_KEY_VALUE = "Yh7m5b1J9P0sR8wQv3KsVJpC1Bl0r2Gn9D6X2g8oZqU="
SECRET_VALUE = "z" * 64


def _reset_app(env: dict):
    for mod in ("app", "operator_panel", "demo_mode", "encryption"):
        if mod in sys.modules:
            del sys.modules[mod]
    base = {
        "APP_DB": tempfile.mktemp(suffix=".sqlite3"),
        "IMPORT_HISTORY_DB": tempfile.mktemp(suffix=".sqlite3"),
        "CSRF_DISABLE": "1",
        "SECRET_KEY": SECRET_VALUE,
        "APP_ENV": "local",
        "ENCRYPTION_KEY": ENC_KEY_VALUE,
        "QBO_CLIENT_ID": "test-client-id",
        "QBO_CLIENT_SECRET": "test-client-secret",
        "QBO_REDIRECT_URI": "https://example.com/oauth/callback",
    }
    for k in ("OPERATOR_EMAILS", "SHOW_OPERATOR_TOOLS", "DEMO_MODE", "APP_DEMO_MODE"):
        os.environ.pop(k, None)
    base.update(env)
    for k, v in base.items():
        os.environ[k] = v
    return importlib.import_module("app")


def _signup(client, firm, email, password="passw0rd!1234"):
    return client.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
        follow_redirects=False,
    )


def t1_demo_hidden_by_default():
    appmod = _reset_app({})
    c = appmod.app.test_client()
    _signup(c, "NoDemo Firm", "alice@nodemo.test")

    r = c.get("/demo")
    assert r.status_code == 404, f"/demo should 404 with DEMO_MODE unset, got {r.status_code}"

    r = c.get("/dashboard")
    assert r.status_code == 200
    assert b">Demo<" not in r.data, "Demo nav link should be hidden when DEMO_MODE is off"

    for slug in ("chart-of-accounts", "trial-balance", "general-ledger", "trust-listing"):
        r = c.get(f"/demo/sample/{slug}.csv")
        assert r.status_code == 404, f"sample {slug} should 404 with DEMO_MODE unset"

    print("T1 OK: demo workspace hidden when DEMO_MODE unset and user is not an operator")


def t2_demo_visible_when_demo_mode_enabled():
    appmod = _reset_app({"DEMO_MODE": "true"})
    c = appmod.app.test_client()
    _signup(c, "Demo Firm", "demo@demo.test")

    r = c.get("/demo")
    assert r.status_code == 200, f"/demo with DEMO_MODE=true should be 200, got {r.status_code}"
    body = r.get_data(as_text=True)
    for needle in ("demo workspace", "Start a new demo", "Step 1", "QuickBooks"):
        assert needle in body, f"/demo missing copy: {needle!r}"

    r = c.get("/dashboard")
    assert b">Demo<" in r.data, "Demo nav link should appear when DEMO_MODE=true"

    # Sample CSVs accessible
    for slug, ctype_needle in [
        ("chart-of-accounts", "account_number"),
        ("trial-balance", "debit_balance"),
        ("general-ledger", "transaction_id"),
        ("trust-listing", "trust_balance"),
    ]:
        r = c.get(f"/demo/sample/{slug}.csv")
        assert r.status_code == 200, f"sample {slug} status {r.status_code}"
        assert "text/csv" in r.headers.get("Content-Type", ""), f"sample {slug} bad mimetype"
        assert ctype_needle in r.get_data(as_text=True), f"sample {slug} missing column"
    print("T2 OK: demo workspace + samples visible when DEMO_MODE=true")


def t3_demo_visible_for_operators_without_demo_mode():
    op_email = "ops@anthro.test"
    appmod = _reset_app({"OPERATOR_EMAILS": op_email})
    c = appmod.app.test_client()
    _signup(c, "Operator Firm", op_email)
    r = c.get("/demo")
    assert r.status_code == 200, f"operator /demo should 200, got {r.status_code}"
    print("T3 OK: operator sees demo workspace even without DEMO_MODE")


def t4_start_new_demo_archives_jobs_for_firm():
    appmod = _reset_app({"DEMO_MODE": "true"})
    appdb = appmod.db

    # Two firms, each with one job. Firm A starts a new demo; firm B's
    # jobs must be untouched (no cross-firm bleed).
    firm_a_id, user_a_id = appdb.create_firm_and_admin("Firm A", "a@a.test", "passw0rd!1234")
    firm_b_id, user_b_id = appdb.create_firm_and_admin("Firm B", "b@b.test", "passw0rd!1234")

    appdb.upsert_job(
        job_id="job-a1", firm_id=firm_a_id, user_id=user_a_id,
        company="A Co", source_file="/tmp/a.csv", encrypted_file="/tmp/a.enc",
        file_sha256="a" * 64, status="In progress",
    )
    appdb.upsert_job(
        job_id="job-b1", firm_id=firm_b_id, user_id=user_b_id,
        company="B Co", source_file="/tmp/b.csv", encrypted_file="/tmp/b.enc",
        file_sha256="b" * 64, status="In progress",
    )

    c = appmod.app.test_client()
    r = c.post("/login", data={"email": "a@a.test", "password": "passw0rd!1234"})
    assert r.status_code in (200, 302), r.status_code

    r = c.post("/demo/start", follow_redirects=False)
    assert r.status_code in (302, 303), f"/demo/start should redirect, got {r.status_code}"

    # Firm A's job is now archived; firm B's job is left alone.
    a_jobs = appdb.list_jobs_for_firm(firm_a_id)
    b_jobs = appdb.list_jobs_for_firm(firm_b_id)
    assert len(a_jobs) == 1 and a_jobs[0]["status"].startswith("Archived (demo reset"), a_jobs[0]
    assert len(b_jobs) == 1 and b_jobs[0]["status"] == "In progress", b_jobs[0]

    # Audit trail recorded.
    audit = appdb.recent_audit_for_firm(firm_a_id, limit=20)
    actions = [row["action"] for row in audit]
    assert "demo_workspace_reset" in actions, f"audit missing demo_workspace_reset: {actions}"
    print("T4 OK: /demo/start archives this firm's jobs only and writes audit row")


def t5_demo_gl_is_balanced_and_run_id_salted():
    appmod = _reset_app({"DEMO_MODE": "true"})
    demo_mode = sys.modules["demo_mode"]

    run_a = demo_mode.new_demo_run_id()
    run_b = demo_mode.new_demo_run_id()
    assert run_a != run_b, "demo run ids must be unique"
    assert run_a.startswith("D-")

    assert demo_mode.gl_is_balanced(run_a), "demo GL must balance"

    gl_a = demo_mode.render_general_ledger_csv(run_a)
    gl_b = demo_mode.render_general_ledger_csv(run_b)

    # Transaction ids must differ between runs; customer/vendor names too.
    assert run_a.replace("-", "")[1:] in gl_a.replace("-", ""), "run_a id should appear in CSV"
    assert run_a not in gl_b and run_b not in gl_a, "runs must be independent"
    assert "Johnson Family Law [" + run_a + "]" in gl_a
    assert "Acme Property Mgmt [" + run_a + "]" in gl_a

    # COA is *not* salted -- account numbers must stay stable so QBO
    # does not accumulate duplicate Chart of Accounts each demo.
    coa_a = demo_mode.render_chart_of_accounts_csv()
    coa_b = demo_mode.render_chart_of_accounts_csv()
    assert coa_a == coa_b, "COA must be stable across demo runs"
    assert run_a not in coa_a, "COA must not embed run id"

    print("T5 OK: demo GL is balanced and run-id-salted; COA is stable")


def t7_unauthenticated_redirect_on_demo_deploy():
    """On a DEMO_MODE=true deploy, an unauthenticated visit to /demo
    redirects to /login?next=/demo instead of 404ing. This is the
    diagnostic improvement that prevents "/demo 404s after deploy"
    confusion: on a demo deploy the route is openly available and the
    login redirect tells the operator they just need to sign in.

    On a non-demo deploy the route still 404s for snoopers (verified by
    t1) so the diagnostic relaxation is scoped to demo deploys only.
    """
    appmod = _reset_app({"DEMO_MODE": "true"})
    c = appmod.app.test_client()
    r = c.get("/demo", follow_redirects=False)
    assert r.status_code in (302, 303), f"/demo on demo deploy should redirect when logged-out, got {r.status_code}"
    loc = r.headers.get("Location", "")
    assert "/login" in loc and "next=" in loc, f"redirect should land on /login?next=..., got {loc!r}"
    print("T7 OK: unauthenticated /demo on DEMO_MODE deploy redirects to /login")


def t8_unauthenticated_404_off_demo_deploy():
    """Inverse of T7: on a non-demo deploy the /demo route still 404s
    for unauthenticated visitors. This preserves the "don't confirm the
    workspace exists" property on production-config'd deploys.
    """
    appmod = _reset_app({})  # DEMO_MODE unset
    c = appmod.app.test_client()
    r = c.get("/demo", follow_redirects=False)
    assert r.status_code == 404, f"/demo on non-demo deploy should 404 when logged-out, got {r.status_code}"
    print("T8 OK: unauthenticated /demo on non-demo deploy still 404s")


def t9_healthz_exposes_demo_mode_flag():
    """/healthz/detailed (operator-only) includes demo_mode_enabled so
    Render configuration can be verified externally without leaking it
    to the public — public /healthz now returns only {status: ok}.
    """
    import json as _json
    token = "demo-mode-healthz-token"
    appmod = _reset_app({"DEMO_MODE": "true", "HEALTHZ_TOKEN": token})
    c = appmod.app.test_client()
    # Public probe must NOT contain demo_mode_enabled.
    pub = c.get("/healthz")
    assert pub.status_code == 200
    assert pub.get_json() == {"status": "ok"}, pub.get_data(as_text=True)
    # Detailed (with token) reveals it.
    r = c.get(f"/healthz/detailed?token={token}")
    assert r.status_code == 200, r.status_code
    body = _json.loads(r.get_data(as_text=True))
    assert body.get("demo_mode_enabled") is True, f"expected demo_mode_enabled=true, got {body!r}"

    appmod = _reset_app({"HEALTHZ_TOKEN": token})
    c = appmod.app.test_client()
    body = _json.loads(c.get(f"/healthz/detailed?token={token}").get_data(as_text=True))
    assert body.get("demo_mode_enabled") is False, f"expected demo_mode_enabled=false, got {body!r}"
    print("T9 OK: /healthz/detailed reports demo_mode_enabled")


def t6_duplicate_protection_unchanged_outside_demo_mode():
    """Demo mode must not weaken duplicate protection for normal imports.

    We check the import-history layer directly: the same sha256 should
    only count as one ingest. The demo run id only mutates demo dataset
    bytes, not how the upload path computes / records sha256.
    """
    appmod = _reset_app({"DEMO_MODE": "true"})  # even with demo on
    history = appmod.history

    sample = b"transaction_id,date\nJE-1,2026-01-01\n"
    import hashlib
    sha = hashlib.sha256(sample).hexdigest()
    realm = "1234567890"

    history.record_import(
        job_id="job-x1", realm_id=realm, file_sha256=sha,
        company_name="Smoke Co", transaction_count=1,
        debit_total="0", credit_total="0", status="success",
    )
    dup = history.has_completed_import(sha, realm)
    assert dup is not None, "duplicate sha lookup should find the prior import"
    assert dup["file_sha256"] == sha
    print("T6 OK: duplicate-protection lookup still functions with DEMO_MODE=true")


if __name__ == "__main__":
    failures = []
    for fn in (
        t1_demo_hidden_by_default,
        t2_demo_visible_when_demo_mode_enabled,
        t3_demo_visible_for_operators_without_demo_mode,
        t4_start_new_demo_archives_jobs_for_firm,
        t5_demo_gl_is_balanced_and_run_id_salted,
        t6_duplicate_protection_unchanged_outside_demo_mode,
        t7_unauthenticated_redirect_on_demo_deploy,
        t8_unauthenticated_404_off_demo_deploy,
        t9_healthz_exposes_demo_mode_flag,
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e}")
    if failures:
        raise SystemExit(f"{len(failures)} test(s) failed")
    print("\nALL DEMO-MODE SMOKE TESTS PASSED")
