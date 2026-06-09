"""Smoke tests for the page-by-page, gated onboarding flow.

Run from project root:

    python3 tests/smoke_onboarding_flow.py

Covers:
  T1  Each step renders separately at its own route (Step 1 always; Steps 2-4
      once their prerequisites are met).
  T2  /onboarding-preview redirects into the flow at Step 1.
  T3  Step 2 is gated: without a confirmed package you're sent back to Step 1
      with "Choose your package first."
  T4  Step 3/4 are gated: with a package but no firm details you're sent back
      to Step 2 with "Complete your firm details first."
  T5  Step 1 POST with a real plan advances to Step 2.
  T6  Step 2 POST with missing required fields re-renders with a validation
      error and does NOT advance.
  T7  Step 2 POST with all required fields advances to Step 3.
  T8  No raw credit-card fields anywhere in the flow; the Stripe reassurance
      copy is present on Step 2.
  T9  No Clio password / 2FA fields; the safe secure-access copy is shown.
  T10 Get Started CTAs (nav, landing, pricing) route to /onboarding/step-1 and
      NOT to the old /intake or /signup workflow.
  T11 Exactly three plans on Step 1, with no "five or more years" wording.
  T12 The reports checklist appears on Step 3 only (not Step 1 or Step 2).
  T13 Step 4 confirmation names the Clio date, the team review, and support.
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
os.environ.setdefault("SECRET_KEY", "smoke-onboarding-flow-secret")

import app as appmod  # noqa: E402
import onboarding_preview  # noqa: E402


# Complete, valid Step 2 payload (every required field filled).
VALID_DETAILS = {
    "first_name": "Dana",
    "last_name": "Lawson",
    "email": "dana@smithhart.example",
    "phone": "555-0100",
    "firm_name": "Smith & Hart LLP",
    "employees": "12",
    "position": "Managing Partner",
    "clio_migration_date": "2026-07-01",
    "username": "dana",
    "password": "a-strong-passphrase",
}


def _fresh_client():
    return appmod.app.test_client()


def _select_package(c, key="standard"):
    return c.post("/onboarding/step-1", data={"package": key})


def t1_steps_render_separately():
    c = _fresh_client()
    # Step 1 is always reachable.
    r1 = c.get("/onboarding/step-1")
    assert r1.status_code == 200, r1.status_code
    assert 'data-testid="onboarding-step1-header"' in r1.get_data(as_text=True)
    # Walk the gates so 2-4 each render on their own route.
    _select_package(c, "standard")
    r2 = c.get("/onboarding/step-2")
    assert r2.status_code == 200, r2.status_code
    assert 'data-testid="onboarding-step2-header"' in r2.get_data(as_text=True)
    c.post("/onboarding/step-2", data=VALID_DETAILS)
    r3 = c.get("/onboarding/step-3")
    assert r3.status_code == 200, r3.status_code
    assert 'data-testid="onboarding-step3-header"' in r3.get_data(as_text=True)
    r4 = c.get("/onboarding/step-4")
    assert r4.status_code == 200, r4.status_code
    assert 'data-testid="onboarding-step4-header"' in r4.get_data(as_text=True)
    print("T1 OK: each step renders separately at its own route")


def t2_preview_redirects_to_step1():
    c = _fresh_client()
    r = c.get("/onboarding-preview")
    assert r.status_code in (301, 302), r.status_code
    assert "/onboarding/step-1" in r.headers.get("Location", "")
    print("T2 OK: /onboarding-preview redirects to Step 1")


def t3_step2_requires_package():
    c = _fresh_client()
    r = c.get("/onboarding/step-2")
    assert r.status_code in (301, 302), r.status_code
    assert "/onboarding/step-1" in r.headers.get("Location", "")
    # Follow + confirm the gate message.
    body = c.get("/onboarding/step-2", follow_redirects=True).get_data(as_text=True)
    assert "Choose your package first." in body, "missing package gate message"
    print("T3 OK: Step 2 requires a confirmed package")


def t4_step3_and_4_require_details():
    c = _fresh_client()
    _select_package(c, "standard")  # package but no details yet
    for path in ("/onboarding/step-3", "/onboarding/step-4"):
        r = c.get(path)
        assert r.status_code in (301, 302), (path, r.status_code)
        assert "/onboarding/step-2" in r.headers.get("Location", ""), path
    body = c.get("/onboarding/step-3", follow_redirects=True).get_data(as_text=True)
    assert "Complete your firm details first." in body, "missing details gate message"
    print("T4 OK: Steps 3 & 4 require completed firm details")


def t5_step1_post_advances():
    c = _fresh_client()
    r = c.post("/onboarding/step-1", data={"package": "essential"})
    assert r.status_code in (301, 302), r.status_code
    assert "/onboarding/step-2" in r.headers.get("Location", "")
    # An unknown package is rejected back to Step 1.
    bad = c.post("/onboarding/step-1", data={"package": "not-a-plan"})
    assert "/onboarding/step-1" in bad.headers.get("Location", "")
    print("T5 OK: Step 1 confirms a real package and advances; rejects bad ones")


def t6_step2_missing_fields_blocks():
    c = _fresh_client()
    _select_package(c, "standard")
    partial = dict(VALID_DETAILS)
    partial["email"] = ""
    partial["phone"] = ""
    r = c.post("/onboarding/step-2", data=partial)
    assert r.status_code == 400, r.status_code
    body = r.get_data(as_text=True)
    assert "please fill in your" in body.lower(), "missing validation message"
    # Still on Step 2, has not advanced.
    assert 'data-testid="onboarding-step2-header"' in body
    # And Step 3 is still gated.
    g = c.get("/onboarding/step-3")
    assert "/onboarding/step-2" in g.headers.get("Location", "")
    print("T6 OK: Step 2 blocks when required fields are missing")


def t7_step2_complete_advances():
    c = _fresh_client()
    _select_package(c, "standard")
    r = c.post("/onboarding/step-2", data=VALID_DETAILS)
    assert r.status_code in (301, 302), r.status_code
    assert "/onboarding/step-3" in r.headers.get("Location", "")
    print("T7 OK: Step 2 advances when all required fields are provided")


def t8_no_card_fields_stripe_copy():
    c = _fresh_client()
    _select_package(c, "standard")
    body = c.get("/onboarding/step-2").get_data(as_text=True)
    low = body.lower()
    for banned in ('name="card"', 'name="cardnumber"', 'name="cvc"',
                   'name="cvv"', 'name="card_number"',
                   'autocomplete="cc-number"', 'autocomplete="cc-csc"'):
        assert banned not in low, f"Step 2 must not collect {banned!r}"
    assert ("Secure payment happens through Stripe. Cutovr never stores your "
            "card details.") in body, "missing Stripe reassurance copy"
    print("T8 OK: no raw card fields; Stripe reassurance copy present on Step 2")


def t9_no_clio_credentials():
    c = _fresh_client()
    _select_package(c, "standard")
    body = c.get("/onboarding/step-2").get_data(as_text=True)
    low = body.lower()
    # The only password field is the Cutovr account password (name="password").
    # No Clio credential or 2FA/OTP fields may be collected.
    for banned in ('name="clio_password"', 'name="otp"', 'name="2fa"',
                   'name="mfa"', 'name="totp"', 'name="2fa_code"',
                   'type="otp"'):
        assert banned not in low, f"Step 2 must not collect {banned!r}"
    assert "coordinate a secure access process separately" in body, \
        "missing safe secure-access copy"
    print("T9 OK: no Clio password/2FA fields; safe secure-access copy shown")


def t10_get_started_ctas_route_to_onboarding():
    c = _fresh_client()
    # Landing hero + nav.
    landing = c.get("/").get_data(as_text=True)
    assert 'data-testid="landing-get-started"' in landing
    # The landing Get Started anchor points at the onboarding flow, not signup.
    m = re.search(r'data-testid="landing-get-started"[^>]*href="([^"]+)"', landing) \
        or re.search(r'href="([^"]+)"[^>]*data-testid="landing-get-started"', landing)
    assert m and "/onboarding/step-1" in m.group(1), f"landing CTA -> {m and m.group(1)}"
    # Nav Get started.
    assert 'data-testid="nav-get-started"' in landing
    nav = re.search(r'data-testid="nav-get-started"[^>]*href="([^"]+)"', landing) \
        or re.search(r'href="([^"]+)"[^>]*data-testid="nav-get-started"', landing)
    assert nav and "/onboarding/step-1" in nav.group(1), f"nav CTA -> {nav and nav.group(1)}"
    # Pricing page "Get started with ..." CTAs (only rendered when Stripe is
    # unconfigured, which is the case in the test env).
    pricing = c.get("/pricing").get_data(as_text=True)
    for tid in ("pricing-cta-essential", "pricing-cta-standard"):
        mm = re.search(tid + r'"[^>]*href="([^"]+)"', pricing) \
            or re.search(r'href="([^"]+)"[^>]*data-testid="' + tid, pricing)
        assert mm and "/onboarding/step-1" in mm.group(1), f"{tid} -> {mm and mm.group(1)}"
    # None of these CTAs point at the old intake/create-account workflow.
    assert 'data-testid="landing-get-started" href="/intake"' not in landing
    print("T10 OK: Get Started CTAs (nav, landing, pricing) route to onboarding Step 1")


def t11_exactly_three_plans_no_five_years():
    c = _fresh_client()
    body = c.get("/onboarding/step-1").get_data(as_text=True)
    n = len(re.findall(
        r'data-testid="onboarding-plan-(?:essential|standard|complete)"', body))
    assert n == 3, f"expected exactly 3 plans, found {n}"
    assert "$999" in body and "$1,499" in body and "Quote" in body
    assert "Current year" in body
    assert "Up to three years" in body
    assert "Three or more years" in body
    low = body.lower()
    for banned in ("five or more", "5+ years", "up to 5 years"):
        assert banned not in low, f"remove {banned!r} wording"
    print("T11 OK: exactly three plans, history-based, no five-or-more-years wording")


def t12_reports_only_on_step3():
    c = _fresh_client()
    step1 = c.get("/onboarding/step-1").get_data(as_text=True)
    assert 'data-testid="onboarding-reports"' not in step1, \
        "reports checklist must NOT appear on Step 1"
    _select_package(c, "standard")
    step2 = c.get("/onboarding/step-2").get_data(as_text=True)
    assert 'data-testid="onboarding-reports"' not in step2, \
        "reports checklist must NOT appear on Step 2"
    c.post("/onboarding/step-2", data=VALID_DETAILS)
    step3 = c.get("/onboarding/step-3").get_data(as_text=True)
    assert 'data-testid="onboarding-reports"' in step3, \
        "reports checklist must appear on Step 3"
    assert "Chart of Accounts" in step3 and "Trust Listing" in step3
    print("T12 OK: reports checklist appears on Step 3 only")


def t13_confirmation_copy():
    c = _fresh_client()
    _select_package(c, "standard")
    c.post("/onboarding/step-2", data=VALID_DETAILS)
    c.post("/onboarding/step-3", data={})  # advance to step 4
    body = c.get("/onboarding/step-4").get_data(as_text=True)
    low = body.lower()
    assert "received your files" in low or "received the information" in low
    assert "reviewing" in low
    # The captured Clio migration date is named.
    assert "2026-07-01" in body, "confirmation should name the Clio migration date"
    assert "Clio migration date" in body
    # Support contact path.
    assert "/support" in body
    print("T13 OK: Step 4 confirmation names Clio date, team review, and support")


if __name__ == "__main__":
    t1_steps_render_separately()
    t2_preview_redirects_to_step1()
    t3_step2_requires_package()
    t4_step3_and_4_require_details()
    t5_step1_post_advances()
    t6_step2_missing_fields_blocks()
    t7_step2_complete_advances()
    t8_no_card_fields_stripe_copy()
    t9_no_clio_credentials()
    t10_get_started_ctas_route_to_onboarding()
    t11_exactly_three_plans_no_five_years()
    t12_reports_only_on_step3()
    t13_confirmation_copy()
    print("\nALL ONBOARDING FLOW SMOKE TESTS PASSED")
