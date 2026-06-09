"""Smoke tests for the legacy /onboarding-preview URL and the app-hosted guides.

Run from project root:

    python3 tests/smoke_onboarding_preview.py

The single-page read-only preview was replaced by a page-by-page, gated
onboarding flow (see tests/smoke_onboarding_flow.py). This file now covers the
pieces that survived that change:

  T1 /onboarding-preview redirects into the live page-by-page flow at Step 1,
     so any old links keep working.
  T2 The customer-facing guide content (reports we need, monthly GL, no Clio
     credentials) is unchanged and still rendered by the app-hosted guides.
  T3 The live /intake flow is untouched.
  T4 The three app-hosted guide routes render with their content.
  T5 The copyable "reports we need" email builder fills dates / placeholder.
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


def t1_preview_redirects_to_step1():
    c = appmod.app.test_client()
    r = c.get("/onboarding-preview")
    assert r.status_code in (301, 302), r.status_code
    assert "/onboarding/step-1" in r.headers.get("Location", ""), r.headers
    # And following it lands on the package-selection step.
    body = c.get("/onboarding/step-1").get_data(as_text=True)
    assert "Choose how much history to move" in body
    print("T1 OK: /onboarding-preview redirects into the page-by-page flow at Step 1")


def t2_guide_content_unchanged():
    c = appmod.app.test_client()
    body = c.get("/guides/reports-needed").get_data(as_text=True)
    for needle in (
        "Chart of Accounts",
        "Trial Balance — beginning",
        "Trial Balance — ending",
        "Trust Listing",
        "General Ledgers — monthly",
        "Trust Ledger",
        "Accounts Receivable",
        "Accounts Payable",
    ):
        assert needle in body, f"missing guide content {needle!r}"
    gl = c.get("/guides/pclaw-general-ledger-export").get_data(as_text=True)
    assert "Export monthly, not yearly" in gl and "more reliable" in gl
    clio = c.get("/guides/clio-quickbooks-overview").get_data(as_text=True)
    assert "never ask for your Clio password" in clio
    assert 'type="password"' not in clio.lower()
    print("T2 OK: app-hosted guide content unchanged (reports, monthly GL, no Clio creds)")


def t3_live_intake_untouched():
    c = appmod.app.test_client()
    r = c.get("/intake")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'data-testid="intake-form"' in body, "live intake form missing"
    print("T3 OK: live /intake flow is untouched")


def t4_guide_routes_render():
    c = appmod.app.test_client()
    for path, needle in (
        ("/guides/pclaw-general-ledger-export", "Exporting your General Ledger from PCLaw"),
        ("/guides/reports-needed", "Reports we need"),
        ("/guides/clio-quickbooks-overview", "Clio and QuickBooks"),
    ):
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert needle in r.get_data(as_text=True), f"{path} missing {needle!r}"
    print("T4 OK: all three app-hosted guide routes render with content")


def t5_copyable_reports_email_builder():
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
    on = onboarding_preview.build_reports_email(include_trust_ledger=True)
    assert "Trust Ledger (add-on selected)" in on
    print("T5 OK: reports email builder fills dates / placeholder")


if __name__ == "__main__":
    t1_preview_redirects_to_step1()
    t2_guide_content_unchanged()
    t3_live_intake_untouched()
    t4_guide_routes_render()
    t5_copyable_reports_email_builder()
    print("\nALL ONBOARDING PREVIEW SMOKE TESTS PASSED")
