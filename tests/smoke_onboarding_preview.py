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
  T6 The payment CTA never collects raw card fields and uses safe wording.
     When Stripe is not configured it shows a "Stripe Checkout will open here
     once payment is connected" pending state.
  T7 The customer-facing guide links are app-hosted (/guides/...), and NO
     shared internal Google Drive links appear on the preview.
  T8 The live /intake flow is untouched (still renders its own form + CTA).
  T9 Large, visible "Step 1..5" labels appear on the preview.
  T10 Step 1 renders selectable plan cards with prices (Essentials $999,
     Standard $1,499, Complete quote) and a Select/Request button each.
  T11 The three app-hosted guide routes render with their content.
  T12 Pricing copy says price is based on history, not firm size.
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
        "Choose your plan",
        "Tell us about your firm",
        "Upload your reports",
        "Add-ons and special cases",
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


def t6_payment_cta_no_card_fields_safe_wording():
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    low = body.lower()
    # Never collect raw card details on this page.
    for banned in ('type="password"', 'name="card"', 'name="cardnumber"',
                   'name="cvc"', 'name="cvv"', 'name="card_number"',
                   'autocomplete="cc-number"', 'autocomplete="cc-csc"'):
        assert banned not in low, f"preview must not collect {banned!r}"
    # The payment step represents Stripe Checkout.
    assert "Continue to secure payment" in body, "missing Stripe-style CTA"
    assert "Stripe" in body, "payment step should reference Stripe"
    # No form posts onboarding data from this preview page.
    assert 'action="/intake"' not in body, "preview must not post to /intake"
    assert 'enctype="multipart/form-data"' not in body, \
        "preview must not contain a file-upload submission form"
    # Stripe is unconfigured in the test env, so the safe pending copy shows
    # and we do not falsely claim payment is live.
    assert "Stripe Checkout will open here once payment is connected" in body, \
        "missing safe pending-state copy when Stripe not configured"
    print("T6 OK: payment CTA has no card fields; safe Stripe pending wording")


def t7_guide_links_app_hosted_no_drive():
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    # App-hosted guide links replace the old internal Drive links.
    assert "/guides/pclaw-general-ledger-export" in body
    assert "/guides/reports-needed" in body
    assert "/guides/clio-quickbooks-overview" in body
    # No shared internal Google Drive links exposed to customers.
    assert "drive.google.com" not in body, \
        "internal Google Drive links must NOT appear on the customer preview"
    print("T7 OK: app-hosted guide links present; no Google Drive links")


def t8_live_intake_untouched():
    c = appmod.app.test_client()
    r = c.get("/intake")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # The live intake still has its real form + submit CTA.
    assert 'data-testid="intake-form"' in body, "live intake form missing"
    assert "Continue to secure payment" in body, "live intake CTA missing"
    print("T8 OK: live /intake flow is untouched")


def t9_large_step_labels_present():
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    for n in ("Step 1", "Step 2", "Step 3", "Step 4", "Step 5"):
        assert n in body, f"missing large step label {n!r}"
    # New, clearer Step 1 heading.
    assert "Choose your plan" in body, "Step 1 heading should be 'Choose your plan'"
    print("T9 OK: large Step 1..5 labels present")


def t10_plan_cards_with_prices_and_buttons():
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    # Plan names + prices.
    assert "Essentials" in body and "$999" in body
    assert "Standard" in body and "$1,499" in body
    assert "Complete" in body and "Quote" in body
    # Five-or-more-years framing for Complete.
    assert "5+ years of history" in body
    # Selection buttons.
    assert 'data-testid="onboarding-preview-plan-select-essential"' in body
    assert 'data-testid="onboarding-preview-plan-select-standard"' in body
    assert 'data-testid="onboarding-preview-plan-select-complete"' in body
    assert "Select Essentials" in body
    assert "Request a quote" in body
    print("T10 OK: plan cards show prices + selection buttons")


def t11_guide_routes_render():
    c = appmod.app.test_client()

    r = c.get("/guides/pclaw-general-ledger-export")
    assert r.status_code == 200, r.status_code
    b = r.get_data(as_text=True)
    assert "Exporting your General Ledger from PCLaw" in b
    assert "Export monthly, not yearly" in b
    assert "more reliable" in b

    r = c.get("/guides/reports-needed")
    assert r.status_code == 200, r.status_code
    b = r.get_data(as_text=True)
    assert "Reports we need" in b
    for needle in ("Chart of Accounts", "Trial Balance — beginning",
                   "Trial Balance — ending", "Trust Listing",
                   "Trust Ledger", "General Ledgers — monthly",
                   "Accounts Payable", "Accounts Receivable"):
        assert needle in b, f"reports-needed guide missing {needle!r}"

    r = c.get("/guides/clio-quickbooks-overview")
    assert r.status_code == 200, r.status_code
    b = r.get_data(as_text=True)
    assert "Clio and QuickBooks" in b
    assert "limits" in b.lower()
    assert "trust" in b.lower()
    # The Clio guide must reassure: never ask for password/2FA.
    assert "never ask for your Clio password" in b
    low = b.lower()
    assert 'type="password"' not in low, "guide must not collect passwords"
    print("T11 OK: all three app-hosted guide routes render with content")


def t12_pricing_basis_is_history_not_firm_size():
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    assert "how much history we bring across" in body, \
        "pricing should be framed around history, not firm size"
    print("T12 OK: pricing copy is history-based, not firm-size-based")


if __name__ == "__main__":
    t1_preview_renders_and_marked_preview()
    t2_required_reports_checklist_present()
    t3_monthly_gl_guidance()
    t4_no_password_or_2fa_collection()
    t5_copyable_reports_email()
    t6_payment_cta_no_card_fields_safe_wording()
    t7_guide_links_app_hosted_no_drive()
    t8_live_intake_untouched()
    t9_large_step_labels_present()
    t10_plan_cards_with_prices_and_buttons()
    t11_guide_routes_render()
    t12_pricing_basis_is_history_not_firm_size()
    print("\nALL ONBOARDING PREVIEW SMOKE TESTS PASSED")
