"""Smoke tests for the top-left workflow back-to-previous-step button.

The customer-facing stepper renders a primary "Next step" CTA at the
top-right and now also a secondary "Back to Step N: <label>" link at
the top-left so a customer can step backward through the migration
without hunting for the right entry route.

Run from project root:

    python3 tests/smoke_workflow_back_button.py

Covers:
  B1  Step 1 (Setup) has NO back button — there is no previous step.
  B2  Step 2 (Upload) shows "Back to Step 1: Setup" pointing at
      /cutover, not a "#" anchor.
  B3  Step 3 (Match) shows "Back to Step 2: Upload reports" pointing
      at a real route (/dashboard#intake — anchored on the upload area).
  B4  Step 4..6 each surface a real previous-step entry route — never
      a bare "#" anchor.
  B5  Page render: the migration-checklist page actually shows the
      back link in the stepper markup once Step 1 is done.
  B6  Page render: the dashboard page on a brand-new firm (Step 1)
      does NOT render the back link.
"""

import os
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
os.environ.setdefault("SECRET_KEY", "smoke-secret-back-button")

import customer_workflow as cw  # noqa: E402
from cutover_workflow import (  # noqa: E402
    ChecklistItem,
    STATUS_NOT_STARTED, STATUS_IN_PROGRESS, STATUS_COMPLETE,
    STEP_CUTOVER_SETUP, STEP_COA_UPLOAD, STEP_OPENING_TB, STEP_GL_UPLOAD,
    STEP_ENDING_TB, STEP_TRUST_LISTING, STEP_QBO_CONNECT,
    STEP_ACCOUNT_MAPPING, STEP_DRY_RUN, STEP_PROD_IMPORT,
    STEP_RECONCILIATION,
)
import app as appmod  # noqa: E402


def _items(**overrides):
    defaults = {
        STEP_CUTOVER_SETUP: STATUS_NOT_STARTED,
        STEP_COA_UPLOAD: STATUS_NOT_STARTED,
        STEP_OPENING_TB: STATUS_NOT_STARTED,
        STEP_GL_UPLOAD: STATUS_NOT_STARTED,
        STEP_ENDING_TB: STATUS_NOT_STARTED,
        STEP_TRUST_LISTING: STATUS_NOT_STARTED,
        STEP_QBO_CONNECT: STATUS_NOT_STARTED,
        STEP_ACCOUNT_MAPPING: STATUS_NOT_STARTED,
        STEP_DRY_RUN: STATUS_NOT_STARTED,
        STEP_PROD_IMPORT: STATUS_NOT_STARTED,
        STEP_RECONCILIATION: STATUS_NOT_STARTED,
    }
    defaults.update(overrides)
    planned = {STEP_TRUST_LISTING}
    return [
        ChecklistItem(
            key=k, label=k, status=s, summary="",
            planned=(k in planned),
        )
        for k, s in defaults.items()
    ]


def _current(items):
    stages = cw.build_customer_stages(items)
    return cw.current_stage(stages), stages


def b1_step1_has_no_back_button():
    cur, _ = _current(_items())
    assert cur is not None and cur.key == cw.STAGE_SETUP, cur and cur.key
    assert cur.back_label == "" and cur.back_url == "", \
        (cur.back_label, cur.back_url)
    print("OK  B1  Step 1 (Setup) has no back button — no previous step")


def b2_step2_shows_back_to_setup():
    items = _items(**{STEP_CUTOVER_SETUP: STATUS_COMPLETE})
    cur, _ = _current(items)
    assert cur.key == cw.STAGE_UPLOAD, cur.key
    assert cur.back_label == "Back to Step 1: Setup", cur.back_label
    # /cutover is the canonical entry — never a dead anchor.
    assert cur.back_url and cur.back_url != "#", cur.back_url
    assert "/cutover" in cur.back_url, cur.back_url
    print("OK  B2  Step 2 shows 'Back to Step 1: Setup' -> /cutover")


def b3_step3_shows_back_to_upload_reports():
    items = _items(**{
        STEP_CUTOVER_SETUP: STATUS_COMPLETE,
        STEP_COA_UPLOAD: STATUS_COMPLETE,
        STEP_OPENING_TB: STATUS_COMPLETE,
        STEP_GL_UPLOAD: STATUS_COMPLETE,
    })
    cur, _ = _current(items)
    assert cur.key == cw.STAGE_MATCH, cur.key
    assert cur.back_label == "Back to Step 2: Upload reports", cur.back_label
    assert cur.back_url and cur.back_url != "#", cur.back_url
    # Upload lives on /dashboard's intake anchor — a real, working anchor
    # on an existing page, not a dead "#" link.
    assert "/dashboard" in cur.back_url, cur.back_url
    assert cur.back_url.endswith("#intake"), cur.back_url
    print("OK  B3  Step 3 shows 'Back to Step 2: Upload reports' -> "
          "/dashboard#intake")


def b4_no_dead_anchors_anywhere():
    """Walk each stage as current and confirm back_url is never bare '#'."""
    progressions = [
        # (overrides, expected_current_key, expected_back_label_prefix)
        ({}, cw.STAGE_SETUP, None),  # no back button
        ({STEP_CUTOVER_SETUP: STATUS_COMPLETE},
         cw.STAGE_UPLOAD, "Back to Step 1: Setup"),
        ({STEP_CUTOVER_SETUP: STATUS_COMPLETE,
          STEP_COA_UPLOAD: STATUS_COMPLETE,
          STEP_OPENING_TB: STATUS_COMPLETE,
          STEP_GL_UPLOAD: STATUS_COMPLETE},
         cw.STAGE_MATCH, "Back to Step 2: Upload reports"),
        ({STEP_CUTOVER_SETUP: STATUS_COMPLETE,
          STEP_COA_UPLOAD: STATUS_COMPLETE,
          STEP_OPENING_TB: STATUS_COMPLETE,
          STEP_GL_UPLOAD: STATUS_COMPLETE,
          STEP_QBO_CONNECT: STATUS_COMPLETE,
          STEP_ACCOUNT_MAPPING: STATUS_COMPLETE},
         cw.STAGE_REVIEW, "Back to Step 3: Match accounts"),
        ({STEP_CUTOVER_SETUP: STATUS_COMPLETE,
          STEP_COA_UPLOAD: STATUS_COMPLETE,
          STEP_OPENING_TB: STATUS_COMPLETE,
          STEP_GL_UPLOAD: STATUS_COMPLETE,
          STEP_QBO_CONNECT: STATUS_COMPLETE,
          STEP_ACCOUNT_MAPPING: STATUS_COMPLETE,
          STEP_DRY_RUN: STATUS_COMPLETE},
         cw.STAGE_IMPORT, "Back to Step 4: Review import"),
        ({STEP_CUTOVER_SETUP: STATUS_COMPLETE,
          STEP_COA_UPLOAD: STATUS_COMPLETE,
          STEP_OPENING_TB: STATUS_COMPLETE,
          STEP_GL_UPLOAD: STATUS_COMPLETE,
          STEP_QBO_CONNECT: STATUS_COMPLETE,
          STEP_ACCOUNT_MAPPING: STATUS_COMPLETE,
          STEP_DRY_RUN: STATUS_COMPLETE,
          STEP_PROD_IMPORT: STATUS_COMPLETE},
         cw.STAGE_RECONCILE, "Back to Step 5: Send to QuickBooks"),
    ]
    for overrides, want_key, want_label in progressions:
        cur, _ = _current(_items(**overrides))
        assert cur is not None and cur.key == want_key, (cur and cur.key, want_key)
        if want_label is None:
            assert cur.back_label == "" and cur.back_url == "", (
                want_key, cur.back_label, cur.back_url
            )
        else:
            assert cur.back_label == want_label, (want_key, cur.back_label)
            assert cur.back_url, (want_key, cur.back_url)
            # Never a bare '#' or empty fragment anchor.
            assert cur.back_url != "#", (want_key, cur.back_url)
            # A trailing '#intake' is fine (it's an anchor on a real page);
            # a bare URL of '#...' with no path is not.
            assert not cur.back_url.startswith("#"), (want_key, cur.back_url)
            # Customer-facing — no raw accounting jargon in the label.
            for jargon in (
                "Chart of Accounts", "Trial Balance", "General Ledger",
                "Journal Entry",
            ):
                assert jargon not in cur.back_label, (want_key, cur.back_label)
    print("OK  B4  Every step's back link points at a real route, "
          "customer-facing copy only")


def _signup_login(client, email):
    client.post("/signup", data={
        "firm_name": "Back-button Test Firm",
        "email": email,
        "password": "passw0rd!1234",
        "confirm_password": "passw0rd!1234",
    }, follow_redirects=False)
    client.post("/login", data={
        "email": email, "password": "passw0rd!1234",
    }, follow_redirects=False)


def _complete_step_1(firm_id):
    """Insert a cutover-setup row so Step 1 rolls up to complete."""
    appmod.db.upsert_cutover_settings(
        firm_id=firm_id,
        cutover_date="2026-01-01",
        opening_balance_date="2026-01-01",
        period_start="2025-01-01",
        period_end="2025-12-31",
        country="US",
        accounting_basis="accrual",
        migration_scope=None,
        notes=None,
        qbo_company_name=None,
        qbo_realm_id=None,
        clio_involved=False,
        ar_ap_strategy="open_only",
    )


def b5_checklist_page_renders_back_link():
    """When the firm has completed Step 1, the stepper should show the
    back link on the migration-checklist page (currently in Step 2)."""
    client = appmod.app.test_client()
    _signup_login(client, "backbutton-step2@example.test")
    user = appmod.db.get_user_by_email("backbutton-step2@example.test")
    _complete_step_1(user["firm_id"])

    r = client.get("/migration-checklist")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # The back-link surface should be present in the unified nav row.
    assert "workflow-stepper__back-link" in body, \
        "back-link element missing from rendered stepper nav row"
    assert "workflow-stepper__nav" in body, \
        "stepper nav row wrapper missing"
    assert "Back to Step 1: Setup" in body, \
        "expected 'Back to Step 1: Setup' label on Step 2 page"
    # It must point at the real Setup entry route (/cutover or
    # /migration-setup — both are bound to the cutover_setup endpoint),
    # never a dead "#" anchor.
    assert ('href="/cutover"' in body or 'href="/migration-setup"' in body), \
        "back-link should point at /cutover or /migration-setup, not '#'"
    print("OK  B5  Step 2 page renders 'Back to Step 1: Setup' -> "
          "Setup entry route")


def b6_step1_page_has_no_back_link():
    """Brand-new firm: stepper is on Step 1, no back button should render."""
    client = appmod.app.test_client()
    _signup_login(client, "backbutton-step1@example.test")

    r = client.get("/dashboard")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # Stepper itself must be there.
    assert "workflow-stepper" in body, "stepper missing from dashboard"
    assert "Step 1 of 6" in body, "expected Step 1 of 6 eyebrow"
    # The back-link element must NOT appear when there is no previous step.
    assert "workflow-stepper__back-link" not in body, \
        "Step 1 should not render a back-to-previous-step button"
    assert "Back to Step" not in body, \
        "Step 1 should not advertise any back-to-previous-step label"
    print("OK  B6  Step 1 page omits the back button (no previous step)")


def main():
    b1_step1_has_no_back_button()
    b2_step2_shows_back_to_setup()
    b3_step3_shows_back_to_upload_reports()
    b4_no_dead_anchors_anywhere()
    b5_checklist_page_renders_back_link()
    b6_step1_page_has_no_back_link()
    print("\nAll workflow back-button smoke tests passed.")


if __name__ == "__main__":
    main()
