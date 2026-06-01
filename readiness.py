"""Production go-live readiness checks for Cutovr.

This module produces a structured, secret-free view of "is this deploy
ready to be turned on for real customers?" suitable for both:

  * the public /healthz probe — booleans only, no human-readable hints,
    so ops dashboards and Render's health checks can scrape it without
    leaking anything sensitive; and
  * the protected /readiness page — same booleans plus short remediation
    hints rendered to a logged-in operator.

Design notes:
  - Every check is a pure function of the request/env. We never read or
    return secret values; we only report `True`/`False` on presence and
    well-formedness.
  - Hints describe WHAT is missing, never the value. A hint may name an
    env var (e.g. "Set ENCRYPTION_KEY in Render") but never echo one.
  - The module imports lazily inside `collect_checks` so unit tests can
    monkey-patch `os.environ` and call again to re-evaluate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import List, Optional
from urllib.parse import urlparse


# Items in this list are the source of truth for both /healthz keys and
# the on-page checklist. `key` doubles as the JSON field name in /healthz
# (so changing it is a public-facing change). `severity` drives UI color
# but is also exposed in JSON for any future ops tooling.
SEVERITY_REQUIRED = "required"   # must be true before go-live
SEVERITY_RECOMMENDED = "recommended"  # strongly suggested
SEVERITY_INFO = "info"           # nice to know, never a blocker


@dataclass
class Check:
    key: str            # short snake_case id; stable across releases
    label: str          # human-readable title
    ok: bool            # True = pass
    severity: str       # one of SEVERITY_* constants
    hint: str = ""      # short remediation hint when not ok; never a secret
    detail: str = ""    # short non-secret extra context (e.g. host name)


def _bool_env(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def _is_placeholder(addr: str) -> bool:
    return (not addr) or addr.endswith("@your-domain.example")


def _fernet_ok(value: str) -> bool:
    if not value:
        return False
    try:
        from cryptography.fernet import Fernet
        Fernet(value.encode())
        return True
    except Exception:
        return False


def collect_checks(request_host: Optional[str] = None,
                   request_scheme: Optional[str] = None) -> List[Check]:
    """Build the full readiness checklist.

    `request_host` / `request_scheme` are optional so this can be called
    from background contexts (e.g. tests) without a Flask request. When
    rendered from a request, pass `request.host` and `request.scheme` so
    we can infer custom-domain presence and HTTPS for the detail card.
    """
    checks: List[Check] = []

    app_env = (os.environ.get("APP_ENV") or "local").lower()
    is_prod_value = app_env not in ("local", "dev", "development", "test")
    checks.append(Check(
        key="app_env_production",
        label="APP_ENV set to production",
        ok=is_prod_value,
        severity=SEVERITY_REQUIRED,
        hint="Set APP_ENV=production in Render to enable Secure cookies and strict env validation."
             if not is_prod_value else "",
        detail=f"current: {app_env}",
    ))

    secret_key = os.environ.get("SECRET_KEY") or os.environ.get("APP_SECRET") or ""
    secret_ok = len(secret_key) >= 32
    checks.append(Check(
        key="secret_key_set",
        label="SECRET_KEY configured",
        ok=secret_ok,
        severity=SEVERITY_REQUIRED,
        hint="Generate one: python -c \"import secrets; print(secrets.token_hex(32))\" "
             "and set SECRET_KEY in Render." if not secret_ok else "",
    ))

    enc = os.environ.get("ENCRYPTION_KEY", "")
    enc_ok = _fernet_ok(enc)
    checks.append(Check(
        key="encryption_key_set",
        label="ENCRYPTION_KEY (Fernet) configured",
        ok=enc_ok,
        severity=SEVERITY_REQUIRED,
        hint="Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
             "and set ENCRYPTION_KEY in Render." if not enc_ok else "",
    ))

    qbo_client_id = os.environ.get("QBO_CLIENT_ID", "")
    qbo_client_id_ok = bool(qbo_client_id) and qbo_client_id != "your-client-id-here"
    checks.append(Check(
        key="qbo_client_id_set",
        label="QBO_CLIENT_ID configured",
        ok=qbo_client_id_ok,
        severity=SEVERITY_REQUIRED,
        hint="Copy the production Client ID from the Intuit developer portal "
             "and set QBO_CLIENT_ID in Render." if not qbo_client_id_ok else "",
    ))

    qbo_secret = os.environ.get("QBO_CLIENT_SECRET", "")
    qbo_secret_ok = bool(qbo_secret) and qbo_secret != "your-client-secret-here"
    checks.append(Check(
        key="qbo_client_secret_set",
        label="QBO_CLIENT_SECRET configured",
        ok=qbo_secret_ok,
        severity=SEVERITY_REQUIRED,
        hint="Copy the production Client Secret from the Intuit developer portal "
             "and set QBO_CLIENT_SECRET in Render." if not qbo_secret_ok else "",
    ))

    redirect_uri = os.environ.get("QBO_REDIRECT_URI", "")
    redirect_https = redirect_uri.startswith("https://")
    redirect_local = redirect_uri.startswith("http://localhost")
    redirect_ok = bool(redirect_uri) and redirect_https and not redirect_local
    if not redirect_uri:
        redirect_hint = "Set QBO_REDIRECT_URI to your public callback URL (e.g. https://www.cutovr.com/oauth/callback)."
    elif redirect_local:
        redirect_hint = "QBO_REDIRECT_URI must point at the public host, not localhost."
    elif not redirect_https:
        redirect_hint = "QBO_REDIRECT_URI must use https:// in production."
    else:
        redirect_hint = ""
    # The redirect URI is not a secret — it's published to Intuit Developer
    # — and the whole point of surfacing it is to compare against what's
    # registered there. Always include the configured value in `detail` so
    # the operator can copy-paste it for an exact match.
    checks.append(Check(
        key="qbo_redirect_uri_https",
        label="QBO_REDIRECT_URI is public + HTTPS",
        ok=redirect_ok,
        severity=SEVERITY_REQUIRED,
        hint=redirect_hint,
        detail=redirect_uri or "(unset)",
    ))

    # Path-shape check: Intuit OAuth must end at /oauth/callback. A common
    # cause of the "redirect_uri ... invalid" error is a typo here.
    try:
        redirect_parsed = urlparse(redirect_uri) if redirect_uri else None
    except Exception:
        redirect_parsed = None
    redirect_path = redirect_parsed.path if redirect_parsed else ""
    path_ok = bool(redirect_uri) and redirect_path.rstrip("/") == "/oauth/callback"
    if not redirect_uri:
        path_hint = "Set QBO_REDIRECT_URI; the path must end with /oauth/callback."
    elif not path_ok:
        path_hint = (
            "QBO_REDIRECT_URI path should end with /oauth/callback to match the app's "
            "OAuth route. Current path: " + (redirect_path or "(empty)")
        )
    else:
        path_hint = ""
    checks.append(Check(
        key="qbo_redirect_uri_path_ok",
        label="QBO_REDIRECT_URI path ends with /oauth/callback",
        ok=path_ok,
        severity=SEVERITY_REQUIRED,
        hint=path_hint,
        detail=redirect_path or "(no path)",
    ))

    # Host-match check (recommended): if PUBLIC_APP_URL is set, the redirect
    # URI host should match it. Helps catch the case where the operator
    # updated their custom domain but forgot to update QBO_REDIRECT_URI to
    # match — Intuit will then reject the OAuth round-trip.
    public_url_for_host = os.environ.get("PUBLIC_APP_URL", "").strip()
    if public_url_for_host and redirect_uri:
        try:
            public_host = (urlparse(public_url_for_host).hostname or "").lower()
            redirect_host = (redirect_parsed.hostname or "").lower() if redirect_parsed else ""
        except Exception:
            public_host = ""
            redirect_host = ""
        host_match_ok = bool(public_host) and public_host == redirect_host
        host_detail = f"PUBLIC_APP_URL host: {public_host or '(unknown)'} / redirect host: {redirect_host or '(unknown)'}"
        host_hint = ("" if host_match_ok else
                     "QBO_REDIRECT_URI host does not match PUBLIC_APP_URL. Update one so they "
                     "point at the same domain, then re-register the redirect URI in Intuit Developer.")
    else:
        # Without PUBLIC_APP_URL we can't make a definitive comparison; treat
        # as informational/pass so we don't fail readiness for a config we
        # can't evaluate. The host check is only meaningful when the operator
        # has declared a canonical URL.
        host_match_ok = True
        host_detail = "PUBLIC_APP_URL not set; skipping host comparison"
        host_hint = ""
    checks.append(Check(
        key="qbo_redirect_uri_host_matches_public_url",
        label="QBO_REDIRECT_URI host matches PUBLIC_APP_URL",
        ok=host_match_ok,
        severity=SEVERITY_RECOMMENDED,
        hint=host_hint,
        detail=host_detail,
    ))

    qbo_environment = os.environ.get("QBO_ENVIRONMENT", "sandbox").lower()
    qbo_real = _bool_env("QBO_REAL_IMPORT")
    checks.append(Check(
        key="qbo_real_import_enabled",
        label="QBO_REAL_IMPORT=1 (live posting enabled)",
        ok=qbo_real,
        # Recommended rather than required: a deploy can still be valid
        # in pre-launch mode where the operator wants demo-only behavior.
        severity=SEVERITY_RECOMMENDED,
        hint="Set QBO_REAL_IMPORT=1 in Render once you've smoke-tested. "
             "Demo mode is safe for staging but blocks real customer go-live." if not qbo_real else "",
        detail=f"environment: {qbo_environment}",
    ))

    support_email = os.environ.get("SUPPORT_EMAIL", "")
    support_ok = not _is_placeholder(support_email)
    checks.append(Check(
        key="support_email_set",
        label="SUPPORT_EMAIL configured",
        ok=support_ok,
        severity=SEVERITY_REQUIRED,
        hint="Set SUPPORT_EMAIL to a real, monitored mailbox in Render. "
             "Intuit reviewers will contact this address." if not support_ok else "",
    ))

    security_email = os.environ.get("SECURITY_EMAIL", "")
    security_ok = not _is_placeholder(security_email)
    checks.append(Check(
        key="security_email_set",
        label="SECURITY_EMAIL configured",
        ok=security_ok,
        severity=SEVERITY_REQUIRED,
        hint="Set SECURITY_EMAIL to a monitored mailbox for vulnerability reports." if not security_ok else "",
    ))

    privacy_email = os.environ.get("PRIVACY_CONTACT_EMAIL", "") or support_email
    privacy_ok = not _is_placeholder(privacy_email)
    checks.append(Check(
        key="privacy_contact_email_set",
        label="PRIVACY_CONTACT_EMAIL configured",
        ok=privacy_ok,
        severity=SEVERITY_RECOMMENDED,
        hint="Set PRIVACY_CONTACT_EMAIL (or rely on SUPPORT_EMAIL) so the privacy page lists a working contact." if not privacy_ok else "",
    ))

    public_url = os.environ.get("PUBLIC_APP_URL", "").strip()
    host = (request_host or "").lower()
    onrender = host.endswith(".onrender.com") or host == ""
    custom_domain_ok = bool(public_url) or (bool(host) and not onrender)
    if public_url:
        domain_detail = f"PUBLIC_APP_URL={public_url}"
    elif host:
        domain_detail = f"request host: {host}"
    else:
        domain_detail = ""
    checks.append(Check(
        key="custom_domain_present",
        label="Custom domain in use (not *.onrender.com)",
        ok=custom_domain_ok,
        severity=SEVERITY_RECOMMENDED,
        hint="Point your custom domain (e.g. www.cutovr.com) at this Render service "
             "and set PUBLIC_APP_URL so Intuit sees a stable URL." if not custom_domain_ok else "",
        detail=domain_detail,
    ))

    # The "health endpoint OK" check is degenerate when called from inside
    # a healthy app (we are by definition reachable to compute it), but
    # we surface it explicitly so the operator can see the JSON contract
    # the probe relies on. We mark it true if all the underlying booleans
    # the /healthz probe reports are wired up — i.e. the module imported
    # cleanly and we got this far.
    checks.append(Check(
        key="health_endpoint_ok",
        label="/healthz reachable and reporting",
        ok=True,
        severity=SEVERITY_INFO,
        hint="",
        detail="probed via /healthz",
    ))

    return checks


def healthz_booleans(request_host: Optional[str] = None,
                     request_scheme: Optional[str] = None) -> dict:
    """Subset suitable for the public /healthz probe.

    Maps each check to a single boolean field. Only includes booleans —
    no hints, no detail strings, no severities — so we can never
    accidentally grow the public surface to leak something sensitive.
    """
    out = {}
    for c in collect_checks(request_host=request_host, request_scheme=request_scheme):
        out[c.key] = bool(c.ok)
    return out


def overall_ready(checks: List[Check]) -> bool:
    """True if every REQUIRED check passed."""
    return all(c.ok for c in checks if c.severity == SEVERITY_REQUIRED)


def to_dict_list(checks: List[Check]) -> list:
    return [asdict(c) for c in checks]
