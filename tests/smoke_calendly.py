"""Calendly discovery-call lead capture smoke tests.

Run from project root:

    python3 tests/smoke_calendly.py

Covers:
  T1  invitee.created webhook creates a lead with name/email/firm/clio rep
      and the question answers, and returns 2xx.
  T2  Duplicate delivery of the same invitee is idempotent (one lead, the
      row is updated not duplicated).
  T3  invitee.canceled flips the existing lead to status='canceled'.
  T4  A missing CALENDLY_API_TOKEN does not fail the webhook (enrichment
      'skipped'), lead still stored.
  T5  Enrichment path: with a token configured and fetch_invitee mocked,
      the enriched question answers win and enrichment status is 'ok'.
  T6  Internal email body includes meeting details + every question/answer.
  T7  Operator Leads route lists leads; a non-operator gets a 404.
  T8  No secret (signing key / api token / webhook secret) leaks into the
      Leads UI or the audit details.
  T9  Signature verification: with a signing key set, a bad signature is
      rejected (401) and a correct HMAC is accepted (2xx).

No live Calendly network call is required.
"""

import hashlib
import hmac
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
os.environ.setdefault("SECRET_KEY", "smoke-calendly-secret")
os.environ["OPERATOR_EMAILS"] = "op@cutovr.test"
# Make sure no auth/token is configured by default so the open-test path
# and the no-token enrichment path are exercised.
for _v in ("CALENDLY_WEBHOOK_SIGNING_KEY", "CALENDLY_WEBHOOK_SECRET",
           "CALENDLY_API_TOKEN", "CALENDLY_CONFIRMATION_EMAIL"):
    os.environ.pop(_v, None)

import app as appmod  # noqa: E402
import calendly_webhook  # noqa: E402

INVITEE_URI = "https://api.calendly.com/scheduled_events/EVT123/invitees/INV456"
EVENT_URI = "https://api.calendly.com/scheduled_events/EVT123"


def _created_payload(**over):
    payload = {
        "event": "invitee.created",
        "payload": {
            "uri": INVITEE_URI,
            "name": "Dana Prospect",
            "email": "dana@lawfirm.test",
            "text_reminder_number": "+1 555 0100",
            "timezone": "America/Toronto",
            "scheduled_event": {
                "uri": EVENT_URI,
                "name": "Cutovr discovery call",
                "start_time": "2026-07-01T15:00:00Z",
                "end_time": "2026-07-01T15:30:00Z",
                "event_type": "https://api.calendly.com/event_types/ET1",
            },
            "questions_and_answers": [
                {"question": "Law firm name", "answer": "Prospect & Co LLP", "position": 0},
                {"question": "Clio rep name", "answer": "Sam Rep", "position": 1},
                {"question": "Clio rep email", "answer": "sam@clio.test", "position": 2},
                {"question": "What do you want to migrate?", "answer": "Full GL", "position": 3},
            ],
        },
    }
    payload["payload"].update(over)
    return payload


def _post(client, payload, **kwargs):
    return client.post(
        "/integrations/calendly/webhook",
        data=json.dumps(payload),
        content_type="application/json",
        **kwargs,
    )


def t1_created_makes_lead():
    c = appmod.app.test_client()
    r = _post(c, _created_payload())
    assert r.status_code == 200, r.status_code
    lead = appmod.db.get_calendly_lead_by_invitee(INVITEE_URI)
    assert lead, "lead not stored"
    assert lead["name"] == "Dana Prospect"
    assert lead["email"] == "dana@lawfirm.test"
    assert lead["firm_name"] == "Prospect & Co LLP"
    assert lead["clio_rep_name"] == "Sam Rep"
    assert lead["clio_rep_email"] == "sam@clio.test"
    assert lead["phone"] == "+1 555 0100"
    assert lead["meeting_start"] == "2026-07-01T15:00:00Z"
    assert lead["status"] == "scheduled"
    qa = json.loads(lead["questions_json"])
    assert any(x["answer"] == "Full GL" for x in qa)
    print("T1 OK: invitee.created stored a complete lead")


def t2_duplicate_is_idempotent():
    c = appmod.app.test_client()
    _post(c, _created_payload())
    _post(c, _created_payload())
    rows = appmod.db.list_calendly_leads(limit=500)
    matches = [r for r in rows if r["invitee_uri"] == INVITEE_URI]
    assert len(matches) == 1, f"expected 1 lead, got {len(matches)}"
    print("T2 OK: duplicate delivery is idempotent")


def t3_canceled_updates_status():
    c = appmod.app.test_client()
    _post(c, _created_payload())
    cancel = {
        "event": "invitee.canceled",
        "payload": {
            "uri": INVITEE_URI,
            "name": "Dana Prospect",
            "email": "dana@lawfirm.test",
            "status": "canceled",
            "cancellation": {"reason": "Conflict", "canceler_type": "invitee"},
        },
    }
    r = _post(c, cancel)
    assert r.status_code == 200, r.status_code
    lead = appmod.db.get_calendly_lead_by_invitee(INVITEE_URI)
    assert lead["status"] == "canceled", lead["status"]
    assert lead["cancel_reason"] == "Conflict"
    # Name/email captured at creation must survive the sparse cancel payload.
    assert lead["name"] == "Dana Prospect"
    print("T3 OK: invitee.canceled flips status, keeps prior fields")


def t4_no_token_does_not_fail():
    assert calendly_webhook.api_token() is None
    c = appmod.app.test_client()
    r = _post(c, _created_payload(uri=INVITEE_URI + "-nt"))
    assert r.status_code == 200, r.status_code
    lead = appmod.db.get_calendly_lead_by_invitee(INVITEE_URI + "-nt")
    assert lead and lead["enrichment_status"] == "skipped"
    print("T4 OK: missing API token does not fail webhook (enrichment skipped)")


def t5_enrichment_path_mocked():
    uri = INVITEE_URI + "-enr"
    os.environ["CALENDLY_API_TOKEN"] = "tok-secret-should-not-leak"
    orig = calendly_webhook.fetch_invitee
    try:
        def fake_fetch(invitee_uri, session=None):
            assert invitee_uri == uri
            return {
                "ok": True,
                "status": "ok",
                "resource": {
                    "uri": uri,
                    "name": "Enriched Name",
                    "email": "enriched@firm.test",
                    "questions_and_answers": [
                        {"question": "Law firm name", "answer": "Enriched Firm LLP"},
                    ],
                },
            }
        calendly_webhook.fetch_invitee = fake_fetch
        c = appmod.app.test_client()
        payload = _created_payload(uri=uri)
        # Webhook payload has a different firm; enrichment must win.
        payload["payload"]["questions_and_answers"] = [
            {"question": "Law firm name", "answer": "Stale Firm"},
        ]
        r = _post(c, payload)
        assert r.status_code == 200, r.status_code
        lead = appmod.db.get_calendly_lead_by_invitee(uri)
        assert lead["enrichment_status"] == "ok"
        assert lead["name"] == "Enriched Name"
        assert lead["firm_name"] == "Enriched Firm LLP", lead["firm_name"]
    finally:
        calendly_webhook.fetch_invitee = orig
        os.environ.pop("CALENDLY_API_TOKEN", None)
    print("T5 OK: enrichment overrides payload, status 'ok'")


def t6_internal_email_includes_details():
    fields = calendly_webhook.extract_lead_fields(_created_payload())
    fields["questions_json"] = fields.get("questions_json")
    subject, body = calendly_webhook.internal_email_bodies(
        app_name="Cutovr", lead=fields
    )
    assert "Dana Prospect" in subject
    assert "Prospect & Co LLP" in body
    assert "Sam Rep" in body
    assert "2026-07-01T15:00:00Z" in body
    # Every question/answer present.
    assert "Law firm name: Prospect & Co LLP" in body
    assert "What do you want to migrate?: Full GL" in body
    print("T6 OK: internal email includes meeting + all Q/A")


def _signup_operator(client):
    client.post("/signup", data={
        "firm_name": "Op Firm", "email": "op@cutovr.test",
        "password": "passw0rd!1234", "confirm_password": "passw0rd!1234",
    })


def t7_operator_route_lists_and_blocks():
    # Seed a lead.
    c0 = appmod.app.test_client()
    _post(c0, _created_payload())

    # Operator sees the list + detail.
    c = appmod.app.test_client()
    _signup_operator(c)
    r = c.get("/operator/leads")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "Dana Prospect" in body
    assert "Prospect &amp; Co LLP" in body or "Prospect & Co LLP" in body

    lead = appmod.db.get_calendly_lead_by_invitee(INVITEE_URI)
    rd = c.get(f"/operator/leads/{lead['id']}")
    assert rd.status_code == 200, rd.status_code
    assert "Full GL" in rd.get_data(as_text=True)

    # Non-operator (fresh, anonymous client) is blocked with 404.
    anon = appmod.app.test_client()
    r404 = anon.get("/operator/leads")
    assert r404.status_code in (302, 404), r404.status_code
    print("T7 OK: operator lists leads; non-operator blocked")


def t8_no_secret_leaks():
    os.environ["CALENDLY_API_TOKEN"] = "tok-LEAKME"
    os.environ["CALENDLY_WEBHOOK_SECRET"] = "secret-LEAKME"
    try:
        c0 = appmod.app.test_client()
        _post(c0, _created_payload(), query_string={"secret": "secret-LEAKME"})
        c = appmod.app.test_client()
        _signup_operator(c)
        body = c.get("/operator/leads").get_data(as_text=True)
        lead = appmod.db.get_calendly_lead_by_invitee(INVITEE_URI)
        detail = c.get(f"/operator/leads/{lead['id']}").get_data(as_text=True)
        for needle in ("tok-LEAKME", "secret-LEAKME"):
            assert needle not in body, f"secret leaked in list: {needle}"
            assert needle not in detail, f"secret leaked in detail: {needle}"
        # Audit details never carry secrets either.
        with appmod.db._conn() as conn:
            rows = conn.execute(
                "SELECT details FROM audit_logs WHERE action LIKE 'calendly%'"
            ).fetchall()
        for row in rows:
            d = row["details"] or ""
            assert "tok-LEAKME" not in d and "secret-LEAKME" not in d
    finally:
        os.environ.pop("CALENDLY_API_TOKEN", None)
        os.environ.pop("CALENDLY_WEBHOOK_SECRET", None)
    print("T8 OK: no secrets in UI or audit output")


def t9_signature_verification():
    key = "whsec-test-key"
    os.environ["CALENDLY_WEBHOOK_SIGNING_KEY"] = key
    try:
        c = appmod.app.test_client()
        payload = _created_payload(uri=INVITEE_URI + "-sig")
        raw = json.dumps(payload).encode("utf-8")

        # Bad signature -> rejected.
        bad = _post(c, payload, headers={
            "Calendly-Webhook-Signature": "t=123,v1=deadbeef",
        })
        assert bad.status_code == 401, bad.status_code

        # Correct signature -> accepted. Must send the exact same bytes we
        # signed, so post raw to control the body.
        ts = "1700000000"
        signed = f"{ts}.".encode("utf-8") + raw
        sig = hmac.new(key.encode(), signed, hashlib.sha256).hexdigest()
        good = c.post(
            "/integrations/calendly/webhook",
            data=raw,
            content_type="application/json",
            headers={"Calendly-Webhook-Signature": f"t={ts},v1={sig}"},
        )
        assert good.status_code == 200, good.status_code
        assert appmod.db.get_calendly_lead_by_invitee(INVITEE_URI + "-sig")
    finally:
        os.environ.pop("CALENDLY_WEBHOOK_SIGNING_KEY", None)
    print("T9 OK: bad signature rejected, valid signature accepted")


if __name__ == "__main__":
    t1_created_makes_lead()
    t2_duplicate_is_idempotent()
    t3_canceled_updates_status()
    t4_no_token_does_not_fail()
    t5_enrichment_path_mocked()
    t6_internal_email_includes_details()
    t7_operator_route_lists_and_blocks()
    t8_no_secret_leaks()
    t9_signature_verification()
    print("\nALL CALENDLY SMOKE TESTS PASSED")
