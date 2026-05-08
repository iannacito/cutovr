"""CSRF smoke test (the only suite that does NOT set CSRF_DISABLE).

Run from project root:

    python3 tests/smoke_csrf.py

Verifies:
  T1 GET /signup and /login render a hidden csrf_token input.
  T2 POST /signup without csrf_token is rejected (no firm created).
  T3 POST /signup with the correct csrf_token succeeds.
  T4 POST /logout without csrf_token is rejected (still logged in).
  T5 POST /logout with the correct csrf_token logs out.
  T6 Reused-but-stale token from a different session is rejected.
  T7 Login form needs CSRF too — bad token rejected, good token accepted.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Use throwaway DBs so reruns are deterministic. We deliberately do NOT
# set CSRF_DISABLE here — this suite exists to exercise the real check.
APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.pop("CSRF_DISABLE", None)
os.environ.setdefault("SECRET_KEY", "smoke-secret")

import app as appmod  # noqa: E402

_CSRF_RE = re.compile(
    r'<input[^>]+name="csrf_token"[^>]+value="([^"]+)"', re.IGNORECASE
)


def get_csrf(client, path="/login"):
    r = client.get(path)
    m = _CSRF_RE.search(r.data.decode("utf-8", "replace"))
    assert m, f"No csrf_token in {path}"
    return m.group(1)


def main():
    # T1: token rendered into the form
    c = appmod.app.test_client()
    for path in ("/login", "/signup"):
        body = c.get(path).data.decode()
        assert _CSRF_RE.search(body), f"{path} has no csrf_token input"
    print("T1 OK: login + signup forms render a csrf_token input")

    # T2: signup without csrf_token rejected
    r = c.post(
        "/signup",
        data={"firm_name": "X", "email": "no-csrf@x.test",
              "password": "passw0rd!1234", "confirm_password": "passw0rd!1234"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "/login" in r.headers["Location"], r.headers["Location"]
    assert appmod.db.authenticate("no-csrf@x.test", "passw0rd!1234") is None
    print("T2 OK: signup without csrf_token rejected, no user created")

    # T3: signup with correct csrf_token succeeds
    token = get_csrf(c, "/signup")
    r = c.post(
        "/signup",
        data={"firm_name": "Acme", "email": "alice@acme.test",
              "password": "passw0rd!1234", "confirm_password": "passw0rd!1234",
              "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard"), r.headers
    assert appmod.db.authenticate("alice@acme.test", "passw0rd!1234"), "user not created"
    print("T3 OK: signup with csrf_token succeeded")

    # T4: logout without csrf_token rejected — user remains logged in
    r = c.post("/logout", follow_redirects=False)
    assert r.status_code == 302 and "/login" not in r.headers["Location"]
    assert c.get("/dashboard").status_code == 200, "should still be logged in"
    print("T4 OK: logout without csrf_token rejected, session intact")

    # T5: logout with csrf_token works
    token = get_csrf(c, "/dashboard")
    r = c.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 302
    # Now /dashboard should bounce to /login
    assert "/login" in c.get("/dashboard").headers.get("Location", "")
    print("T5 OK: logout with csrf_token logged user out")

    # T6: stale token from a different client is rejected
    c_other = appmod.app.test_client()
    other_token = get_csrf(c_other, "/login")
    # Log alice back in (with HER own token, fetched fresh)
    alice_login = get_csrf(c, "/login")
    r = c.post(
        "/login",
        data={"email": "alice@acme.test", "password": "passw0rd!1234",
              "csrf_token": alice_login},
        follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")
    # Try to logout with the OTHER session's token
    r = c.post("/logout", data={"csrf_token": other_token}, follow_redirects=False)
    assert r.status_code == 302
    # alice still logged in
    assert c.get("/dashboard").status_code == 200, "stale-token logout should not have worked"
    print("T6 OK: stale token from a different session rejected")

    # T7: login also needs CSRF
    c_fresh = appmod.app.test_client()
    r = c_fresh.post(
        "/login",
        data={"email": "alice@acme.test", "password": "passw0rd!1234"},
        follow_redirects=False,
    )
    # No token → redirected (back to /login).
    assert "/login" in r.headers["Location"]
    assert c_fresh.get("/dashboard").status_code == 302  # not logged in
    # Try with the right token
    fresh_token = get_csrf(c_fresh, "/login")
    r = c_fresh.post(
        "/login",
        data={"email": "alice@acme.test", "password": "passw0rd!1234",
              "csrf_token": fresh_token},
        follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")
    print("T7 OK: login requires csrf_token")

    print("\nALL CSRF SMOKE TESTS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        for p in (APP_DB, HIST_DB):
            try:
                os.unlink(p)
            except OSError:
                pass
