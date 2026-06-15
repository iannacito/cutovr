"""Post-purchase onboarding intake smoke tests.

Run from project root:

    python3 tests/smoke_intake.py

Covers:
  T1 /intake renders 200 with all required fields, the recommended-report
     guidance, and the "upload whatever you have" tagline.
  T2 Submitting with missing required fields re-renders the form with a
     400 and an error (no record stored).
  T3 A complete submission stores a record, redirects to the success page,
     and the success page shows the all-set message + reference.
  T4 Email fallback: with SMTP NOT configured, intake still succeeds and the
     stored record's email_status is "skipped" (no real email sent).
  T5 Email path: with SMTP "configured" but send monkeypatched, both the
     customer and internal emails are attempted and the record is "sent".
  T6 internal_recipients honors INTERNAL_INTAKE_EMAILS and falls back to a
     real SUPPORT_EMAIL but never a placeholder.
  T7 Stripe checkout success page links to the onboarding intake form.
  T8 Operator intake list shows a stored submission (firm, plan, files).
"""

import io
import json
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
os.environ.setdefault("SECRET_KEY", "smoke-intake-secret")
# Operator panel needs an allowlist to be enabled.
os.environ["OPERATOR_EMAILS"] = "op@cutovr.test"

import app as appmod  # noqa: E402
import intake  # noqa: E402
import email_sender  # noqa: E402


def _complete_form(**overrides):
    data = {
        "firm_name": "Smith & Hart LLP",
        "first_name": "Jordan",
        "last_name": "Smith",
        "position": "Office manager",
        "phone": "(555) 123-4567",
        "email": "jordan@smithhart.test",
        "clio_migration_date": "2026-03-05",
        "plan": "standard",
    }
    data.update(overrides)
    return data


def t1_form_renders():
    c = appmod.app.test_client()
    r = c.get("/intake")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    for needle in (
        "Law firm name", "First name", "Last name",
        "Phone number", "Email", "position at the firm",
        "Clio migration date",
        "Chart of Accounts", "General Ledger", "Trial Balance",
        "Trust Listing",
        "Upload whatever you have",
        # Guided one-page section headers.
        "Your firm", "Migration date", "Upload your PCLaw reports",
        "What happens next",
        # Discovery-call / pre-call form messaging (no payment, no price).
        "Book a discovery call",
        "Calendly booking form",
        "Send migration details",
    ):
        assert needle in body, f"missing {needle!r} in /intake"
    # No raw card-collection inputs and no payment/checkout language here.
    assert 'name="card_number"' not in body and "cardnumber" not in body.lower(), \
        "intake must not collect raw card details"
    assert "Continue to secure payment" not in body, \
        "intake must not show a checkout/payment CTA"
    # No public dollar amounts.
    for amount in ("$999", "$1,499", "$1499"):
        assert amount not in body, f"intake must not show {amount!r}"
    print("T1 OK: /intake renders fields + report guidance + discovery-call messaging, no price/payment")


def t1b_plan_preselection_shows_no_price():
    c = appmod.app.test_client()
    # A plan slug may still arrive from a private link, but the public intake
    # form must never surface a dollar amount or a checkout CTA.
    for slug in ("essential", "standard", "complete"):
        body = c.get(f"/intake?plan={slug}").get_data(as_text=True)
        for amount in ("$999", "$1,499", "$1499"):
            assert amount not in body, f"intake (plan={slug}) must not show {amount!r}"
        assert "Continue to secure payment" not in body
        assert "Book a discovery call" in body
    print("T1b OK: intake shows no price/checkout regardless of plan slug")


def t2_missing_required_fields_rejected():
    c = appmod.app.test_client()
    r = c.post("/intake", data={"first_name": "Jordan"},
               content_type="multipart/form-data")
    assert r.status_code == 400, r.status_code
    body = r.get_data(as_text=True)
    assert "Please fill in" in body, body[:400]
    # No record should have been stored by this rejected submission.
    assert appmod.db.recent_intake_submissions(limit=5) == [], \
        "rejected submission must not store a record"
    print("T2 OK: missing required fields rejected with 400")


def t3_complete_submission_succeeds():
    # SMTP not configured for this one — exercises the no-email path too.
    for k in ("SMTP_HOST", "MAIL_SERVER", "SMTP_USER", "SMTP_USERNAME",
              "MAIL_USERNAME", "SMTP_PASSWORD", "MAIL_PASSWORD",
              "SMTP_FROM", "SMTP_FROM_EMAIL", "MAIL_DEFAULT_SENDER"):
        os.environ.pop(k, None)
    c = appmod.app.test_client()
    r = c.post("/intake", data=_complete_form(),
               content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "You're all set" in body, body[:400]
    assert "INT-" in body, "reference not shown on success page"
    # Payment is pending (Stripe not collecting here) — must NOT claim paid
    # and must NOT show a receipt.
    assert "have not been charged yet" in body, body[:600]
    assert "Payment received" not in body, "must not falsely confirm payment"
    rows = appmod.db.recent_intake_submissions(limit=5)
    assert rows, "no intake record stored"
    rec = rows[0]
    assert rec["firm_name"] == "Smith & Hart LLP"
    assert rec["plan"] == "standard"
    assert rec["clio_migration_date"] == "2026-03-05"
    assert rec["payment_status"] == "pending", rec["payment_status"]
    print("T3 OK: complete submission stores record + pending payment + success page")


def t4_email_fallback_when_smtp_unconfigured():
    # Continues from t3's unconfigured SMTP. The most recent record's
    # email_status should be "skipped".
    rows = appmod.db.recent_intake_submissions(limit=1)
    assert rows and rows[0]["email_status"] == "skipped", rows
    print("T4 OK: SMTP unconfigured -> intake stored, email_status=skipped")


def t5_emails_attempted_when_configured(monkeypatch_env=True):
    # Fake an SMTP config and capture send_email calls without sending.
    os.environ["SMTP_HOST"] = "smtp.test"
    os.environ["SMTP_USER"] = "u"
    os.environ["SMTP_PASSWORD"] = "p"
    os.environ["SMTP_FROM"] = "from@cutovr.test"
    os.environ["SUPPORT_EMAIL"] = "support@cutovr.test"
    os.environ["INTERNAL_INTAKE_EMAILS"] = "ops@cutovr.test, team@cutovr.test"

    sent = []
    orig = appmod.email_sender.send_email

    def fake_send(*, to, subject, body_text, body_html=None):
        sent.append({"to": to, "subject": subject, "body": body_text})
        return True

    appmod.email_sender.send_email = fake_send
    try:
        c = appmod.app.test_client()
        r = c.post("/intake", data=_complete_form(email="cust@firm.test"),
                   content_type="multipart/form-data", follow_redirects=True)
        assert r.status_code == 200
    finally:
        appmod.email_sender.send_email = orig

    tos = [m["to"] for m in sent]
    assert "cust@firm.test" in tos, f"customer email not sent: {tos}"
    assert "ops@cutovr.test" in tos and "team@cutovr.test" in tos, tos
    # Internal email body must carry firm/plan/clio + uploaded count.
    internal = [m for m in sent if m["to"] == "ops@cutovr.test"][0]
    assert "Smith & Hart LLP" in internal["body"]
    assert "Standard" in internal["body"]
    assert "2026-03-05" in internal["body"]
    # Internal email carries payment status; customer email says pending and
    # is NOT a receipt.
    assert "Payment status:" in internal["body"] and "Pending" in internal["body"]
    customer = [m for m in sent if m["to"] == "cust@firm.test"][0]
    assert "not been charged yet" in customer["body"], customer["body"]
    assert "this is not a receipt" in customer["body"]
    rec = appmod.db.recent_intake_submissions(limit=1)[0]
    assert rec["email_status"] == "sent", rec["email_status"]
    print("T5 OK: customer + internal emails attempted; status=sent; payment pending")


def t6_internal_recipients_resolution():
    os.environ["INTERNAL_INTAKE_EMAILS"] = "a@x.test, b@x.test, a@x.test"
    got = intake.internal_recipients("support@cutovr.test")
    assert got == ["a@x.test", "b@x.test"], got
    os.environ.pop("INTERNAL_INTAKE_EMAILS", None)
    # Falls back to a real support email.
    assert intake.internal_recipients("support@cutovr.test") == ["support@cutovr.test"]
    # Never a placeholder.
    assert intake.internal_recipients("support@your-domain.example") == []
    print("T6 OK: internal_recipients env + fallback + placeholder guard")


def t7_success_page_links_to_intake():
    c = appmod.app.test_client()
    r = c.get("/pricing/checkout/success")
    body = r.get_data(as_text=True)
    assert ('href="/intake"' in body or 'href="/onboarding/start"' in body), \
        "success page missing intake link"
    assert "Start onboarding" in body
    print("T7 OK: Stripe success page links to onboarding intake")


def t8_operator_intake_list():
    # Sign up an operator user so the operator panel grants access.
    c = appmod.app.test_client()
    c.post("/signup", data={
        "firm_name": "Op Firm", "email": "op@cutovr.test",
        "password": "passw0rd!1234", "confirm_password": "passw0rd!1234",
    })
    r = c.get("/operator/intake")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "Smith &amp; Hart LLP" in body or "Smith & Hart LLP" in body, body[:600]
    # Operator view surfaces payment status so the team knows who has paid.
    assert "Payment" in body, "operator view missing payment column"
    assert "Pending" in body, "operator view should show pending status"
    print("T8 OK: operator intake list shows stored submissions + payment status")


def t9_paid_path_emits_receipt_wording():
    # Direct unit check of the honest-receipt logic: only a genuine "paid"
    # status yields receipt/confirmation wording.
    _, paid_body = intake.customer_email_bodies(
        first_name="Jordan", app_name="Cutovr", support_email="support@cutovr.test",
        plan="standard", clio_migration_date="2026-03-05", uploads=[],
        payment_status=intake.PAYMENT_PAID,
    )
    assert "confirmation of payment" in paid_body
    assert "not a receipt" not in paid_body
    _, pending_body = intake.customer_email_bodies(
        first_name="Jordan", app_name="Cutovr", support_email="support@cutovr.test",
        plan="standard", payment_status=intake.PAYMENT_PENDING,
    )
    assert "not been charged yet" in pending_body
    assert "confirmation of payment" not in pending_body
    # is_paid is strict.
    assert intake.is_paid("paid") and not intake.is_paid("pending")
    assert not intake.is_paid(None) and not intake.is_paid("bogus")
    print("T9 OK: receipt wording only when genuinely paid")


if __name__ == "__main__":
    t1_form_renders()
    t1b_plan_preselection_shows_no_price()
    t2_missing_required_fields_rejected()
    t3_complete_submission_succeeds()
    t4_email_fallback_when_smtp_unconfigured()
    t5_emails_attempted_when_configured()
    t6_internal_recipients_resolution()
    t7_success_page_links_to_intake()
    t8_operator_intake_list()
    t9_paid_path_emits_receipt_wording()
    print("\nALL INTAKE SMOKE TESTS PASSED")
