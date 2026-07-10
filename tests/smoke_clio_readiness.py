"""Clio Accounting readiness service-lane smoke tests.

Run from project root:

    python3 tests/smoke_clio_readiness.py

Covers the two new service lanes (PC Law -> Clio Accounting Readiness and
QuickBooks Online -> Clio Accounting Readiness) alongside the preserved
PC Law -> QuickBooks default:

  T1  service_lanes: constants, normalize/default, is_clio_accounting,
      uses_qbo_posting gating, free-text detection.
  T2  clio_accounting.ClioAccountingIntegrationStatus: not enabled by default,
      flips with CLIO_ACCOUNTING_API_ENABLED, never leaks scary customer copy.
  T3  /intake renders all three selectable service-lane options.
  T4  Intake submit with a Clio lane persists service_lane and the success
      page links to the Clio readiness page (no QBO posting language).
  T5  Intake submit with NO lane defaults to the PCLaw->QBO flow (service_lane
      stored NULL, no Clio readiness link) — backward compatible.
  T6  /clio-accounting-readiness renders lane-specific document checklists that
      DIFFER between the two lanes, the readiness stages, and never shows a
      "Send to QuickBooks" action.
  T7  Operator intake list shows a Service column + the lane label.
  T8  Calendly webhook derives service_lane from the event/answers, and the
      operator leads view surfaces it.
"""

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
os.environ.setdefault("SECRET_KEY", "smoke-clio-secret")
os.environ["OPERATOR_EMAILS"] = "op@cutovr.test"
# SMTP unconfigured so intake never tries to send a real email.
for _k in ("SMTP_HOST", "MAIL_SERVER", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
    os.environ.pop(_k, None)

import app as appmod  # noqa: E402
import service_lanes as sl  # noqa: E402
import clio_accounting  # noqa: E402
import calendly_webhook  # noqa: E402


def _form(**overrides):
    data = {
        "firm_name": "Meridian Law LLP",
        "first_name": "Casey",
        "last_name": "Nguyen",
        "email": "casey@meridian.test",
        "clio_migration_date": "2026-04-01",
    }
    data.update(overrides)
    return data


def t1_service_lanes_module():
    assert sl.DEFAULT_LANE == sl.PCLAW_TO_QBO
    assert set(sl.SELECTABLE_LANES) == {
        sl.PCLAW_TO_QBO, sl.PCLAW_TO_CLIO_ACCOUNTING, sl.QBO_TO_CLIO_ACCOUNTING
    }
    # normalize + default behavior for legacy/missing values.
    assert sl.normalize(None) is None
    assert sl.normalize("bogus") is None
    assert sl.normalize("PCLAW_TO_QBO") == sl.PCLAW_TO_QBO
    assert sl.effective_lane(None) == sl.PCLAW_TO_QBO
    # Clio classification.
    assert sl.is_clio_accounting(sl.PCLAW_TO_CLIO_ACCOUNTING)
    assert sl.is_clio_accounting(sl.QBO_TO_CLIO_ACCOUNTING)
    assert not sl.is_clio_accounting(sl.PCLAW_TO_QBO)
    assert not sl.is_clio_accounting(None)
    # QBO posting gate: ONLY the default lane posts to QuickBooks. Missing
    # lanes fall back to the default (posting), preserving legacy behavior.
    assert sl.uses_qbo_posting(sl.PCLAW_TO_QBO)
    assert sl.uses_qbo_posting(None)
    assert not sl.uses_qbo_posting(sl.PCLAW_TO_CLIO_ACCOUNTING)
    assert not sl.uses_qbo_posting(sl.QBO_TO_CLIO_ACCOUNTING)
    # Display fallback.
    assert sl.label(None) == "Not specified"
    assert "Clio Accounting" in sl.label(sl.QBO_TO_CLIO_ACCOUNTING)
    # Free-text detection.
    assert sl.detect_service_lane("PC Law to Clio Accounting") == sl.PCLAW_TO_CLIO_ACCOUNTING
    assert sl.detect_service_lane("QuickBooks Online → Clio Accounting") == sl.QBO_TO_CLIO_ACCOUNTING
    assert sl.detect_service_lane("PCLaw to QuickBooks") == sl.PCLAW_TO_QBO
    assert sl.detect_service_lane("just a chat") is None
    # Checklists exist for both Clio lanes and are non-empty.
    assert sl.readiness_documents(sl.PCLAW_TO_CLIO_ACCOUNTING)
    assert sl.readiness_documents(sl.QBO_TO_CLIO_ACCOUNTING)
    assert sl.readiness_documents(sl.PCLAW_TO_QBO) == []
    assert len(sl.readiness_stages()) == 7
    print("T1 OK: service_lanes constants, gating, detection, checklists")


def t2_integration_status_placeholder():
    os.environ.pop("CLIO_ACCOUNTING_API_ENABLED", None)
    st = clio_accounting.integration_status()
    assert st.status == clio_accounting.STATUS_NOT_ENABLED
    assert st.enabled is False and clio_accounting.is_enabled() is False
    # Operator message may name the API gap; customer message must stay calm.
    assert "not enabled" in st.operator_message.lower()
    assert "unavailable" not in st.customer_message.lower()
    assert "not enabled" not in st.customer_message.lower()
    # Future flip.
    os.environ["CLIO_ACCOUNTING_API_ENABLED"] = "1"
    try:
        assert clio_accounting.is_enabled() is True
        assert clio_accounting.integration_status().status == clio_accounting.STATUS_ENABLED
    finally:
        os.environ.pop("CLIO_ACCOUNTING_API_ENABLED", None)
    print("T2 OK: ClioAccountingIntegrationStatus not-enabled default + safe copy")


def t3_intake_renders_service_options():
    c = appmod.app.test_client()
    body = c.get("/intake").get_data(as_text=True)
    assert "Which migration?" in body
    for label in (
        "PC Law to QuickBooks Online migration",
        "PC Law to Clio Accounting Readiness",
        "QuickBooks Online to Clio Accounting Readiness",
    ):
        assert label in body, f"intake missing service option: {label!r}"
    assert 'name="service_lane"' in body
    print("T3 OK: /intake renders all three service-lane options")


def t4_intake_submit_clio_lane_persists_and_links():
    c = appmod.app.test_client()
    r = c.post(
        "/intake",
        data=_form(email="clio-lane@meridian.test",
                   service_lane=sl.PCLAW_TO_CLIO_ACCOUNTING),
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "You're all set" in body
    # Success page routes the firm to the Clio readiness page, not QBO posting.
    assert "/clio-accounting-readiness" in body
    assert "Send to QuickBooks" not in body
    rec = appmod.db.recent_intake_submissions(limit=1)[0]
    assert rec["service_lane"] == sl.PCLAW_TO_CLIO_ACCOUNTING, rec["service_lane"]
    print("T4 OK: Clio-lane intake persists service_lane + links to readiness")


def t5_intake_submit_default_is_backward_compatible():
    c = appmod.app.test_client()
    # No service_lane field at all — mimics a legacy client / plain submit.
    r = c.post(
        "/intake",
        data=_form(email="default-lane@meridian.test"),
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Default lane is PCLaw->QBO: no Clio readiness link on the success page.
    assert "/clio-accounting-readiness" not in body
    rec = appmod.db.recent_intake_submissions(limit=1)[0]
    # Stored as the default lane; treated as PCLaw->QBO by the workflow.
    assert sl.effective_lane(rec["service_lane"]) == sl.PCLAW_TO_QBO
    assert sl.uses_qbo_posting(rec["service_lane"]) is True  # default posts to QBO
    print("T5 OK: no-lane intake defaults to PCLaw->QBO (backward compatible)")


def t6_readiness_page_checklists_differ_no_qbo():
    c = appmod.app.test_client()
    pclaw = c.get("/clio-accounting-readiness?lane=pclaw_to_clio_accounting")
    qbo = c.get("/clio-accounting-readiness?lane=qbo_to_clio_accounting")
    assert pclaw.status_code == 200 and qbo.status_code == 200
    pbody = pclaw.get_data(as_text=True)
    qbody = qbo.get_data(as_text=True)
    # Framed as preparing a cutover package, not importing into Clio.
    assert "prepare your Clio Accounting cutover package" in pbody
    assert "import directly into Clio" not in pbody.lower().replace("&nbsp;", " ")
    # Clio lanes must NOT expose QBO posting actions/language.
    for banned in ("Send to QuickBooks", "Step 5", "Post to QuickBooks"):
        assert banned not in pbody, f"readiness page leaked QBO action: {banned}"
        assert banned not in qbody
    # PCLaw-specific items appear on the PCLaw lane, not (all) on the QBO lane.
    assert "Beginning Trial Balance" in pbody
    assert "Trust Listing as of cutover" in pbody
    assert "Beginning Trial Balance" not in qbody
    # QBO-specific items appear on the QBO lane.
    assert "A/R Aging" in qbody
    assert "Reconciliation reports" in qbody
    assert "A/R Aging" not in pbody
    # Readiness stages render on both.
    assert "Ready for discovery / accountant review" in pbody
    assert "Data collected" in qbody
    print("T6 OK: readiness checklists differ per lane, no QBO posting actions")


def t7_operator_intake_shows_service():
    c = appmod.app.test_client()
    c.post("/signup", data={
        "firm_name": "Op Firm", "email": "op@cutovr.test",
        "password": "passw0rd!1234", "confirm_password": "passw0rd!1234",
    })
    body = c.get("/operator/intake").get_data(as_text=True)
    assert "Service" in body, "operator intake missing Service column"
    assert "PC Law to Clio Accounting Readiness" in body, \
        "operator intake should show the captured lane label"
    print("T7 OK: operator intake list shows Service column + lane")


def t8_calendly_lead_service_lane():
    # extract_lead_fields should detect the lane from the event name.
    payload = {
        "event": "invitee.created",
        "payload": {
            "uri": "https://api.calendly.com/scheduled_events/EV1/invitees/INV1",
            "name": "Dana Cutover",
            "email": "dana@firm.test",
            "scheduled_event": {
                "uri": "https://api.calendly.com/scheduled_events/EV1",
                "name": "QuickBooks Online to Clio Accounting Readiness",
                "start_time": "2026-05-01T15:00:00Z",
            },
            "questions_and_answers": [
                {"question": "Law firm name", "answer": "Firm Test"},
            ],
        },
    }
    fields = calendly_webhook.extract_lead_fields(payload)
    assert fields.get("service_lane") == sl.QBO_TO_CLIO_ACCOUNTING, fields.get("service_lane")

    # Persist and confirm the operator leads view surfaces the label.
    invitee_uri = payload["payload"]["uri"]
    fields["invitee_uuid"] = "INV1"
    appmod.db.upsert_calendly_lead(invitee_uri=invitee_uri, fields=fields)
    stored = appmod.db.get_calendly_lead_by_invitee(invitee_uri)
    assert stored["service_lane"] == sl.QBO_TO_CLIO_ACCOUNTING

    c = appmod.app.test_client()
    # Operator account already exists from T7; log in to get a session.
    c.post("/login", data={"email": "op@cutovr.test", "password": "passw0rd!1234"})
    body = c.get("/operator/leads").get_data(as_text=True)
    assert "Service" in body
    assert "QuickBooks Online to Clio Accounting Readiness" in body
    print("T8 OK: calendly lead captures + operator leads shows service lane")


def _signup(client, email, firm="Gate Firm"):
    client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": "passw0rd!1234", "confirm_password": "passw0rd!1234",
    })
    return appmod.db.get_user_by_email(email)


def _make_gl_job(firm_id, user_id, job_id):
    appmod.db.upsert_job(
        job_id=job_id, firm_id=firm_id, user_id=user_id, company="Co",
        source_file="gl.csv", encrypted_file="gl.csv.enc",
        file_sha256="deadbeef", status="ready",
    )


def t9_qbo_posting_gate():
    """The QBO posting flow must be reachable for PCLaw->QBO firms and blocked
    (fail-closed) for Clio readiness firms, at every entry point."""
    # --- (4) Default/NULL-lane firm resolves to PCLaw->QBO: gate lets it pass.
    c_def = appmod.app.test_client()
    u_def = _signup(c_def, "gate-default@meridian.test", "Default Firm")
    assert appmod.db.resolve_service_lane_for_firm(u_def["firm_id"]) is None
    assert sl.uses_qbo_posting(
        appmod.db.resolve_service_lane_for_firm(u_def["firm_id"])
    ) is True
    # (1) /send-to-qbo for the default firm must NOT divert to Clio readiness
    # (no GL jobs yet -> it redirects to the dashboard, which is fine).
    r = c_def.get("/send-to-qbo", follow_redirects=False)
    assert r.status_code in (301, 302), r.status_code
    assert "/clio-accounting-readiness" not in r.headers.get("Location", ""), \
        "default PCLaw->QBO firm was wrongly diverted to Clio readiness"
    # With a GL job present, the entry resolves to the job-scoped Step 5 page
    # (connect-qbo / step5), still never the Clio readiness page.
    _make_gl_job(u_def["firm_id"], u_def["id"], "job-default-gl")
    r = c_def.get("/send-to-qbo", follow_redirects=False)
    assert "/clio-accounting-readiness" not in r.headers.get("Location", "")

    # --- (2)+(3) Clio readiness firms are blocked at all three entry points.
    for email, lane in (
        ("gate-pclaw-clio@meridian.test", sl.PCLAW_TO_CLIO_ACCOUNTING),
        ("gate-qbo-clio@meridian.test", sl.QBO_TO_CLIO_ACCOUNTING),
    ):
        c = appmod.app.test_client()
        u = _signup(c, email, "Clio Firm")
        appmod.db.set_firm_service_lane(u["firm_id"], lane)
        assert appmod.db.resolve_service_lane_for_firm(u["firm_id"]) == lane
        assert sl.uses_qbo_posting(lane) is False

        # Firm-level Step 5 entry -> readiness page for this lane.
        r = c.get("/send-to-qbo", follow_redirects=False)
        assert r.status_code in (301, 302), r.status_code
        loc = r.headers.get("Location", "")
        assert "/clio-accounting-readiness" in loc and lane in loc, loc

        # Job-scoped Step 5 page -> readiness page.
        jid = f"job-{lane}"
        _make_gl_job(u["firm_id"], u["id"], jid)
        r = c.get(f"/jobs/{jid}/send-to-qbo", follow_redirects=False)
        loc = r.headers.get("Location", "")
        assert "/clio-accounting-readiness" in loc and lane in loc, loc

        # The actual posting POST is fail-closed -> readiness page, never posts.
        r = c.post(f"/jobs/{jid}/import-to-qbo", follow_redirects=False)
        loc = r.headers.get("Location", "")
        assert "/clio-accounting-readiness" in loc and lane in loc, \
            f"import-to-qbo POST not gated for {lane}: {loc!r}"

        # post-ob and push-entity-list (other real QBO-write routes) redirect too.
        for path in (f"/jobs/{jid}/post-ob", f"/jobs/{jid}/push-entity-list"):
            r = c.post(path, follow_redirects=False)
            loc = r.headers.get("Location", "")
            assert "/clio-accounting-readiness" in loc and lane in loc, \
                f"{path} not gated for {lane}: {loc!r}"

        # The JS-driven initialpost endpoints are fail-closed too: they return a
        # JSON block (403 + redirect) instead of starting a QBO posting sequence.
        r = c.post(f"/initialpost/start/{jid}")
        assert r.status_code == 403, r.status_code
        body = r.get_json() or {}
        assert body.get("blocked") is True
        assert "/clio-accounting-readiness" in (body.get("redirect") or "")
        assert lane in (body.get("redirect") or "")
        r = c.post(f"/initialpost/retry/{jid}/some_step")
        assert r.status_code == 403, r.status_code
        assert "/clio-accounting-readiness" in ((r.get_json() or {}).get("redirect") or "")

    # --- Signup propagation: a lane captured at intake time (by email) is
    # stamped onto the firm at signup, so the gate fires without a manual set.
    appmod.db.create_intake_submission(
        reference="GATEPROP1", firm_name="Propagate Firm", first_name="Pat",
        last_name="Lee", email="gate-propagate@meridian.test",
        service_lane=sl.QBO_TO_CLIO_ACCOUNTING,
    )
    c_prop = appmod.app.test_client()
    u_prop = _signup(c_prop, "gate-propagate@meridian.test", "Propagate Firm")
    firm = appmod.db.get_firm(u_prop["firm_id"])
    assert firm.get("service_lane") == sl.QBO_TO_CLIO_ACCOUNTING, \
        f"signup did not stamp lane from intake: {firm.get('service_lane')!r}"
    r = c_prop.get("/send-to-qbo", follow_redirects=False)
    assert "/clio-accounting-readiness" in r.headers.get("Location", "")
    print("T9 OK: QBO posting gate — PCLaw->QBO passes, Clio lanes fail-closed")


def t10_firm_lane_migration_idempotent():
    """firms.service_lane migration is safe to run repeatedly on an existing DB."""
    import app_db
    # Re-open the same app DB twice more; add_col must swallow duplicate column.
    app_db.AppDB(os.environ["APP_DB"])
    app_db.AppDB(os.environ["APP_DB"])
    import sqlite3
    con = sqlite3.connect(os.environ["APP_DB"])
    cols = [r[1] for r in con.execute("PRAGMA table_info(firms)").fetchall()]
    con.close()
    assert "service_lane" in cols, "firms.service_lane column missing"
    print("T10 OK: firms.service_lane migration is idempotent")


if __name__ == "__main__":
    try:
        t1_service_lanes_module()
        t2_integration_status_placeholder()
        t3_intake_renders_service_options()
        t4_intake_submit_clio_lane_persists_and_links()
        t5_intake_submit_default_is_backward_compatible()
        t6_readiness_page_checklists_differ_no_qbo()
        t7_operator_intake_shows_service()
        t8_calendly_lead_service_lane()
        t9_qbo_posting_gate()
        t10_firm_lane_migration_idempotent()
        print("\nALL CLIO READINESS SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
