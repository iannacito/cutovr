"""Production go-live readiness checks.

Builds a structured list of named checks describing whether the deploy is
configured to go live for Intuit production review. Every check returns
booleans and short human messages — never the actual secret values — so the
result can be safely rendered to a logged-in admin and (in trimmed form) to
the public ``/healthz`` probe.

Notes:
- Branding emails are read live from ``branding`` so changes to the env take
  effect on the next request without needing a restart of this module.
- ``request_host`` (optional) lets the page report which hostname the app is
  currently being served on. Combined with ``PUBLIC_APP_URL`` this is how an
  operator confirms the custom domain (e.g. www.pclawmigrate.com) is wired
  up end to end.
"""

from __future__ import annotations

import os
from typing import Iterable
from urllib.parse import urlparse

import branding


_PROD_ENVS = {"production", "prod"}
_NON_PROD_ENVS = {"local", "dev", "development", "test", "staging"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _check(key: str, label: str, ok: bool, message: str, *, severity: str = "required") -> dict:
    return {
        "key": key,
        "label": label,
        "ok": bool(ok),
        "message": message,
        "severity": severity,
    }


def _qbo_redirect_uri_ok(uri: str, app_env: str) -> tuple[bool, str]:
    if not uri:
        return False, "Not set."
    parsed = urlparse(uri)
    if parsed.scheme not in ("http", "https"):
        return False, "Must be a full URL with http:// or https://."
    if app_env in _PROD_ENVS:
        if parsed.scheme != "https":
            return False, "Must use https:// in production."
        if parsed.hostname in (None, "localhost", "127.0.0.1"):
            return False, "Must point at the live public hostname, not localhost."
    return True, f"Configured: {parsed.scheme}://{parsed.hostname or ''}{parsed.path or ''}"


def _custom_domain_status(public_app_url: str, request_host: str | None) -> tuple[bool, str]:
    """Heuristic for whether the deploy is on a real custom domain.

    Either ``PUBLIC_APP_URL`` is set to a non-Render hostname, OR the current
    request is being served from a non-Render hostname. We treat the default
    Render hostname as "not yet a custom domain" so the operator notices.
    """
    candidate_host = ""
    if public_app_url:
        try:
            candidate_host = urlparse(public_app_url).hostname or ""
        except Exception:  # noqa: BLE001
            candidate_host = ""
    if not candidate_host and request_host:
        candidate_host = request_host.split(":")[0]

    if not candidate_host:
        return False, "PUBLIC_APP_URL not set and request hostname unknown."

    if candidate_host.endswith(".onrender.com"):
        return False, f"Currently serving on default Render hostname ({candidate_host})."

    if candidate_host in ("localhost", "127.0.0.1"):
        return False, f"Currently on local development host ({candidate_host})."

    return True, f"Custom domain detected: {candidate_host}"


def collect_checks(*, request_host: str | None = None) -> list[dict]:
    """Return the list of readiness checks. Pure function: no I/O besides env reads."""
    app_env = (os.environ.get("APP_ENV") or "local").lower()
    secret_key = os.environ.get("SECRET_KEY") or os.environ.get("APP_SECRET") or ""
    encryption_key = os.environ.get("ENCRYPTION_KEY") or ""
    qbo_client_id = os.environ.get("QBO_CLIENT_ID") or ""
    qbo_client_secret = os.environ.get("QBO_CLIENT_SECRET") or ""
    qbo_redirect_uri = os.environ.get("QBO_REDIRECT_URI") or ""
    qbo_environment = os.environ.get("QBO_ENVIRONMENT") or ""
    qbo_real_import = _truthy(os.environ.get("QBO_REAL_IMPORT"))
    public_app_url = (os.environ.get("PUBLIC_APP_URL") or "").strip()

    checks: list[dict] = []

    # 1. APP_ENV
    if app_env in _PROD_ENVS:
        checks.append(_check("app_env_production", "APP_ENV=production",
                             True, f"APP_ENV={app_env}."))
    elif app_env in _NON_PROD_ENVS:
        checks.append(_check("app_env_production", "APP_ENV=production",
                             False,
                             f"APP_ENV is currently '{app_env}'. Set APP_ENV=production on Render to enable production hardening."))
    else:
        checks.append(_check("app_env_production", "APP_ENV=production",
                             False, f"APP_ENV='{app_env}' is not a recognized value."))

    # 2. SECRET_KEY
    if not secret_key:
        msg = "Not set. Generate with: python -c \"import secrets; print(secrets.token_hex(32))\"."
        checks.append(_check("secret_key", "SECRET_KEY configured", False, msg))
    elif len(secret_key) < 32:
        checks.append(_check("secret_key", "SECRET_KEY configured", False,
                             "Set but shorter than 32 characters; rotate to a longer random value."))
    else:
        checks.append(_check("secret_key", "SECRET_KEY configured", True,
                             "Set (length looks reasonable; value not displayed)."))

    # 3. ENCRYPTION_KEY (Fernet)
    if not encryption_key:
        msg = ("Not set. Generate with: python -c \"from cryptography.fernet "
               "import Fernet; print(Fernet.generate_key().decode())\".")
        checks.append(_check("encryption_key", "ENCRYPTION_KEY configured", False, msg))
    else:
        try:
            from cryptography.fernet import Fernet
            Fernet(encryption_key.encode())
            checks.append(_check("encryption_key", "ENCRYPTION_KEY configured", True,
                                 "Set and is a valid Fernet key."))
        except Exception:  # noqa: BLE001
            checks.append(_check("encryption_key", "ENCRYPTION_KEY configured", False,
                                 "Set but is not a valid Fernet key (must be 32 url-safe base64-encoded bytes)."))

    # 4. QBO_CLIENT_ID
    placeholder_client_id = qbo_client_id in ("", "your-client-id-here")
    checks.append(_check("qbo_client_id", "QBO_CLIENT_ID configured",
                         not placeholder_client_id,
                         "Not set." if placeholder_client_id else "Set (value not displayed)."))

    # 5. QBO_CLIENT_SECRET
    placeholder_client_secret = qbo_client_secret in ("", "your-client-secret-here")
    checks.append(_check("qbo_client_secret", "QBO_CLIENT_SECRET configured",
                         not placeholder_client_secret,
                         "Not set." if placeholder_client_secret else "Set (value not displayed)."))

    # 6. QBO_REDIRECT_URI + HTTPS
    redirect_ok, redirect_msg = _qbo_redirect_uri_ok(qbo_redirect_uri, app_env)
    checks.append(_check("qbo_redirect_uri", "QBO_REDIRECT_URI uses HTTPS in production",
                         redirect_ok, redirect_msg))

    # 7. QBO_REAL_IMPORT
    checks.append(_check("qbo_real_import", "QBO_REAL_IMPORT enabled",
                         qbo_real_import,
                         "Real-import mode is on; uploads will post journal entries to QuickBooks." if qbo_real_import
                         else "Real-import is off; the app is in demo mode. Set QBO_REAL_IMPORT=1 to go live.",
                         severity="recommended"))

    # 8. SUPPORT_EMAIL
    support_ok = not branding.is_placeholder_email(branding.SUPPORT_EMAIL)
    checks.append(_check("support_email", "SUPPORT_EMAIL configured",
                         support_ok,
                         f"Configured as {branding.SUPPORT_EMAIL}." if support_ok
                         else "Still using the placeholder address; set SUPPORT_EMAIL to a real, monitored mailbox."))

    # 9. SECURITY_EMAIL
    security_ok = not branding.is_placeholder_email(branding.SECURITY_EMAIL)
    checks.append(_check("security_email", "SECURITY_EMAIL configured",
                         security_ok,
                         f"Configured as {branding.SECURITY_EMAIL}." if security_ok
                         else "Still using the placeholder address; set SECURITY_EMAIL for vulnerability reports."))

    # 10. PRIVACY_CONTACT_EMAIL
    privacy_ok = not branding.is_placeholder_email(branding.PRIVACY_CONTACT_EMAIL)
    checks.append(_check("privacy_contact_email", "PRIVACY_CONTACT_EMAIL configured",
                         privacy_ok,
                         f"Configured as {branding.PRIVACY_CONTACT_EMAIL}." if privacy_ok
                         else "Still using the placeholder address; set PRIVACY_CONTACT_EMAIL for privacy inquiries.",
                         severity="recommended"))

    # 11. Custom domain
    domain_ok, domain_msg = _custom_domain_status(public_app_url, request_host)
    checks.append(_check("custom_domain", "Custom domain in use",
                         domain_ok, domain_msg, severity="recommended"))

    # 12. Health endpoint OK (always true if this code is running, but we
    # surface it so an operator sees the full picture in one card grid).
    checks.append(_check("health_endpoint", "Health endpoint responding",
                         True, "/healthz is reachable (you are reading the readiness page it powers)."))

    return checks


def summary_booleans(checks: Iterable[dict]) -> dict:
    """Flatten checks into a {key: bool} map for /healthz. No messages, no values."""
    return {c["key"]: bool(c["ok"]) for c in checks}


def overall_status(checks: Iterable[dict]) -> dict:
    """Compute a readiness summary: required passing, optional passing, total."""
    checks = list(checks)
    required = [c for c in checks if c["severity"] == "required"]
    recommended = [c for c in checks if c["severity"] == "recommended"]
    required_passed = sum(1 for c in required if c["ok"])
    recommended_passed = sum(1 for c in recommended if c["ok"])
    return {
        "required_total": len(required),
        "required_passed": required_passed,
        "recommended_total": len(recommended),
        "recommended_passed": recommended_passed,
        "all_required_ok": required_passed == len(required),
    }
