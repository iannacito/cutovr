"""Smoke tests for the workflow polish & stability PR.

Covers user-reported production issues from the post-launch user-testing
pass:

  P1  Post-login, a firm with an in-progress migration is routed to the
      `/welcome-back` chooser (not silently dropped back into the middle
      of the workflow). A brand-new firm with no jobs skips the chooser
      and lands directly on the dashboard.
  P2  /welcome-back renders both "Continue where you left off" and
      "Start a new migration" choices in plain English (no "demo",
      "workspace reset", or accountant jargon).
  P3  /welcome-back/start-fresh archives prior jobs (does NOT delete
      them from the DB) and redirects to Step 1 (cutover_setup) so the
      user sees a clean slate. No QuickBooks Online side effects.
  P4  The Step 2 "Add more files" CTA is surfaced above the fold:
      a primary-styled link in the ready-card and a full prominent
      card immediately under it (no longer buried below a long table).
  P5  The Step 5 "Import complete" success panel renders BEFORE the
      hero/clarification content so users don't have to scroll to see
      that their migration succeeded.
  P6  The workflow stepper no longer renders the redundant
      `workflow-stepper__title` / `__eyebrow` / `__lede` block. Step
      pages keep their own hero copy below.
  P7  Step 1 (cutover_setup) renders the stepper CTA as
      "Step 2: Upload Your Reports" (not "Proceed to Step 2: Upload
      reports") and points at the upload area on the dashboard, not
      back at itself.
  P8  Step 6 reconciliation summary splits completed/blocked/pending
      lines from "skipped" optional lines. Skipped lines do not render
      with the "Skipped" badge in the prominent list — they go inside a
      quiet <details> disclosure.
  P9  The OAuth callback no-session path emits an
      `oauth_callback_no_session` audit row so operators can spot a
      pattern, and the user message no longer claims an explicit
      "session timed out" (which scares users into thinking they lost
      work). It uses an informational flash that says progress is
      preserved.
  P10 `session.modified = True` is set per-request for logged-in users
      so the cookie expiry rolls forward — protecting against the
      "session expired mid-OAuth" report.

Run from project root::

    python3 tests/smoke_workflow_polish_stability.py
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
os.environ.setdefault("SECRET_KEY", "smoke-workflow-polish-stability")

import app as appmod  # noqa: E402
import customer_workflow as cw  # noqa: E402


def _signup_and_login(client, email, firm="Polish LLP"):
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


def _make_gl_job(firm_id, user_id, job_id="job_polish"):
    appmod.db.upsert_job(
        job_id=job_id, firm_id=firm_id, user_id=user_id,
        company="Polish LLP", source_file="gl.csv",
        encrypted_file="x.enc", file_sha256="0" * 64, status="uploaded",
    )
    appmod.db.save_job_state(job_id, {
        "status": "uploaded", "report_type": "general_ledger",
    })


def p1_login_routes_in_progress_to_welcome_back():
    """A firm with an uploaded job is bounced to /welcome-back after
    login. A brand-new firm with no work yet goes straight to the
    dashboard so first-time users don't see the chooser unnecessarily.
    """
    # Brand-new firm: no in-progress migration -> dashboard.
    client = appmod.app.test_client()
    email = "p1-new@example.test"
    pwd = "passw0rd!1234"
    r = client.post("/signup", data={
        "firm_name": "Polish New LLP", "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    # Signup logs the user in already; log out so login() is exercised.
    client.post("/logout", follow_redirects=False)
    r = client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)
    assert r.status_code == 302
    assert "/welcome-back" not in r.headers.get("Location", ""), (
        f"new firm should not be sent to /welcome-back, got "
        f"{r.headers.get('Location')!r}"
    )
    assert "/dashboard" in r.headers.get("Location", "")

    # In-progress firm: at least one uploaded job -> welcome-back.
    client2 = appmod.app.test_client()
    email2 = "p1-prog@example.test"
    pwd2 = "passw0rd!1234"
    client2.post("/signup", data={
        "firm_name": "Polish Prog LLP", "email": email2,
        "password": pwd2, "confirm_password": pwd2,
    }, follow_redirects=False)
    user = appmod.db.get_user_by_email(email2)
    _complete_step1(user["firm_id"])
    _make_gl_job(user["firm_id"], user["id"], job_id="job_p1_prog")
    client2.post("/logout", follow_redirects=False)
    r = client2.post("/login", data={"email": email2, "password": pwd2},
                     follow_redirects=False)
    assert r.status_code == 302, r.status_code
    assert "/welcome-back" in r.headers.get("Location", ""), (
        f"in-progress firm should land on /welcome-back, got "
        f"{r.headers.get('Location')!r}"
    )
    print("OK  P1  login routes new -> dashboard, in-progress -> welcome-back")


def p2_welcome_back_page_renders_both_choices_in_plain_english():
    client = appmod.app.test_client()
    email = "p2@example.test"
    _signup_and_login(client, email)
    user = appmod.db.get_user_by_email(email)
    _complete_step1(user["firm_id"])
    _make_gl_job(user["firm_id"], user["id"], job_id="job_p2")

    r = client.get("/welcome-back", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)

    assert 'data-testid="welcome-back-page"' in body
    assert 'data-testid="welcome-back-continue-cta"' in body
    assert 'data-testid="welcome-back-start-fresh-cta"' in body
    # Plain-English copy — no accountant or technical jargon.
    for jargon in ("workspace reset", "COA", "GL ", "realmId", "demo run"):
        assert jargon not in body, (
            f"welcome-back should not surface {jargon!r} to users"
        )
    # Reassures that QuickBooks data is safe.
    assert "Nothing is deleted from QuickBooks" in body
    print("OK  P2  welcome-back renders continue + start-fresh in plain English")


def p3_start_fresh_archives_jobs_and_redirects_to_step1():
    client = appmod.app.test_client()
    email = "p3@example.test"
    _signup_and_login(client, email)
    user = appmod.db.get_user_by_email(email)
    _complete_step1(user["firm_id"])
    _make_gl_job(user["firm_id"], user["id"], job_id="job_p3")

    # Sanity: the job is "active" before start-fresh.
    active_before = appmod.demo_mode.filter_active_jobs(
        appmod.db.list_jobs_for_firm(user["firm_id"], limit=10)
    )
    assert len(active_before) >= 1

    r = client.post("/welcome-back/start-fresh", follow_redirects=False)
    assert r.status_code == 302, r.status_code
    loc = r.headers.get("Location", "")
    assert "/cutover" in loc or "/migration-setup" in loc, (
        f"start-fresh should redirect to Step 1 (cutover), got "
        f"{loc!r}"
    )

    # After start-fresh: jobs are archived (filter_active_jobs drops them)
    # but the underlying DB row is still there for audit history.
    active_after = appmod.demo_mode.filter_active_jobs(
        appmod.db.list_jobs_for_firm(user["firm_id"], limit=10)
    )
    assert len(active_after) == 0, (
        f"start-fresh should archive prior jobs; {len(active_after)} "
        "still active"
    )
    raw = appmod.db.list_jobs_for_firm(user["firm_id"], limit=10)
    assert len(raw) >= 1, "start-fresh must NOT delete the DB rows"
    print("OK  P3  start-fresh archives prior jobs (not delete) and "
          "redirects to Step 1")


def p4_step2_add_more_files_visible_above_the_fold():
    """The 'Add more files' CTA on the Step 2 review page must be a
    prominent above-the-fold action — not buried inside a small footer
    link or below a long table.

    We assert it on the source template because the route requires a
    real bulk-upload job in the DB; the template is the canonical
    place where its prominence is encoded.
    """
    tmpl = (ROOT / "templates" / "bulk-upload-review.html").read_text()
    # Top-of-page button (in the ready-card) wired with a testid.
    assert 'data-testid="step2-add-more-files-top"' in tmpl, (
        "ready-card must surface an 'Add more files' button above the "
        "fold with data-testid=step2-add-more-files-top"
    )
    # And a real full card section (not a tiny footer link) for the
    # upload form, also above any per-file detection table.
    assert 'data-testid="step2-add-more-section"' in tmpl
    assert 'data-testid="step2-add-more-form"' in tmpl
    assert 'data-testid="step2-add-more-submit"' in tmpl
    # Section is rendered BEFORE the per-file detection details block
    # so it's visible without scrolling past the table.
    section_idx = tmpl.find('data-testid="step2-add-more-section"')
    details_idx = tmpl.find("Per-file detection summary")
    assert section_idx != -1 and details_idx != -1, (
        f"expected section and details markers (section={section_idx}, "
        f"details={details_idx})"
    )
    assert section_idx < details_idx, (
        "Add-more-files card must render before the per-file detection "
        "table so it's visible above the fold"
    )
    print("OK  P4  Step 2 'Add more files' is surfaced above the fold")


def p5_import_complete_panel_renders_before_hero():
    """When a job has already been imported, /send-to-qbo must render
    the import-complete success panel as the FIRST content after the
    stepper — not below the hero / connect copy. User testing: lawyers
    scrolled past the hero looking for confirmation and thought the
    import had failed."""
    tmpl = (ROOT / "templates" / "send-to-qbo.html").read_text()
    # Panel testid is present.
    assert 'data-testid="already-imported"' in tmpl
    # And the panel renders inside the `already_imported` branch BEFORE
    # the hero block. Find both positions in the template.
    panel_idx = tmpl.find('data-testid="already-imported"')
    hero_idx = tmpl.find('<div class="hero">')
    assert panel_idx != -1 and hero_idx != -1, (
        f"missing markers (panel={panel_idx}, hero={hero_idx})"
    )
    assert panel_idx < hero_idx, (
        "Import-complete panel must render before the hero block so "
        f"users don't have to scroll (panel={panel_idx}, hero={hero_idx})"
    )
    # Plain-English heading; no "Sent" eyebrow (replaced with "Import
    # complete" which reads cleaner to lawyers).
    assert "Import complete" in tmpl
    print("OK  P5  Step 5 import-complete panel renders before the hero")


def p6_stepper_no_longer_renders_redundant_title_block():
    """The repeated 'Migration progress · Step X of 6 / step name /
    description' h2/eyebrow/lede block inside the stepper partial is
    gone — every step page already prints the same eyebrow + h1 in
    its own hero immediately below. Two copies confused lawyers."""
    tmpl = (ROOT / "templates" / "_workflow_stepper.html").read_text()
    # The class names that scoped the removed block must not appear in
    # the partial anymore.
    assert "workflow-stepper__title" not in tmpl, (
        "redundant workflow-stepper__title block should be removed"
    )
    assert "workflow-stepper__lede" not in tmpl, (
        "redundant workflow-stepper__lede block should be removed"
    )
    assert "workflow-stepper__eyebrow" not in tmpl, (
        "redundant workflow-stepper__eyebrow block should be removed"
    )
    # Progress bar + back/next nav still render.
    assert "workflow-progress" in tmpl
    assert "workflow-stepper__nav" in tmpl
    print("OK  P6  stepper no longer renders the redundant title block")


def p7_step1_forward_cta_is_step2_upload_your_reports():
    stages = cw.build_customer_stages(
        [],  # no checklist items -> setup stage is current
    )
    setup = next((s for s in stages if s.key == cw.STAGE_SETUP), None)
    assert setup is not None
    assert setup.status == cw.STAGE_STATUS_CURRENT, setup.status
    assert setup.cta_label == "Step 2: Upload Your Reports", (
        f"Step 1 forward CTA label should be plain English: got "
        f"{setup.cta_label!r}"
    )
    # And it points forward (not at /cutover, which is the same page).
    assert "/dashboard" in setup.cta_url and "#intake" in setup.cta_url, (
        f"Step 1 forward CTA should target the upload area, got "
        f"{setup.cta_url!r}"
    )
    # Step 1 has no back link (no Step 0).
    assert setup.back_label == "" and setup.back_url == "", (
        f"Step 1 must NOT render a back link (got "
        f"label={setup.back_label!r}, url={setup.back_url!r})"
    )
    print("OK  P7  Step 1 CTA = 'Step 2: Upload Your Reports' -> upload area, "
          "no back link")


def p8_step6_skipped_lines_collapsed_into_details():
    """The Step 6 reconciliation summary template must split completed/
    blocked/pending lines from 'skipped' optional lines so the user
    doesn't see three prominent 'Skipped' badges on a finished
    migration. Skipped lines belong inside a quiet <details> section."""
    tmpl = (ROOT / "templates" / "reconcile-balances.html").read_text()
    # The split must exist.
    assert 'data-testid="step6-skipped-details"' in tmpl
    assert 'data-testid="step6-skipped-lines"' in tmpl
    # Core lines still render the success/pending/blocked badges, but
    # the `else` branch that emitted a "Skipped" badge in the core list
    # must be gone — skipped lines should only appear in the quiet
    # details section.
    core_block_match = re.search(
        r'data-testid="step6-reconcile-lines".*?</ul>',
        tmpl, re.DOTALL,
    )
    assert core_block_match, "step6-reconcile-lines block must still exist"
    core_block = core_block_match.group(0)
    assert ">Skipped<" not in core_block, (
        "core reconciliation list must NOT render 'Skipped' badges — "
        "skipped optional reports belong in the quiet details section"
    )
    # And the prominent eyebrow label reads "What we completed", not
    # "Reconciliation" (which sounded ominous).
    assert "What we completed" in tmpl
    print("OK  P8  Step 6 skipped lines collapsed into quiet details")


def p9_oauth_callback_no_session_audits_and_uses_friendly_copy():
    """When the OAuth callback runs without a logged-in user, the
    handler must:
      - emit an `oauth_callback_no_session` audit row (so operators can
        spot a pattern if this happens regularly),
      - redirect to /login with a `next=` for the originating job
        page so the user comes back to the right place,
      - avoid the alarming "Your session timed out" language; instead
        reassure that progress was preserved.
    """
    src = (ROOT / "app.py").read_text()
    # Audit action exists in the source.
    assert "oauth_callback_no_session" in src, (
        "OAuth no-session branch must audit oauth_callback_no_session"
    )
    # The old "Your session timed out during the QuickBooks redirect"
    # copy is gone — that flash scared users into thinking they had
    # lost their migration.
    assert "Your session timed out during the QuickBooks redirect" not in src, (
        "old 'session timed out' copy must be replaced with a friendlier "
        "message that confirms uploads are preserved"
    )
    # New copy reassures that uploads + progress are safe. The string
    # is split across multiple source-line concatenations, so strip
    # adjacent string-literal joiners ("\n            ") before search.
    src_norm = re.sub(r'"\s*\n\s*"', "", src)
    assert "uploads and saved progress are still here" in src_norm, (
        "new no-session OAuth message should reassure that uploads + "
        "saved progress are preserved"
    )
    print("OK  P9  OAuth callback no-session path audits + uses friendly copy")


def p10_session_modified_set_for_logged_in_users():
    """`session.modified = True` must be set per-request for logged-in
    users so the session cookie's expiry rolls forward. Without this,
    a slow QuickBooks OAuth round-trip (sign-in + 2FA + company-pick on
    Intuit can take several minutes) could land back on the app *just*
    after the cookie's recorded expiry, making the callback look like
    an auth timeout."""
    src = (ROOT / "app.py").read_text()
    # The per-request handler exists and bumps session.modified.
    assert "session.modified = True" in src, (
        "session.modified must be set per-request to roll cookie expiry "
        "forward and protect against mid-OAuth session expiry"
    )
    print("OK  P10  session.modified=True rolls cookie expiry forward")


def main():
    p1_login_routes_in_progress_to_welcome_back()
    p2_welcome_back_page_renders_both_choices_in_plain_english()
    p3_start_fresh_archives_jobs_and_redirects_to_step1()
    p4_step2_add_more_files_visible_above_the_fold()
    p5_import_complete_panel_renders_before_hero()
    p6_stepper_no_longer_renders_redundant_title_block()
    p7_step1_forward_cta_is_step2_upload_your_reports()
    p8_step6_skipped_lines_collapsed_into_details()
    p9_oauth_callback_no_session_audits_and_uses_friendly_copy()
    p10_session_modified_set_for_logged_in_users()
    print()
    print("ALL workflow-polish-stability smoke tests passed.")


if __name__ == "__main__":
    main()
