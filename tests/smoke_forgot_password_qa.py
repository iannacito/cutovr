"""Targeted QA for the forgot-password flow + cross-firm 404 + support
email placeholder handling.

Run from project root:

    python3 tests/smoke_forgot_password_qa.py

Covers:
  Q1  Login page shows a visible "Forgot your password?" link.
  Q2  /forgot-password GET renders with reassuring plain-English copy.
  Q3  Generic success message uses the "If an account ... exists" wording
      (so we can't be used as an account-existence oracle).
  Q4  Reset page renders for a freshly-issued token; uses "Reset
      password" headline copy.
  Q5  Logging in with the new password works and the old one fails.
  Q6  Cross-firm POST to /jobs/<other>/import-to-qbo returns 404, not a
      friendly recovery 200 (regression guard for the wrapper exception
      swallow).
  Q7  When SUPPORT_EMAIL is the deploy-default placeholder, no rendered
      page leaks "your-domain.example" through the support-assistant
      widget that ships on every page.
"""

import io
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-forgot-qa-test-key-x" * 2)

for var in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
            "SMTP_FROM", "SUPPORT_EMAIL", "SECURITY_EMAIL"):
    os.environ.pop(var, None)

import app as appmod  # noqa: E402


GOOD_PW = "CorrectHorseBattery12!"
NEW_PW = "anotherLongPassword!99"


def fresh_client():
    return appmod.app.test_client()


def _signup(c, firm, email, password=GOOD_PW):
    return c.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
        follow_redirects=False,
    )


def main():
    captured = {}
    orig_send = appmod._send_reset_email

    def capture(user, url):
        captured["url"] = url
        return False

    appmod._send_reset_email = capture

    try:
        # Q1: login shows forgot-password link
        c = fresh_client()
        r = c.get("/login")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Forgot your password?" in body, "login should link to forgot password"
        assert "/forgot-password" in body
        print("Q1 OK: login shows 'Forgot your password?' link")

        # Q2: forgot-password GET has reassuring plain-English copy
        r = c.get("/forgot-password")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Forgot your" in body and "password" in body
        assert "secure" in body.lower() or "email" in body.lower()
        # Form must POST and have email field
        assert 'name="email"' in body
        print("Q2 OK: /forgot-password GET renders plain-English form")

        # Q3: generic success wording
        _signup(fresh_client(), "Alpha", "alpha@a.test")
        c2 = fresh_client()
        r = c2.post(
            "/forgot-password",
            data={"email": "alpha@a.test"},
            follow_redirects=False,
        )
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "If an account with that email exists" in body
        # Same wording when the email is unknown.
        c3 = fresh_client()
        r = c3.post(
            "/forgot-password",
            data={"email": "ghost@nowhere.test"},
            follow_redirects=False,
        )
        body2 = r.get_data(as_text=True)
        assert "If an account with that email exists" in body2
        # Neither response leaks the token URL.
        assert "/reset-password/" not in body
        assert "/reset-password/" not in body2
        print("Q3 OK: generic success wording, no token in body")

        # Q4: reset-password GET renders for a valid token
        assert "url" in captured, "capture shim should have fired"
        m = re.search(r"/reset-password/([A-Za-z0-9_\-]+)", captured["url"])
        assert m, captured["url"]
        token = m.group(1)
        c4 = fresh_client()
        r = c4.get(f"/reset-password/{token}")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Reset" in body and "password" in body
        print("Q4 OK: reset-password GET renders for valid token")

        # Q5: successful reset works and old password fails
        r = c4.post(
            f"/reset-password/{token}",
            data={"password": NEW_PW, "confirm_password": NEW_PW},
            follow_redirects=False,
        )
        assert r.status_code == 302 and "/login" in r.headers["Location"]
        assert appmod.db.authenticate("alpha@a.test", NEW_PW) is not None
        assert appmod.db.authenticate("alpha@a.test", GOOD_PW) is None
        print("Q5 OK: new password works, old password fails")

        # Q6: cross-firm /import-to-qbo returns 404, not a 200 recovery
        # card. Regression guard for the exception-swallow bug.
        ca = fresh_client()
        _signup(ca, "Owner Firm", "owner@o.test")
        gl = (b"Date,Account,Description,Debit,Credit\n"
              b"2024-01-01,Cash,Open,100,\n"
              b"2024-01-01,Equity,Open,,100\n")
        r = ca.post(
            "/upload",
            data={"company_name": "Owner Co",
                  "ledger_file": (io.BytesIO(gl), "gl.csv")},
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        # Find the just-created job for the owner.
        owner = appmod.db.authenticate("owner@o.test", GOOD_PW)
        owner_jobs = appmod.db.list_jobs_for_firm(owner["firm_id"])
        assert owner_jobs, "owner should have at least one job"
        owner_job_id = owner_jobs[0]["id"]

        # A different firm tries to drive imports on owner's job.
        cb = fresh_client()
        _signup(cb, "Other Firm", "other@o.test")
        r = cb.post(f"/jobs/{owner_job_id}/import-to-qbo")
        assert r.status_code == 404, (
            f"cross-firm /import-to-qbo should 404 (was {r.status_code})"
        )
        print("Q6 OK: cross-firm /import-to-qbo returns 404, no recovery leak")

        # Q7: placeholder support email must not leak through the global
        # support-assistant widget. Hit a few high-traffic pages.
        c7 = fresh_client()
        for path in ("/", "/login", "/forgot-password", "/signup", "/support"):
            r = c7.get(path)
            assert r.status_code in (200, 302), (path, r.status_code)
            if r.status_code != 200:
                continue
            body = r.get_data(as_text=True)
            assert "your-domain.example" not in body, (
                f"{path} leaks placeholder support email"
            )
        print("Q7 OK: no page leaks 'your-domain.example' through support widget")

        print("\nALL FORGOT-PASSWORD QA CHECKS PASSED")
    finally:
        appmod._send_reset_email = orig_send


if __name__ == "__main__":
    try:
        main()
    finally:
        for p in (APP_DB, HIST_DB):
            try:
                os.unlink(p)
            except OSError:
                pass
