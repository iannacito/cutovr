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
  T10 CSV export returns text/csv with the attachment filename, the full
      column header, the seeded lead's data, and its question answers; a
      non-operator is blocked.
  T11 CSV export does not leak any secret material.
  T12 A sparse invitee.created (only uri+name+email) still saves a lead.
  T13 Real payload variations (nested scheduled_event/event_type object,
      first_name/last_name, missing top-level 'event') still save correctly.
  T14 The empty Leads state shows the exact webhook endpoint to configure,
      and the page links to the diagnostics view.
  T15 /operator/calendly diagnostics reports the expected non-secret
      statuses (webhook URL, signing/api flags, lead count) and is gated.
  T16 The diagnostics page never renders any secret value.
  T17 calendly_webhook.diagnostics() reports correct booleans, no secrets.

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
    # Contact inbox falls back to the Cutovr support address when no real
    # SUPPORT_EMAIL is supplied.
    assert "support@cutovr.com" in body
    print("T6 OK: internal email includes meeting + all Q/A + contact")


def t6b_support_email_in_customer_and_contact_resolution():
    # contact_email prefers a real configured address...
    assert calendly_webhook.contact_email("hello@firm.test") == "hello@firm.test"
    # ...and falls back to support@cutovr.com for placeholder/empty input.
    assert calendly_webhook.contact_email("") == "support@cutovr.com"
    assert calendly_webhook.contact_email(None) == "support@cutovr.com"
    assert calendly_webhook.contact_email(
        "support@your-domain.example"
    ) == "support@cutovr.com"

    # Customer confirmation email shows the Cutovr support address when the
    # deploy still has the placeholder SUPPORT_EMAIL.
    lead = calendly_webhook.extract_lead_fields(_created_payload())
    _, body = calendly_webhook.customer_email_bodies(
        app_name="Cutovr", lead=lead,
        support_email="support@your-domain.example",
    )
    assert "support@cutovr.com" in body, body
    # A real configured address is used verbatim (central config wins).
    _, body2 = calendly_webhook.customer_email_bodies(
        app_name="Cutovr", lead=lead, support_email="real@cutovr.com",
    )
    assert "real@cutovr.com" in body2
    assert "support@cutovr.com" not in body2
    print("T6b OK: support@cutovr.com used in customer copy + contact fallback")


def _signup_operator(client):
    """Establish an operator session on ``client``.

    Signs up the operator account on first use; on later calls (the account
    already exists) signup is a no-op that does not create a session, so we
    fall back to logging in. This keeps each test order-independent.
    """
    client.post("/signup", data={
        "firm_name": "Op Firm", "email": "op@cutovr.test",
        "password": "passw0rd!1234", "confirm_password": "passw0rd!1234",
    })
    if client.get("/operator/leads").status_code != 200:
        client.post("/login", data={
            "email": "op@cutovr.test", "password": "passw0rd!1234",
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
    # Admin help copy points operators at the Cutovr support inbox.
    assert "support@cutovr.com" in body

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


def t10_csv_export():
    import csv as _csv
    from io import StringIO

    # Seed a lead with full data + a migration-date question answer.
    c0 = appmod.app.test_client()
    payload = _created_payload(uri=INVITEE_URI + "-csv")
    payload["payload"]["questions_and_answers"].append(
        {"question": "Target migration date", "answer": "2026-08-15", "position": 4},
    )
    _post(c0, payload)

    # Operator can download the export.
    c = appmod.app.test_client()
    _signup_operator(c)
    r = c.get("/operator/leads.csv")
    assert r.status_code == 200, r.status_code
    assert r.mimetype == "text/csv", r.mimetype
    cd = r.headers.get("Content-Disposition", "")
    assert "attachment" in cd and "cutovr-calendly-leads.csv" in cd, cd

    text = r.get_data(as_text=True)
    reader = _csv.DictReader(StringIO(text))
    header = reader.fieldnames
    expected_cols = [
        "Lead ID", "Status", "Meeting Start", "Meeting End", "Timezone",
        "Name", "Email", "Phone", "Law Firm / Company",
        "Clio Rep Name", "Clio Rep Email", "Clio Migration Date",
        "Created At", "Updated At", "Calendly Event URI", "Invitee URI",
        "Questions/Answers",
    ]
    assert header == expected_cols, header

    rows = list(reader)
    match = [x for x in rows if x["Invitee URI"] == INVITEE_URI + "-csv"]
    assert len(match) == 1, f"expected 1 exported row, got {len(match)}"
    row = match[0]
    assert row["Name"] == "Dana Prospect"
    assert row["Email"] == "dana@lawfirm.test"
    assert row["Law Firm / Company"] == "Prospect & Co LLP"
    assert row["Clio Rep Name"] == "Sam Rep"
    assert row["Clio Rep Email"] == "sam@clio.test"
    assert row["Phone"] == "+1 555 0100"
    assert row["Status"] == "scheduled"
    assert row["Meeting Start"] == "2026-07-01T15:00:00Z"
    # Migration date pulled out of the free-form question answers.
    assert row["Clio Migration Date"] == "2026-08-15", row["Clio Migration Date"]
    # Question answers rendered in the combined cell.
    assert "Full GL" in row["Questions/Answers"]
    assert "What do you want to migrate?: Full GL" in row["Questions/Answers"]

    # Raw payload column is never present in the export.
    assert "raw_payload_json" not in header
    assert "Raw" not in " ".join(header)

    # Non-operator (anonymous) is blocked.
    anon = appmod.app.test_client()
    r404 = anon.get("/operator/leads.csv")
    assert r404.status_code in (302, 404), r404.status_code
    print("T10 OK: CSV export content type, columns, data, Q/A; non-op blocked")


def t11_csv_export_no_secret_leaks():
    os.environ["CALENDLY_API_TOKEN"] = "tok-CSVLEAK"
    os.environ["CALENDLY_WEBHOOK_SECRET"] = "secret-CSVLEAK"
    try:
        c0 = appmod.app.test_client()
        _post(c0, _created_payload(uri=INVITEE_URI + "-csvsec"),
              query_string={"secret": "secret-CSVLEAK"})
        c = appmod.app.test_client()
        _signup_operator(c)
        text = c.get("/operator/leads.csv").get_data(as_text=True)
        for needle in ("tok-CSVLEAK", "secret-CSVLEAK"):
            assert needle not in text, f"secret leaked in CSV: {needle}"
    finally:
        os.environ.pop("CALENDLY_API_TOKEN", None)
        os.environ.pop("CALENDLY_WEBHOOK_SECRET", None)
    print("T11 OK: no secrets in CSV export")


def t12_minimal_payload_still_saves():
    """A realistic-but-sparse invitee.created (no firm/clio/phone/event
    details, only uri+name+email) must still create a scheduled lead."""
    uri = INVITEE_URI + "-min"
    minimal = {
        "event": "invitee.created",
        "payload": {"uri": uri, "name": "Min Imal", "email": "min@firm.test"},
    }
    c = appmod.app.test_client()
    r = _post(c, minimal)
    assert r.status_code == 200, r.status_code
    lead = appmod.db.get_calendly_lead_by_invitee(uri)
    assert lead, "minimal lead not stored"
    assert lead["name"] == "Min Imal"
    assert lead["email"] == "min@firm.test"
    assert lead["status"] == "scheduled"
    # Optional fields absent, not an error.
    assert lead["firm_name"] in (None, "")
    print("T12 OK: minimal payload (missing optionals) still saves a lead")


def t13_nested_and_first_last_name_payload():
    """Real Calendly variations: scheduled_event with event_type as an object,
    first_name/last_name instead of name, missing top-level 'event' string."""
    uri = INVITEE_URI + "-nest"
    payload = {
        # No top-level "event" key — must still classify as scheduled.
        "payload": {
            "uri": uri,
            "first_name": "Casey",
            "last_name": "Counsel",
            "email": "casey@firm.test",
            "timezone": "America/New_York",
            "scheduled_event": {
                "uri": "https://api.calendly.com/scheduled_events/EVTX",
                "name": "Discovery",
                "start_time": "2026-09-09T10:00:00Z",
                "end_time": "2026-09-09T10:30:00Z",
                "event_type": {"uri": "https://api.calendly.com/event_types/ETX"},
            },
            "questions_and_answers": [
                {"question": "Company", "answer": "Counsel LLP"},
            ],
        },
    }
    c = appmod.app.test_client()
    r = _post(c, payload)
    assert r.status_code == 200, r.status_code
    lead = appmod.db.get_calendly_lead_by_invitee(uri)
    assert lead, "nested-variation lead not stored"
    assert lead["name"] == "Casey Counsel", lead["name"]
    assert lead["status"] == "scheduled"
    assert lead["meeting_start"] == "2026-09-09T10:00:00Z"
    assert lead["event_type_uri"] == "https://api.calendly.com/event_types/ETX"
    assert lead["firm_name"] == "Counsel LLP"
    print("T13 OK: nested/first+last-name/no-event-string payload saves correctly")


def t14_empty_state_shows_webhook_endpoint():
    """The Leads empty state must tell the operator the exact webhook URL.

    Renders the operator-leads template directly with no leads so the test is
    independent of the shared DB (which other tests populate). Also confirms
    the populated page always links to the diagnostics page.
    """
    diag = appmod.calendly_webhook.diagnostics()
    with appmod.app.test_request_context("/operator/leads"):
        empty_html = appmod.render_template(
            "operator-leads.html", leads=[], calendly=diag
        )
    assert "/integrations/calendly/webhook" in empty_html, "webhook endpoint missing from empty Leads state"
    assert "operator-leads-empty-webhook-url" in empty_html
    assert "/operator/calendly" in empty_html

    # The live (possibly populated) page must still link to diagnostics.
    c = appmod.app.test_client()
    _signup_operator(c)
    body = c.get("/operator/leads").get_data(as_text=True)
    assert "/operator/calendly" in body or "Calendly setup diagnostics" in body
    print("T14 OK: empty Leads state shows webhook endpoint; page links diagnostics")


def t15_diagnostics_route_reports_statuses():
    """The /operator/calendly diagnostics page reports the expected
    non-secret statuses and is operator-gated."""
    # Seed a lead so the count/last-received are populated.
    c0 = appmod.app.test_client()
    _post(c0, _created_payload(uri=INVITEE_URI + "-diag"))

    os.environ["CALENDLY_WEBHOOK_SIGNING_KEY"] = "whsec-diag"
    try:
        c = appmod.app.test_client()
        _signup_operator(c)
        r = c.get("/operator/calendly")
        assert r.status_code == 200, r.status_code
        body = r.get_data(as_text=True)
        assert "/integrations/calendly/webhook" in body
        # Signing configured -> shows verified / yes.
        assert "Verified" in body or "Yes" in body
        # Stored-lead count present.
        assert "Stored Calendly leads" in body
        assert "Most recent webhook received" in body

        # Non-operator blocked.
        anon = appmod.app.test_client()
        r404 = anon.get("/operator/calendly")
        assert r404.status_code in (302, 404), r404.status_code
    finally:
        os.environ.pop("CALENDLY_WEBHOOK_SIGNING_KEY", None)
    print("T15 OK: diagnostics route reports statuses; non-operator blocked")


def t16_diagnostics_no_secret_leaks():
    """Diagnostics page renders presence flags but never any secret value."""
    os.environ["CALENDLY_WEBHOOK_SIGNING_KEY"] = "whsec-DIAGLEAK"
    os.environ["CALENDLY_API_TOKEN"] = "tok-DIAGLEAK"
    os.environ["CALENDLY_WEBHOOK_SECRET"] = "secret-DIAGLEAK"
    try:
        c = appmod.app.test_client()
        _signup_operator(c)
        body = c.get("/operator/calendly").get_data(as_text=True)
        for needle in ("whsec-DIAGLEAK", "tok-DIAGLEAK", "secret-DIAGLEAK"):
            assert needle not in body, f"secret leaked in diagnostics: {needle}"
    finally:
        os.environ.pop("CALENDLY_WEBHOOK_SIGNING_KEY", None)
        os.environ.pop("CALENDLY_API_TOKEN", None)
        os.environ.pop("CALENDLY_WEBHOOK_SECRET", None)
    print("T16 OK: no secrets rendered on diagnostics page")


def t17_diagnostics_helper_unit():
    """Unit-level: calendly_webhook.diagnostics reports the right booleans
    and never includes secret values."""
    os.environ["CALENDLY_WEBHOOK_SIGNING_KEY"] = "whsec-UNIT"
    os.environ["APP_ENV"] = "production"
    try:
        d = calendly_webhook.diagnostics(lead_count=3, last_lead_at="2026-06-19T00:00:00")
        assert d["signing_key_configured"] is True
        assert d["authenticity_mode"] == "verified"
        assert d["is_production"] is True
        assert d["lead_count"] == 3
        assert d["webhook_endpoint_url"].endswith("/integrations/calendly/webhook")
        # No secret value anywhere in the dict.
        assert "whsec-UNIT" not in json.dumps(d)
    finally:
        os.environ.pop("CALENDLY_WEBHOOK_SIGNING_KEY", None)
        os.environ.pop("APP_ENV", None)
    # Without auth in a non-prod env, mode is unverified-open.
    d2 = calendly_webhook.diagnostics(app_env="local")
    assert d2["authenticity_mode"] == "unverified-open"
    assert d2["signing_key_configured"] is False
    print("T17 OK: diagnostics() reports correct booleans, no secrets")


if __name__ == "__main__":
    t1_created_makes_lead()
    t2_duplicate_is_idempotent()
    t3_canceled_updates_status()
    t4_no_token_does_not_fail()
    t5_enrichment_path_mocked()
    t6_internal_email_includes_details()
    t6b_support_email_in_customer_and_contact_resolution()
    t7_operator_route_lists_and_blocks()
    t8_no_secret_leaks()
    t9_signature_verification()
    t10_csv_export()
    t11_csv_export_no_secret_leaks()
    t12_minimal_payload_still_saves()
    t13_nested_and_first_last_name_payload()
    t14_empty_state_shows_webhook_endpoint()
    t15_diagnostics_route_reports_statuses()
    t16_diagnostics_no_secret_leaks()
    t17_diagnostics_helper_unit()
    print("\nALL CALENDLY SMOKE TESTS PASSED")
