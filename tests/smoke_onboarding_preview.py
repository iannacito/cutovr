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
import re
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
    # The guided section titles, in the new package-first sequence.
    for needle in (
        "Choose how much history to move",
        "Tell us about your firm",
        "Upload the reports we need",
        "What happens next",
    ):
        assert needle in body, f"missing section {needle!r}"
    print("T1 OK: /onboarding-preview renders, marked preview, has the guided sections")


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
    for n in ("Step 1", "Step 2", "Step 3", "Step 4"):
        assert n in body, f"missing large step label {n!r}"
    # Step 1 heading per the latest copy guidance.
    assert "Choose how much history to move" in body, \
        "Step 1 heading should be 'Choose how much history to move'"
    print("T9 OK: large Step 1..4 labels present")


def t10_plan_cards_with_prices_and_buttons():
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    # Exactly three plan cards.
    n_cards = len(re.findall(r'data-testid="onboarding-preview-plan-(?:essential|standard|complete)"', body))
    assert n_cards == 3, f"expected exactly 3 plan cards, found {n_cards}"
    # Plan names + prices.
    assert "Essentials" in body and "$999" in body
    assert "Standard" in body and "$1,499" in body
    assert "Complete" in body and "Quote" in body
    # History-based periods, in the latest wording.
    assert "Current year" in body
    assert "Up to three years" in body
    assert "Three or more years" in body
    # The retired "five or more years" / "5+ years" framing must be gone.
    low = body.lower()
    assert "five or more" not in low, "remove 'five or more years' wording"
    assert "5+ years" not in low, "remove '5+ years' wording"
    assert "up to 5 years" not in low, "remove 'up to 5 years' wording"
    # Selection buttons.
    assert 'data-testid="onboarding-preview-plan-select-essential"' in body
    assert 'data-testid="onboarding-preview-plan-select-standard"' in body
    assert 'data-testid="onboarding-preview-plan-select-complete"' in body
    assert "Select Essentials" in body
    assert "Request a quote" in body
    print("T10 OK: exactly three plan cards, history-based periods, no 5+ wording")


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


def t13_reports_come_after_payment():
    """The reports checklist + upload must appear AFTER the payment step in
    the page sequence — never before package/details/payment."""
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)

    payment = body.find('data-testid="onboarding-preview-payment"')
    after_band = body.find('data-testid="onboarding-preview-after-payment-band"')
    reports = body.find('data-testid="onboarding-preview-reports"')
    upload = body.find('data-testid="onboarding-preview-upload"')

    assert payment != -1, "payment panel missing"
    assert after_band != -1, "after-payment band missing"
    assert reports != -1, "reports checklist missing"
    assert upload != -1, "upload area missing"

    # Order: payment -> after-payment band -> reports/upload.
    assert payment < after_band, "payment must come before the after-payment band"
    assert after_band < reports, "reports must come after the after-payment band"
    assert after_band < upload, "upload must come after the after-payment band"
    # The band is explicitly labelled so reports don't read as a pre-pay ask.
    assert "After payment" in body
    print("T13 OK: reports + upload are gated after the payment step")


def t14_step2_firm_detail_fields_present():
    """Step 2 collects the firm/account details, including firm size,
    position, Clio migration date, and Cutovr username/password."""
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    for key in (
        "first_name", "last_name", "email", "phone",
        "firm_name", "employees", "position",
        "clio_migration_date", "username", "password",
    ):
        assert f'data-testid="onboarding-preview-field-{key}"' in body, \
            f"Step 2 missing field {key!r}"
    # Visible labels for the less-obvious fields.
    assert "Number of employees at the firm" in body
    assert "Your position at the firm" in body
    assert "Clio migration date" in body
    assert "Create a username" in body
    assert "Create a password" in body
    # The password is a Cutovr login password, not a Clio credential.
    assert "not your Clio login" in body
    print("T14 OK: Step 2 firm/account fields present (size, position, clio date, login)")


def t15_no_password_or_card_fields_collected():
    """The preview never collects a real password or raw card details, even
    though it now shows a 'Create a password' field label."""
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    low = body.lower()
    # No password input type and no field literally named "password".
    assert 'type="password"' not in low, "preview must not collect passwords"
    assert 'name="password"' not in low, "preview must not name a password field"
    # No raw card fields.
    for banned in ('name="card"', 'name="cardnumber"', 'name="cvc"',
                   'name="cvv"', 'name="card_number"',
                   'autocomplete="cc-number"', 'autocomplete="cc-csc"'):
        assert banned not in low, f"preview must not collect {banned!r}"
    # Stripe reassurance copy, verbatim from the product guidance.
    assert "Secure payment happens through Stripe. Cutovr never stores your card details." in body
    print("T15 OK: no password/card inputs; Stripe reassurance copy present")


def t16_confirmation_copy_has_clio_date_and_review():
    """The confirmation step names the Clio migration date and the team-review
    wording, both on-page and in the sample confirmation email."""
    c = appmod.app.test_client()
    body = c.get("/onboarding-preview").get_data(as_text=True)
    # On-page confirmation banner. The apostrophe may be HTML-escaped.
    assert "received your files" in body
    assert "reviewing them now" in body
    assert "Clio migration date" in body
    # Sample confirmation email content (apostrophes may be HTML-escaped).
    assert 'data-testid="onboarding-preview-confirmation-email"' in body
    assert "received the information and files you submitted" in body.lower()
    assert "team is now reviewing your data" in body.lower()
    assert "same date as your clio migration date" in body.lower()

    # Unit-level checks of the confirmation email builder.
    email = onboarding_preview.build_confirmation_email(
        firm_name="Smith & Hart LLP",
        clio_migration_date="2026-07-01",
        contact_email="dan@smithhart.example",
    )
    assert "2026-07-01" in email
    assert "reviewing your data" in email.lower()
    assert "dan@smithhart.example" in email
    empty = onboarding_preview.build_confirmation_email()
    assert "YYYY-MM-DD" in empty, "missing date placeholder fallback"
    print("T16 OK: confirmation copy includes Clio date + team-review wording")


def t17_get_started_routes_to_package_first_flow():
    """The landing 'Get Started' CTA routes into the package-first onboarding
    flow (the /onboarding-preview package selection)."""
    c = appmod.app.test_client()
    body = c.get("/").get_data(as_text=True)
    assert 'data-testid="landing-get-started"' in body, "landing missing Get Started CTA"
    assert 'href="/onboarding-preview"' in body, \
        "Get Started should route to the package-first onboarding flow"
    # And that flow opens on the package-selection step.
    flow = c.get("/onboarding-preview").get_data(as_text=True)
    assert "Choose how much history to move" in flow
    print("T17 OK: Get Started routes into the package-first onboarding flow")


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
    t13_reports_come_after_payment()
    t14_step2_firm_detail_fields_present()
    t15_no_password_or_card_fields_collected()
    t16_confirmation_copy_has_clio_date_and_review()
    t17_get_started_routes_to_package_first_flow()
    print("\nALL ONBOARDING PREVIEW SMOKE TESTS PASSED")
