"""SMTP email delivery for transactional emails (currently password reset).

Configuration is read from environment variables:

    SMTP_HOST       - mail server host (e.g. "smtp.zoho.com")
    SMTP_PORT       - 587 (STARTTLS, default) or 465 (implicit TLS)
    SMTP_USER       - SMTP auth username (often the same as SMTP_FROM)
    SMTP_PASSWORD   - SMTP auth password / app password
    SMTP_FROM       - From: address (must be allowed by the provider)

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
from typing import Optional


log = logging.getLogger("email_sender")


REQUIRED_VARS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")


def is_smtp_configured() -> bool:
    return all(os.environ.get(v) for v in REQUIRED_VARS)


def smtp_status() -> dict:
    """Return a redacted status dict suitable for audit logs.

    Never includes SMTP_PASSWORD. Host/port/user are non-secret operator
    config and useful for diagnosing 'why didn't the email send'.
    """
    return {
        "configured": is_smtp_configured(),
        "host": os.environ.get("SMTP_HOST") or None,
        "port": os.environ.get("SMTP_PORT") or None,
        "user_set": bool(os.environ.get("SMTP_USER")),
        "from_set": bool(os.environ.get("SMTP_FROM")),
    }


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

    host = os.environ["SMTP_HOST"]
    port_raw = os.environ["SMTP_PORT"]
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ["SMTP_FROM"]

    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        log.warning("SMTP_PORT is not an integer; cannot send email")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
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
                s.starttls(context=ctx)
                s.ehlo()
                s.login(user, password)
                s.send_message(msg)
        return True
    except Exception:
        # Don't leak token URL or recipient address into logs beyond the
        # short host/port context. Stack traces from smtplib include the
        # remote server response, which is fine.
        log.exception("SMTP send failed for host=%s port=%s", host, port_raw)
        return False
