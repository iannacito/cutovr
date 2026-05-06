"""Parse QuickBooks Online error payloads into customer-friendly hints.

QBO returns errors in two shapes:

  1. JSON envelope
     {"Fault": {"Error": [{"Message": "...", "Detail": "...", "code": "..."}]}}

  2. Plain text / HTML when the load balancer or proxy rejects upstream.

For both we want a beginner-friendly summary + a likely-next-action so a
non-engineer can recover (usually: open the account-mapping page, or
reconnect QBO). The full raw error stays available in the UI as a
collapsible "technical detail" block — we never hide it.
"""

from __future__ import annotations

import json
import re
from typing import Optional


# Token-substring → (friendly summary, next action) mapping. Matched
# case-insensitively; first match wins. Patterns are intentionally narrow
# so we never paste an irrelevant suggestion.
_HINTS: list[tuple[str, str, str]] = [
    (
        "invalid grant",
        "QuickBooks no longer accepts the saved authorization for this job.",
        "Click Disconnect QuickBooks, then Connect to QuickBooks again to issue a fresh authorization.",
    ),
    (
        "token expired",
        "The QuickBooks session for this job has expired.",
        "Reconnect QuickBooks from the job page.",
    ),
    (
        "needs to be assigned to a customer",
        "QuickBooks needs a Customer attached to an Accounts Receivable line.",
        "Open Map accounts and confirm the source CSV row has a customer name; the importer will create or match it automatically.",
    ),
    (
        "needs to be assigned to a vendor",
        "QuickBooks needs a Vendor attached to an Accounts Payable line.",
        "Open Map accounts and confirm the source CSV row has a vendor name; the importer will create or match it automatically.",
    ),
    (
        "account is inactive",
        "One of the QuickBooks accounts you mapped to is inactive.",
        "Open Map accounts and pick an active QBO account, or re-activate the account in QuickBooks → Chart of Accounts.",
    ),
    (
        "account not found",
        "QuickBooks could not find an account that the import referenced.",
        "Open Map accounts to point this PCLaw account at a real QBO account.",
    ),
    (
        "duplicate document number",
        "QuickBooks rejected a journal entry because its document number already exists.",
        "If this was a re-run of a prior import, use the Reverse this import flow first; otherwise contact support.",
    ),
    (
        "throttle",
        "QuickBooks throttled the request (too many calls).",
        "Wait a minute and click Import to QuickBooks again.",
    ),
    (
        "service unavailable",
        "QuickBooks Online is temporarily unavailable.",
        "Try again in a few minutes; nothing has been double-posted.",
    ),
    (
        "401",
        "QuickBooks rejected the saved access token.",
        "Disconnect QuickBooks and reconnect to refresh the authorization.",
    ),
    (
        "403",
        "QuickBooks denied access to this company for the saved connection.",
        "Disconnect, then reconnect — and pick the same QBO company on Intuit's screen.",
    ),
]


def _extract_qbo_messages(raw: str) -> list[str]:
    """Pull human-readable messages out of a QBO JSON error envelope.

    Falls back to the raw string when JSON parsing fails or the shape is
    unexpected (e.g. an HTML 502 page from a proxy).
    """
    if not raw:
        return []
    raw = raw.strip()
    if not raw.startswith("{"):
        return [raw]
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return [raw]
    fault = obj.get("Fault") if isinstance(obj, dict) else None
    if not isinstance(fault, dict):
        return [raw]
    errors = fault.get("Error") or []
    out: list[str] = []
    for err in errors:
        if not isinstance(err, dict):
            continue
        msg = (err.get("Message") or "").strip()
        detail = (err.get("Detail") or "").strip()
        code = err.get("code")
        parts = []
        if msg:
            parts.append(msg)
        if detail and detail != msg:
            parts.append(detail)
        line = " — ".join(parts) if parts else ""
        if code:
            line = f"[{code}] {line}".strip()
        if line:
            out.append(line)
    return out or [raw]


# Strip the QBO-style "QBO returned 400: { ... }" outer envelope our QBOClient
# wraps the body in. We want the customer to see the inner message, not the
# transport prefix.
_OUTER_RE = re.compile(r"^QBO returned (\d{3})(?: [^:]*)?:\s*", re.IGNORECASE)


def parse(raw_error: str) -> dict:
    """Return {summary, action, technical_detail, status_code} for a raw QBO error.

    summary  — short human sentence ("QuickBooks rejected the import …").
    action   — concrete next step the user can take, or None.
    technical_detail — the full original message (for the collapsible).
    status_code — int HTTP code if we could extract one, else None.
    """
    raw = (raw_error or "").strip()
    technical_detail = raw
    status_code = None

    m = _OUTER_RE.match(raw)
    if m:
        try:
            status_code = int(m.group(1))
        except ValueError:
            status_code = None
        body = raw[m.end():]
    else:
        body = raw

    messages = _extract_qbo_messages(body)
    summary = messages[0] if messages else "QuickBooks rejected the import."
    if len(summary) > 240:
        summary = summary[:237] + "..."

    haystack = " ".join(messages).lower() + " " + body.lower()
    action: Optional[str] = None
    for needle, friendly, next_action in _HINTS:
        if needle in haystack:
            # Prefer the friendly summary the matcher provides over the raw
            # QBO line (which is often a stack-trace fragment).
            summary = friendly
            action = next_action
            break

    return {
        "summary": summary,
        "action": action,
        "technical_detail": technical_detail,
        "status_code": status_code,
    }
