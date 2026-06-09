"""Smoke tests for the package-first onboarding Stripe + email integration.

Run from project root:

    python3 tests/smoke_onboarding_stripe_email.py

No live network: the `stripe` SDK is replaced with an in-memory fake before
`app` is imported, and SMTP is exercised by patching email_sender.send_email.

Covers:
  T1  Step 2 creates a durable DB onboarding record (no plaintext password).
  T2  Paid plan with Stripe configured creates a Checkout Session linked to
      the record (metadata + client_reference_id) and 303s to the Stripe URL.
  T3  No raw card fields anywhere in the flow.
  T4  Missing Stripe config is handled gracefully (non-prod demo simulation;
      production shows a friendly unavailable message, no crash).
  T5  Stripe success return verifies payment server-side, marks the record
      paid, and lets Step 3 through.
  T6  Step 3 is blocked until payment is paid for a paid plan.
  T7  Webhook verifies signature, marks payment paid, and is idempotent.
  T8  Webhook with a bad signature is rejected (400) and changes nothing.
  T9  Quote plan never touches Stripe and routes to the quote confirmation.
  T10 Customer + internal emails render with Clio date, plan, payment status,
      and upload summary; never the plaintext password; no secrets in logs.
"""

import io
import json
import logging
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-onboarding-stripe-email")

for _k in (
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PRICE_ESSENTIAL", "STRIPE_PRICE_STANDARD",
    "STRIPE_PRICE_CURRENT_YEAR", "STRIPE_PRICE_UP_TO_THREE_YEARS",
):
    os.environ.pop(_k, None)


# ---------------------------------------------------------------------------
# In-memory fake `stripe` SDK. Installed in sys.modules so the lazy
# `import stripe` inside stripe_checkout.py picks it up — no network.
# ---------------------------------------------------------------------------

class _FakeSession(dict):
    """Behaves like a Stripe Session object: attribute + dict access."""
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as e:
            raise AttributeError(k) from e


_CREATED = {}        # session_id -> session dict
_SEQ = {"n": 0}      # monotonic session counter; never reset by _CREATED.clear()
_NEXT_PAYMENT = {"status": "paid"}  # controls retrieve() payment_status


def _make_fake_stripe():
    fake = types.ModuleType("stripe")
    fake.api_key = None

    class _SessionAPI:
        @staticmethod
        def create(**kwargs):
            # Monotonic id: stays unique even after _CREATED.clear() (T9), so a
            # reused session id can't collide with a record from an earlier test.
            _SEQ["n"] += 1
            sid = f"cs_test_{_SEQ['n']:04d}"
            sess = _FakeSession({
                "id": sid,
                "url": f"https://checkout.stripe.com/c/pay/{sid}",
                "payment_status": "unpaid",
                "metadata": kwargs.get("metadata") or {},
                "client_reference_id": kwargs.get("client_reference_id"),
                "amount_total": 149900,
                "currency": "usd",
                "payment_intent": "pi_test_123",
            })
            _CREATED[sid] = sess
            return sess

        @staticmethod
        def retrieve(sid):
            sess = _CREATED.get(sid)
            if sess is None:
                sess = _FakeSession({"id": sid})
            out = _FakeSession(dict(sess))
            out["payment_status"] = _NEXT_PAYMENT["status"]
            return out

    checkout = types.SimpleNamespace(Session=_SessionAPI)
    fake.checkout = checkout

    class _Webhook:
        @staticmethod
        def construct_event(payload, sig, secret):
            # Our fake "signature" is just the literal secret. Anything else
            # raises, mimicking signature failure without real crypto.
            if sig != f"sig::{secret}":
                raise ValueError("bad signature")
            return json.loads(payload.decode("utf-8"))

    fake.Webhook = _Webhook
    return fake


sys.modules["stripe"] = _make_fake_stripe()

import app as appmod  # noqa: E402
import stripe_checkout  # noqa: E402
import intake  # noqa: E402


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
    "password": "a-very-secret-passphrase",
}

STRIPE_ENV = {
    "STRIPE_SECRET_KEY": "sk_test_dummy",
    "STRIPE_PRICE_CURRENT_YEAR": "price_cy_x",
    "STRIPE_PRICE_UP_TO_THREE_YEARS": "price_u3y_x",
}


def _client():
    return appmod.app.test_client()


def _select(c, key="standard"):
    return c.post("/onboarding/step-1", data={"package": key})


def t1_step2_creates_db_record_no_plaintext_password():
    _NEXT_PAYMENT["status"] = "paid"
    with mock.patch.dict(os.environ, STRIPE_ENV):
        c = _client()
        _select(c, "standard")
        c.post("/onboarding/step-2", data=VALID_DETAILS)
    rows = appmod.db.recent_intake_submissions(limit=10)
    rec = next((r for r in rows if r["email"] == "dana@smithhart.example"), None)
    assert rec is not None, "Step 2 should persist an onboarding record"
    assert rec["plan"] == "standard"
    assert rec["clio_migration_date"] == "2026-07-01"
    assert rec["username"] == "dana"
    assert rec["payment_status"] == "pending"
    # The plaintext password must never be stored, in any column.
    blob = json.dumps(dict(rec))
    assert VALID_DETAILS["password"] not in blob, "plaintext password leaked into DB row"
    print("T1 OK: Step 2 persists a record; no plaintext password stored")


def t2_paid_plan_creates_linked_stripe_session():
    with mock.patch.dict(os.environ, STRIPE_ENV):
        c = _client()
        _select(c, "standard")
        r = c.post("/onboarding/step-2", data=VALID_DETAILS, follow_redirects=False)
        assert r.status_code in (302, 303), r.status_code
        loc = r.headers.get("Location", "")
        assert "checkout.stripe.com" in loc, f"should redirect to Stripe, got {loc}"
    # The created session must carry our metadata link back to the record.
    sess = list(_CREATED.values())[-1]
    assert sess["metadata"].get("onboarding_ref", "").startswith("ONB-"), sess["metadata"]
    assert sess["client_reference_id"] == sess["metadata"]["onboarding_ref"]
    # And the record stores the session id.
    rec = appmod.db.get_intake_by_stripe_session(sess["id"])
    assert rec is not None, "record should be linked to the Stripe session id"
    print("T2 OK: paid plan creates a metadata-linked Stripe Checkout session")


def t3_no_raw_card_fields():
    with mock.patch.dict(os.environ, STRIPE_ENV):
        c = _client()
        _select(c, "standard")
        body = c.get("/onboarding/step-2").get_data(as_text=True).lower()
    for banned in ('name="card"', 'name="cardnumber"', 'name="cvc"',
                   'name="cvv"', 'name="card_number"', 'autocomplete="cc-number"'):
        assert banned not in body, f"raw card field {banned!r} must not exist"
    print("T3 OK: no raw card fields in the flow")


def t4_missing_stripe_config_graceful():
    # Non-production, no Stripe: Step 2 should not crash; it simulates payment
    # and advances to Step 3 (clearly marked demo mode).
    c = _client()
    _select(c, "standard")
    r = c.post("/onboarding/step-2", data=VALID_DETAILS, follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    assert "/onboarding/step-3" in r.headers.get("Location", ""), r.headers.get("Location")
    body = c.get("/onboarding/step-3").get_data(as_text=True)
    assert "demo mode" in body.lower(), "demo-mode banner expected when Stripe unconfigured"

    # Production, no Stripe: must show a friendly unavailable message, no crash,
    # and must NOT advance to Step 3.
    with mock.patch.object(appmod, "IS_PRODUCTION", True):
        c2 = _client()
        _select(c2, "standard")
        r2 = c2.post("/onboarding/step-2", data=VALID_DETAILS, follow_redirects=False)
        assert r2.status_code == 503, f"prod w/o Stripe should be 503, got {r2.status_code}"
        b2 = r2.get_data(as_text=True).lower()
        assert "payment isn't available" in b2 or "payment is not available" in b2, b2[:400]
    print("T4 OK: missing Stripe config handled gracefully (dev sim, prod message)")


def t5_success_return_verifies_and_unlocks_step3():
    _NEXT_PAYMENT["status"] = "paid"
    with mock.patch.dict(os.environ, STRIPE_ENV):
        c = _client()
        _select(c, "standard")
        c.post("/onboarding/step-2", data=VALID_DETAILS)
        sess = list(_CREATED.values())[-1]
        ref = sess["metadata"]["onboarding_ref"]
        # Step 3 is blocked before payment is confirmed.
        blocked = c.get("/onboarding/step-3", follow_redirects=False)
        assert "/onboarding/step-2" in blocked.headers.get("Location", ""), "Step 3 must be gated pre-payment"
        # Stripe sends the customer back to our return URL.
        ret = c.get(f"/onboarding/payment/return?ref={ref}&session_id={sess['id']}",
                    follow_redirects=False)
        assert "/onboarding/step-3" in ret.headers.get("Location", ""), ret.headers.get("Location")
        rec = appmod.db.get_intake_by_reference(ref)
        assert intake.is_paid(rec["payment_status"]), "record should be marked paid after verify"
        # Now Step 3 renders.
        ok = c.get("/onboarding/step-3")
        assert ok.status_code == 200 and "Upload the reports" in ok.get_data(as_text=True)
    print("T5 OK: success return verifies payment server-side and unlocks Step 3")


def t6_step3_blocked_until_paid():
    # Stripe configured but payment NOT completed -> Step 3 stays gated.
    _NEXT_PAYMENT["status"] = "unpaid"
    with mock.patch.dict(os.environ, STRIPE_ENV):
        c = _client()
        _select(c, "standard")
        c.post("/onboarding/step-2", data=VALID_DETAILS)
        sess = list(_CREATED.values())[-1]
        ref = sess["metadata"]["onboarding_ref"]
        # Return before payment clears: should NOT unlock.
        ret = c.get(f"/onboarding/payment/return?ref={ref}&session_id={sess['id']}",
                    follow_redirects=False)
        assert "/onboarding/step-2" in ret.headers.get("Location", ""), "unpaid return must not unlock"
        g = c.get("/onboarding/step-3", follow_redirects=False)
        assert "/onboarding/step-2" in g.headers.get("Location", ""), "Step 3 must remain gated"
    _NEXT_PAYMENT["status"] = "paid"
    print("T6 OK: Step 3 blocked until payment is paid for paid plans")


def t7_webhook_marks_paid_idempotently():
    secret = "whsec_test_secret"
    with mock.patch.dict(os.environ, {**STRIPE_ENV, "STRIPE_WEBHOOK_SECRET": secret}):
        c = _client()
        _select(c, "essential")
        c.post("/onboarding/step-2", data=VALID_DETAILS)
        sess = list(_CREATED.values())[-1]
        ref = sess["metadata"]["onboarding_ref"]
        rec_before = appmod.db.get_intake_by_reference(ref)
        assert rec_before["payment_status"] == "pending"

        event = {
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": sess["id"],
                "payment_status": "paid",
                "metadata": {"onboarding_ref": ref},
                "client_reference_id": ref,
                "payment_intent": "pi_test_999",
                "amount_total": 99900,
                "currency": "usd",
            }},
        }
        payload = json.dumps(event).encode("utf-8")
        headers = {"Stripe-Signature": f"sig::{secret}"}
        r1 = c.post("/onboarding/stripe/webhook", data=payload,
                    headers=headers, content_type="application/json")
        assert r1.status_code == 200, r1.status_code
        rec = appmod.db.get_intake_by_reference(ref)
        assert intake.is_paid(rec["payment_status"]), "webhook should mark paid"
        assert rec["stripe_payment_intent_id"] == "pi_test_999"
        paid_at = rec["paid_at"]
        # Replay the same event -> still 200, still paid, paid_at unchanged.
        r2 = c.post("/onboarding/stripe/webhook", data=payload,
                    headers=headers, content_type="application/json")
        assert r2.status_code == 200
        rec2 = appmod.db.get_intake_by_reference(ref)
        assert rec2["paid_at"] == paid_at, "idempotent: paid_at must not change on replay"
    print("T7 OK: webhook marks paid and is idempotent")


def t8_webhook_bad_signature_rejected():
    secret = "whsec_test_secret"
    with mock.patch.dict(os.environ, {**STRIPE_ENV, "STRIPE_WEBHOOK_SECRET": secret}):
        c = _client()
        _select(c, "standard")
        c.post("/onboarding/step-2", data=VALID_DETAILS)
        sess = list(_CREATED.values())[-1]
        ref = sess["metadata"]["onboarding_ref"]
        event = {"type": "checkout.session.completed",
                 "data": {"object": {"id": sess["id"], "payment_status": "paid",
                                     "metadata": {"onboarding_ref": ref}}}}
        r = c.post("/onboarding/stripe/webhook", data=json.dumps(event).encode(),
                   headers={"Stripe-Signature": "sig::WRONG"},
                   content_type="application/json")
        assert r.status_code == 400, f"bad signature should 400, got {r.status_code}"
        rec = appmod.db.get_intake_by_reference(ref)
        assert rec["payment_status"] == "pending", "bad-signature event must change nothing"
    print("T8 OK: webhook with bad signature is rejected and changes nothing")


def t9_quote_plan_skips_stripe():
    _CREATED.clear()
    with mock.patch.dict(os.environ, STRIPE_ENV):
        c = _client()
        _select(c, "complete")
        r = c.post("/onboarding/step-2", data=VALID_DETAILS, follow_redirects=False)
        assert "/onboarding/quote" in r.headers.get("Location", ""), r.headers.get("Location")
        assert not _CREATED, "quote plan must NOT create a Stripe session"
        body = c.get("/onboarding/quote").get_data(as_text=True)
        assert "prepare your quote" in body.lower()
        # Quote-plan record exists and is not gated by Stripe.
        rows = appmod.db.recent_intake_submissions(limit=5)
        assert any(r["plan"] == "complete" for r in rows)
    print("T9 OK: quote plan skips Stripe and routes to the quote confirmation")


def t10_emails_render_and_no_secrets():
    sent = []

    def _fake_send(*, to, subject, body_text, body_html=None):
        sent.append({"to": to, "subject": subject, "body": body_text})
        return True

    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    logging.getLogger().addHandler(handler)
    try:
        with mock.patch.dict(os.environ, {
            **STRIPE_ENV,
            "SMTP_HOST": "smtp.example.test", "SMTP_USER": "u",
            "SMTP_PASSWORD": "p", "SMTP_FROM": "from@cutovr.test",
            "INTERNAL_INTAKE_EMAILS": "team@cutovr.test",
        }), mock.patch.object(appmod.email_sender, "send_email", _fake_send):
            _NEXT_PAYMENT["status"] = "paid"
            c = _client()
            _select(c, "standard")
            c.post("/onboarding/step-2", data=VALID_DETAILS)
            sess = list(_CREATED.values())[-1]
            ref = sess["metadata"]["onboarding_ref"]
            c.get(f"/onboarding/payment/return?ref={ref}&session_id={sess['id']}")
            # Upload + submit on Step 3 triggers the emails.
            data = {
                "report_files": (io.BytesIO(b"col1,col2\n1,2\n"), "trial_balance.csv"),
            }
            c.post("/onboarding/step-3", data=data,
                   content_type="multipart/form-data")
    finally:
        logging.getLogger().removeHandler(handler)

    assert len(sent) >= 2, f"expected customer + internal emails, got {len(sent)}"
    customer = next((m for m in sent if m["to"] == "dana@smithhart.example"), None)
    internal = next((m for m in sent if m["to"] == "team@cutovr.test"), None)
    assert customer and internal, "both customer and internal emails must send"

    # Customer email: Clio date + plan + upload summary, never the password.
    cb = customer["body"]
    assert "2026-07-01" in cb, "customer email should name the Clio migration date"
    assert "Standard" in cb, "customer email should name the plan"
    assert "trial_balance.csv" in cb, "customer email should summarise uploads"
    assert VALID_DETAILS["password"] not in cb, "password must never appear in email"

    # Internal email: plan, payment status (paid), Clio date, upload summary.
    ib = internal["body"]
    assert "Standard" in ib and "2026-07-01" in ib
    assert "Paid" in ib, "internal email should show payment status"
    assert "trial_balance.csv" in ib
    assert VALID_DETAILS["password"] not in ib

    # No secrets in logs.
    logs = log_buf.getvalue()
    for secret in ("sk_test_dummy", VALID_DETAILS["password"], "whsec_"):
        assert secret not in logs, f"secret {secret!r} leaked into logs"
    print("T10 OK: emails render with Clio date/plan/status/uploads; no secrets")


if __name__ == "__main__":
    try:
        t1_step2_creates_db_record_no_plaintext_password()
        t2_paid_plan_creates_linked_stripe_session()
        t3_no_raw_card_fields()
        t4_missing_stripe_config_graceful()
        t5_success_return_verifies_and_unlocks_step3()
        t6_step3_blocked_until_paid()
        t7_webhook_marks_paid_idempotently()
        t8_webhook_bad_signature_rejected()
        t9_quote_plan_skips_stripe()
        t10_emails_render_and_no_secrets()
        print("\nALL ONBOARDING STRIPE + EMAIL SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
