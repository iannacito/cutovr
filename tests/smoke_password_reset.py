"""Smoke test: password reset + rate limiting + password length policy.

Run from project root:

    python3 tests/smoke_password_reset.py

Covers:
  T1  Signup now rejects passwords shorter than 12 chars.
  T2  /forgot-password returns the same generic message whether the email
      exists or not, and never echoes the token/URL.
  T3  Requesting a reset for a real account creates a hashed token row in
      the DB (plaintext token is not stored).
  T4  The emitted reset URL actually works: GET renders the form, POST
      with a valid new password succeeds.
  T5  A reset token can only be used once; re-using it fails closed.
  T6  The /reset-password page enforces the 12-char minimum.
  T7  Login rate limit triggers after N failed attempts and returns a
      generic friendly message (no lockout-leak).
  T8  Forgot-password rate limit triggers after N attempts from one IP.
  T9  An expired token is rejected.
"""

import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-password-reset-test-key-xx")

# Ensure SMTP is NOT configured so we exercise the "no SMTP" branch without
# actually trying to send mail.
for var in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
    os.environ.pop(var, None)

import app as appmod  # noqa: E402


GOOD_PW = "CorrectHorseBattery12!"  # 22 chars
NEW_PW = "anotherLongPassword!99"   # 22 chars


def fresh_client():
    return appmod.app.test_client()


def signup(client, firm, email, password=GOOD_PW):
    return client.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
        follow_redirects=False,
    )


def _capture_reset_url_for(email):
    """Read the most recent password_reset_tokens row for this email and
    build the URL the way the route does.

    We don't parse a log file — the route stores the hash. The only way
    to get the *plaintext* token back is to grab it in-process. For the
    smoke test we monkeypatch _send_reset_email to capture the URL.
    """
    # Nothing to do here; callers install a monkeypatch instead.
    raise NotImplementedError


def main():
    # Install a capture shim around _send_reset_email so the test can pluck
    # the plaintext reset URL out of the flow without SMTP.
    captured = {}
    orig_send = appmod._send_reset_email

    def capture(user, url):
        captured["url"] = url
        captured["user_email"] = user["email"]
        return False  # pretend SMTP not configured

    appmod._send_reset_email = capture

    try:
        # T1 short password rejected
        c = fresh_client()
        r = c.post(
            "/signup",
            data={"firm_name": "Too Short", "email": "short@acme.test",
                  "password": "short123", "confirm_password": "short123"},
            follow_redirects=False,
        )
        assert r.status_code == 200, r.status_code
        assert b"at least 12" in r.data, r.data[:200]
        print("T1 OK: signup rejects <12 char password")

        # Signup a legit user with a long password for the rest of the tests
        c = fresh_client()
        r = signup(c, "Acme Law", "alice@acme.test")
        assert r.status_code == 302, r.status_code

        # T2 generic response for existing vs unknown email
        c2 = fresh_client()
        r = c2.post("/forgot-password", data={"email": "alice@acme.test"},
                    follow_redirects=False)
        assert r.status_code == 200
        body_known = r.data.decode()
        assert "If an account with that email exists" in body_known

        c3 = fresh_client()
        r = c3.post("/forgot-password", data={"email": "nobody@nowhere.test"},
                    follow_redirects=False)
        assert r.status_code == 200
        body_unknown = r.data.decode()
        assert "If an account with that email exists" in body_unknown

        # Body must not contain the reset token / URL for either path
        assert "/reset-password/" not in body_known
        assert "/reset-password/" not in body_unknown
        assert "token" not in body_known.lower().split("password")[0]
        print("T2 OK: generic forgot-password response; no token leaked")

        # T3 hashed token stored, plaintext not in DB
        assert "url" in captured, "capture shim never fired"
        m = re.search(r"/reset-password/([A-Za-z0-9_\-]+)", captured["url"])
        assert m, f"no token in captured url: {captured['url']}"
        plain_token = m.group(1)
        assert len(plain_token) >= 30

        # Pull the stored row and confirm it's hashed
        import hashlib
        expected_hash = hashlib.sha256(plain_token.encode("utf-8")).hexdigest()
        row = appmod.db.get_password_reset_token(expected_hash)
        assert row is not None, "reset token not stored"
        assert row["used_at"] is None
        # Plaintext token must NOT appear anywhere in the row
        assert plain_token not in str(row), "plaintext token leaked into DB row"
        print("T3 OK: reset token stored hashed; plaintext absent from DB")

        # T4 reset flow end-to-end
        c4 = fresh_client()
        r = c4.get(f"/reset-password/{plain_token}")
        assert r.status_code == 200, r.status_code
        assert b"Reset" in r.data
        r = c4.post(
            f"/reset-password/{plain_token}",
            data={"password": NEW_PW, "confirm_password": NEW_PW},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.status_code
        assert "/login" in r.headers["Location"]
        # New password works
        user = appmod.db.authenticate("alice@acme.test", NEW_PW)
        assert user is not None, "new password should authenticate"
        # Old password no longer works
        assert appmod.db.authenticate("alice@acme.test", GOOD_PW) is None
        print("T4 OK: reset link accepted and password updated")

        # T5 token single-use: reusing it fails
        c5 = fresh_client()
        r = c5.post(
            f"/reset-password/{plain_token}",
            data={"password": "yetAnother1234!", "confirm_password": "yetAnother1234!"},
            follow_redirects=False,
        )
        # On reuse we redirect back to /forgot-password with a flash
        assert r.status_code == 302, r.status_code
        assert "/forgot-password" in r.headers["Location"]
        # Password should still be NEW_PW (not changed again)
        assert appmod.db.authenticate("alice@acme.test", NEW_PW) is not None
        assert appmod.db.authenticate("alice@acme.test", "yetAnother1234!") is None
        print("T5 OK: used reset token can't be reused")

        # T6 short password rejected on reset as well
        # Request a new reset link.
        captured.clear()
        c6 = fresh_client()
        c6.post("/forgot-password", data={"email": "alice@acme.test"})
        m = re.search(r"/reset-password/([A-Za-z0-9_\-]+)", captured["url"])
        tok2 = m.group(1)
        r = c6.post(
            f"/reset-password/{tok2}",
            data={"password": "tooshort", "confirm_password": "tooshort"},
            follow_redirects=False,
        )
        assert r.status_code == 200
        assert b"at least 12" in r.data
        # Token still usable (not consumed by failed validation)
        r = c6.post(
            f"/reset-password/{tok2}",
            data={"password": "longEnoughPW!!11", "confirm_password": "longEnoughPW!!11"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        print("T6 OK: reset enforces 12-char minimum")

        # T7 login rate limit
        # Fresh DB state: the limiter counts across prior T4 attempts but
        # those were a successful login + some auth checks via db.authenticate.
        # Use a brand-new email-and-IP combo to get a clean window.
        c7 = fresh_client()
        signup(c7, "Rate Co", "rate@acme.test")
        # Logout of the implicit session so /login actually runs the limiter
        c7.post("/logout")
        hits_429 = False
        for i in range(appmod.LOGIN_RATE_LIMIT_MAX + 3):
            r = c7.post("/login",
                        data={"email": "rate@acme.test", "password": "wrongpw"},
                        follow_redirects=False)
            if r.status_code == 429:
                hits_429 = True
                break
        assert hits_429, "expected login rate limit to trigger"
        # Friendly message is shown
        assert b"Too many attempts" in r.data
        # No token / secret leaked in response
        assert b"password_hash" not in r.data
        assert b"SECRET_KEY" not in r.data
        print("T7 OK: login rate limit returns friendly generic message")

        # T8 forgot-password rate limit
        c8 = fresh_client()
        hits_429 = False
        for i in range(appmod.FORGOT_RATE_LIMIT_MAX + 3):
            r = c8.post("/forgot-password",
                        data={"email": "someone@nowhere.test"},
                        follow_redirects=False)
            if r.status_code == 429:
                hits_429 = True
                break
        assert hits_429, "expected forgot-password rate limit to trigger"
        assert b"Too many attempts" in r.data
        print("T8 OK: forgot-password rate limit triggers with friendly message")

        # T9 expired token rejected
        from datetime import datetime, timedelta
        captured.clear()
        # Clear the forgot-password rate-limit bucket left over from T8 so
        # this request actually gets through to the token-issuing path.
        with appmod.db._conn() as conn:
            conn.execute("DELETE FROM rate_limit_events")
        c9 = fresh_client()
        c9.post("/forgot-password", data={"email": "alice@acme.test"})
        m = re.search(r"/reset-password/([A-Za-z0-9_\-]+)", captured["url"])
        tok3 = m.group(1)
        tok3_hash = hashlib.sha256(tok3.encode("utf-8")).hexdigest()
        # Manually backdate the expiry.
        past = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
        with appmod.db._conn() as conn:
            conn.execute(
                "UPDATE password_reset_tokens SET expires_at = ? WHERE token_hash = ?",
                (past, tok3_hash),
            )
        r = c9.get(f"/reset-password/{tok3}", follow_redirects=False)
        assert r.status_code == 302 and "/forgot-password" in r.headers["Location"]
        print("T9 OK: expired reset token rejected")

        print("\nALL PASSWORD-RESET / RATE-LIMIT SMOKE TESTS PASSED")
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
