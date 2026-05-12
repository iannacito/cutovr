"""Smoke tests for the customer-facing workflow stepper.

Run from project root:

    python3 tests/smoke_customer_workflow.py

Covers:
  C1  build_customer_stages returns 6 stages in canonical order, exactly
      one current, the rest complete/upcoming, regardless of which
      checklist items are done.
  C2  When every underlying step is complete, every stage is complete and
      current_stage() returns None.
  C3  progress_percent reflects how many stages are complete.
  C4  The dashboard page renders the stepper and the "Next" CTA.
  C5  The migration-checklist page renders the stepper.
  C6  The cutover / setup page renders the stepper.
  C7  The friendly-terms vocabulary covers the accounting jargon
      surfaced to customers (so we don't silently regress simpler
      language coverage).
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
os.environ.setdefault("SECRET_KEY", "smoke-secret-customer-workflow")

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
    """Build a full checklist with everything STATUS_NOT_STARTED, then
    override individual steps to make assertions readable."""
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


def c1_six_stages_one_current():
    items = _items()
    stages = cw.build_customer_stages(items, has_jobs=False)
    assert len(stages) == 6, f"want 6 stages, got {len(stages)}"
    keys = [s.key for s in stages]
    assert keys == [
        cw.STAGE_SETUP, cw.STAGE_UPLOAD, cw.STAGE_MATCH,
        cw.STAGE_REVIEW, cw.STAGE_IMPORT, cw.STAGE_RECONCILE,
    ], keys
    currents = [s for s in stages if s.status == cw.STAGE_STATUS_CURRENT]
    assert len(currents) == 1, f"want 1 current stage, got {len(currents)}"
    # With nothing done, the current stage is Setup.
    assert currents[0].key == cw.STAGE_SETUP
    assert currents[0].cta_label  # CTA always populated for current stage
    # Upcoming stages have no CTA.
    for s in stages:
        if s.status != cw.STAGE_STATUS_CURRENT:
            assert not s.cta_url
    print("OK  C1  stepper has 6 stages, exactly one current, correct CTA")


def c1b_current_advances_with_progress():
    items = _items(**{STEP_CUTOVER_SETUP: STATUS_COMPLETE})
    stages = cw.build_customer_stages(items)
    cur = cw.current_stage(stages)
    assert cur is not None
    assert cur.key == cw.STAGE_UPLOAD, f"want upload current, got {cur.key}"
    # Earlier stage is complete.
    setup = next(s for s in stages if s.key == cw.STAGE_SETUP)
    assert setup.status == cw.STAGE_STATUS_COMPLETE

    items = _items(**{
        STEP_CUTOVER_SETUP: STATUS_COMPLETE,
        STEP_COA_UPLOAD: STATUS_COMPLETE,
        STEP_OPENING_TB: STATUS_COMPLETE,
        STEP_GL_UPLOAD: STATUS_COMPLETE,
    })
    stages = cw.build_customer_stages(items)
    cur = cw.current_stage(stages)
    assert cur is not None and cur.key == cw.STAGE_MATCH, cur and cur.key
    print("OK  C1b stepper current advances as steps complete")


def c2_all_complete():
    items = _items(**{
        STEP_CUTOVER_SETUP: STATUS_COMPLETE,
        STEP_COA_UPLOAD: STATUS_COMPLETE,
        STEP_OPENING_TB: STATUS_COMPLETE,
        STEP_GL_UPLOAD: STATUS_COMPLETE,
        STEP_ENDING_TB: STATUS_COMPLETE,
        # Trust listing is planned — in_progress should still count as
        # "complete enough" for the stepper.
        STEP_TRUST_LISTING: STATUS_IN_PROGRESS,
        STEP_QBO_CONNECT: STATUS_COMPLETE,
        STEP_ACCOUNT_MAPPING: STATUS_COMPLETE,
        STEP_DRY_RUN: STATUS_COMPLETE,
        STEP_PROD_IMPORT: STATUS_COMPLETE,
        STEP_RECONCILIATION: STATUS_COMPLETE,
    })
    stages = cw.build_customer_stages(items)
    assert all(s.status == cw.STAGE_STATUS_COMPLETE for s in stages), [
        (s.key, s.status) for s in stages
    ]
    assert cw.current_stage(stages) is None
    assert cw.progress_percent(stages) == 100
    print("OK  C2  every stage complete, no current, 100%% progress")


def c3_progress_percent():
    # 3 of 6 stages worth of work done — but stages are rollups, so let's
    # complete enough underlying steps to mark exactly 3 stages complete:
    # Setup, Upload, Match.
    items = _items(**{
        STEP_CUTOVER_SETUP: STATUS_COMPLETE,
        STEP_COA_UPLOAD: STATUS_COMPLETE,
        STEP_OPENING_TB: STATUS_COMPLETE,
        STEP_GL_UPLOAD: STATUS_COMPLETE,
        STEP_QBO_CONNECT: STATUS_COMPLETE,
        STEP_ACCOUNT_MAPPING: STATUS_COMPLETE,
    })
    stages = cw.build_customer_stages(items)
    completed = [s.key for s in stages
                 if s.status == cw.STAGE_STATUS_COMPLETE]
    assert set(completed) == {cw.STAGE_SETUP, cw.STAGE_UPLOAD,
                              cw.STAGE_MATCH}, completed
    assert cw.progress_percent(stages) == 50, cw.progress_percent(stages)
    print("OK  C3  progress percent reflects completed stages")


def _signup_login(client, email="customer@example.test"):
    """Sign up a fresh firm + log in for template tests."""
    appmod.db.delete_all_users_for_email(email) if hasattr(
        appmod.db, "delete_all_users_for_email"
    ) else None
    # Try signup; if a previous run already created the user fall back to login.
    r = client.post("/signup", data={
        "firm_name": "Stepper Test Firm",
        "email": email,
        "password": "passw0rd!1234",
        "confirm_password": "passw0rd!1234",
    }, follow_redirects=False)
    if r.status_code in (200,):
        # signup may have failed because user exists -> log in
        client.post("/login", data={
            "email": email, "password": "passw0rd!1234",
        }, follow_redirects=False)


def c4_dashboard_renders_stepper():
    client = appmod.app.test_client()
    _signup_login(client, "dashboard@stepper.test")
    r = client.get("/dashboard")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    must_contain = [
        # Stepper container + ARIA role
        'workflow-stepper',
        'role="progressbar"',
        # Stage short labels in canonical order
        'Setup', 'Upload', 'Match', 'Review', 'Import', 'Reconcile',
        # Top-level "Next:" guidance
        'Migration progress',
        'Step 1 of 6',
        # Customer-friendly intake copy
        'Transaction history',
    ]
    for token in must_contain:
        assert token in body, f"dashboard missing {token!r}"
    print("OK  C4  dashboard renders stepper, progress copy, friendlier language")


def c5_checklist_renders_stepper():
    client = appmod.app.test_client()
    _signup_login(client, "checklist@stepper.test")
    r = client.get("/migration-checklist")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    for token in [
        'workflow-stepper', 'workflow-progress',
        'Setup', 'Reconcile',
        'Step 1 of 6',
    ]:
        assert token in body, f"checklist missing {token!r}"
    print("OK  C5  migration-checklist renders stepper")


def c6_cutover_renders_stepper():
    client = appmod.app.test_client()
    _signup_login(client, "cutover@stepper.test")
    r = client.get("/cutover")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    for token in [
        'workflow-stepper', 'Step 1 of 6',
        # Friendlier headline for the setup page (keeps existing
        # "Tell us about the cutover" but adds a plain-English parenthetical)
        'your switchover day',
    ]:
        assert token in body, f"cutover missing {token!r}"
    print("OK  C6  cutover-setup renders stepper + friendlier headline")


def c7_friendly_terms_coverage():
    # The whole point of this PR is that customers see plainer language
    # alongside accounting terms. Make sure the dictionary still has the
    # core entries we lean on so a future trim doesn't silently break it.
    must_have = {
        "Chart of Accounts", "Opening Trial Balance", "General Ledger",
        "Ending Trial Balance", "Trust Listing", "Journal Entry", "Cutover",
    }
    missing = must_have - set(cw.FRIENDLY_TERMS)
    assert not missing, f"friendly-terms dictionary missing: {missing}"
    for term, expl in cw.FRIENDLY_TERMS.items():
        assert isinstance(expl, str) and expl, f"empty explanation for {term}"
    print("OK  C7  friendly-terms vocabulary covers the customer-facing jargon")


def main():
    c1_six_stages_one_current()
    c1b_current_advances_with_progress()
    c2_all_complete()
    c3_progress_percent()
    c4_dashboard_renders_stepper()
    c5_checklist_renders_stepper()
    c6_cutover_renders_stepper()
    c7_friendly_terms_coverage()
    print("\nAll customer-workflow smoke tests passed.")


if __name__ == "__main__":
    main()
