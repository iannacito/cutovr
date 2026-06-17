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


APP_NAME = _env("APP_NAME", "Cutovr")
COMPANY_NAME = _env("COMPANY_NAME", "Cutovr")
SUPPORT_EMAIL = _env("SUPPORT_EMAIL", "support@your-domain.example")
SECURITY_EMAIL = _env("SECURITY_EMAIL", "security@your-domain.example")
PRIVACY_CONTACT_EMAIL = _env("PRIVACY_CONTACT_EMAIL", SUPPORT_EMAIL)

# Canonical public URL of the production app. Templates that need to point a
# user at production (e.g. the demo-banner "go to the real site" link, terms /
# privacy) read this so we never hard-code a stale *.onrender.com host. Prefer
# the deploy's PUBLIC_APP_URL; fall back to the marketing domain.
PUBLIC_APP_URL = _env("PUBLIC_APP_URL", "https://www.cutovr.com").rstrip("/")

# Calendly booking link for the Cutovr discovery call. This is the URL the
# in-app booking page (/book-discovery-call) embeds as a Calendly inline
# widget. The primary public CTA across the app routes to that in-app page,
# not directly to this external URL.
# Defaults to the real Cutovr Calendly link so the booking page works without
# extra deploy config; a deploy MAY override it via the DISCOVERY_CALL_URL env.
DISCOVERY_CALL_URL = _env(
    "DISCOVERY_CALL_URL",
    "https://calendly.com/cutovr-discovery-call/cutovr-discovery-call",
)


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
        "public_app_url": PUBLIC_APP_URL,
        "discovery_call_url": DISCOVERY_CALL_URL,
    }
