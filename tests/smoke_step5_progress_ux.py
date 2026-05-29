"""Step 5 send-to-QuickBooks progress UX smoke test.

Run from project root:

    python3 tests/smoke_step5_progress_ux.py

Larger imports can take several minutes. Without a visible loading
state, lawyers see a frozen-looking button and try to click again or
close the tab. This pins:

  T1 The Step 5 send page renders the loading panel HTML (hidden by
     default — display:none) with the plain-English copy Dan asked
     for ('Sending your data to QuickBooks', 'a few minutes',
     'keep this tab open').
  T2 The form has a submit handler that disables the button and
     reveals the panel.
  T3 The CTA button still says 'Send to QuickBooks' so the
     pre-existing Step 5 contract is unchanged.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-step5-progress")

import app as appmod  # noqa: E402

TEMPLATE = (ROOT / "templates" / "send-to-qbo.html").read_text(encoding="utf-8")


def t1_loading_copy_present_in_template():
    for needle in (
        "Sending your data to QuickBooks",
        "may take a few minutes",
        "keep this tab open",
        'role="status"',
        "send-to-qbo-progress",
    ):
        assert needle in TEMPLATE, f"missing {needle!r} in send-to-qbo.html"
    # Loading panel hidden on first render.
    assert 'id="send-to-qbo-progress"' in TEMPLATE
    assert "display:none" in TEMPLATE.split("send-to-qbo-progress", 1)[1][:400], \
        "loading panel should be hidden until form submit"
    print("T1 OK: send-to-qbo loading panel + plain-English copy present")


def t2_submit_handler_disables_button_and_shows_panel():
    # The JS handler is inline in the template — assert the key actions
    # are wired up so we won't silently regress to a non-blocking form.
    for needle in (
        "send-to-qbo-form",
        "send-to-qbo-btn",
        'addEventListener("submit"',
        "btn.disabled = true",
        'panel.style.display = "block"',
    ):
        assert needle in TEMPLATE, f"missing JS hook {needle!r}"
    print("T2 OK: form submit disables CTA and reveals progress panel")


def t3_cta_label_unchanged_for_send():
    assert "Send to QuickBooks" in TEMPLATE
    # Mid-submit copy is the loading message
    assert "Sending your data to QuickBooks…" in TEMPLATE or "Sending your data to QuickBooks&hellip;" in TEMPLATE
    print("T3 OK: Step 5 CTA still says 'Send to QuickBooks'")


if __name__ == "__main__":
    t1_loading_copy_present_in_template()
    t2_submit_handler_disables_button_and_shows_panel()
    t3_cta_label_unchanged_for_send()
    print("ALL STEP 5 PROGRESS UX SMOKE TESTS PASSED")
