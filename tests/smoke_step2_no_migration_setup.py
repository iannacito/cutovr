"""Smoke tests: Step 2 (Upload) must not show Migration Setup content.

Context
-------
Follow-up to PRs #44/#45/#46. A user still reported seeing
Migration Setup / cutover-setup content on the Step 2 Upload page,
which violates the one-step-per-page UX principle the prior PRs
established. Step 1 (/cutover, /migration-setup) is the Migration
Setup screen; Step 2 (/dashboard at the upload stage,
/uploaded-reports, /bulk-upload-review/<id>) is the Upload screen
and must contain only:

  - the workflow stepper / header
  - the upload form
  - uploaded-report status / actions
  - back to Step 1 and proceed to Step 3 navigation

Anything that looks like the Step 1 Migration Setup form, card,
section, or heading must not render on Step 2 routes.

The "Back to Step 1: Setup" link is allowed (it's navigation,
not Migration Setup content); the cutover_setup *form* (with
cutover_date / accounting_basis / opening_balance_date inputs),
the `Migration setup` H1, and any `Migration details` / `Edit
setup` / `Complete setup` panels are not.

Run from project root::

    python3 tests/smoke_step2_no_migration_setup.py
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="pclaw_uploads_")
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_")
os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("DEMO_MODE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-step2-no-migration-setup")

import app as appmod  # noqa: E402


# Form-input names that uniquely identify the Step 1 Migration
# Setup form (templates/cutover.html). If any of these show up on
# a Step 2 page, the Migration Setup form has leaked.
CUTOVER_FORM_FIELDS = (
    "cutover_date",
    "opening_balance_date",
    "accounting_basis",
    "ar_ap_strategy",
)

# Phrases that uniquely identify Migration Setup as a card or
# section heading. The bare word "Setup" is allowed because the
# stepper labels Step 1 "Setup" and Step 2 has a "Back to Step 1:
# Setup" navigation link.
MIGRATION_SETUP_PHRASES = (
    "Migration setup",         # cutover.html <h1>
    "Set up your migration",   # workflow stepper title for STAGE_SETUP
    "Open cutover setup",      # next-step CTA copy
    "Complete cutover setup",  # checklist nudge copy
    "Edit setup",              # migration-checklist details card button
    "Complete setup",          # migration-checklist details card button
    "Migration details",       # migration-checklist details card
)


def _signup_and_login(client, email, firm="Step2 Cleanup LLP"):
    pwd = "passw0rd!1234"
    r = client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


def _complete_step1(firm_id):
    appmod.db.upsert_cutover_settings(
        firm_id=firm_id,
        cutover_date="2026-04-01",
        opening_balance_date="2026-04-01",
        period_start="2025-01-01",
        period_end="2025-12-31",
        country="US",
        accounting_basis="accrual",
        migration_scope=None, notes=None,
        qbo_company_name=None, qbo_realm_id=None,
        clio_involved=False,
        ar_ap_strategy="open_only",
    )


def _assert_no_migration_setup(body, page_label):
    """Step 2 routes must not contain the Migration Setup form or copy."""
    # No Step 1 form fields.
    for name in CUTOVER_FORM_FIELDS:
        marker = f'name="{name}"'
        assert marker not in body, (
            f"{page_label}: Step 2 must not contain Migration Setup form "
            f"input {marker!r}"
        )
    # No form posting to /cutover or /migration-setup.
    assert not re.search(
        r'<form[^>]+action="[^"]*(?:/cutover|/migration-setup)\b[^"]*"',
        body,
    ), (
        f"{page_label}: Step 2 must not embed a form posting to "
        "/cutover or /migration-setup"
    )
    # No Migration Setup card / heading copy.
    for phrase in MIGRATION_SETUP_PHRASES:
        assert phrase not in body, (
            f"{page_label}: Step 2 must not contain {phrase!r}"
        )


def s1_step1_setup_page_still_shows_migration_setup_form():
    """Sanity check: /cutover (Step 1) still IS the Migration Setup
    form. If we accidentally tore out the form, Step 2 staying clean
    would be a vacuous truth."""
    client = appmod.app.test_client()
    _signup_and_login(client, "s1@example.test")

    r = client.get("/cutover", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)

    assert "Migration setup" in body, (
        "Step 1 (/cutover) must still display the 'Migration setup' heading"
    )
    for name in CUTOVER_FORM_FIELDS:
        assert f'name="{name}"' in body, (
            f"Step 1 (/cutover) must still render the Migration Setup form "
            f"input {name!r}"
        )
    assert re.search(
        r'<form[^>]+action="[^"]*(?:/cutover|/migration-setup)\b[^"]*"',
        body,
    ), "Step 1 (/cutover) must still post to the cutover_setup endpoint"

    # Same form, both URL aliases.
    r2 = client.get("/migration-setup", follow_redirects=False)
    assert r2.status_code == 200
    assert "Migration setup" in r2.get_data(as_text=True)

    print("S1 OK: Step 1 still renders the Migration Setup form "
          "(at /cutover and /migration-setup)")


def s2_step2_dashboard_has_no_migration_setup_content():
    """At the upload stage, /dashboard (Step 2) must not show any
    Migration Setup form, card, section, or heading."""
    client = appmod.app.test_client()
    _signup_and_login(client, "s2@example.test")
    user = appmod.db.get_user_by_email("s2@example.test")
    _complete_step1(user["firm_id"])

    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)

    # The page must self-identify as Step 2.
    assert "Step 2 of 6" in body, (
        "Step 2 /dashboard must render the Step 2 eyebrow"
    )
    # The upload form (Step 2's real job) must still be present.
    assert "Upload your PCLaw reports" in body
    assert 'name="ledger_files"' in body

    _assert_no_migration_setup(body, "/dashboard (Step 2 upload stage)")
    # And the "Back to Step 1: Setup" link still works as navigation,
    # which is what survives. It's an anchor, not Migration Setup
    # content — verify it stayed.
    assert 'data-testid="step2-back-link"' in body

    print("S2 OK: /dashboard at Step 2 contains the upload form and "
          "no Migration Setup form/card/section/copy")


def s3_step2_uploaded_reports_has_no_migration_setup_content():
    """The Step 2 'View uploaded reports' page must also be clean."""
    client = appmod.app.test_client()
    _signup_and_login(client, "s3@example.test")
    user = appmod.db.get_user_by_email("s3@example.test")
    _complete_step1(user["firm_id"])

    r = client.get("/uploaded-reports", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)

    assert 'data-testid="uploaded-reports-page"' in body
    assert "Step 2 of 6" in body

    _assert_no_migration_setup(body, "/uploaded-reports (Step 2)")

    print("S3 OK: /uploaded-reports (Step 2) contains no Migration "
          "Setup form/card/section/copy")


def s4_step2_back_link_targets_step1_setup():
    """The Step 2 page must still expose 'Back to Step 1: Setup' as
    navigation — that's a link, not content. Confirm it survived the
    cleanup and points at /cutover (a.k.a. /migration-setup)."""
    client = appmod.app.test_client()
    _signup_and_login(client, "s4@example.test")
    user = appmod.db.get_user_by_email("s4@example.test")
    _complete_step1(user["firm_id"])

    r = client.get("/dashboard", follow_redirects=False)
    body = r.get_data(as_text=True)

    # The navigation footer link exists.
    assert 'data-testid="step2-back-link"' in body
    # It targets /cutover (or its alias /migration-setup), not '#'.
    # Anchor attributes may appear in any order; match an <a ...>
    # tag that carries both href and the step2-back-link testid.
    nav_link = re.search(
        r'<a\b[^>]*\bhref="([^"]+)"[^>]*\bdata-testid="step2-back-link"',
        body,
    ) or re.search(
        r'<a\b[^>]*\bdata-testid="step2-back-link"[^>]*\bhref="([^"]+)"',
        body,
    )
    assert nav_link is not None, "step2-back-link must render with an href"
    href = nav_link.group(1)
    assert "/cutover" in href or "/migration-setup" in href, (
        f"step2-back-link should target Step 1 setup route, got {href!r}"
    )
    # And the visible label still says 'Back to Step 1: Setup'.
    assert "Back to Step 1: Setup" in body

    print("S4 OK: Step 2 navigation still links back to Step 1 Setup "
          "via /cutover")


def main():
    s1_step1_setup_page_still_shows_migration_setup_form()
    s2_step2_dashboard_has_no_migration_setup_content()
    s3_step2_uploaded_reports_has_no_migration_setup_content()
    s4_step2_back_link_targets_step1_setup()
    print("\nALL STEP 2 NO-MIGRATION-SETUP SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
