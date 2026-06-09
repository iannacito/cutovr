"""Smoke tests for the read-only onboarding PREVIEW flow.

Run from project root:

    python3 tests/smoke_onboarding_preview.py

Covers:
  T1 /onboarding-preview renders 200 and is clearly marked as a preview
     (not live), without disrupting the production /intake flow.
  T2 The required reports checklist content is present, including the
     beginning/ending Trial Balance, Trust Listing at cutover, COA, A/R,
     A/P, vendor/customer lists, and the trust-ledger add-on.
  T3 The General Ledger guidance recommends MONTHLY over yearly.
  T4 The page never collects a Clio password or 2FA code; instead it shows
     the safe "coordinate secure access separately" copy.
  T5 The copyable "reports we need" email body has the expected lines and
     fills in dates (with a clean YYYY-MM-DD fallback when missing).
  T6 The CTA is a non-functional preview (disabled, no form submission) and
     the page contains no <form> that posts onboarding data.
  T7 Secondary resource links render (Clio overview, PCLaw GL export guide).
  T8 The live /intake flow is untouched (still renders its own form + CTA).
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
os.environ.setdefault("SECRET_KEY", "smoke-onboarding-preview-secret")

import app as appmod  # noqa: E402
import onboarding_preview  # noqa: E402


def t1_preview_renders_and_marked_preview():
    c = appmod.app.test_client()
    r = c.get("/onboarding-preview")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # Clearly marked as a preview, not the live path.
    assert "Preview &mdash; not live" in body or "Preview — not live" in body, body[:400]
    assert "Your migration, prepared for you" in body
    # The five guided section titles.
    for needle in (
        "Package and migration period",
        "Firm details",
        "Reports to upload",
        "Optional add-ons and special cases",
        "What happens next",
    ):
        assert needle in body, f"missing section {needle!r}"
    print("T1 OK: /onboarding-preview renders, marked preview, has all 5 sections")


def t2_required_reports_checklist_present():
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    for needle in (
        "Chart of Accounts",
        "Trial Balance — beginning",
        "Trial Balance — ending",
        "Trust Listing",
        "migration cutover date",
        "General Ledgers — monthly",
        "Trust Ledger",
        "Accounts Receivable",
        "Accounts Payable",
        "Vendor list and customer",
    ):
        assert needle in body, f"missing checklist item {needle!r}"
    # Trust ledger is positioned as an add-on.
    assert "add-on" in body, "trust ledger should be marked as an add-on"
    # Cash-basis caveat for A/R and A/P.
    assert "cash-basis" in body, "missing cash-basis caveat for A/R / A/P"
    print("T2 OK: required reports checklist present (TB begin/end, trust, GL, A/R, A/P)")


def t3_monthly_gl_guidance():
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    assert "Monthly files are preferred" in body, "GL monthly guidance missing"
    assert "more reliable" in body, "GL reliability rationale missing"
    print("T3 OK: General Ledger guidance recommends monthly over yearly")


def t4_no_password_or_2fa_collection():
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    low = body.lower()
    # No INPUT FIELD that could collect Clio credentials or 2FA codes. We
    # check for field markup (type=/name=) rather than prose — the page is
    # allowed to *mention* passwords/2FA in the reassurance copy that says we
    # never ask for them.
    assert 'type="password"' not in low, "preview must not collect passwords"
    for banned in ('name="password"', 'name="otp"', 'name="2fa"',
                   'name="mfa"', 'name="totp"', 'name="clio_password"',
                   'name="2fa_code"', 'type="otp"'):
        assert banned not in low, f"preview must not collect {banned!r}"
    # Instead it shows the safe coordinate-separately copy.
    assert "coordinate a secure access process separately" in body, \
        "missing safe secure-access copy"
    print("T4 OK: no password/2FA fields; safe secure-access copy shown")


def t5_copyable_reports_email():
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    # The rendered sample email is present on the page.
    assert "Reports we need" in body
    assert "Trial Balance beginning as at" in body
    assert "Trial Balance ending as at" in body
    assert "Trust Listing as at migration cutover" in body
    assert "General Ledgers, monthly from start date" in body

    # Unit-level checks of the builder: dates fill in, placeholder fallback.
    filled = onboarding_preview.build_reports_email(
        tb_beginning_date="2024-12-31",
        tb_ending_date="2026-03-31",
        cutover_date="2026-03-31",
        start_date="2025-01-01",
        end_date="2026-03-31",
    )
    assert "(2024-12-31)" in filled and "(2025-01-01)" in filled, filled
    empty = onboarding_preview.build_reports_email()
    assert "(YYYY-MM-DD)" in empty, "missing YYYY-MM-DD placeholder fallback"
    # Add-on toggles the trust-ledger line wording.
    on = onboarding_preview.build_reports_email(include_trust_ledger=True)
    assert "Trust Ledger (add-on selected)" in on
    off = onboarding_preview.build_reports_email(include_trust_ledger=False)
    assert "only if you've added" in off
    print("T5 OK: copyable reports email renders + builder fills dates / placeholder")


def t6_cta_is_preview_only_no_submission():
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    # The CTA is disabled (preview only).
    assert "Save onboarding preview" in body
    assert "disabled" in body, "preview CTA should be disabled"
    # No form posts onboarding data from this preview page. (_base.html still
    # carries the logout + support-assistant forms, which is fine; we only
    # assert nothing here submits to the live intake endpoint.)
    assert 'action="/intake"' not in body, "preview must not post to /intake"
    assert 'enctype="multipart/form-data"' not in body, \
        "preview must not contain a file-upload submission form"
    print("T6 OK: CTA is disabled preview-only; no onboarding submission form")


def t7_resource_links_present():
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    assert "Clio to QuickBooks integration overview" in body
    assert "PCLaw GL Export Guide" in body
    assert "drive.google.com" in body
    # External links open safely in a new tab.
    assert 'rel="noopener noreferrer"' in body
    print("T7 OK: secondary resource links present and open safely")


def t8_live_intake_untouched():
    c = appmod.app.test_client()
    r = c.get("/intake")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # The live intake still has its real form + submit CTA.
    assert 'data-testid="intake-form"' in body, "live intake form missing"
    assert "Continue to secure payment" in body, "live intake CTA missing"
    print("T8 OK: live /intake flow is untouched")


if __name__ == "__main__":
    t1_preview_renders_and_marked_preview()
    t2_required_reports_checklist_present()
    t3_monthly_gl_guidance()
    t4_no_password_or_2fa_collection()
    t5_copyable_reports_email()
    t6_cta_is_preview_only_no_submission()
    t7_resource_links_present()
    t8_live_intake_untouched()
    print("\nALL ONBOARDING PREVIEW SMOKE TESTS PASSED")
