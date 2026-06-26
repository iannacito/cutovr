"""Calendly discovery-call lead capture.

Calendly remains the booking + form UI. Cutovr does not rebuild the form;
it subscribes to Calendly webhooks and stores each booked discovery call as
a lead so the team has its own database and admin view.

Webhook events handled
----------------------

  invitee.created      A prospect filled out the Calendly form and booked.
                       Stored as a new lead (status='scheduled').
  invitee.canceled     A previously-booked call was canceled. The matching
                       lead is flipped to status='canceled' with any reason.
  routing_form_submission.created
                       Optional. A routing-form answer set, captured as a
                       lightweight lead so the team sees pre-booking intent.

Idempotency
-----------

Every lead is keyed on the Calendly *invitee URI* (a globally-unique,
canonical identifier for one person on one event). A duplicate webhook
delivery updates the existing row rather than inserting a second lead, so
Calendly's at-least-once delivery never produces duplicates. Routing-form
submissions, which have no invitee URI, key on the submission URI instead.

Authenticity
------------

Calendly signs webhook payloads with a per-subscription signing key using
an HMAC-SHA256 over ``<timestamp>.<body>``, sent in the
``Calendly-Webhook-Signature`` header as ``t=<ts>,v1=<sig>``. When
``CALENDLY_WEBHOOK_SIGNING_KEY`` is configured we verify that signature.
As a simpler fallback (useful before the signing key is wired up, or for a
plain shared-secret gate), ``CALENDLY_WEBHOOK_SECRET`` can be set and is
compared in constant time against a token in the URL/header. If neither is
configured the endpoint still accepts payloads (so a first test booking
works) but records that the delivery was unverified.

Enrichment
----------

If ``CALENDLY_API_TOKEN`` is configured and the payload carries an invitee
URI, we fetch the full invitee record (which includes custom question
answers) from Calendly's API. The fetch has a short timeout and fails soft:
a network error never fails the webhook — the lead is still stored from the
payload and marked enrichment 'failed'/'unavailable'.

This module never logs secrets (signing key, API token, webhook secret).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Optional

import requests


log = logging.getLogger("calendly_webhook")

CALENDLY_API_BASE = "https://api.calendly.com"
_API_TIMEOUT_SECONDS = 6

# Customer-facing contact address shown in the optional prospect
# confirmation email when no real SUPPORT_EMAIL is configured (the deploy
# default is a placeholder). Central support config still wins when set via
# the SUPPORT_EMAIL env var / branding.SUPPORT_EMAIL.
DEFAULT_SUPPORT_EMAIL = "support@cutovr.com"


def contact_email(support_email: Optional[str]) -> str:
    """Resolve the customer-facing contact address.

    Prefers a real configured ``support_email``; falls back to the Cutovr
    support mailbox when the caller passes nothing or a deploy-default
    placeholder. Never returns an empty string so confirmation copy always
    gives the prospect a way to reach us.
    """
    s = (support_email or "").strip()
    if s and not s.endswith("@your-domain.example"):
        return s
    return DEFAULT_SUPPORT_EMAIL


# ---------------------------------------------------------------------------
# Config helpers (read at call time so tests can monkeypatch env)
# ---------------------------------------------------------------------------

def _env(name: str) -> Optional[str]:
    val = os.environ.get(name)
    return val.strip() if val else None


def api_token() -> Optional[str]:
    return _env("CALENDLY_API_TOKEN")


def signing_key() -> Optional[str]:
    return _env("CALENDLY_WEBHOOK_SIGNING_KEY")


def shared_secret() -> Optional[str]:
    return _env("CALENDLY_WEBHOOK_SECRET")


def booking_url() -> Optional[str]:
    return _env("DISCOVERY_CALL_URL")


def confirmation_email_enabled() -> bool:
    raw = (os.environ.get("CALENDLY_CONFIRMATION_EMAIL") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


WEBHOOK_PATH = "/integrations/calendly/webhook"


def webhook_endpoint_url(public_base_url: Optional[str] = None) -> str:
    """Return the exact URL Calendly must POST to.

    Uses the deploy's PUBLIC_APP_URL (or an explicit ``public_base_url``)
    so the diagnostics panel / setup docs always show the operator the
    real endpoint to paste into Calendly, not a hard-coded guess. Falls
    back to the production host when nothing is configured.
    """
    base = (public_base_url or _env("PUBLIC_APP_URL") or "https://www.cutovr.com").rstrip("/")
    return base + WEBHOOK_PATH


def diagnostics(*, app_env: Optional[str] = None,
                public_base_url: Optional[str] = None,
                lead_count: Optional[int] = None,
                last_lead_at: Optional[str] = None,
                operator_emails_count: Optional[int] = None) -> dict:
    """Return a secret-free Calendly setup status snapshot.

    Reports only presence booleans, the (non-secret) webhook endpoint to
    paste into Calendly, and counts — never the value of any signing key,
    API token, or shared secret. ``authenticity_mode`` mirrors the policy
    in ``authenticate``: in a non-dev deploy a delivery is rejected unless a
    signing key or shared secret is configured, so an unverified-open state
    is flagged as a setup gap to fix.
    """
    env = (app_env or os.environ.get("APP_ENV") or "local").lower()
    is_prod = env not in ("local", "dev", "development", "test")

    has_signing = bool(signing_key())
    has_secret = bool(shared_secret())
    auth_configured = has_signing or has_secret

    # Mirror authenticate(): if a signing key or shared secret is configured,
    # deliveries must pass it ("verified"); otherwise they are accepted but
    # flagged "unverified-open" so a first test booking still lands. In a
    # production deploy, running unverified-open is a setup gap to close.
    if auth_configured:
        authenticity_mode = "verified"
    else:
        authenticity_mode = "unverified-open"

    bk = booking_url()
    return {
        "app_env": env,
        "is_production": is_prod,
        "webhook_endpoint_url": webhook_endpoint_url(public_base_url),
        "booking_url_configured": bool(bk),
        "booking_url_is_default": not bool(bk),
        "signing_key_configured": has_signing,
        "shared_secret_configured": has_secret,
        "api_token_configured": bool(api_token()),
        "confirmation_email_enabled": confirmation_email_enabled(),
        "authenticity_mode": authenticity_mode,
        "lead_count": lead_count if lead_count is not None else None,
        "last_lead_at": last_lead_at,
        "operator_emails_count": operator_emails_count,
    }


# ---------------------------------------------------------------------------
# Authenticity
# ---------------------------------------------------------------------------

def _parse_signature_header(header: str) -> tuple[Optional[str], Optional[str]]:
    """Split Calendly's ``t=<ts>,v1=<sig>`` header into (timestamp, sig)."""
    ts = None
    sig = None
    for part in (header or "").split(","):
        part = part.strip()
        if part.startswith("t="):
            ts = part[2:]
        elif part.startswith("v1="):
            sig = part[3:]
    return ts, sig


def verify_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """Verify the Calendly webhook HMAC signature.

    Returns True only when a signing key is configured AND the header's v1
    signature matches HMAC-SHA256(key, "<t>.<body>"). Returns False on any
    mismatch or missing pieces. Constant-time comparison.
    """
    key = signing_key()
    if not key or not signature_header:
        return False
    ts, sig = _parse_signature_header(signature_header)
    if not ts or not sig:
        return False
    signed = f"{ts}.".encode("utf-8") + (raw_body or b"")
    expected = hmac.new(key.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def check_shared_secret(provided: Optional[str]) -> bool:
    """Constant-time compare of a provided token against CALENDLY_WEBHOOK_SECRET."""
    secret = shared_secret()
    if not secret or not provided:
        return False
    return hmac.compare_digest(secret, provided.strip())


def authenticate(raw_body: bytes, *, signature_header: Optional[str],
                 provided_secret: Optional[str]) -> dict:
    """Decide whether a webhook delivery is authentic.

    Returns a dict {"verified": bool, "method": str}. ``method`` is one of
    "signature", "shared_secret", "unverified-open" (no auth configured, so
    we accept to allow a first test), or "rejected".

    Policy: if a signing key OR shared secret is configured, the delivery
    MUST satisfy one of them, otherwise it's rejected. If neither is
    configured we accept but flag it unverified so the operator notices.
    """
    have_sig = bool(signing_key())
    have_secret = bool(shared_secret())
    if not have_sig and not have_secret:
        return {"verified": False, "method": "unverified-open"}
    if have_sig and verify_signature(raw_body, signature_header):
        return {"verified": True, "method": "signature"}
    if have_secret and check_shared_secret(provided_secret):
        return {"verified": True, "method": "shared_secret"}
    return {"verified": False, "method": "rejected"}


# ---------------------------------------------------------------------------
# Payload extraction
# ---------------------------------------------------------------------------

# Question texts we map onto first-class lead columns. Calendly question
# labels are operator-defined, so we match loosely (lowercased substring).
#
# Order matters: each answered question is assigned to the FIRST field below
# whose hints match, then skipped. This resolves overlaps such as "What is
# your role at the firm?" (which contains "firm") — ``role`` is checked before
# ``firm_name`` so the role question is not mis-filed as the firm name.
_QA_FIELD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("role", ("your role", "role at", "role/title", "job title", "what is your role",
              "position at", "your title")),
    ("migration_date", ("migration date", "clio migration", "migrate date",
                        "go live", "go-live", "cutover date", "target date",
                        "clio date", "migration target")),
    ("years_history", ("years of history", "history to bring", "years of data",
                       "how many years", "years to migrate", "years to bring")),
    ("volume", ("rough volume", "volume", "transactions or reports", "gl rows",
                "number of transactions", "transaction volume")),
    ("notes", ("notes & timeline", "notes and timeline", "notes/timeline",
               "notes", "timeline", "anything else", "additional context")),
    ("clio_rep_email", ("clio rep email", "clio email",
                        "clio representative email")),
    ("clio_rep_name", ("clio rep name", "clio representative",
                       "clio contact name", "clio rep")),
    ("phone", ("phone", "mobile", "cell", "telephone")),
    ("firm_name", ("law firm", "firm name", "name of your firm", "your firm",
                   "company", "organization", "organisation", "firm")),
)

# Per-field max lengths so a long free-form answer can't blow out a column.
_QA_FIELD_LIMITS = {
    "firm_name": 300, "role": 200, "migration_date": 100, "years_history": 100,
    "volume": 500, "notes": 2000, "clio_rep_name": 300, "clio_rep_email": 254,
    "phone": 60,
}


def _match_hint(label: str, hints) -> bool:
    low = (label or "").strip().lower()
    return any(h in low for h in hints)


def classify_qa_fields(qa: list[dict]) -> dict:
    """Map a normalized Q&A list onto first-class lead columns.

    Each answered question is assigned to at most one field, using the first
    matching rule in ``_QA_FIELD_RULES`` (priority order). Empty answers are
    ignored. Returns only the fields that were found, each truncated to its
    column limit. Never raises.
    """
    out: dict = {}
    for item in qa or []:
        q = (item.get("question") or "")
        a = (item.get("answer") or "").strip()
        if not a:
            continue
        for field, hints in _QA_FIELD_RULES:
            if field in out:
                continue
            if _match_hint(q, hints):
                out[field] = a[: _QA_FIELD_LIMITS.get(field, 300)]
                break
    return out


def _normalize_qa(questions_and_answers) -> list[dict]:
    """Normalize Calendly's questions_and_answers list to [{question, answer}].

    Calendly's payload uses keys ``question`` and ``answer`` (and a
    ``position``). We keep only question/answer and coerce to strings so
    the JSON we store and render is predictable.
    """
    out: list[dict] = []
    if not isinstance(questions_and_answers, list):
        return out
    for qa in questions_and_answers:
        if not isinstance(qa, dict):
            continue
        q = qa.get("question")
        a = qa.get("answer")
        if q is None and a is None:
            continue
        out.append({
            "question": "" if q is None else str(q),
            "answer": "" if a is None else str(a),
        })
    return out


def extract_lead_fields(payload: dict, enriched_invitee: Optional[dict] = None) -> dict:
    """Build the lead column dict from a webhook payload (+ optional API data).

    Accepts the full webhook envelope ({"event": ..., "payload": {...}}) and,
    optionally, an ``enriched_invitee`` dict (the ``resource`` from the
    Calendly invitee API) whose question answers take precedence over the
    webhook payload because they are authoritative and complete.

    Returns a dict suitable for AppDB.upsert_calendly_lead(fields=...). The
    caller is responsible for adding raw_payload_json + enrichment_status.
    Never raises on missing/oddly-shaped data — absent fields are simply
    omitted.
    """
    event = (payload.get("event") or payload.get("event_type") or "").strip()
    body = payload.get("payload")
    if not isinstance(body, dict):
        # Some integrations / API responses POST the bare invitee with no
        # envelope, or use {"resource": {...}}. Fall back to those so a real
        # booking is never dropped just because the envelope shape differs.
        if isinstance(payload.get("resource"), dict):
            body = payload["resource"]
        elif payload.get("uri") or payload.get("email"):
            body = payload
        else:
            body = {}

    # The invitee object is the webhook payload itself for invitee.* events,
    # but some deliveries nest it one level deeper under "invitee". Merge the
    # nested invitee in first so its fields are available, then let the
    # top-level body win, then enrichment (most authoritative) win last.
    invitee: dict = {}
    nested = body.get("invitee")
    if isinstance(nested, dict):
        invitee.update(nested)
    invitee.update({k: v for k, v in body.items() if k != "invitee"})
    if enriched_invitee and isinstance(enriched_invitee, dict):
        for k, v in enriched_invitee.items():
            if v is not None:
                invitee[k] = v

    fields: dict = {}

    invitee_uri = invitee.get("uri")
    if invitee_uri:
        fields["invitee_uuid"] = str(invitee_uri).rstrip("/").rsplit("/", 1)[-1]

    name = invitee.get("name")
    if not name:
        first = invitee.get("first_name") or ""
        last = invitee.get("last_name") or ""
        name = (first + " " + last).strip()
    if name:
        fields["name"] = str(name)[:300]

    if invitee.get("email"):
        fields["email"] = str(invitee["email"]).strip()[:254]

    # Scheduled event details (start/end/timezone/name). The webhook may
    # carry these inline (scheduled_event) or only an event URI.
    sched = invitee.get("scheduled_event") or body.get("scheduled_event") or {}
    if isinstance(sched, dict):
        if sched.get("uri"):
            fields["event_uri"] = str(sched["uri"])
        if sched.get("name"):
            fields["event_name"] = str(sched["name"])[:300]
        if sched.get("start_time"):
            fields["meeting_start"] = str(sched["start_time"])
        if sched.get("end_time"):
            fields["meeting_end"] = str(sched["end_time"])
        et = sched.get("event_type")
        if isinstance(et, dict):
            # Calendly sometimes expands event_type into an object; keep its uri.
            if et.get("uri"):
                fields["event_type_uri"] = str(et["uri"])
        elif et:
            fields["event_type_uri"] = str(et)
    if invitee.get("event"):
        fields.setdefault("event_uri", str(invitee["event"]))
    if invitee.get("event_type") and "event_type_uri" not in fields:
        et = invitee["event_type"]
        fields["event_type_uri"] = str(et.get("uri")) if isinstance(et, dict) else str(et)
    if invitee.get("timezone"):
        fields["timezone"] = str(invitee["timezone"])

    # Custom question answers. Store the full list for the Details view and
    # derive first-class columns (firm, role, migration date, years, volume,
    # notes, Clio rep, phone) from it.
    qa = _normalize_qa(invitee.get("questions_and_answers"))
    if qa:
        fields["questions_json"] = json.dumps(qa)
        for field, value in classify_qa_fields(qa).items():
            fields.setdefault(field, value)

    # Calendly also exposes a dedicated text_reminder_number / phone field.
    if "phone" not in fields:
        for key in ("text_reminder_number", "phone_number"):
            if invitee.get(key):
                fields["phone"] = str(invitee[key])[:60]
                break

    # Status / cancellation. We key primarily off the event name but also
    # honor an explicit status / cancellation block in the payload so a
    # delivery with a missing or unexpected ``event`` field still classifies
    # correctly instead of being dropped. Anything that isn't clearly a
    # cancellation is treated as a scheduled booking.
    is_canceled = (
        event == "invitee.canceled"
        or invitee.get("status") == "canceled"
        or bool(invitee.get("cancellation"))
        or bool(invitee.get("canceled_at"))
    )
    if is_canceled:
        fields["status"] = "canceled"
        cancel = invitee.get("cancellation") or {}
        if isinstance(cancel, dict):
            if cancel.get("reason"):
                fields["cancel_reason"] = str(cancel["reason"])[:500]
            if cancel.get("canceler_type") or cancel.get("canceled_by"):
                fields["canceled_by"] = str(
                    cancel.get("canceled_by") or cancel.get("canceler_type")
                )[:120]
    else:
        fields["status"] = "scheduled"

    if invitee.get("rescheduled"):
        fields["rescheduled"] = 1

    return fields


def invitee_uri_from_payload(payload: dict) -> Optional[str]:
    """Return the invitee URI (idempotency key) from a webhook envelope.

    Resolves the canonical ``uri`` across the shapes Calendly (and the
    occasional proxy/integration) actually send: the standard
    ``{"payload": {...}}`` envelope, a ``{"resource": {...}}`` wrapper, a
    bare invitee object, or a body that nests the invitee under ``invitee``.
    """
    if not isinstance(payload, dict):
        return None
    body = payload.get("payload")
    if not isinstance(body, dict):
        body = payload.get("resource") if isinstance(payload.get("resource"), dict) else payload
    nested = body.get("invitee") if isinstance(body, dict) else None
    uri = body.get("uri") if isinstance(body, dict) else None
    if not uri and isinstance(nested, dict):
        uri = nested.get("uri")
    # invitee.* and routing_form_submission.* payloads both carry their
    # canonical uri, which is the idempotency key. Prefer an invitee URI but
    # accept any uri so a routing-form submission is still keyed.
    return str(uri) if uri else None


# ---------------------------------------------------------------------------
# Enrichment (server-side Calendly API fetch). Fails soft.
# ---------------------------------------------------------------------------

def fetch_invitee(invitee_uri: str, *, session: Optional["requests.Session"] = None) -> dict:
    """Fetch full invitee details (incl. question answers) from Calendly.

    Returns {"ok": bool, "resource": dict|None, "status": str}. Never
    raises. ``status`` is one of: "ok", "no_token", "no_uri", "http_error",
    "network_error". The token is sent as a Bearer header and is never
    logged.
    """
    token = api_token()
    if not token:
        return {"ok": False, "resource": None, "status": "no_token"}
    if not invitee_uri:
        return {"ok": False, "resource": None, "status": "no_uri"}

    sess = session or requests
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        resp = sess.get(invitee_uri, headers=headers, timeout=_API_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - network/timeout/SSL all fail soft
        log.warning("Calendly enrichment network error for invitee fetch")
        return {"ok": False, "resource": None, "status": "network_error"}

    if resp.status_code != 200:
        log.warning("Calendly enrichment HTTP %s", resp.status_code)
        return {"ok": False, "resource": None, "status": "http_error"}
    try:
        data = resp.json() or {}
    except ValueError:
        return {"ok": False, "resource": None, "status": "http_error"}
    return {"ok": True, "resource": data.get("resource") or {}, "status": "ok"}


def _auth_headers() -> Optional[dict]:
    token = api_token()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get_json(url: str, *, params: Optional[dict] = None,
              session: Optional["requests.Session"] = None) -> dict:
    """GET a Calendly API URL with the bearer token. Fails soft.

    Returns {"ok": bool, "data": dict, "status": str}. ``status`` is one of
    "ok", "no_token", "http_error", "network_error". The token is never
    logged.
    """
    headers = _auth_headers()
    if not headers:
        return {"ok": False, "data": {}, "status": "no_token"}
    sess = session or requests
    try:
        resp = sess.get(url, headers=headers, params=params,
                        timeout=_API_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - network/timeout/SSL all fail soft
        log.warning("Calendly API network error")
        return {"ok": False, "data": {}, "status": "network_error"}
    if resp.status_code != 200:
        log.warning("Calendly API HTTP %s", resp.status_code)
        return {"ok": False, "data": {}, "status": "http_error"}
    try:
        return {"ok": True, "data": resp.json() or {}, "status": "ok"}
    except ValueError:
        return {"ok": False, "data": {}, "status": "http_error"}


def fetch_scheduled_event(event_uri: str, *,
                          session: Optional["requests.Session"] = None) -> dict:
    """Fetch a scheduled event (start/end/name/status) from Calendly.

    The invitee webhook/API record carries the question answers but not
    always the meeting start/end; the scheduled-event resource does. Returns
    {"ok": bool, "resource": dict|None, "status": str}. Never raises.
    """
    if not event_uri:
        return {"ok": False, "resource": None, "status": "no_uri"}
    r = _get_json(event_uri, session=session)
    if not r["ok"]:
        return {"ok": False, "resource": None, "status": r["status"]}
    return {"ok": True, "resource": r["data"].get("resource") or {}, "status": "ok"}


# ---------------------------------------------------------------------------
# Backfill / sync. Pull recent scheduled events + invitees from the Calendly
# API and upsert them as leads, so bookings made while the webhook was
# misconfigured still populate. Fails soft; never raises.
# ---------------------------------------------------------------------------

def fetch_current_organization(*, session: Optional["requests.Session"] = None) -> dict:
    """Resolve the token's organization URI via GET /users/me. Fails soft.

    Returns {"ok": bool, "organization": str|None, "status": str}.
    """
    r = _get_json(f"{CALENDLY_API_BASE}/users/me", session=session)
    if not r["ok"]:
        return {"ok": False, "organization": None, "status": r["status"]}
    resource = r["data"].get("resource") or {}
    org = resource.get("current_organization")
    if not org:
        return {"ok": False, "organization": None, "status": "no_org"}
    return {"ok": True, "organization": str(org), "status": "ok"}


def fetch_scheduled_events(organization: str, *, count: int = 20,
                           session: Optional["requests.Session"] = None) -> dict:
    """List recent scheduled events for an organization (newest first).

    Returns {"ok": bool, "events": list, "status": str}. Fails soft.
    """
    if not organization:
        return {"ok": False, "events": [], "status": "no_org"}
    params = {
        "organization": organization,
        "count": max(1, min(int(count or 20), 100)),
        "sort": "start_time:desc",
    }
    r = _get_json(f"{CALENDLY_API_BASE}/scheduled_events", params=params,
                  session=session)
    if not r["ok"]:
        return {"ok": False, "events": [], "status": r["status"]}
    events = r["data"].get("collection") or []
    return {"ok": True, "events": events if isinstance(events, list) else [],
            "status": "ok"}


def fetch_event_invitees(event_uri: str, *,
                         session: Optional["requests.Session"] = None) -> dict:
    """List invitees for a scheduled event. Returns {ok, invitees, status}."""
    if not event_uri:
        return {"ok": False, "invitees": [], "status": "no_uri"}
    r = _get_json(f"{event_uri.rstrip('/')}/invitees",
                  params={"count": 100}, session=session)
    if not r["ok"]:
        return {"ok": False, "invitees": [], "status": r["status"]}
    invitees = r["data"].get("collection") or []
    return {"ok": True, "invitees": invitees if isinstance(invitees, list) else [],
            "status": "ok"}


def lead_fields_from_event_and_invitee(event: dict, invitee: dict) -> dict:
    """Build lead column fields from a scheduled-event + invitee API pair.

    Reuses ``extract_lead_fields`` by nesting the event under the invitee as
    ``scheduled_event`` so meeting start/end/name and the question answers are
    all extracted by the same code path the webhook uses.
    """
    inv = dict(invitee or {})
    if event:
        inv["scheduled_event"] = event
    payload = {"event": "invitee.created", "payload": inv}
    return extract_lead_fields(payload)


def sync_recent_bookings(*, upsert, count: int = 20,
                         session: Optional["requests.Session"] = None) -> dict:
    """Backfill leads from recent Calendly scheduled events.

    ``upsert`` is a callable ``(invitee_uri, fields) -> lead_id`` (normally a
    thin wrapper over AppDB.upsert_calendly_lead) so this module stays free of
    any DB dependency and is easy to test with a fake.

    Fetches the token's organization, the most recent ``count`` scheduled
    events, and each event's invitees, then upserts one lead per invitee
    (keyed on the invitee URI, so it merges with any webhook-captured row
    instead of duplicating). Fails soft: a missing token or any API error
    yields a summary with ``ok=False`` and a ``status`` describing the gap;
    it never raises and never touches secrets in its return value.

    Returns a summary dict: {ok, status, events, invitees, upserted,
    errors}.
    """
    summary = {"ok": False, "status": "", "events": 0, "invitees": 0,
               "upserted": 0, "errors": 0}

    if not api_token():
        summary["status"] = "no_token"
        return summary

    org = fetch_current_organization(session=session)
    if not org["ok"]:
        summary["status"] = f"org_{org['status']}"
        return summary

    ev = fetch_scheduled_events(org["organization"], count=count, session=session)
    if not ev["ok"]:
        summary["status"] = f"events_{ev['status']}"
        return summary

    summary["events"] = len(ev["events"])
    for event in ev["events"]:
        if not isinstance(event, dict):
            continue
        event_uri = event.get("uri")
        if not event_uri:
            continue
        inv = fetch_event_invitees(event_uri, session=session)
        if not inv["ok"]:
            summary["errors"] += 1
            continue
        for invitee in inv["invitees"]:
            if not isinstance(invitee, dict):
                continue
            invitee_uri = invitee.get("uri")
            if not invitee_uri:
                continue
            summary["invitees"] += 1
            try:
                fields = lead_fields_from_event_and_invitee(event, invitee)
                fields["enrichment_status"] = "synced"
                upsert(str(invitee_uri), fields)
                summary["upserted"] += 1
            except Exception:  # noqa: BLE001 - one bad row must not abort sync
                log.exception("Calendly sync upsert failed for an invitee")
                summary["errors"] += 1

    summary["ok"] = True
    summary["status"] = "ok"
    return summary


# ---------------------------------------------------------------------------
# Email bodies
# ---------------------------------------------------------------------------

def _format_qa_block(questions_json: Optional[str]) -> list[str]:
    if not questions_json:
        return ["  (no custom questions were answered)"]
    try:
        qa = json.loads(questions_json)
    except (ValueError, TypeError):
        return ["  (questions could not be parsed)"]
    if not qa:
        return ["  (no custom questions were answered)"]
    lines = []
    for item in qa:
        q = (item.get("question") or "").strip() or "(question)"
        a = (item.get("answer") or "").strip() or "(no answer)"
        lines.append(f"  - {q}: {a}")
    return lines


def internal_email_bodies(*, app_name: str, lead: dict,
                          support_email: Optional[str] = None) -> tuple[str, str]:
    """Build (subject, body_text) for the internal new-discovery-call alert.

    Includes the prospect name/email, firm, Clio rep, meeting time, and the
    full question/answer set so the team has the form details *before* the
    call. Never includes any secret. ``support_email`` is the inbox prospects
    were told to use; it's echoed in the footer for the team's reference.
    """
    name = lead.get("name") or "(name not provided)"
    email = lead.get("email") or "(email not provided)"
    status = lead.get("status") or "scheduled"
    verb = "canceled" if status == "canceled" else "scheduled"
    subject = f"[{app_name}] Discovery call {verb}: {name}"

    lines = [
        f"A discovery call was {verb} on Calendly.",
        "",
        f"Name:        {name}",
        f"Email:       {email}",
    ]
    if lead.get("phone"):
        lines.append(f"Phone:       {lead['phone']}")
    if lead.get("firm_name"):
        lines.append(f"Firm:        {lead['firm_name']}")
    if lead.get("clio_rep_name") or lead.get("clio_rep_email"):
        rep = " ".join(
            x for x in (lead.get("clio_rep_name"), lead.get("clio_rep_email")) if x
        )
        lines.append(f"Clio rep:    {rep}")
    if lead.get("event_name"):
        lines.append(f"Event:       {lead['event_name']}")
    if lead.get("meeting_start"):
        when = lead["meeting_start"]
        if lead.get("timezone"):
            when += f" ({lead['timezone']})"
        lines.append(f"Meeting:     {when}")
    lines.append(f"Status:      {status}")
    if status == "canceled" and lead.get("cancel_reason"):
        lines.append(f"Cancel note: {lead['cancel_reason']}")
    lines.append("")
    lines.append("Form answers:")
    lines.extend(_format_qa_block(lead.get("questions_json")))
    if lead.get("event_uri"):
        lines.append("")
        lines.append(f"Calendly event: {lead['event_uri']}")
    lines.append("")
    lines.append(f"Prospect contact inbox: {contact_email(support_email)}")
    return subject, "\n".join(lines)


def customer_email_bodies(*, app_name: str, lead: dict,
                          support_email: Optional[str] = None) -> tuple[str, str]:
    """Build (subject, body_text) for an optional Cutovr next-steps email.

    Deliberately does NOT duplicate Calendly's own confirmation: it simply
    tells the prospect we received their details and will review them before
    the call. Safe to skip entirely when SMTP is unavailable.

    The contact address is resolved via ``contact_email`` so the prospect
    always gets a real mailbox (the Cutovr support address) even on a deploy
    that hasn't overridden the placeholder SUPPORT_EMAIL.
    """
    first = (lead.get("name") or "there").split(" ")[0] or "there"
    contact = contact_email(support_email)
    subject = f"We received your {app_name} discovery-call details"
    lines = [
        f"Hi {first},",
        "",
        f"Thanks for booking a discovery call with {app_name}. We've received "
        "the details you shared and will review them before we meet, so we can "
        "make the most of your time.",
        "",
        "You'll have already received Calendly's confirmation with the meeting "
        "time and a calendar invite. There's nothing else you need to do right "
        "now.",
    ]
    if lead.get("meeting_start"):
        when = lead["meeting_start"]
        if lead.get("timezone"):
            when += f" ({lead['timezone']})"
        lines += ["", f"Your call is scheduled for: {when}"]
    lines += [
        "",
        f"If anything changes, reply to this email or reach us at {contact}.",
    ]
    lines += ["", f"— The {app_name} team"]
    return subject, "\n".join(lines)
