"""Configurable app/company branding and contact settings.

These values are read from environment variables at import time and exposed
to Flask templates via a context processor in app.py.

Defaults are deliberately benign so the local/dev workflow keeps working
without any extra setup. Production deploys SHOULD override at minimum
SUPPORT_EMAIL and SECURITY_EMAIL with real, monitored mailboxes; we surface
this as a warning in PRODUCTION_READINESS.md rather than failing startup
to avoid breaking existing Render deploys.
"""

from __future__ import annotations

import os


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


APP_NAME = _env("APP_NAME", "Cutover")
COMPANY_NAME = _env("COMPANY_NAME", "Cutover")
SUPPORT_EMAIL = _env("SUPPORT_EMAIL", "support@your-domain.example")
SECURITY_EMAIL = _env("SECURITY_EMAIL", "security@your-domain.example")
PRIVACY_CONTACT_EMAIL = _env("PRIVACY_CONTACT_EMAIL", SUPPORT_EMAIL)


def is_placeholder_email(addr: str) -> bool:
    """Return True if the address is a deploy-default placeholder.

    Used by /healthz so an operator can spot from a single probe whether the
    Render env still has the example values.
    """
    return addr.endswith("@your-domain.example")


def context() -> dict:
    """Dict of branding values for Flask context_processor injection."""
    return {
        "app_name": APP_NAME,
        "company_name": COMPANY_NAME,
        "support_email": SUPPORT_EMAIL,
        "security_email": SECURITY_EMAIL,
        "privacy_contact_email": PRIVACY_CONTACT_EMAIL,
    }
