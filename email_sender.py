"""SMTP email delivery for transactional emails.

Configuration is read from environment variables. We accept the
SMTP_*-style names that have always been used internally **and** the
Flask-Mail / MAIL_*-style names plus a couple of vendor variants so
operators don't have to relearn naming conventions when wiring up
Render + Zoho:

    Host        SMTP_HOST or MAIL_SERVER
    Port        SMTP_PORT or MAIL_PORT  (default 587)
    Username    SMTP_USER, SMTP_USERNAME, or MAIL_USERNAME
    Password    SMTP_PASSWORD or MAIL_PASSWORD
    From addr   SMTP_FROM, SMTP_FROM_EMAIL, or MAIL_DEFAULT_SENDER
    From name   SMTP_FROM_NAME or MAIL_FROM_NAME  (optional)

TLS handling:
    Port 465 -> implicit TLS (SMTP_SSL).
    Any other port -> STARTTLS by default. Set SMTP_USE_TLS=0 to
    opt out (e.g. for a local devmail server). MAIL_USE_TLS is also
    honored when SMTP_USE_TLS isn't set.

If any of the required vars are missing, `is_smtp_configured()` returns
False and `send_email()` returns False without raising. Callers are
expected to fall back to a generic UI message and (in production) record
a safe audit warning.

This module never logs or surfaces the message body or token URL.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional


log = logging.getLogger("email_sender")


# Ordered fallback aliases for each setting. The first non-empty value
# wins. Keeping these grouped here (rather than scattered through
# is_smtp_configured / send_email) means tests and operators have a
# single place to read what names are recognized.
_HOST_VARS = ("SMTP_HOST", "MAIL_SERVER")
_PORT_VARS = ("SMTP_PORT", "MAIL_PORT")
_USER_VARS = ("SMTP_USER", "SMTP_USERNAME", "MAIL_USERNAME")
_PASSWORD_VARS = ("SMTP_PASSWORD", "MAIL_PASSWORD")
_FROM_VARS = ("SMTP_FROM", "SMTP_FROM_EMAIL", "MAIL_DEFAULT_SENDER")
_FROM_NAME_VARS = ("SMTP_FROM_NAME", "MAIL_FROM_NAME")
_TLS_VARS = ("SMTP_USE_TLS", "MAIL_USE_TLS")

# Required for SMTP to be considered configured: host, user, password,
# from address. Port has a sane default. From-name is optional.
_REQUIRED_GROUPS = (_HOST_VARS, _USER_VARS, _PASSWORD_VARS, _FROM_VARS)


def _resolve(names) -> Optional[str]:
    """Return the first non-empty env value from the given alias list."""
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    return None


def _resolve_port() -> int:
    raw = _resolve(_PORT_VARS)
    if not raw:
        return 587
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 587


def _resolve_use_tls(port: int) -> bool:
    """STARTTLS by default for non-465 ports, unless explicitly disabled."""
    raw = _resolve(_TLS_VARS)
    if raw is None:
        return port != 465
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def is_smtp_configured() -> bool:
    return all(_resolve(group) for group in _REQUIRED_GROUPS)


def smtp_status() -> dict:
    """Return a redacted status dict suitable for audit logs.

    Never includes the SMTP password. Host/port/user are non-secret
    operator config and useful for diagnosing 'why didn't the email
    send'.
    """
    return {
        "configured": is_smtp_configured(),
        "host": _resolve(_HOST_VARS),
        "port": _resolve(_PORT_VARS) or (587 if is_smtp_configured() else None),
        "user_set": bool(_resolve(_USER_VARS)),
        "from_set": bool(_resolve(_FROM_VARS)),
    }


def _from_header() -> str:
    """Build the From header, optionally with a display name."""
    addr = _resolve(_FROM_VARS) or ""
    name = _resolve(_FROM_NAME_VARS)
    if name:
        return formataddr((name, addr))
    return addr


def send_email(
    *,
    to: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
) -> bool:
    """Send a single transactional email via SMTP.

    Returns True on success, False on configuration or transport failure.
    Never raises — the caller treats failure the same as 'not configured'
    and shows the user the generic message.
    """
    if not is_smtp_configured():
        return False

    host = _resolve(_HOST_VARS) or ""
    port = _resolve_port()
    user = _resolve(_USER_VARS) or ""
    password = _resolve(_PASSWORD_VARS) or ""
    use_tls = _resolve_use_tls(port)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _from_header()
    msg["To"] = to
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo()
                if use_tls:
                    s.starttls(context=ctx)
                    s.ehlo()
                s.login(user, password)
                s.send_message(msg)
        return True
    except Exception:
        # Don't leak credentials or report body into logs. The remote
        # server response in smtplib's exception chain is fine — it's
        # the only useful thing for an operator triaging "why didn't
        # the email send".
        log.exception("SMTP send failed for host=%s port=%s", host, port)
        return False


def send_quote_request(form, *, reference: str) -> bool:
    """Forward a Complete-tier quote-request form to the support inbox.

    Returns True if SMTP is configured AND the message was accepted by
    the relay; False otherwise (configuration missing or transport
    error). Caller is responsible for confirming receipt to the user
    without lying about delivery status.
    """
    support_addr = os.environ.get("SUPPORT_EMAIL") or _resolve(_FROM_VARS)
    if not support_addr:
        return False
    if not is_smtp_configured():
        return False

    subject = f"[PCLaw Migrate] Quote request {reference}"
    lines = [
        f"Reference: {reference}",
        f"Firm name: {form.get('firm_name', '')}",
        f"Work email: {form.get('email', '')}",
        f"Years of history: {form.get('years_history', '')}",
        f"Approximate volume: {form.get('volume', '')}",
        "",
        "Notes / timeline:",
        form.get("notes", "") or "(none)",
    ]
    body_text = "\n".join(lines)
    return send_email(
        to=support_addr,
        subject=subject,
        body_text=body_text,
    )
