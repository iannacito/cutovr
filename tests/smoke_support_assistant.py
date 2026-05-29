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


def t11_panel_hidden_attribute_actually_hides_panel():
    """Real bug from production: PR #76 toggled `panel.hidden` and
    `data-state`, but the panel's own CSS rule set `display: flex` —
    which beats the UA stylesheet's `[hidden]{display:none}`. So
    clicking Minimize updated attributes but the panel stayed visible
    on screen. The user-facing symptom: "clicking minimize does
    nothing."

    Without an explicit author rule like
    `.support-assistant__panel[hidden] { display: none; }`, the
    `hidden` attribute is purely cosmetic on this element. This test
    pins down that rule so it cannot regress."""
    css_path = ROOT / "static" / "style.css"
    css = css_path.read_text()
    # The override must exist for ANY panel-class selector combined
    # with [hidden]. We accept the canonical form below; if the
    # selector is refactored, update this guard accordingly.
    assert ".support-assistant__panel[hidden]" in css, (
        "missing CSS override; clicking Minimize will set [hidden] but "
        "the panel's display:flex rule will keep it visible. Add: "
        ".support-assistant__panel[hidden] { display: none; }"
    )
    # And that rule must actually set display:none, not just exist.
    idx = css.find(".support-assistant__panel[hidden]")
    chunk = css[idx : idx + 200]
    assert "display: none" in chunk or "display:none" in chunk, (
        f"[hidden] override must set display:none — got: {chunk!r}"
    )
    print("T11 OK: CSS [hidden] override is present so Minimize actually hides")


def t12_minimize_js_drives_visibility_via_hidden_attribute():
    """The JS contract assumed by the CSS override above: setOpen(false)
    must set `panel.hidden = true` (not just data-state). If a future
    refactor switches to data-state-only, the CSS override would no
    longer fire and the bug returns silently. Pin the contract."""
    js_path = ROOT / "static" / "support-assistant.js"
    js = js_path.read_text()
    assert "panel.hidden = true" in js, (
        "support-assistant.js must set panel.hidden=true when closing — "
        "the CSS [hidden] override depends on this attribute"
    )
    assert "panel.hidden = false" in js, (
        "support-assistant.js must set panel.hidden=false when opening"
    )
    # Close button click handler must call setOpen(false) or equivalent.
    assert ".support-assistant__close" in js, \
        "JS must locate the close button by class"
    print("T12 OK: JS sets panel.hidden to drive the [hidden] CSS override")


def t14_widget_loads_stylesheet_and_script_app_wide():
    """The Minimize fix is a CSS rule. It only helps if `style.css` is
    loaded on every page where the widget renders, alongside the JS
    that drives `panel.hidden`. The widget HTML, the <link
    rel="stylesheet" href=".../style.css">, and the <script
    src=".../support-assistant.js"> all live in the shared _base.html
    template, so any page that `extends "_base.html"` inherits all
    three together.

    This test does two things:

      1. Confirms every Jinja template that defines a user-facing
         page extends `_base.html`. If a future page is added that
         hand-rolls its <html> and skips _base, the widget + fix
         would silently not apply to it.
      2. Walks a representative slice of customer-facing AND
         authenticated app routes, renders each, and asserts the
         widget element, the style.css link, and the
         support-assistant.js script tag are all present in the same
         response. That's the closest a Flask test client can get to
         "the fix applies app-wide.\""""
    templates_dir = ROOT / "templates"
    skip = {"_base.html", "_workflow_stepper.html"}
    non_extending = []
    for tpl in sorted(templates_dir.glob("*.html")):
        if tpl.name in skip:
            continue
        head = tpl.read_text().splitlines()[0:3]
        if not any('extends "_base.html"' in line for line in head):
            non_extending.append(tpl.name)
    assert not non_extending, (
        "These page templates do not extend _base.html, so the support "
        "assistant widget + Minimize CSS fix would not apply to them: "
        f"{non_extending}. Either extend _base.html or document why."
    )

    # Representative sample across public + app-shell pages. Keeps the
    # check broad without exploding test time.
    sample_paths = [
        "/",                  # landing
        "/login",
        "/signup",
        "/pricing",
        "/security",
        "/privacy",
        "/terms",
        "/support",
        "/onboarding",
        "/quickbooks-guide",
        "/about",
        "/forgot-password",
    ]
    c = appmod.app.test_client()
    for path in sample_paths:
        resp = c.get(path)
        # Some routes may redirect (auth, etc.) — follow once.
        if resp.status_code in (301, 302, 303, 307, 308):
            resp = c.get(path, follow_redirects=True)
        if resp.status_code != 200:
            # Skip routes that genuinely can't render without auth /
            # state — but flag them so we don't silently drop coverage.
            print(f"T14 NOTE: {path} returned {resp.status_code}, skipping")
            continue
        body = resp.get_data(as_text=True)
        assert 'data-testid="support-assistant"' in body, (
            f"widget HTML missing on {path}"
        )
        assert "static/style.css" in body or 'href="/static/style.css' in body, (
            f"style.css link missing on {path} — Minimize CSS fix won't load"
        )
        assert "support-assistant.js" in body, (
            f"support-assistant.js missing on {path} — toggle/Minimize won't bind"
        )
    print(
        f"T14 OK: all {len([t for t in templates_dir.glob('*.html') if t.name not in skip])} "
        "page templates extend _base.html; widget + CSS + JS co-load on every sampled route"
    )


def t13_minimize_round_trip_via_jsdom_if_available():
    """End-to-end functional check: parse the page HTML, inject the
    real static CSS, run the real static JS, simulate the click on
    Minimize, the click on the launcher, and an ESC press, and assert
    the panel's computed display flips correctly on each step.

    Caveat: jsdom's CSS engine honors [hidden]{display:none} more
    aggressively than real Chromium/Firefox, so this test alone would
    not catch the specific PR #76 regression where author CSS
    `display: flex` beat the UA [hidden] rule. T11 (the rule-based
    CSS guard) is the load-bearing check for that. T13 is the
    structural functional check that the click + state + ESC wiring
    all line up end-to-end.

    Skips cleanly if Node + jsdom aren't available — the rule-based
    guards above (T11, T12) still pin the failure mode."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        print("T13 SKIP: node not available; relying on T11/T12 rule guards")
        return

    runner = ROOT / "tests" / "jsdom_support_assistant.js"
    if not runner.exists():
        print(f"T13 SKIP: runner {runner} missing")
        return

    # Locate a jsdom install. We don't ship one — try a couple of
    # common locations the dev/CI environment might have.
    candidates = [
        ROOT / "node_modules",
        Path("/tmp/node_modules"),
        Path.home() / "node_modules",
    ]
    jsdom_root = None
    for c in candidates:
        if (c / "jsdom").exists():
            jsdom_root = c
            break
    if jsdom_root is None:
        print("T13 SKIP: jsdom not installed (npm i jsdom in /tmp or repo root)")
        return

    env = os.environ.copy()
    env["NODE_PATH"] = str(jsdom_root)
    env["SUPPORT_ASSISTANT_REPO_ROOT"] = str(ROOT)
    result = subprocess.run(
        [node, str(runner)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(
            "jsdom minimize round-trip failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    print("T13 OK: jsdom round-trip — Minimize hides panel, launcher reopens, ESC closes")


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
    t11_panel_hidden_attribute_actually_hides_panel()
    t12_minimize_js_drives_visibility_via_hidden_attribute()
    t14_widget_loads_stylesheet_and_script_app_wide()
    t13_minimize_round_trip_via_jsdom_if_available()
    print("\nAll support assistant smoke tests OK.")
