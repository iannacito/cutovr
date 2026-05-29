"""Support assistant widget + API smoke tests.

Verifies the floating "Need help?" widget renders on public/landing
pages and inside the migration app, and the deterministic FAQ
endpoint at /support/assistant returns useful answers (or a clean
support-email fallback) for representative queries.

Run from project root:

    python3 tests/smoke_support_assistant.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-for-support-assistant-32chars")

import app as appmod  # noqa: E402
import support_assistant  # noqa: E402


PAGES_WITH_WIDGET = [
    "/",
    "/login",
    "/signup",
    "/pricing",
    "/security",
    "/privacy",
    "/terms",
    "/support",
    "/onboarding",
    "/quickbooks-guide",
]


def t1_widget_rendered_on_public_pages():
    c = appmod.app.test_client()
    for path in PAGES_WITH_WIDGET:
        body = c.get(path).get_data(as_text=True)
        assert 'data-testid="support-assistant"' in body, (
            f"support assistant widget missing on {path}"
        )
        assert 'data-testid="support-assistant-toggle"' in body, (
            f"toggle button missing on {path}"
        )
        assert "Need help?" in body, f"'Need help?' label missing on {path}"
    print(f"T1 OK: assistant widget renders on {len(PAGES_WITH_WIDGET)} pages")


def t2_widget_has_suggested_topics():
    c = appmod.app.test_client()
    body = c.get("/").get_data(as_text=True)
    # All starter prompts should appear as topic buttons.
    for topic in support_assistant.suggested_topics():
        assert topic["label"] in body, f"topic '{topic['label']}' missing"
    print("T2 OK: starter prompts render in widget")


def t3_assistant_api_returns_useful_answer():
    c = appmod.app.test_client()
    r = c.post(
        "/support/assistant",
        data=json.dumps({"query": "how do I connect QuickBooks?"}),
        content_type="application/json",
    )
    assert r.status_code == 200, r.status_code
    payload = r.get_json()
    assert payload["matched"] is True, payload
    assert "QuickBooks" in payload["answer"]
    assert payload["topic"] == "quickbooks"
    print("T3 OK: assistant returns matched answer for 'connect QuickBooks'")


def t4_assistant_api_pricing_query():
    c = appmod.app.test_client()
    r = c.post(
        "/support/assistant",
        data=json.dumps({"query": "How much does this cost?"}),
        content_type="application/json",
    )
    payload = r.get_json()
    assert payload["matched"] is True
    assert "$799" in payload["answer"]
    print("T4 OK: pricing query returns the $799 anchor answer")


def t5_assistant_fallback_for_unknown_query():
    c = appmod.app.test_client()
    r = c.post(
        "/support/assistant",
        data=json.dumps({"query": "zxqv blahblah ???"}),
        content_type="application/json",
    )
    payload = r.get_json()
    assert payload["matched"] is False
    assert payload["support_email"] in payload["answer"]
    print("T5 OK: unknown query falls back to support-email message")


def t6_assistant_does_not_promise_private_access():
    """Critical: the assistant must never claim to read a customer's
    QuickBooks or PCLaw data. Spot-check the fallback + every FAQ answer
    to make sure that promise isn't accidentally made."""
    forbidden = (
        "i can access your quickbooks",
        "i can see your quickbooks",
        "i can read your pclaw",
        "i'll look up your account",
        "i have access to your",
    )
    for _topic, _kw, text in support_assistant._FAQ:
        lowered = text.lower()
        for bad in forbidden:
            assert bad not in lowered, f"answer leaks private-access claim: {bad!r}"
    # Fallback contains the safety disclaimer.
    fallback = support_assistant.answer("???")
    assert "can't see your account" in fallback["answer"].lower() or \
        "can't" in fallback["answer"].lower()
    print("T6 OK: assistant never claims private-data access")


def t7_assistant_endpoint_safe_with_empty_body():
    c = appmod.app.test_client()
    r = c.post("/support/assistant", data="", content_type="application/json")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["matched"] is False
    assert payload["support_email"]
    print("T7 OK: assistant endpoint tolerates empty body")


def t8_support_page_shows_inline_assistant_prompts():
    c = appmod.app.test_client()
    body = c.get("/support").get_data(as_text=True)
    assert 'data-testid="support-assistant-inline"' in body, \
        "support page should include the inline assistant prompts section"
    print("T8 OK: support page surfaces inline assistant prompts")


def t9_widget_has_minimize_control():
    """The widget must ship with an accessible minimize control and start
    in the 'closed' state. JS toggles data-state between 'open' and
    'closed' so CSS can hide the launcher pill while the panel is open
    and keep the minimize control unambiguous."""
    c = appmod.app.test_client()
    body = c.get("/").get_data(as_text=True)
    assert 'data-testid="support-assistant-close"' in body, \
        "minimize/close button missing"
    # Accessible name + visible label so lawyers (not accountants) can
    # find it without hunting for a tiny X.
    assert 'aria-label="Minimize support assistant"' in body, \
        "minimize button should have a clear aria-label"
    assert "Minimize" in body, "visible 'Minimize' label missing"
    # Initial state is closed (panel hidden, launcher pill visible).
    assert 'data-state="closed"' in body, \
        "widget should start in the closed/minimized state"
    # The minimize control is a real <button>, keyboard focusable.
    assert 'class="support-assistant__close"' in body
    print("T9 OK: widget renders an accessible Minimize control, starts closed")


def t10_close_button_is_a_button_element():
    """Regression guard: the minimize control must be a real <button>
    (not a div/span) so it's keyboard focusable and announced as a
    button by screen readers."""
    c = appmod.app.test_client()
    body = c.get("/").get_data(as_text=True)
    # Look for `<button ... class="support-assistant__close"` — order of
    # attributes can vary, so check both fragments are present in the
    # same opening tag.
    idx = body.find('class="support-assistant__close"')
    assert idx != -1, "close button class missing"
    # Walk back to the nearest '<' to confirm the tag is <button.
    tag_start = body.rfind("<", 0, idx)
    assert tag_start != -1
    opening = body[tag_start : tag_start + 8]
    assert opening.startswith("<button"), \
        f"minimize control must be a <button> element, got: {opening!r}"
    print("T10 OK: minimize control is a real <button>")


if __name__ == "__main__":
    t1_widget_rendered_on_public_pages()
    t2_widget_has_suggested_topics()
    t3_assistant_api_returns_useful_answer()
    t4_assistant_api_pricing_query()
    t5_assistant_fallback_for_unknown_query()
    t6_assistant_does_not_promise_private_access()
    t7_assistant_endpoint_safe_with_empty_body()
    t8_support_page_shows_inline_assistant_prompts()
    t9_widget_has_minimize_control()
    t10_close_button_is_a_button_element()
    print("\nAll support assistant smoke tests OK.")
