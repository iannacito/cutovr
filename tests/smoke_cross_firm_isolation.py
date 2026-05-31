"""Comprehensive cross-firm isolation audit for sensitive job routes.

Run from project root:

    python3 tests/smoke_cross_firm_isolation.py

smoke_auth.py already covers the core handful (/jobs/<id>, api, delete,
import-to-qbo, disconnect-qbo). This widens the net to the *full* set of
sensitive per-job routes the hardening task calls out: previews, reconcile
/ ending-TB, downloadable reports, account-mapping, reverse-import,
opening-balance, verify, coa-* and trust-reconciliation.

Contract under test: a logged-in user from Firm B must get a consistent
404 (never 200, never a redirect-to-content, never 403 that confirms the
job exists) for any of Firm A's job routes. The job owner is unaffected.
A 404 is the deliberate choice across the app so existence isn't leaked.
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
os.environ.setdefault("SECRET_KEY", "smoke-secret-cross-firm")

import app as appmod  # noqa: E402

GL = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()


def _signup(client, firm, email, password="passw0rd!1234"):
    return client.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
        follow_redirects=False,
    )


# (method, path-suffix). Every one must 404 for a foreign firm.
GET_ROUTES = [
    "",
    "/preview-import",
    "/account-mapping",
    "/opening-balance",
    "/coa-preview",
    "/connect-qbo",
    "/trust-reconciliation",
    "/ending-tb-reconciliation",
    "/validation-report.csv",
    "/reconciliation-report.csv",
    "/ending-tb-reconciliation.csv",
    "/trust-reconciliation.csv",
]
POST_ROUTES = [
    "/import-to-qbo",
    "/reverse-import",
    "/verify",
    "/delete",
    "/disconnect-qbo",
    "/account-mapping/create-missing",
    "/account-mapping/refresh",
    "/coa-apply",
]


def main():
    c_a = appmod.app.test_client()
    _signup(c_a, "Alpha Law", "alice@alpha.test")
    r = c_a.post(
        "/upload",
        data={"company_name": "Alpha Co", "ledger_file": (io.BytesIO(GL), "gl.csv")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert r.status_code == 302, f"upload failed: {r.status_code}"
    job_id_a = sorted(appmod.jobs.keys())[-1]

    c_b = appmod.app.test_client()
    _signup(c_b, "Bravo Firm", "bob@bravo.test")

    failures = []
    for suffix in GET_ROUTES:
        path = f"/jobs/{job_id_a}{suffix}"
        r = c_b.get(path)
        if r.status_code != 404:
            failures.append(f"GET {path} -> {r.status_code} (expected 404)")
    for suffix in POST_ROUTES:
        path = f"/jobs/{job_id_a}{suffix}"
        r = c_b.post(path)
        if r.status_code != 404:
            failures.append(f"POST {path} -> {r.status_code} (expected 404)")

    assert not failures, "Cross-firm leak(s):\n  " + "\n  ".join(failures)
    print(f"T1 OK: all {len(GET_ROUTES) + len(POST_ROUTES)} foreign job routes return 404")

    # Owner is unaffected on the read routes that don't require extra state.
    r = c_a.get(f"/jobs/{job_id_a}")
    assert r.status_code == 200, f"owner /jobs/<id> -> {r.status_code}"
    r = c_a.get(f"/jobs/{job_id_a}/account-mapping")
    assert r.status_code in (200, 302), f"owner account-mapping -> {r.status_code}"
    print("T2 OK: owner access to its own job is unaffected")

    # A nonexistent job id is also 404 (not 500), for both firms.
    for cl in (c_a, c_b):
        r = cl.get("/jobs/does-not-exist")
        assert r.status_code == 404, f"missing job -> {r.status_code}"
    print("T3 OK: nonexistent job id returns 404 (no info leak, no 500)")


if __name__ == "__main__":
    main()
    print("\nALL cross-firm isolation smoke tests passed.")
