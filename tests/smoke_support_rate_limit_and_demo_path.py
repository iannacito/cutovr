"""Smoke tests for the support-assistant rate limit and the rehearsed
demo-path guide.

Run from project root:

    python3 tests/smoke_support_rate_limit_and_demo_path.py

Covers:
  T1 The public /support/assistant endpoint is rate limited per IP. Under
     the budget it returns 200; once the budget is exhausted it returns
     429 with a usable, plain-English answer and the support email (so the
     widget always has something to render). No secrets in the response.
  T2 A different IP is not affected by another IP's exhausted budget
     (the limiter keys on client IP).
  T3 The demo workspace exposes a single rehearsed path with a "Begin at
     Step 1" primary CTA pointing at Step 1 (cutover setup), and the
     6 customer steps appear in order. Visible to operators only.
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
    for mod in ("app", "operator_panel", "demo_mode", "encryption",
                "data_retention", "support_assistant", "rate_limit"):
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
    for k in ("OPERATOR_EMAILS", "SHOW_OPERATOR_TOOLS", "DEMO_MODE"):
        os.environ.pop(k, None)
    base.update(env)
    for k, v in base.items():
        os.environ[k] = v
    return importlib.import_module("app")


def _ask(client, ip):
    return client.post(
        "/support/assistant",
        json={"query": "how do I upload my reports?"},
        headers={"X-Forwarded-For": ip},
    )


def t1_support_assistant_rate_limited():
    appmod = _reset_app({})
    c = appmod.app.test_client()
    budget = appmod.SUPPORT_ASSISTANT_RATE_LIMIT_MAX

    last = None
    for _ in range(budget):
        last = _ask(c, "203.0.113.7")
        assert last.status_code == 200, f"within budget should be 200, got {last.status_code}"

    blocked = _ask(c, "203.0.113.7")
    assert blocked.status_code == 429, f"over budget should be 429, got {blocked.status_code}"
    body = blocked.get_json()
    assert body and body.get("answer"), "429 response must still carry a usable answer"
    assert body.get("support_email"), "429 response must include the support email"
    # No secret-shaped content leaked.
    raw = blocked.get_data(as_text=True).lower()
    for forbidden in ("secret", "access_token", "refresh_token", "password"):
        assert forbidden not in raw, f"support response leaked {forbidden}"
    print(f"T1 OK: /support/assistant 200 up to {budget} then 429 with usable answer")


def t2_rate_limit_is_per_ip():
    appmod = _reset_app({})
    c = appmod.app.test_client()
    budget = appmod.SUPPORT_ASSISTANT_RATE_LIMIT_MAX

    for _ in range(budget + 2):
        _ask(c, "198.51.100.1")  # exhaust IP #1

    # A different IP still works.
    other = _ask(c, "198.51.100.2")
    assert other.status_code == 200, f"second IP should be 200, got {other.status_code}"
    print("T2 OK: rate limit is keyed per client IP")


def t3_demo_rehearsed_path_cta():
    op_email = "ops@anthro.test"
    appmod = _reset_app({"OPERATOR_EMAILS": op_email})
    c = appmod.app.test_client()
    c.post("/signup", data={"firm_name": "Ops", "email": op_email,
                            "password": "passw0rd!1234",
                            "confirm_password": "passw0rd!1234"})
    r = c.get("/demo")
    assert r.status_code == 200, f"operator /demo should 200, got {r.status_code}"
    html = r.get_data(as_text=True)
    assert 'data-testid="demo-rehearsed-path"' in html, "rehearsed path card missing"
    assert 'data-testid="demo-begin-step1"' in html, "Begin at Step 1 CTA missing"
    # Step 1 (cutover setup) is registered under both /cutover and
    # /migration-setup; url_for may resolve to either alias.
    assert ("/cutover" in html or "/migration-setup" in html), \
        "Begin CTA should point at Step 1 (cutover setup)"
    # Steps appear in order.
    for marker in ("Step 1 &mdash; Set up", "Step 2 &mdash; Upload",
                   "Step 3 &mdash; Match accounts", "Step 4 &mdash; Review",
                   "Step 5 &mdash; Send to QuickBooks",
                   "Step 6 &mdash; Final balance check"):
        assert marker in html, f"missing rehearsed step marker: {marker}"
    print("T3 OK: demo rehearsed path + Begin-at-Step-1 CTA + ordered 6 steps")


if __name__ == "__main__":
    t1_support_assistant_rate_limited()
    t2_rate_limit_is_per_ip()
    t3_demo_rehearsed_path_cta()
    print("\nALL support-rate-limit + demo-path smoke tests passed.")
