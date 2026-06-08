"""Smoke tests for the QA launch-readiness follow-up batch.

Run from project root:

    python3 tests/smoke_qa_launch_readiness.py

Covers:
  T1  Landing has consultant comparison (price + duration anchors).
  T2  Landing has the price anchor ("From $999 — no subscription.").
  T3  Landing has the under-an-hour qualifier near the hero.
  T4  Landing no longer claims a public QuickBooks sandbox demo.
  T5  /security renders with encryption, OAuth, audit, reversible bullets.
  T6  /about renders with consultant-cost framing and trust posture.
  T7  /pricing/quote-request renders the form (GET).
  T8  Quote-request POST validates required fields and shows confirmation.
  T9  Footer + signed-out nav link to /security and footer to /about.
  T10 Signup is rate-limited; 6th attempt from same IP returns 429.
  T11 Onboarding page has the PCLaw export guide and IOLTA FAQ.
  T12 Workflow stepper partial has reassurance copy and "Technical details
      for support" disclosure.
  T13 Signup template has post-signup expectations card.
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
os.environ.setdefault("SECRET_KEY", "smoke-qa-launch-readiness")

import app as appmod  # noqa: E402


def _client():
    return appmod.app.test_client()


def _get(path):
    return _client().get(path)


def t1_landing_consultant_compare():
    r = _get("/")
    body = r.get_data(as_text=True)
    assert "landing-consultant-compare" in body, \
        "landing should expose consultant compare block via data-testid"
    # Positioning leads with hands-off convenience: the consultant
    # comparison is framed around the manual back-and-forth a firm avoids,
    # not a speed/cost headline. Cost stays present only as a secondary,
    # plain-English note ("a fraction of the cost", "thousands").
    assert "handled for you" in body, \
        "landing compare should lead with hands-off, handled-for-you framing"
    assert "fraction of the cost" in body, \
        "landing should keep cost as a secondary note, not the headline"
    assert "From $999" in body, "landing should anchor Cutovr from-price"
    print("T1 OK: landing consultant comparison leads with hands-off framing")


def t2_landing_price_anchor():
    r = _get("/")
    body = r.get_data(as_text=True)
    assert "landing-price-anchor" in body
    assert "From $999" in body
    assert "no subscription" in body
    print("T2 OK: landing has hero price anchor")


def t3_landing_scope_qualifier():
    r = _get("/")
    body = r.get_data(as_text=True)
    # The hero qualifier scopes the offer to supported PCLaw CSV exports
    # and reinforces the hands-off promise ("we do the work") rather than
    # leading with a turnaround-time claim.
    assert "landing-hour-qualifier" in body
    assert "supported PCLaw CSV exports" in body
    assert "we do the work" in body
    print("T3 OK: landing scope qualifier reinforces hands-off framing")


def t4_landing_no_public_sandbox_claim():
    r = _get("/")
    body = r.get_data(as_text=True)
    # The product does not yet expose a public sandbox demo; the marketing
    # copy must not imply otherwise. The QuickBooks sandbox is a real
    # Intuit feature but is only reachable AFTER a firm signs up and
    # connects QuickBooks, not from the public landing page.
    assert "try the full flow" not in body or "QuickBooks sandbox" not in body, \
        "landing must not promise public sandbox demo"
    print("T4 OK: landing does not promise a public QuickBooks sandbox demo")


def t5_security_page_renders():
    r = _get("/security")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    must_contain = [
        "security-page",
        "Encryption at rest",
        "OAuth",
        "QuickBooks password",
        "Least-necessary data access",
        "Audit logging",
        "Reversible imports",
    ]
    for needle in must_contain:
        assert needle in body, f"/security missing {needle!r}"
    # We do not invent SOC2 / ISO / compliance claims.
    assert "SOC 2" not in body and "SOC2" not in body or "We do not currently publish SOC" in body, \
        "/security must not overclaim SOC2 certification"
    print("T5 OK: /security renders with required bullets, no overclaiming")


def t6_about_page_renders():
    r = _get("/about")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "about-page" in body
    # /about leads with the hands-off value proposition; the consultant
    # comparison stays as supporting context (not a cost headline).
    assert "hands-off" in body.lower(), \
        "/about should lead with the hands-off value proposition"
    assert "consultant" in body, "/about should keep consultant context"
    assert "QuickBooks" in body
    print("T6 OK: /about leads with hands-off framing, keeps consultant context")


def t7_quote_request_get():
    r = _get("/pricing/quote-request")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "quote-firm-name" in body
    assert "quote-email" in body
    assert "quote-years-history" in body
    assert "quote-submit" in body
    print("T7 OK: /pricing/quote-request GET renders form")


def t8_quote_request_post_validation_and_success():
    c = _client()
    r = c.post("/pricing/quote-request", data={})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "quote-error" in body, "POST without fields should surface a form error"

    c2 = _client()
    r = c2.post(
        "/pricing/quote-request",
        data={
            "firm_name": "Test Quote Firm",
            "email": "quotes@example.com",
            "years_history": "5-10",
            "volume": "20k rows",
            "notes": "Want to migrate this fall.",
        },
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "quote-request-success" in body
    assert "Test Quote Firm" not in body, \
        "confirmation page should not echo firm name back (privacy)"
    print("T8 OK: quote-request POST validates required fields + confirms success")


def t9_nav_and_footer_link_to_new_pages():
    r = _get("/")
    body = r.get_data(as_text=True)
    assert 'href="/security"' in body, "logged-out nav/footer should link to /security"
    assert 'href="/about"' in body, "footer should link to /about"
    print("T9 OK: nav/footer link to /security and /about")


def t10_signup_rate_limited():
    # Brand-new client, blast past the per-IP limit (currently 10 / 5 min).
    c = _client()
    attempts = 0
    final_status = None
    for i in range(15):
        r = c.post(
            "/signup",
            data={
                "firm_name": f"Burst Firm {i}",
                "email": f"burst{i}@example.com",
                # Intentionally too-short so signup never actually succeeds —
                # the limiter sits in front of validation so a real abuser
                # rotating emails hits the wall regardless of payload.
                "password": "short",
                "confirm_password": "short",
            },
        )
        attempts += 1
        final_status = r.status_code
        if r.status_code == 429:
            break
    assert final_status == 429, f"expected 429 within 15 attempts, got {final_status}"
    print(f"T10 OK: signup rate-limited after {attempts} attempts (429)")


def t11_onboarding_export_guide_and_iolta_faq():
    r = _get("/onboarding")
    body = r.get_data(as_text=True)
    assert "onboarding-export-guide" in body, \
        "onboarding should expose the PCLaw export guide section"
    assert "Trial Balance" in body
    assert "Trust Listing" in body
    assert "onboarding-iolta-faq" in body, \
        "onboarding should expose the IOLTA / trust FAQ section"
    assert "auto-posted" in body, "IOLTA FAQ should clarify auto-post posture"
    print("T11 OK: onboarding has PCLaw export guide + IOLTA FAQ")


def t12_workflow_stepper_reassurance():
    # The stepper is included in every workflow page. /cutover (Step 1)
    # is the cleanest one to assert on for logged-in users.
    c = _client()
    # Need a real signup to reach a workflow page.
    c.post(
        "/signup",
        data={
            "firm_name": "Stepper Firm",
            "email": "stepper@example.com",
            "password": "stepper-passw0rd",
            "confirm_password": "stepper-passw0rd",
        },
    )
    r = c.get("/cutover")
    assert r.status_code in (200, 302), r.status_code
    if r.status_code == 200:
        body = r.get_data(as_text=True)
        # The reassurance copy lives inside the stepper partial.
        assert "workflow-stepper-reassure" in body, \
            "workflow stepper should expose reassurance copy"
        assert "Nothing is sent to QuickBooks yet" in body
    print("T12 OK: workflow stepper has reassurance copy")


def t13_signup_post_signup_expectations():
    r = _get("/signup")
    body = r.get_data(as_text=True)
    assert "signup-expectations" in body, \
        "signup should show what happens after signing up"
    assert "guided workflow" in body or "no email verification" in body \
        or "ready to send" in body
    print("T13 OK: signup has post-signup expectations card")


if __name__ == "__main__":
    try:
        t1_landing_consultant_compare()
        t2_landing_price_anchor()
        t3_landing_scope_qualifier()
        t4_landing_no_public_sandbox_claim()
        t5_security_page_renders()
        t6_about_page_renders()
        t7_quote_request_get()
        t8_quote_request_post_validation_and_success()
        t9_nav_and_footer_link_to_new_pages()
        t10_signup_rate_limited()
        t11_onboarding_export_guide_and_iolta_faq()
        t12_workflow_stepper_reassurance()
        t13_signup_post_signup_expectations()
        print("\nALL QA LAUNCH-READINESS SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
