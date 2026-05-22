from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort
from werkzeug.utils import secure_filename
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps
from typing import Optional
import os, secrets
import hashlib
import logging
import re
import requests

from app_db import AppDB
import branding
import demo_mode
import operator_panel
import readiness
from pclaw_parser import parse_pclaw_csv, export_qbo_csv
from pclaw_pipeline import (
    load_general_ledger_csv,
    is_gl_format,
    build_account_mapping_from_numbers,
    build_account_mapping_from_names,
    build_account_type_index,
    build_journal_entries_from_gl,
    find_unmapped_accounts,
    build_test_journal_entry,
    group_rows_by_transaction,
    money,
    GL_REQUIRED_COLUMNS,
)
from qbo_auth import QBOAuthHandler
from qbo_client import QBOClient, QBOError
from encryption import encrypt_file, decrypt_file, encrypt_token, decrypt_token
from import_history import ImportHistory, sha256_of_file
from preflight import build_preflight_summary, friendly_validation_message
from migration_quality import (
    build_dry_run_preview,
    render_validation_csv,
    build_reconciliation_report,
    render_reconciliation_csv,
)
from report_types import (
    REPORT_GENERAL_LEDGER,
    REPORT_CHART_OF_ACCOUNTS,
    REPORT_TRIAL_BALANCE,
    REPORT_TRUST_LISTING,
    REPORT_TYPES,
    REPORT_LABELS,
    REPORT_QBO_BEHAVIOR,
    is_valid_report_type,
    detect_report_type,
    parse_chart_of_accounts,
    parse_trial_balance,
    parse_trust_listing,
    build_coa_preflight,
    build_trial_balance_preflight,
    build_trust_listing_preflight,
    build_coa_dry_run_preview,
)
from coa_apply import build_create_plan, apply_create_plan
from coa_hierarchy import (
    build_hierarchy_plan,
    annotate_create_plan_with_hierarchy,
    detect_hierarchy,
)
from opening_balance import (
    build_opening_balance_plan,
    OPENING_BALANCE_CONFIRMATION_PHRASE,
    build_opening_je_payload,
)
from tb_reconciliation import build_ending_tb_reconciliation
from tb_coa_validation import (
    validate_tb_against_coa,
    STATUS_READY as TBV_STATUS_READY,
    STATUS_CREATED_IN_QBO as TBV_STATUS_CREATED_IN_QBO,
)
from unmapped_account_guidance import classify_unmapped_accounts
from trust_reconciliation import build_trust_listing_reconciliation
from ar_ap_strategy import (
    validate_ar_ap_strategy,
    AR_AP_STRATEGY_CHOICES,
    STRATEGY_SKIP as AR_AP_STRATEGY_SKIP,
    guidance_for_strategy,
)
import qbo_error_hint
import email_sender
import cutover_workflow
import customer_workflow
import bulk_upload
from rate_limit import RateLimiter, client_ip
import csv as _csv
from io import StringIO
from flask import Response

app = Flask(__name__)

# Production-vs-local environment switch. Anything other than "local"/"dev"
# means we expect the operator to provide a real SECRET_KEY and serve over
# HTTPS. Set APP_ENV=production on Render.
APP_ENV = os.environ.get("APP_ENV", "local").lower()
IS_PRODUCTION = APP_ENV not in ("local", "dev", "development", "test")

# ---------------------------------------------------------------------------
# Trusted-proxy handling (Render, and any other reverse-proxy deploy).
#
# Render terminates TLS at its edge load balancer and forwards plaintext
# HTTP to the app, setting `X-Forwarded-Proto: https` and `X-Forwarded-Host:
# <user-facing-host>`. Without ProxyFix, `request.scheme` returns "http" and
# `url_for(..., _external=True)` builds links like
# http://app:5000/reset-password/... which:
#
#   * break Intuit's redirect-URI exact-match check on OAuth callbacks
#   * email password-reset links that don't enforce HTTPS
#   * cause `Secure` cookies to be skipped on what we *think* is HTTP
#
# Werkzeug ships a hardened `ProxyFix` middleware that consumes exactly N
# forwarded hops worth of headers. We only trust ONE hop — Render's edge.
# That blocks an attacker from spoofing arbitrary X-Forwarded-* headers by
# tunnelling through our own app: only the outermost proxy's value wins.
#
# Operators on platforms that put more than one trusted proxy in front of
# the app (e.g. CloudFront -> ALB -> app = 2 hops) can override the count
# via TRUSTED_PROXY_HOPS, but the safer default is 1.
#
# Local dev (APP_ENV=local) skips ProxyFix entirely so direct
# http://localhost:5000 testing isn't tricked by stray Forwarded headers.
# ---------------------------------------------------------------------------
try:
    _trusted_proxy_hops = int(os.environ.get("TRUSTED_PROXY_HOPS", "1"))
except ValueError:
    _trusted_proxy_hops = 1
_trusted_proxy_hops = max(0, _trusted_proxy_hops)

if IS_PRODUCTION and _trusted_proxy_hops > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=_trusted_proxy_hops,
        x_proto=_trusted_proxy_hops,
        x_host=_trusted_proxy_hops,
        x_port=0,
        x_prefix=0,
    )

# SECRET_KEY is the conventional Flask name; APP_SECRET is kept as a fallback
# for backward compatibility with previous versions of this app.
_secret = os.environ.get("SECRET_KEY") or os.environ.get("APP_SECRET")
if not _secret:
    if IS_PRODUCTION:
        raise RuntimeError(
            "SECRET_KEY environment variable is required when APP_ENV != 'local'. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    # Local-only fallback: an ephemeral key. Sessions will reset on each
    # restart, which is fine for development.
    _secret = secrets.token_hex(32)
    print("WARNING: SECRET_KEY not set; generated an ephemeral key for local dev.")
app.secret_key = _secret

# Cookie hardening. SESSION_COOKIE_SECURE is enabled in production so the
# session cookie is only sent over HTTPS.
#
# PERMANENT_SESSION_LIFETIME bounds how long an idle session stays valid
# (default Flask is 31 days, which is too long for law-firm financial
# data). 12 hours covers a normal work day; SESSION_HOURS env var lets
# operators tune it per deploy without a code change.
#
# MAX_CONTENT_LENGTH caps any single request body. Real PCLaw GL exports
# for a year of data fit comfortably under 25 MB; this stops a runaway
# upload from filling the Render disk.
try:
    _session_hours = int(os.environ.get("SESSION_HOURS", "12"))
except ValueError:
    _session_hours = 12
try:
    _max_upload_mb = int(os.environ.get("MAX_UPLOAD_MB", "25"))
except ValueError:
    _max_upload_mb = 25

from datetime import timedelta as _timedelta  # local alias to avoid clobbering datetime above

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    PERMANENT_SESSION_LIFETIME=_timedelta(hours=max(1, _session_hours)),
    MAX_CONTENT_LENGTH=max(1, _max_upload_mb) * 1024 * 1024,
)


@app.before_request
def _make_session_permanent():
    """Opt every session into PERMANENT_SESSION_LIFETIME so the idle-
    timeout actually applies. Without ``session.permanent = True`` Flask
    treats the session as a transient browser-lifetime cookie and
    PERMANENT_SESSION_LIFETIME is ignored.
    """
    session.permanent = True


@app.errorhandler(413)
def _request_entity_too_large(_e):
    """Friendly message for uploads that exceed MAX_CONTENT_LENGTH."""
    flash(
        f"That file is larger than the {_max_upload_mb} MB upload limit. "
        "Export a smaller PCLaw range or split the file and try again.",
        "error",
    )
    return redirect(url_for("dashboard")), 302


@app.after_request
def _security_headers(resp):
    """Conservative defensive headers for the whole app.

    These are quick wins that don't require a CSP audit:
      - X-Content-Type-Options stops MIME sniffing.
      - Referrer-Policy keeps the QBO realm/job IDs out of cross-origin
        Referer headers if a customer ever clicks an outbound link.
      - X-Frame-Options blocks clickjacking via iframe embedding.
      - Permissions-Policy disables sensors we never use.
      - HSTS only emitted in production (where TLS is terminated by
        Render); emitting it on http://localhost would be useless.
    """
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    # Conservative CSP: app templates do not load any third-party scripts
    # at runtime (only Google Fonts CSS + font files via <link>). We allow
    # 'self' for everything plus the two Google Fonts hosts. No inline
    # scripts; no eval. If a future feature needs an inline <script> the
    # right move is a nonce, not loosening this header.
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self' https://appcenter.intuit.com; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "object-src 'none'",
    )
    if IS_PRODUCTION:
        resp.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return resp

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "processed"
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# Persistent import history (SQLite). DB path can be overridden so production
# can point at a writable disk on Render.
DB_PATH = os.environ.get("IMPORT_HISTORY_DB", str(DATA_DIR / "import_history.sqlite3"))
history = ImportHistory(DB_PATH)

# App database for auth + tenancy (firms, users, jobs metadata, audit log).
APP_DB_PATH = os.environ.get("APP_DB", str(DATA_DIR / "app.sqlite3"))
db = AppDB(APP_DB_PATH)

# Password policy (applies to signup AND password reset). 12 chars is the
# baseline NIST SP 800-63B "memorized secret" floor. We don't add complexity
# rules — length is what matters for offline cracking resistance.
MIN_PASSWORD_LENGTH = 12
PASSWORD_TOO_SHORT_MSG = (
    f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
)

# Password reset tokens are single-use, time-limited. 30 minutes is short
# enough to limit exposure if a mailbox is compromised after the email is
# sent, long enough that a user pulling the reset link off a phone won't
# get unlucky.
PASSWORD_RESET_TTL_MINUTES = 30

# Rate limit budgets. Tuned so legit users (typo their password 2-3 times,
# request a reset link once) never hit them; brute-force attempts do. Values
# are intentionally conservative for a single-instance Render deploy.
LOGIN_RATE_LIMIT_MAX = 10
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 5 * 60
FORGOT_RATE_LIMIT_MAX = 5
FORGOT_RATE_LIMIT_WINDOW_SECONDS = 15 * 60

login_limiter = RateLimiter(
    db,
    max_events=LOGIN_RATE_LIMIT_MAX,
    window_seconds=LOGIN_RATE_LIMIT_WINDOW_SECONDS,
)
forgot_limiter = RateLimiter(
    db,
    max_events=FORGOT_RATE_LIMIT_MAX,
    window_seconds=FORGOT_RATE_LIMIT_WINDOW_SECONDS,
)

# QBO OAuth configuration (set these via environment variables in real use)
QBO_CLIENT_ID = os.environ.get("QBO_CLIENT_ID", "your-client-id-here")
QBO_CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "your-client-secret-here")
QBO_REDIRECT_URI = os.environ.get("QBO_REDIRECT_URI", "http://localhost:5000/oauth/callback")
QBO_ENVIRONMENT = os.environ.get("QBO_ENVIRONMENT", "sandbox")  # 'sandbox' or 'production'

# When set to a truthy value, the "Import to QuickBooks" button performs a real
# QBO write instead of the previous demo-mode simulation. Default is off so
# the existing demo flow keeps working until the user opts in.
QBO_REAL_IMPORT = os.environ.get("QBO_REAL_IMPORT", "0").lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Production environment validation.
#
# Fail fast at startup with a clear, secret-free error if a required env var
# is missing or malformed. Only enforced when APP_ENV != 'local'/'dev' so the
# beginner-friendly local workflow keeps working with sensible defaults.
# ---------------------------------------------------------------------------
def _validate_production_env():
    errors = []

    sk = os.environ.get("SECRET_KEY") or os.environ.get("APP_SECRET") or ""
    if len(sk) < 32:
        errors.append("SECRET_KEY must be set and at least 32 characters")

    enc = os.environ.get("ENCRYPTION_KEY", "")
    if not enc:
        errors.append("ENCRYPTION_KEY is required (generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\")")
    else:
        try:
            from cryptography.fernet import Fernet
            Fernet(enc.encode())
        except Exception:
            errors.append("ENCRYPTION_KEY is not a valid Fernet key (must be 32 url-safe base64-encoded bytes)")

    if QBO_CLIENT_ID == "your-client-id-here" or not QBO_CLIENT_ID:
        errors.append("QBO_CLIENT_ID is required")
    if QBO_CLIENT_SECRET == "your-client-secret-here" or not QBO_CLIENT_SECRET:
        errors.append("QBO_CLIENT_SECRET is required")
    if not QBO_REDIRECT_URI or QBO_REDIRECT_URI.startswith("http://localhost"):
        errors.append("QBO_REDIRECT_URI must be set to the public HTTPS callback URL")
    elif not QBO_REDIRECT_URI.startswith("https://"):
        errors.append("QBO_REDIRECT_URI must use https:// in production")

    if QBO_ENVIRONMENT not in ("sandbox", "production"):
        errors.append("QBO_ENVIRONMENT must be 'sandbox' or 'production'")

    if errors:
        bullets = "\n  - " + "\n  - ".join(errors)
        raise RuntimeError(
            "Production environment validation failed (APP_ENV=%s).%s\n"
            "Set the missing/invalid variables in your hosting provider and redeploy. "
            "Do not paste real secret values into source code or logs."
            % (APP_ENV, bullets)
        )


if IS_PRODUCTION:
    _validate_production_env()


qbo_auth = QBOAuthHandler(QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_REDIRECT_URI, QBO_ENVIRONMENT)


def _qbo_production_blockers():
    """Return a list of operator-safe reason strings if this deploy is NOT
    safe to connect a real QuickBooks Online customer company.

    Only enforced when QBO_ENVIRONMENT=production. Sandbox deploys keep the
    looser checks in place because Intuit's sandbox tooling tolerates
    localhost redirects, missing PUBLIC_APP_URL, etc.

    Returns ``[]`` when production-mode connect is safe to attempt.
    The list contains short, non-secret strings such as
    "QBO_REDIRECT_URI must use https://" — never an actual env value.
    """
    if QBO_ENVIRONMENT != "production":
        return []

    blockers = []
    if QBO_CLIENT_ID == "your-client-id-here" or not QBO_CLIENT_ID:
        blockers.append("QBO_CLIENT_ID is not configured")
    if QBO_CLIENT_SECRET == "your-client-secret-here" or not QBO_CLIENT_SECRET:
        blockers.append("QBO_CLIENT_SECRET is not configured")
    if not QBO_REDIRECT_URI:
        blockers.append("QBO_REDIRECT_URI is not configured")
    elif not QBO_REDIRECT_URI.startswith("https://"):
        blockers.append("QBO_REDIRECT_URI must use https:// in production")
    elif QBO_REDIRECT_URI.startswith("http://localhost"):
        blockers.append("QBO_REDIRECT_URI must point at the public host, not localhost")

    if not QBO_REAL_IMPORT:
        blockers.append(
            "QBO_REAL_IMPORT must be set to 1 before posting real customer data"
        )

    if not IS_PRODUCTION:
        blockers.append("APP_ENV must be set to production")

    if branding.is_placeholder_email(branding.SUPPORT_EMAIL):
        blockers.append("SUPPORT_EMAIL must be a real, monitored mailbox")

    return blockers

jobs = {}
qbo_connections = {}  # {job_id: {realm_id, access_token_enc, refresh_token_enc, expires_at, connected_at}}

# In-memory cache of bulk-upload sessions. {bulk_id: {firm_id, created_at,
# company, results:[{filename, report_type, status, ...}]}}.  Lives only as
# long as the process — the review screen is a one-shot post-upload step,
# and each underlying job is already persisted to the DB through the
# normal per-file upload pipeline.
bulk_uploads = {}


# ---------------------------------------------------------------------------
# CSRF protection (small, no extra dependency)
#
# Strategy:
#   - First time we render any page, mint a per-session token and stash it
#     in session["_csrf_token"].
#   - Every <form method="post"> includes <input name="csrf_token" value="...">.
#     Templates get a `csrf_token()` callable injected via context_processor.
#   - A `before_request` hook compares form.csrf_token to session value on
#     every state-changing request (POST/PUT/PATCH/DELETE). Mismatch → 400.
#   - The Intuit OAuth callback (GET) is exempt because it's a third-party
#     redirect; safety there comes from the OAuth `state` value, which we
#     already check against the user's session firm_id.
# ---------------------------------------------------------------------------

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FORM_FIELD = "csrf_token"
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Tests run with `CSRF_DISABLE=1` so they don't need to scrape the token
# from every rendered page. NEVER set this in production.
CSRF_DISABLED = os.environ.get("CSRF_DISABLE", "0").lower() in ("1", "true", "yes", "on")
if CSRF_DISABLED and IS_PRODUCTION:
    raise RuntimeError("CSRF_DISABLE must not be set when APP_ENV=production")


def _ensure_csrf_token():
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_token():
    """Template helper: returns the current per-session CSRF token."""
    return _ensure_csrf_token()


@app.context_processor
def _inject_csrf():
    return {"csrf_token": csrf_token}


@app.before_request
def _csrf_protect():
    if CSRF_DISABLED:
        return None
    if request.method in _CSRF_SAFE_METHODS:
        return None
    # Static files are GET-only, so they wouldn't reach here. The OAuth
    # callback is a GET. We still want to skip on, e.g., a future webhook
    # path — define exempt list explicitly so it's auditable.
    if request.endpoint in {"static"}:
        return None
    expected = session.get(CSRF_SESSION_KEY)
    submitted = request.form.get(CSRF_FORM_FIELD) or request.headers.get("X-CSRF-Token")
    if not expected or not submitted or not secrets.compare_digest(str(expected), str(submitted)):
        # Friendly message + safe redirect to the login page (which always
        # works regardless of auth state). 400 is the technically correct
        # status code; we use it for non-form clients via the Accept header.
        if request.accept_mimetypes.best == "application/json":
            return jsonify({"error": "csrf token missing or invalid"}), 400
        flash("Your session expired or this form was missing a security token. Please try again.", "error")
        # If they had a session, send them somewhere useful; otherwise login.
        return redirect(url_for("dashboard" if current_user() else "login"))
    return None


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _is_safe_local_redirect(target):
    """Return True if `target` is a safe same-origin redirect target.

    Rejects:
      - empty / None
      - protocol-relative ("//evil.example/...")
      - full URLs ("https://evil.example/...")
      - backslash-prefixed paths ("/\\evil.example") which some browsers
        normalise to a scheme-relative URL
      - anything that is not a path beginning with a single '/'
    """
    if not target or not isinstance(target, str):
        return False
    if len(target) > 512:
        return False
    if not target.startswith("/"):
        return False
    if target.startswith("//"):
        return False
    if target.startswith("/\\"):
        return False
    # Reject any embedded CR/LF (header-injection guard) or NUL.
    if any(ch in target for ch in ("\r", "\n", "\x00")):
        return False
    return True


def current_user():
    """Return the logged-in user dict (with firm_id) or None."""
    uid = session.get("user_id")
    if not uid:
        return None
    user = db.get_user(uid)
    if not user:
        # Stale session (e.g. user deleted). Clear it so the user is forced to log in again.
        session.clear()
    return user


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_user():
    """Make `user`, `firm`, `now_year`, and configurable branding available
    to every template."""
    user = current_user()
    firm = db.get_firm(user["firm_id"]) if user else None
    ctx = {"user": user, "firm": firm, "now_year": datetime.utcnow().year}
    ctx.update(branding.context())
    # Templates use these to render a "Sandbox Testing Mode" banner near
    # any QuickBooks connect/import affordance, so beta testers don't try
    # to authorize a real QBO company against sandbox-only credentials.
    ctx["qbo_environment"] = QBO_ENVIRONMENT
    ctx["qbo_is_sandbox"] = (QBO_ENVIRONMENT == "sandbox")
    return ctx


def _get_job(job_id):
    """Return the live job dict, rehydrating from the DB if the in-memory
    cache was lost (e.g. after a restart).

    The DB is the source of truth for everything except the running QBO
    client object. The cache is write-through: every status change writes
    both. This helper makes the read path symmetric.
    """
    job = jobs.get(job_id)
    if job:
        return job
    rehydrated = db.hydrate_job(job_id)
    if rehydrated is None:
        return None
    jobs[job_id] = rehydrated
    return rehydrated


def _job_or_403(job_id):
    """Return the job dict + user if the current user owns its firm.

    Aborts 401 if not logged in, 404 if the job doesn't exist OR belongs
    to a different firm (we deliberately avoid 403 to not leak existence).
    """
    user = current_user()
    if not user:
        abort(401)
    job = _get_job(job_id)
    if not job:
        abort(404)
    if job.get("firm_id") != user["firm_id"]:
        abort(404)
    return job, user


def _get_qbo_connection(job_id):
    """Return the live qbo_connection dict (with decrypted access tokens
    available via decrypt_token), rehydrating from the DB if needed.

    Returns None if no connection exists for this job.
    """
    conn = qbo_connections.get(job_id)
    if conn and conn.get("access_token_enc"):
        return conn
    row = db.get_qbo_connection(job_id)
    if not row or not row.get("access_token_enc") or not row.get("refresh_token_enc"):
        return None
    rehydrated = {
        "realm_id": row["realm_id"],
        "access_token_enc": row["access_token_enc"],
        "refresh_token_enc": row["refresh_token_enc"],
        "expires_at": row.get("expires_at") if isinstance(row, dict) else row["expires_at"],
        "company_name": row.get("company_name"),
        "legal_name": row.get("legal_name"),
        "country": row.get("country"),
        "company_info_error": row.get("company_info_error"),
        "connected_at": row["connected_at"],
    }
    qbo_connections[job_id] = rehydrated
    return rehydrated


def _save_job(job_id):
    """Mirror the in-memory job state to the DB."""
    job = jobs.get(job_id)
    if not job:
        return
    db.save_job_state(job_id, job)


# Refresh the access token if it expires within this many seconds. 5 minutes
# leaves a comfortable margin around clock skew without refreshing too often.
TOKEN_REFRESH_LEEWAY_SECONDS = 5 * 60


class QBOAuthExpired(Exception):
    """Raised when the stored refresh token is no longer accepted by Intuit."""


def _qbo_token_is_fresh(qbo_conn):
    expires_at = qbo_conn.get("expires_at")
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return False
    return (exp - datetime.utcnow()).total_seconds() > TOKEN_REFRESH_LEEWAY_SECONDS


def _refresh_qbo_tokens(job_id, qbo_conn, firm_id):
    """Exchange the refresh token for a new access token; persist the rotation.

    Intuit returns a new refresh_token on every refresh and invalidates the
    previous one, so we must save both. On any exception, raises
    QBOAuthExpired so the caller can prompt the user to reconnect.
    """
    refresh_plain = decrypt_token(qbo_conn["refresh_token_enc"])
    try:
        new = qbo_auth.refresh_access_token(refresh_plain)
    except Exception as e:  # noqa: BLE001
        # Pull the intuit_tid off the exception (or the handler) so the
        # caller can include it in the audit row when we surface the
        # "connection expired" flash. Opaque, safe to log.
        tid = getattr(e, "intuit_tid", None) or getattr(qbo_auth, "last_intuit_tid", None)
        msg = str(e)
        if tid:
            msg = f"{msg} (intuit_tid={tid})"
        raise QBOAuthExpired(msg) from e

    enc_access = encrypt_token(new["access_token"])
    enc_refresh = encrypt_token(new["refresh_token"])

    db.upsert_qbo_connection(
        job_id=job_id,
        firm_id=firm_id,
        realm_id=qbo_conn["realm_id"],
        access_token_enc=enc_access,
        refresh_token_enc=enc_refresh,
        company_name=qbo_conn.get("company_name"),
        legal_name=qbo_conn.get("legal_name"),
        country=qbo_conn.get("country"),
        expires_at=new["expires_at"],
        company_info_error=qbo_conn.get("company_info_error"),
    )
    qbo_conn["access_token_enc"] = enc_access
    qbo_conn["refresh_token_enc"] = enc_refresh
    qbo_conn["expires_at"] = new["expires_at"]
    qbo_connections[job_id] = qbo_conn
    return qbo_conn


def _get_qbo_client(job_id, user):
    """Return a ready-to-call (QBOClient, qbo_conn) for this job.

    Refreshes the access token if it is missing, expired, or within
    `TOKEN_REFRESH_LEEWAY_SECONDS` of expiry. On a refresh failure raises
    QBOAuthExpired — the caller is responsible for translating that into a
    user-facing flash + redirect.
    """
    qbo_conn = _get_qbo_connection(job_id)
    if not qbo_conn:
        return None, None
    if not _qbo_token_is_fresh(qbo_conn):
        qbo_conn = _refresh_qbo_tokens(job_id, qbo_conn, user["firm_id"])
        _audit("qbo_token_refreshed", target_type="job", target_id=job_id)
    qbo = QBOClient(
        access_token=decrypt_token(qbo_conn["access_token_enc"]),
        realm_id=qbo_conn["realm_id"],
        environment=QBO_ENVIRONMENT,
    )
    return qbo, qbo_conn


def _redact_email_for_audit(email: str) -> str:
    """Return a privacy-preserving rendering of an email for audit logs.

    A SOC2 reviewer or law-firm DPO who reads the audit table should be
    able to correlate rows to a user (for support / forensics) without
    seeing the full personal email address everywhere. We keep the first
    character of the local-part plus the full domain, e.g.
    ``alice@acme.test`` -> ``a***@acme.test``. The user_id column still
    points at the canonical row, so reduced detail loses no support
    value.
    """
    if not email or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    if not local:
        return f"@{domain}"
    if len(local) <= 1:
        return f"{local}***@{domain}"
    return f"{local[0]}***@{domain}"


_AUDIT_DETAILS_MAX_LEN = 500
_SECRETY_TOKEN_RE = re.compile(
    r"\b(?:access_token|refresh_token|client_secret|authorization|"
    r"bearer|password|api[_-]?key)\b[\s:=]*['\"]?[\w\-\.~+/=]+",
    re.IGNORECASE,
)


def _sanitize_audit_details(details):
    """Scrub obvious token / credential strings out of an audit detail.

    QBOError / requests exception strings can pull in chunks of the
    upstream response body. We do not want raw access tokens, refresh
    tokens, or `Authorization: Bearer ...` headers in the audit log even
    by accident — the audit table is read by support and is the most
    likely place to grep for incidents, so it should be free of credential
    material. We also truncate to `_AUDIT_DETAILS_MAX_LEN` so a 4KB QBO
    response body doesn't bloat the row.

    Returns the cleaned string (or the original value when it isn't a
    plain string).
    """
    if details is None:
        return None
    if not isinstance(details, str):
        return details
    cleaned = _SECRETY_TOKEN_RE.sub("[redacted]", details)
    if len(cleaned) > _AUDIT_DETAILS_MAX_LEN:
        cleaned = cleaned[:_AUDIT_DETAILS_MAX_LEN] + "…(truncated)"
    return cleaned


def _audit_details_with_tid(details, intuit_tid):
    """Append the Intuit transaction id to an audit detail string, when one
    is present. The tid is opaque (no token / secret material), so it's
    safe to include alongside the existing detail text. The detail text
    itself is sanitized so raw QBO response bodies cannot smuggle a token
    into the audit log.
    """
    details = _sanitize_audit_details(details)
    if not intuit_tid:
        return details
    if not details:
        return f"intuit_tid={intuit_tid}"
    return f"{details} intuit_tid={intuit_tid}"


def _audit(action, target_type=None, target_id=None, details=None):
    user = current_user()
    db.audit(
        action=action,
        firm_id=user["firm_id"] if user else None,
        user_id=user["id"] if user else None,
        target_type=target_type,
        target_id=target_id,
        details=_sanitize_audit_details(details),
    )


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        firm_name = (request.form.get("firm_name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not firm_name or not email or not password:
            flash("Firm name, email, and password are required.", "error")
            return render_template("signup.html", firm_name=firm_name, email=email)
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("signup.html", firm_name=firm_name, email=email)
        if len(password) < MIN_PASSWORD_LENGTH:
            flash(PASSWORD_TOO_SHORT_MSG, "error")
            return render_template("signup.html", firm_name=firm_name, email=email)
        try:
            firm_id, user_id = db.create_firm_and_admin(firm_name, email, password)
        except ValueError as e:
            flash(str(e), "error")
            return render_template("signup.html", firm_name=firm_name, email=email)
        session.clear()
        session["user_id"] = user_id
        session["firm_id"] = firm_id
        db.audit(action="signup", firm_id=firm_id, user_id=user_id,
                 target_type="firm", target_id=str(firm_id),
                 details=_redact_email_for_audit(email))
        flash(f"Welcome to {firm_name}!", "success")
        return redirect(url_for("dashboard"))
    return render_template("signup.html")


RATE_LIMIT_FRIENDLY_MSG = (
    "Too many attempts from your network. Please wait a few minutes and "
    "try again."
)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        # Rate-limit on (route, ip) AND (route, email) so an attacker
        # rotating either dimension still hits a wall, while a shared
        # office IP doesn't lock everyone out as long as the targeted
        # email is rotating too. The limiter is permissive enough that
        # legit users won't notice.
        ip_key = f"login:ip:{client_ip(request)}"
        email_key = f"login:email:{email}" if email else None
        ip_ok, _ = login_limiter.check_and_record(ip_key)
        email_ok = True
        if email_key:
            email_ok, _ = login_limiter.check_and_record(email_key)
        if not (ip_ok and email_ok):
            db.audit(action="login_rate_limited",
                     details=f"ip={client_ip(request)}")
            flash(RATE_LIMIT_FRIENDLY_MSG, "error")
            return render_template("login.html", email=email), 429

        user = db.authenticate(email, password)
        if not user:
            db.audit(action="login_failed",
                     details=_redact_email_for_audit(email))
            flash("Invalid email or password.", "error")
            return render_template("login.html", email=email)
        session.clear()
        session["user_id"] = user["id"]
        session["firm_id"] = user["firm_id"]
        db.audit(action="login", firm_id=user["firm_id"], user_id=user["id"],
                 details=_redact_email_for_audit(email))
        next_url = request.args.get("next") or request.form.get("next")
        if _is_safe_local_redirect(next_url):
            return redirect(next_url)
        return redirect(url_for("dashboard"))
    return render_template("login.html")


# ---------------------------------------------------------------------------
# Password reset
#
# Flow:
#   1) User submits email at /forgot-password.
#   2) We always show the same generic "if that email exists, we sent a link"
#      response, so the page can't be used as an account-existence oracle.
#   3) If the email matches a real user, we generate a single-use,
#      time-limited token. Only the SHA-256 hash is stored in the DB; the
#      plaintext is delivered to the user via SMTP (or, in dev only, via the
#      server log) and never appears in the HTTP response.
#   4) /reset-password/<token> validates the token and lets the user pick a
#      new password (subject to the same length policy as signup).
#   5) On success we mark the token used, invalidate any other outstanding
#      tokens for that user, and audit the reset.
#
# What we deliberately do NOT do here:
#   - We never expose the token in the user-facing HTTP response.
#   - We never include the email body or token URL in the audit log.
#   - We do not auto-sign-in the user after a reset (they go to /login).
# ---------------------------------------------------------------------------

_pwreset_log = logging.getLogger("password_reset")


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_reset_token() -> str:
    # 32 bytes (~256 bits) of url-safe randomness. Plenty of entropy for a
    # 30-minute single-use token.
    return secrets.token_urlsafe(32)


def _send_reset_email(user: dict, reset_url: str) -> bool:
    """Send the reset email. Returns True if delivered via SMTP.

    In non-production we log the URL so a developer running locally can copy
    it from the console. In production we never log the URL — instead we
    record an audit warning that SMTP is not configured or delivery failed
    so the operator can notice. The token itself NEVER appears in the
    user-facing response, and we never log the email body or recipient
    address beyond what is strictly needed for operator triage.
    """
    subject = f"Reset your {branding.context().get('app_name', 'Cutover')} password"
    body_text = (
        "Someone (hopefully you) requested a password reset for your account.\n\n"
        f"Open this link to choose a new password:\n\n  {reset_url}\n\n"
        f"The link expires in {PASSWORD_RESET_TTL_MINUTES} minutes and can "
        "only be used once. If you did not request this, you can safely "
        "ignore this email.\n"
    )
    if email_sender.is_smtp_configured():
        ok = email_sender.send_email(
            to=user["email"], subject=subject, body_text=body_text
        )
        if ok:
            return True
        # SMTP is configured but delivery failed (transport, auth, or
        # rejected). Surface to operators via the audit log + a structured
        # log line so an alert can fire. We do NOT include the token URL,
        # the recipient address, or any SMTP credential material — only
        # the non-secret connection metadata from email_sender.smtp_status().
        status = email_sender.smtp_status()
        _pwreset_log.warning(
            "password_reset_email_delivery_failed host=%s port=%s",
            status.get("host"), status.get("port"),
        )
        db.audit(
            action="password_reset_email_send_failed",
            firm_id=user.get("firm_id"),
            user_id=user.get("id"),
            details=(
                f"smtp_host={status.get('host')} smtp_port={status.get('port')} "
                "delivery_failed=yes"
            ),
        )
        return False
    if not IS_PRODUCTION:
        # Dev convenience: print to the same stdout the rest of the app
        # uses. Production never reaches this branch.
        _pwreset_log.warning(
            "SMTP not configured; reset URL for %s: %s",
            user["email"], reset_url,
        )
    else:
        # Production with no SMTP: record an operator-visible warning so
        # someone notices the misconfiguration. Do NOT include the URL or
        # token, only the fact that we couldn't send. Also emit a structured
        # log line so external log shipping picks it up immediately even if
        # the operator hasn't opened the audit panel.
        _pwreset_log.warning(
            "password_reset_smtp_not_configured app_env=%s", APP_ENV,
        )
        db.audit(
            action="password_reset_smtp_missing",
            firm_id=user.get("firm_id"),
            user_id=user.get("id"),
            details="SMTP env vars not set; reset email NOT delivered",
        )
    return False


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()

        # Rate-limit on (route, ip) so a single attacker can't enumerate
        # accounts by spamming this endpoint.
        ip_key = f"forgot:ip:{client_ip(request)}"
        ok, _ = forgot_limiter.check_and_record(ip_key)
        if not ok:
            db.audit(action="forgot_password_rate_limited",
                     details=f"ip={client_ip(request)}")
            flash(RATE_LIMIT_FRIENDLY_MSG, "error")
            return render_template("forgot-password.html", email=email), 429

        # Generic response regardless of whether the email exists.
        generic_msg = (
            "If an account with that email exists, we've sent a password "
            "reset link. Check your inbox (and spam folder)."
        )

        user = db.get_user_by_email(email) if email else None
        if user:
            token = _generate_reset_token()
            token_hash = _hash_reset_token(token)
            expires_at = (
                datetime.utcnow() + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES)
            ).isoformat()
            db.create_password_reset_token(user["id"], token_hash, expires_at)
            reset_url = url_for("reset_password", token=token, _external=True)
            delivered = _send_reset_email(user, reset_url)
            db.audit(
                action="password_reset_requested",
                firm_id=user.get("firm_id"),
                user_id=user["id"],
                details=f"smtp_delivered={'yes' if delivered else 'no'}",
            )
        else:
            # Still audit the attempt (no user_id) so operators can spot
            # high-volume probing. We do not record the email itself in
            # plaintext to avoid a junk log of typo'd addresses; the IP
            # is enough.
            db.audit(
                action="password_reset_requested_unknown_email",
                details=f"ip={client_ip(request)}",
            )

        flash(generic_msg, "success")
        return render_template("forgot-password.html", submitted=True)
    return render_template("forgot-password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_hash = _hash_reset_token(token or "")
    row = db.get_password_reset_token(token_hash) if token else None

    def _invalid():
        flash(
            "This password reset link is invalid or has expired. Please "
            "request a new one.",
            "error",
        )
        return redirect(url_for("forgot_password"))

    if not row:
        return _invalid()
    if row.get("used_at"):
        return _invalid()
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except (TypeError, ValueError):
        return _invalid()
    if expires < datetime.utcnow():
        return _invalid()

    user = db.get_user(row["user_id"])
    if not user:
        return _invalid()

    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("reset-password.html", token=token)
        if len(password) < MIN_PASSWORD_LENGTH:
            flash(PASSWORD_TOO_SHORT_MSG, "error")
            return render_template("reset-password.html", token=token)

        db.update_user_password(user["id"], password)
        db.mark_password_reset_used(row["id"])
        # Invalidate any other outstanding tokens for this user so a
        # second emailed link can't be used after the password changes.
        db.invalidate_user_reset_tokens(user["id"])
        # Force any active session — including the attacker's, if any —
        # to re-authenticate. We only have access to *this* request's
        # session; other sessions can't be revoked without a session
        # store. That's an accepted tradeoff for the simple version.
        session.clear()
        db.audit(
            action="password_reset_completed",
            firm_id=user.get("firm_id"),
            user_id=user["id"],
        )
        flash(
            "Your password has been reset. Please sign in with your new "
            "password.",
            "success",
        )
        return redirect(url_for("login"))

    return render_template("reset-password.html", token=token)


@app.route("/logout", methods=["POST"])
def logout():
    user = current_user()
    if user:
        db.audit(action="logout", firm_id=user["firm_id"], user_id=user["id"])
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


def _firm_account_mapping_count(firm_id):
    """Total saved PCLaw→QBO account mappings across every connected QBO
    company for a firm. Used by the checklist to derive the
    'account_mapping' step status.
    """
    total = 0
    for conn in db.list_qbo_connections_for_firm(firm_id):
        total += len(db.list_account_mappings(firm_id, conn["realm_id"]))
    return total


def _build_firm_checklist(firm_id):
    """Helper that loads everything needed for the migration checklist
    and returns (cutover, checklist_items, next_step).
    """
    cutover = db.get_cutover_settings(firm_id)
    firm_jobs = db.list_jobs_for_firm(firm_id, limit=500)
    # Hydrate the jobs we know about so checklist can look at `preflight`,
    # `import_summary`, `verification`. list_jobs_for_firm returns raw rows
    # (with *_json columns); hydrate_job decodes them.
    hydrated = []
    for row in firm_jobs:
        h = db.hydrate_job(row["id"])
        hydrated.append(h or row)
    qbo_conns = db.list_qbo_connections_for_firm(firm_id)
    items = cutover_workflow.build_checklist(
        cutover,
        hydrated,
        has_qbo_connection=bool(qbo_conns),
        account_mapping_count=_firm_account_mapping_count(firm_id),
    )
    return cutover, items, cutover_workflow.next_recommended_step(items)


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    firm_jobs = db.list_jobs_for_firm(user["firm_id"], limit=20)
    cutover, checklist_items, next_step = _build_firm_checklist(user["firm_id"])
    stages = customer_workflow.build_customer_stages(
        checklist_items,
        url_for=url_for,
        has_jobs=bool(firm_jobs),
    )
    current = customer_workflow.current_stage(stages)
    return render_template(
        "dashboard.html",
        firm_jobs=firm_jobs,
        qbo_configured=QBO_CLIENT_ID != "your-client-id-here",
        recent_audit=db.recent_audit_for_firm(user["firm_id"], limit=10),
        cutover=cutover,
        checklist_items=checklist_items,
        next_step=next_step,
        workflow_stages=[s.to_dict() for s in stages],
        workflow_current=current.to_dict() if current else None,
        workflow_progress=customer_workflow.progress_percent(stages),
        workflow_completed=customer_workflow.completed_count(stages),
        workflow_terms=customer_workflow.FRIENDLY_TERMS,
    )


@app.route("/cutover", methods=["GET", "POST"])
@app.route("/migration-setup", methods=["GET", "POST"])
@login_required
def cutover_setup():
    """Create or update the firm's cutover/migration context.

    Idempotent — every POST is an upsert, so the firm admin can come
    back to refine fields. No values written here are secrets and no
    QBO writes happen from this route.
    """
    user = current_user()
    firm_id = user["firm_id"]
    existing = db.get_cutover_settings(firm_id)

    if request.method == "POST":
        def _form(name, max_len=200):
            return (request.form.get(name) or "").strip()[:max_len] or None

        cutover_date = _form("cutover_date", 32)
        opening_balance_date = _form("opening_balance_date", 32)
        period_start = _form("period_start", 32)
        period_end = _form("period_end", 32)
        country = _form("country", 16)
        accounting_basis = _form("accounting_basis", 16)
        migration_scope = _form("migration_scope", 200)
        notes = _form("notes", 4000)
        qbo_company_name = _form("qbo_company_name", 200)
        qbo_realm_id = _form("qbo_realm_id", 64)
        clio_involved = bool(request.form.get("clio_involved"))
        ar_ap_strategy = validate_ar_ap_strategy(_form("ar_ap_strategy", 32))

        if country and country not in {c[0] for c in cutover_workflow.COUNTRY_CHOICES}:
            country = "OTHER"
        if accounting_basis and accounting_basis not in {
            b[0] for b in cutover_workflow.ACCOUNTING_BASIS_CHOICES
        }:
            accounting_basis = "unknown"

        db.upsert_cutover_settings(
            firm_id,
            cutover_date=cutover_date,
            opening_balance_date=opening_balance_date,
            period_start=period_start,
            period_end=period_end,
            country=country,
            accounting_basis=accounting_basis,
            migration_scope=migration_scope,
            notes=notes,
            source_system="PCLaw",
            target_system="QBO",
            clio_involved=clio_involved,
            qbo_company_name=qbo_company_name,
            qbo_realm_id=qbo_realm_id,
            ar_ap_strategy=ar_ap_strategy or None,
        )
        db.audit(
            action="cutover_settings_saved",
            firm_id=firm_id,
            user_id=user["id"],
            target_type="firm",
            target_id=str(firm_id),
        )
        flash("Cutover settings saved.", "success")
        return redirect(url_for("migration_checklist"))

    ar_ap_strategy = (existing or {}).get("ar_ap_strategy") if existing else ""
    # On demo deploys, default the AR/AP strategy to "skip" so the demo
    # workflow stays clean for lawyers who aren't accountants. We only
    # apply this when the firm has no explicit choice on file yet — any
    # saved value (including a deliberate "Not decided yet" picked later)
    # is preserved unchanged.
    ar_ap_strategy_default = ""
    if not ar_ap_strategy and demo_mode.is_demo_mode_enabled():
        ar_ap_strategy_default = AR_AP_STRATEGY_SKIP
    effective_strategy = ar_ap_strategy or ar_ap_strategy_default
    return render_template(
        "cutover.html",
        cutover=existing or {},
        guidance=cutover_workflow.GUIDANCE_TEXT,
        country_choices=cutover_workflow.COUNTRY_CHOICES,
        accounting_basis_choices=cutover_workflow.ACCOUNTING_BASIS_CHOICES,
        ar_ap_strategy_choices=AR_AP_STRATEGY_CHOICES,
        ar_ap_strategy_default=ar_ap_strategy_default,
        ar_ap_guidance=guidance_for_strategy(
            effective_strategy,
            country=(existing or {}).get("country"),
            accounting_basis=(existing or {}).get("accounting_basis"),
            clio_involved=bool((existing or {}).get("clio_involved")),
        ),
        **_workflow_stepper_context(firm_id),
    )


@app.route("/migration-checklist")
@login_required
def migration_checklist():
    """Render the per-firm migration checklist + next-step nudge."""
    user = current_user()
    cutover, items, next_step = _build_firm_checklist(user["firm_id"])
    firm_jobs = db.list_jobs_for_firm(user["firm_id"], limit=1)
    stages = customer_workflow.build_customer_stages(
        items, url_for=url_for, has_jobs=bool(firm_jobs),
    )
    current = customer_workflow.current_stage(stages)
    upload_ready = customer_workflow.upload_stage_ready_to_advance(items)
    upload_missing = customer_workflow.upload_stage_missing_reports(items)
    return render_template(
        "migration-checklist.html",
        cutover=cutover,
        checklist_items=items,
        next_step=next_step,
        guidance=cutover_workflow.GUIDANCE_TEXT,
        workflow_stages=[s.to_dict() for s in stages],
        workflow_current=current.to_dict() if current else None,
        workflow_progress=customer_workflow.progress_percent(stages),
        workflow_completed=customer_workflow.completed_count(stages),
        workflow_terms=customer_workflow.FRIENDLY_TERMS,
        upload_ready_to_advance=upload_ready,
        upload_missing_reports=upload_missing,
    )


@app.route("/match-accounts")
@login_required
def match_accounts_entry():
    """Step 3 entry point: send the user to the real match-accounts UI.

    Step 3 in our customer-facing workflow ("Match accounts") is
    implemented as per-job pages (``/jobs/<id>/connect-qbo`` and
    ``/jobs/<id>/account-mapping``). The migration-checklist needs a
    single, stable URL it can point at without having to know which
    job is the right one — this route does that dispatch:

      * If the firm has at least one general-ledger job AND any
        connected QuickBooks company, redirect to that job's
        account-mapping page (the actual mapping UI).
      * If the firm has a GL job but no QBO connection yet, redirect
        to that job's ``connect-qbo`` flow — that is the real
        prerequisite, and we want the user to hit it directly rather
        than land on a dead button.
      * If the firm has no GL job at all, send them back to the
        migration checklist with a clear flash explaining what's
        missing (transaction history upload).

    Production and demo deploys both use this route. AR/AP defaulting
    is the only demo-specific behavior in this PR.
    """
    user = current_user()
    firm_id = user["firm_id"]
    gl_jobs = _firm_latest_jobs_by_type(firm_id, REPORT_GENERAL_LEDGER, limit=500)
    if not gl_jobs:
        flash(
            "Upload your transaction history (general ledger) first — "
            "we need at least one general-ledger upload before we can "
            "match accounts to QuickBooks.",
            "error",
        )
        return redirect(url_for("migration_checklist"))

    primary = gl_jobs[0]
    primary_id = primary["id"]

    # Prefer a GL job that already has a QBO connection — that's where
    # the live account-mapping UI works without an extra connect step.
    for job in gl_jobs:
        if _get_qbo_connection(job["id"]):
            return redirect(url_for("account_mapping", job_id=job["id"]))

    # No QBO connection on any GL job yet. Send them to connect for the
    # most-recent GL job so the next click actually starts Step 3.
    return redirect(url_for("connect_qbo", job_id=primary_id))


@app.route("/firm/imports")
@login_required
def firm_imports():
    """Per-firm import-history summary. Read-only.

    Lists every import this firm has ever attempted (success and failure)
    across all its jobs, with per-row links back to the source job.
    Constrained to the logged-in user's firm so this works as a
    lightweight admin/operator view without needing a separate role
    model. We do not expose other firms' data.
    """
    user = current_user()
    firm_jobs = db.list_jobs_for_firm(user["firm_id"], limit=500)
    job_index = {j["id"]: j for j in firm_jobs}
    imports = history.get_history_for_jobs(job_index.keys())
    # Annotate each import with the parent job's company + last_error
    # summary if any, so the user sees a one-glance "what failed" view.
    for imp in imports:
        parent = job_index.get(imp["job_id"]) or {}
        imp["job_company"] = parent.get("company")
        imp["job_status"] = parent.get("status")
    return render_template(
        "firm-imports.html",
        firm_jobs=firm_jobs,
        imports=imports,
    )


def _resolve_entity_hints(qbo, payloads):
    """Replace `_pclaw_entity_hint` markers on JE lines with real Entity refs.

    For every line tagged Customer or Vendor, find or create the matching
    QBO entity, then add the `Entity` block QBO requires for A/R and A/P
    journal lines. Returns a list of (kind, name, id) tuples for the
    entities that were created (for UI feedback).
    """
    customer_cache = {}
    vendor_cache = {}
    created = []  # list of (kind, name, id)

    for payload in payloads:
        for line in payload.get("Line", []):
            hint = line.pop("_pclaw_entity_hint", None)
            if not hint:
                continue
            kind = hint["type"]
            name = hint["name"]

            if kind == "Customer":
                if name not in customer_cache:
                    existing = qbo.find_customer_by_name(name)
                    if existing:
                        customer_cache[name] = existing.get("Id")
                    else:
                        new_obj = qbo.create_customer(name)
                        customer_cache[name] = new_obj.get("Id")
                        created.append(("Customer", name, customer_cache[name]))
                entity_id = customer_cache[name]
            elif kind == "Vendor":
                if name not in vendor_cache:
                    existing = qbo.find_vendor_by_name(name)
                    if existing:
                        vendor_cache[name] = existing.get("Id")
                    else:
                        new_obj = qbo.create_vendor(name)
                        vendor_cache[name] = new_obj.get("Id")
                        created.append(("Vendor", name, vendor_cache[name]))
                entity_id = vendor_cache[name]
            else:
                continue

            line.setdefault("JournalEntryLineDetail", {})["Entity"] = {
                "Type": kind,
                "EntityRef": {"value": entity_id, "name": name},
            }

    return created


def _verify_import(job, qbo):
    """Re-query each created JournalEntry from QBO and compare totals.

    QBO API limitation: when posting a JE we get back the full JournalEntry
    JSON in the response. Re-fetching by Id gives us a fresh read confirming
    the entry is committed, the line totals are what we sent, and it has
    not been deleted/voided in between.

    The verification result is attached to job["verification"] for the UI.
    """
    created = job.get("qbo_results") or []
    summary = job.get("import_summary") or {}
    fetched = []
    qbo_debit_total = Decimal("0.00")
    qbo_credit_total = Decimal("0.00")

    for entry in created:
        je_id = entry.get("Id")
        if not je_id:
            continue
        # Use the query endpoint so 1 call works whether or not we know the JE shape.
        result = qbo.query(f"SELECT * FROM JournalEntry WHERE Id = '{je_id}'")
        items = result.get("QueryResponse", {}).get("JournalEntry", [])
        if not items:
            fetched.append({"Id": je_id, "found": False})
            continue
        je = items[0]
        fetched.append({
            "Id": je.get("Id"),
            "DocNumber": je.get("DocNumber"),
            "TxnDate": je.get("TxnDate"),
            "found": True,
        })
        for line in je.get("Line", []):
            detail = line.get("JournalEntryLineDetail") or {}
            posting = detail.get("PostingType")
            amount = Decimal(str(line.get("Amount") or "0"))
            if posting == "Debit":
                qbo_debit_total += amount
            elif posting == "Credit":
                qbo_credit_total += amount

    source_debit = Decimal(summary.get("source_debit_total") or "0")
    source_credit = Decimal(summary.get("source_credit_total") or "0")
    je_count_match = len(fetched) == summary.get("qbo_je_count", -1)
    debits_match = qbo_debit_total == source_debit
    credits_match = qbo_credit_total == source_credit
    not_found = [f["Id"] for f in fetched if not f["found"]]

    job["verification"] = {
        "status": "ok" if (je_count_match and debits_match and credits_match and not not_found) else "mismatch",
        "method": "QBO query JournalEntry by Id (response-confirmed)",
        "qbo_je_count": len(fetched),
        "qbo_debit_total": str(qbo_debit_total),
        "qbo_credit_total": str(qbo_credit_total),
        "source_debit_total": str(source_debit),
        "source_credit_total": str(source_credit),
        "je_count_match": je_count_match,
        "debits_match": debits_match,
        "credits_match": credits_match,
        "not_found_ids": not_found,
        "verified_at": datetime.utcnow().isoformat(),
    }


@app.route("/")
def index():
    """Public landing page for prospects; authenticated users go to dashboard."""
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/privacy")
def privacy():
    """Public privacy page. Linked from login/signup/dashboard footer.

    Content is a starter template — see docs/INTUIT_PRODUCTION_REVIEW.md for
    the legal-review caveat before pointing Intuit at this URL in production.
    """
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    """Public terms-of-service page (MVP / private beta starter copy)."""
    return render_template("terms.html")


@app.route("/support")
def support():
    """Public support / contact page including a security-reporting hint."""
    return render_template("support.html")


@app.route("/quickbooks-guide")
def quickbooks_guide():
    """Plain-English orientation to QuickBooks Online for new customers.

    Public so lawyers can read it before signing up or connecting QBO.
    Covers: what PCLaw Migrate posts, what does not happen automatically,
    where to find imported data inside QuickBooks Online, and a short
    after-import review checklist.
    """
    return render_template("quickbooks-guide.html")


# ---------------------------------------------------------------------------
# Onboarding / import-prep guide. Public page so customers can read it
# before signing in. The accompanying CSV downloads are also public — they
# are static demo data and do not reveal anything about real ledgers.
# ---------------------------------------------------------------------------

ONBOARDING_TEMPLATE_CSV = (
    "transaction_id,date,account_number,account_name,debit,credit,memo\n"
    "JE-0001,2026-04-01,1000,Operating Bank,1000.00,0.00,Opening operating cash\n"
    "JE-0001,2026-04-01,3000,Owner Equity,0.00,1000.00,Opening operating cash\n"
    "JE-0002,2026-04-02,1100,Accounts Receivable,2500.00,0.00,Sample matter invoice\n"
    "JE-0002,2026-04-02,4000,Legal Fees Revenue,0.00,2500.00,Sample matter invoice\n"
)


@app.route("/onboarding")
def onboarding():
    """Customer-facing onboarding & import-prep guide.

    Public so prospective customers can read it before signing up. Linked
    from the dashboard nav for logged-in firm admins.
    """
    return render_template("onboarding.html")


@app.route("/onboarding/template.csv")
def onboarding_template_csv():
    """Tiny, hand-curated CSV demonstrating the required columns.

    Used by the onboarding page download button. Plain CSV body so it
    opens in Excel / Numbers / Sheets cleanly. No customer data — these
    are obviously-fake transactions on month/year boundaries.
    """
    return Response(
        ONBOARDING_TEMPLATE_CSV,
        mimetype="text/csv",
        headers={
            "Content-Disposition": (
                "attachment; filename=pclaw_qbo_template.csv"
            ),
            "Cache-Control": "no-store",
        },
    )


@app.route("/onboarding/sample.csv")
def onboarding_sample_csv():
    """Larger sample GL covering trust, A/R, A/P, expenses.

    Reuses the bundled multi-transaction demo file from `test_data/` so
    the file customers see matches what the smoke-test suite exercises.
    Falls back to the small template if the demo file is missing.
    """
    sample_path = BASE_DIR / "test_data" / "02_general_ledger.csv"
    try:
        body = sample_path.read_text(encoding="utf-8")
    except OSError:
        body = ONBOARDING_TEMPLATE_CSV
    return Response(
        body,
        mimetype="text/csv",
        headers={
            "Content-Disposition": (
                "attachment; filename=pclaw_qbo_sample_general_ledger.csv"
            ),
            "Cache-Control": "no-store",
        },
    )


# Sample-download routes for the non-GL reports. These reuse the bundled
# demo data under test_data/ so the file customers download matches what
# the smoke-test suite exercises. They are public because the bundled
# files are obviously-fake demo data; switching to login_required would
# block prospects from previewing the format before signup.
_REPORT_SAMPLE_FILES = {
    "chart_of_accounts": ("test_data/01_chart_of_accounts.csv", "pclaw_qbo_sample_chart_of_accounts.csv"),
    "trial_balance": ("test_data/03_trial_balance.csv", "pclaw_qbo_sample_trial_balance.csv"),
    "trust_listing": ("test_data/05_trust_listing.csv", "pclaw_qbo_sample_trust_listing.csv"),
}


@app.route("/onboarding/sample/<report_type>.csv")
def onboarding_sample_report_csv(report_type):
    """Download a sample CSV for one of the supported report types.

    Only the report types listed in _REPORT_SAMPLE_FILES are served. Any
    other value returns 404 so an attacker can't read arbitrary files via
    this route.
    """
    entry = _REPORT_SAMPLE_FILES.get(report_type)
    if not entry:
        abort(404)
    rel_path, filename = entry
    sample_path = BASE_DIR / rel_path
    try:
        body = sample_path.read_text(encoding="utf-8")
    except OSError:
        abort(404)
    return Response(
        body,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "no-store",
        },
    )


@app.route("/favicon.ico")
def favicon_ico():
    """Serve the SVG favicon at /favicon.ico for legacy clients that
    request the well-known path. Modern browsers use the <link> tags in
    _base.html and load the SVG directly. We deliberately reuse the same
    SVG bytes (with the SVG mimetype) rather than ship a separate ICO so
    the asset stays under version control as a single editable file.
    """
    return app.send_static_file("favicon.svg")


@app.route("/healthz")
def healthz():
    """Lightweight, public health probe.

    Reports presence (not values) of critical config so Render and humans
    can confirm the deploy is healthy without leaking secrets. The detailed,
    human-readable readiness checklist is at /readiness and requires login.
    """
    body = {
        "status": "ok",
        "app_env": APP_ENV,
        "qbo_environment": QBO_ENVIRONMENT,
        "qbo_real_import": QBO_REAL_IMPORT,
        # Backward-compatible booleans (kept for existing scrapers).
        "secret_key_set": bool(os.environ.get("SECRET_KEY") or os.environ.get("APP_SECRET")),
        "encryption_key_set": bool(os.environ.get("ENCRYPTION_KEY")),
        "qbo_client_id_set": QBO_CLIENT_ID != "your-client-id-here" and bool(QBO_CLIENT_ID),
        "qbo_redirect_uri_set": bool(QBO_REDIRECT_URI) and not QBO_REDIRECT_URI.startswith("http://localhost"),
        "branding_support_email_set": not branding.is_placeholder_email(branding.SUPPORT_EMAIL),
        "branding_security_email_set": not branding.is_placeholder_email(branding.SECURITY_EMAIL),
        "demo_mode_enabled": demo_mode.is_demo_mode_enabled(),
    }
    # Merge in the structured go-live readiness booleans. Only booleans
    # are exposed here; hints + details stay behind login at /readiness.
    body["readiness"] = readiness.healthz_booleans(
        request_host=request.host, request_scheme=request.scheme,
    )
    body["ready_for_go_live"] = readiness.overall_ready(
        readiness.collect_checks(request_host=request.host, request_scheme=request.scheme)
    )
    return jsonify(body), 200


@app.route("/readiness")
@login_required
def readiness_page():
    """Protected, human-readable go-live readiness checklist.

    Same source of truth as /healthz, but includes remediation hints and
    visual grouping so an operator can fix red items before flipping the
    deploy live for real customers.
    """
    checks = readiness.collect_checks(
        request_host=request.host, request_scheme=request.scheme,
    )
    required = [c for c in checks if c.severity == readiness.SEVERITY_REQUIRED]
    recommended = [c for c in checks if c.severity == readiness.SEVERITY_RECOMMENDED]
    info = [c for c in checks if c.severity == readiness.SEVERITY_INFO]
    return render_template(
        "readiness.html",
        checks=checks,
        required=required,
        recommended=recommended,
        info=info,
        overall_ready=readiness.overall_ready(checks),
        required_failing=[c for c in required if not c.ok],
        recommended_failing=[c for c in recommended if not c.ok],
        public_url=os.environ.get("PUBLIC_APP_URL", "").strip(),
        request_host=request.host,
        request_scheme=request.scheme,
    )


def _process_uploaded_csv(
    file_storage,
    company: str,
    user_email: str,
    user: dict,
    user_picked_report_type: Optional[str] = None,
):
    """Run the existing single-file PCLaw upload pipeline.

    Returns a dict with keys:

      ok          (bool)
      job_id      (str | None)
      report_type (str | None)
      detected    (str | None)   — what the auto-detector decided
      message     (str)          — flash-style status text
      category    ("success"|"error"|"info")
      filename    (str)

    Used by both the legacy ``/upload`` route (one file at a time, with
    flash messages) and the newer ``/upload/bulk`` route (many files at
    once, aggregated into a review summary).

    The DB / encryption / preflight / persistence behaviour is identical
    to the original inline route body — this is a refactor extraction,
    not a behaviour change.
    """
    if not file_storage or not file_storage.filename:
        return {
            "ok": False,
            "job_id": None,
            "report_type": None,
            "detected": None,
            "filename": "",
            "message": "No file was attached.",
            "category": "error",
        }
    safe_name = secure_filename(file_storage.filename)
    if not safe_name.lower().endswith(".csv"):
        return {
            "ok": False,
            "job_id": None,
            "report_type": None,
            "detected": None,
            "filename": safe_name or file_storage.filename or "",
            "message": (
                "Only .csv files exported from PCLaw are supported. "
                "Re-export the report as CSV and try again."
            ),
            "category": "error",
        }
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    job_suffix = secrets.token_urlsafe(12)
    job_id = f"job_{timestamp}_{job_suffix}"
    fs_prefix = f"{timestamp}_{job_suffix}"

    upload_path = UPLOAD_DIR / f"{fs_prefix}_{safe_name}"
    file_storage.save(upload_path)
    file_sha256 = sha256_of_file(upload_path)
    encrypted_path = UPLOAD_DIR / f"{fs_prefix}_{safe_name}.enc"
    encrypt_file(upload_path, encrypted_path)
    upload_path.unlink()

    jobs[job_id] = {
        "id": job_id,
        "firm_id": user["firm_id"],
        "user_id": user["id"],
        "company": company,
        "email": user_email,
        "source_file": f"{fs_prefix}_{safe_name}",
        "encrypted_file": encrypted_path.name,
        "file_sha256": file_sha256,
        "status": "File uploaded (encrypted)",
        "created_at": datetime.utcnow().isoformat(),
        "summary": {},
        "qbo_connected": False,
        "report_type": user_picked_report_type or REPORT_GENERAL_LEDGER,
        "report_type_user_picked": user_picked_report_type,
    }
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company=company, source_file=f"{fs_prefix}_{safe_name}",
        encrypted_file=encrypted_path.name, file_sha256=file_sha256,
        status="File uploaded (encrypted)",
    )
    _audit("upload", target_type="job", target_id=job_id,
           details=f"{company} / {safe_name}")

    temp_path = UPLOAD_DIR / f"{fs_prefix}_temp.csv"
    decrypt_file(encrypted_path, temp_path)

    message = ""
    category = "success"
    detected = None
    effective_report_type: Optional[str] = None
    try:
        with temp_path.open("r", newline="", encoding="utf-8-sig") as _f:
            _reader = _csv.DictReader(_f)
            _fieldnames = list(_reader.fieldnames or [])
            _is_gl = is_gl_format(_fieldnames)
            _gl_rows = list(_reader) if _is_gl else []
            _row_count = len(_gl_rows) if _is_gl else sum(
                1 for _ in _csv.DictReader(temp_path.open("r", newline="", encoding="utf-8-sig"))
            )

        detected = detect_report_type(_fieldnames)
        if user_picked_report_type:
            effective_report_type = user_picked_report_type
        elif _is_gl:
            effective_report_type = REPORT_GENERAL_LEDGER
        elif detected:
            effective_report_type = detected
        else:
            effective_report_type = REPORT_GENERAL_LEDGER
        jobs[job_id]["report_type"] = effective_report_type
        jobs[job_id]["report_type_detected"] = detected
        jobs[job_id]["report_type_label"] = REPORT_LABELS[effective_report_type]
        jobs[job_id]["qbo_behavior"] = REPORT_QBO_BEHAVIOR[effective_report_type]

        if effective_report_type == REPORT_GENERAL_LEDGER and _is_gl:
            preflight = build_preflight_summary(_gl_rows, _fieldnames)
            preflight["report_type"] = REPORT_GENERAL_LEDGER
            preflight["report_label"] = REPORT_LABELS[REPORT_GENERAL_LEDGER]
            jobs[job_id]["status"] = "Ready for QBO connection"
            jobs[job_id]["summary"] = {
                "row_count": _row_count,
                "format": "GL (transaction_id)",
                "balanced": preflight["balanced"],
                "report_type": REPORT_GENERAL_LEDGER,
            }
            jobs[job_id]["preflight"] = preflight
            if preflight["ready"]:
                message = (
                    "PCLaw GL file accepted. Review the preflight checklist, "
                    "then connect QuickBooks to continue."
                )
                category = "success"
            else:
                message = (
                    "PCLaw GL file uploaded with warnings. Review the "
                    "preflight checklist on the job page before connecting "
                    "QuickBooks."
                )
                category = "error"
        elif effective_report_type == REPORT_CHART_OF_ACCOUNTS:
            coa_rows, _fn, missing = parse_chart_of_accounts(temp_path)
            preflight = build_coa_preflight(coa_rows, _fn, missing)
            jobs[job_id]["status"] = (
                "Chart of Accounts ready for QBO preview"
                if preflight["ready"]
                else "Chart of Accounts uploaded with warnings"
            )
            jobs[job_id]["summary"] = {
                "row_count": preflight["account_count"],
                "format": REPORT_LABELS[REPORT_CHART_OF_ACCOUNTS],
                "report_type": REPORT_CHART_OF_ACCOUNTS,
            }
            jobs[job_id]["preflight"] = preflight
            jobs[job_id]["parsed_coa"] = coa_rows
            message = (
                "Chart of Accounts file accepted. Connect QuickBooks to "
                "see which accounts already exist and which would be "
                "created. No QBO writes happen until you confirm."
            )
            category = "success" if preflight["ready"] else "error"
        elif effective_report_type == REPORT_TRIAL_BALANCE:
            tb_rows, _fn, missing = parse_trial_balance(temp_path)
            preflight = build_trial_balance_preflight(tb_rows, _fn, missing)
            jobs[job_id]["status"] = (
                "Trial Balance validated"
                if preflight["ready"]
                else "Trial Balance uploaded with warnings"
            )
            jobs[job_id]["summary"] = {
                "row_count": preflight["account_count"],
                "format": REPORT_LABELS[REPORT_TRIAL_BALANCE],
                "report_type": REPORT_TRIAL_BALANCE,
                "balanced": preflight["balanced"],
            }
            jobs[job_id]["preflight"] = preflight
            jobs[job_id]["parsed_trial_balance"] = tb_rows
            message = (
                "Trial Balance accepted. This report is parsed for "
                "validation and reconciliation only — no QuickBooks "
                "writes are performed for Trial Balance uploads."
            )
            category = "success" if preflight["ready"] else "error"
        elif effective_report_type == REPORT_TRUST_LISTING:
            trust_rows, _fn, missing = parse_trust_listing(temp_path)
            preflight = build_trust_listing_preflight(trust_rows, _fn, missing)
            jobs[job_id]["status"] = (
                "Trust Listing validated"
                if preflight["ready"]
                else "Trust Listing uploaded with warnings"
            )
            jobs[job_id]["summary"] = {
                "row_count": preflight["row_count"],
                "format": REPORT_LABELS[REPORT_TRUST_LISTING],
                "report_type": REPORT_TRUST_LISTING,
            }
            jobs[job_id]["preflight"] = preflight
            jobs[job_id]["parsed_trust_listing"] = trust_rows
            message = (
                "Trust Listing accepted. This report is parsed for "
                "validation and reconciliation only — no QuickBooks "
                "writes are performed for Trust Listing uploads."
            )
            category = "success" if preflight["ready"] else "error"
        else:
            rows = parse_pclaw_csv(temp_path)
            out_path = OUTPUT_DIR / f"{fs_prefix}_qbo_import.csv"
            summary = export_qbo_csv(rows, out_path)
            encrypted_out = OUTPUT_DIR / f"{fs_prefix}_qbo_import.csv.enc"
            encrypt_file(out_path, encrypted_out)
            out_path.unlink()
            jobs[job_id]["status"] = "Ready for QBO connection"
            jobs[job_id]["summary"] = summary
            jobs[job_id]["output_file"] = f"{fs_prefix}_qbo_import.csv"
            jobs[job_id]["encrypted_output"] = encrypted_out.name
            message = (
                "Migration package prepared successfully. "
                "Connect to QuickBooks to complete."
            )
            category = "success"
    except Exception as e:  # noqa: BLE001
        headline, action = friendly_validation_message(e)
        jobs[job_id]["status"] = f"Error: {headline}"
        jobs[job_id]["last_validation_error"] = {
            "headline": headline,
            "action": action,
        }
        message = f"{headline} {action}"
        category = "error"
    finally:
        temp_path.unlink(missing_ok=True)

    _save_job(job_id)
    return {
        "ok": category != "error",
        "job_id": job_id,
        "report_type": effective_report_type,
        "detected": detected,
        "filename": safe_name,
        "message": message,
        "category": category,
    }


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    user = current_user()
    company = request.form.get("company_name", "").strip()
    user_email = request.form.get("email", "").strip() or user["email"]
    file = request.files.get("ledger_file")
    # report_type is optional for backward compatibility. Missing / blank /
    # "auto" all mean "detect from headers (and fall back to GL behavior)".
    raw_report_type = (request.form.get("report_type") or "").strip().lower()
    user_picked_report_type = raw_report_type if is_valid_report_type(raw_report_type) else None

    if not company or not file:
        flash("Company name and PCLaw export file are required.", "error")
        return redirect(url_for("dashboard"))

    safe_name = secure_filename(file.filename)
    # Reject obviously wrong file types early. We only accept .csv from
    # PCLaw — refusing .exe / .zip / .xlsx / .pdf at the gate is a cheap
    # safety win and gives a clearer error than a CSV parse failure.
    result = _process_uploaded_csv(
        file_storage=file,
        company=company,
        user_email=user_email,
        user=user,
        user_picked_report_type=user_picked_report_type,
    )
    if result["message"]:
        flash(result["message"], result["category"])
    if not result["job_id"]:
        return redirect(url_for("dashboard"))
    return redirect(url_for("job_detail", job_id=result["job_id"]))


# Hard cap on how many files we'll accept in a single bulk submission.
# Even at 25 MB per file the worst case is bounded by MAX_CONTENT_LENGTH
# on the whole request, but a fixed file-count cap protects against
# pathological inputs (e.g. 500 empty CSVs).
_BULK_MAX_FILES = 12


def _classify_and_process_files(files, *, company, user_email, user):
    """Classify and persist a list of uploaded PCLaw CSVs.

    Shared by the initial /upload/bulk submission and the
    "Add more reports" /upload/bulk/<id>/append flow so the two paths
    stay byte-for-byte identical in how they:

      - detect non-CSV files
      - run classify_csv on each file
      - hand the file off to _process_uploaded_csv to encrypt + persist
      - record a per-file entry (with job_id, status, warning, etc.)

    Returns a list of per-file dicts in the same shape the bulk review
    template renders (i.e. what was previously inlined into upload_bulk).
    Caller is responsible for filtering empties and enforcing
    _BULK_MAX_FILES before calling.
    """
    aggregated: list[dict] = []
    for f in files:
        original_name = f.filename or ""
        safe_name = secure_filename(original_name)
        if not safe_name.lower().endswith(".csv"):
            aggregated.append({
                "filename": original_name,
                "report_type": None,
                "report_label": "",
                "confidence": bulk_upload.CONFIDENCE_NONE,
                "status": bulk_upload.STATUS_REJECTED,
                "reason": (
                    "Only .csv files exported from PCLaw are supported. "
                    "Re-export this report as CSV."
                ),
                "warning": "",
                "job_id": None,
                "job_status": None,
            })
            continue

        sniff_path = UPLOAD_DIR / (
            f"bulk_sniff_{secrets.token_urlsafe(8)}_{safe_name}"
        )
        try:
            f.save(sniff_path)
            classification = bulk_upload.classify_csv(sniff_path, safe_name)
        except Exception as exc:  # noqa: BLE001
            classification = bulk_upload.ClassificationResult(
                filename=safe_name,
                report_type=None,
                status=bulk_upload.STATUS_UNREADABLE,
                confidence=bulk_upload.CONFIDENCE_NONE,
                reason=f"Could not read the file ({type(exc).__name__}).",
            )
        finally:
            sniff_path.unlink(missing_ok=True)
            try:
                f.stream.seek(0)
            except Exception:
                pass

        picked = (
            classification.report_type
            if classification.status == bulk_upload.STATUS_CATEGORIZED
            and classification.confidence in (
                bulk_upload.CONFIDENCE_HIGH,
                bulk_upload.CONFIDENCE_MEDIUM,
            )
            else None
        )
        try:
            processed = _process_uploaded_csv(
                file_storage=f,
                company=company,
                user_email=user_email,
                user=user,
                user_picked_report_type=picked,
            )
        except Exception as exc:  # noqa: BLE001
            processed = {
                "ok": False, "job_id": None, "report_type": None,
                "detected": None, "filename": safe_name,
                "message": f"Could not save {safe_name}: {type(exc).__name__}",
                "category": "error",
            }

        entry = classification.to_dict()
        entry["job_id"] = processed.get("job_id")
        entry["job_status"] = None
        entry["report_type"] = processed.get("report_type") or entry.get("report_type")
        if entry["report_type"]:
            entry["report_label"] = REPORT_LABELS.get(
                entry["report_type"], entry.get("report_label") or ""
            )
        if processed.get("job_id"):
            saved_job = jobs.get(processed["job_id"]) or {}
            entry["job_status"] = saved_job.get("status")
            if (
                processed.get("report_type")
                and classification.report_type
                and processed["report_type"] != classification.report_type
                and entry["status"] == bulk_upload.STATUS_CATEGORIZED
            ):
                entry["status"] = bulk_upload.STATUS_NEEDS_REVIEW
                entry["warning"] = (
                    "Auto-detection and the parser disagreed on this "
                    "file — please confirm the report type below."
                )
        if processed.get("category") == "error":
            entry["warning"] = (
                entry.get("warning") or processed.get("message") or ""
            )
            if entry["status"] == bulk_upload.STATUS_CATEGORIZED:
                entry["status"] = bulk_upload.STATUS_NEEDS_REVIEW
        aggregated.append(entry)
    return aggregated


@app.route("/upload/bulk", methods=["POST"])
@login_required
def upload_bulk():
    """Bulk upload: accept multiple PCLaw CSV files in a single
    submission and auto-classify each one.

    The flow:
      1. For each uploaded file we save it to a temp path, run
         ``bulk_upload.classify_csv`` against the temp copy, and then
         hand it off to ``_process_uploaded_csv`` with the classifier's
         best guess as ``user_picked_report_type`` (unless the customer
         explicitly chose "auto", in which case we let the legacy
         per-file detector decide). The per-file pipeline encrypts the
         file, builds the preflight, and persists the job — exactly the
         same path the single-file ``/upload`` route uses.
      2. The aggregate result (one entry per file) is stashed in the
         ``bulk_uploads`` dict so the review screen can render it.
      3. We redirect to the review screen so the customer sees what
         was identified, what needs review, and what is still missing.

    Nothing here imports into QBO. Each per-file job sits in the same
    "uploaded / awaiting review" state as a single-file upload, and the
    existing review/confirmation/import gates remain in place.
    """
    user = current_user()
    company = request.form.get("company_name", "").strip()
    user_email = request.form.get("email", "").strip() or user["email"]
    files = request.files.getlist("ledger_files") or []
    # Filter out empty form fields (browsers send an empty FileStorage
    # when an input has no selection).
    files = [f for f in files if f and (f.filename or "").strip()]

    if not company:
        flash("Company name is required for bulk upload.", "error")
        return redirect(url_for("dashboard") + "#intake")
    if not files:
        flash(
            "Pick one or more PCLaw CSV exports to upload. "
            "Hold Ctrl/Cmd to select multiple files at once.",
            "error",
        )
        return redirect(url_for("dashboard") + "#intake")
    if len(files) > _BULK_MAX_FILES:
        flash(
            f"Bulk upload accepts up to {_BULK_MAX_FILES} files at a time. "
            f"Upload the rest in a second batch.",
            "error",
        )
        return redirect(url_for("dashboard") + "#intake")

    # First pass: peek at each file so we can classify before we hand
    # it off to the persistence pipeline. We don't trust filenames
    # alone; ``bulk_upload.classify_csv`` combines headers, filename
    # hints, and content patterns to choose a report type and a
    # confidence label.
    bulk_id = f"bulk_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_urlsafe(8)}"
    aggregated = _classify_and_process_files(
        files, company=company, user_email=user_email, user=user,
    )

    # Reconstruct ClassificationResult objects so the collision logic
    # can run on a uniform shape; resolve_collisions mutates in place.
    cr_objects: list[bulk_upload.ClassificationResult] = []
    for e in aggregated:
        cr_objects.append(bulk_upload.ClassificationResult(
            filename=e.get("filename") or "",
            report_type=e.get("report_type"),
            report_label=e.get("report_label") or "",
            confidence=e.get("confidence") or bulk_upload.CONFIDENCE_NONE,
            status=e.get("status") or bulk_upload.STATUS_NEEDS_REVIEW,
            reason=e.get("reason") or "",
            warning=e.get("warning") or "",
        ))
    bulk_upload.resolve_collisions(cr_objects)
    for original, cr in zip(aggregated, cr_objects):
        original["status"] = cr.status
        original["warning"] = cr.warning

    summary = bulk_upload.summarize_bulk(cr_objects)
    bulk_uploads[bulk_id] = {
        "id": bulk_id,
        "firm_id": user["firm_id"],
        "user_id": user["id"],
        "company": company,
        "email": user_email,
        "created_at": datetime.utcnow().isoformat(),
        "results": aggregated,
        "summary": summary,
    }
    _audit(
        "bulk_upload",
        target_type="bulk_upload",
        target_id=bulk_id,
        details=(
            f"{company} / {len(files)} files / "
            f"{summary['categorized']} categorized / "
            f"{summary['needs_review']} needs review"
        ),
    )

    if summary["categorized"]:
        flash(
            f"Uploaded {len(files)} file(s). We identified "
            f"{summary['categorized']} report(s) automatically. "
            "Review the summary below.",
            "success" if not summary["needs_review"] else "info",
        )
    else:
        flash(
            "We couldn't auto-identify any of the uploaded files. "
            "Pick a report type for each and continue, or re-upload "
            "with files renamed to include their report type.",
            "error",
        )
    return redirect(url_for("bulk_upload_review", bulk_id=bulk_id))


def _bulk_or_404(bulk_id: str):
    """Return the bulk-upload record if it belongs to the current
    user's firm, else 404."""
    user = current_user()
    if not user:
        abort(401)
    bulk = bulk_uploads.get(bulk_id)
    if not bulk:
        abort(404)
    if bulk.get("firm_id") != user["firm_id"]:
        abort(404)
    return bulk, user


@app.route("/upload/bulk/<bulk_id>", methods=["GET"])
@login_required
def bulk_upload_review(bulk_id):
    """Per-firm review screen for a bulk upload. Shows per-file
    detection results and what is still missing for the checklist."""
    bulk, user = _bulk_or_404(bulk_id)
    # Refresh summary in case manual corrections were applied via the
    # POST handler since the record was created.
    cr_objects = [bulk_upload.ClassificationResult(
        filename=e.get("filename") or "",
        report_type=e.get("report_type"),
        report_label=e.get("report_label") or "",
        confidence=e.get("confidence") or bulk_upload.CONFIDENCE_NONE,
        status=e.get("status") or bulk_upload.STATUS_NEEDS_REVIEW,
        reason=e.get("reason") or "",
        warning=e.get("warning") or "",
    ) for e in bulk["results"]]
    summary = bulk_upload.summarize_bulk(cr_objects)
    bulk["summary"] = summary
    missing_labels = [
        REPORT_LABELS.get(rt, rt) for rt in summary["missing_required"]
    ]
    return render_template(
        "bulk-upload-review.html",
        bulk=bulk,
        results=bulk["results"],
        summary=summary,
        missing_required_labels=missing_labels,
        report_label_map=REPORT_LABELS,
        report_types=list(REPORT_TYPES),
        confidence_labels={
            bulk_upload.CONFIDENCE_HIGH: "High confidence",
            bulk_upload.CONFIDENCE_MEDIUM: "Medium confidence",
            bulk_upload.CONFIDENCE_LOW: "Low confidence",
            bulk_upload.CONFIDENCE_NONE: "Not identified",
        },
        status_labels={
            bulk_upload.STATUS_CATEGORIZED: "Categorized",
            bulk_upload.STATUS_NEEDS_REVIEW: "Needs review",
            bulk_upload.STATUS_DUPLICATE: "Duplicate — pick one",
            bulk_upload.STATUS_UNREADABLE: "Could not read",
            bulk_upload.STATUS_REJECTED: "Rejected",
        },
        **_workflow_stepper_context(user["firm_id"]),
    )


@app.route("/upload/bulk/<bulk_id>/correct", methods=["POST"])
@login_required
def bulk_upload_correct(bulk_id):
    """Allow the customer to manually pick the report type for a file
    that the classifier flagged as needs_review / duplicate.

    For categorized jobs (those with a job_id), we update the job's
    report_type *only when the underlying file has not yet been written
    to QBO*. Trial Balance / Trust Listing / COA jobs are read-only on
    QBO; GL jobs that haven't been imported are also safe to retype.
    """
    bulk, user = _bulk_or_404(bulk_id)
    filename = (request.form.get("filename") or "").strip()
    new_rt = (request.form.get("report_type") or "").strip().lower()
    if not bulk_upload.is_acceptable_override(new_rt):
        flash("Unknown report type.", "error")
        return redirect(url_for("bulk_upload_review", bulk_id=bulk_id))
    target = None
    for entry in bulk["results"]:
        if entry.get("filename") == filename:
            target = entry
            break
    if target is None:
        flash("That file is no longer in this bulk upload.", "error")
        return redirect(url_for("bulk_upload_review", bulk_id=bulk_id))

    job_id = target.get("job_id")
    if job_id:
        job = jobs.get(job_id)
        if job and job.get("firm_id") == user["firm_id"]:
            current_status = (job.get("status") or "").lower()
            if "imported" in current_status and "not" not in current_status:
                flash(
                    "This file's job has already been imported to "
                    "QuickBooks. Open the job page to make corrections.",
                    "error",
                )
                return redirect(url_for("bulk_upload_review", bulk_id=bulk_id))
            if new_rt:
                # Re-parse the encrypted upload under the new report
                # type so the preflight panel reflects the customer's
                # choice. We reuse the existing job slot.
                job["report_type"] = new_rt
                job["report_type_user_picked"] = new_rt
                job["report_type_label"] = REPORT_LABELS.get(
                    new_rt, REPORT_LABELS[REPORT_GENERAL_LEDGER]
                )
                job["qbo_behavior"] = REPORT_QBO_BEHAVIOR.get(
                    new_rt, "importable"
                )
                # Note: we don't re-run the parser here to keep this
                # operation cheap; the job-detail page already re-parses
                # on demand via ``_reparse_report_rows`` when needed.
                _save_job(job_id)
    if new_rt:
        target["report_type"] = new_rt
        target["report_label"] = REPORT_LABELS.get(new_rt, "")
        target["status"] = bulk_upload.STATUS_CATEGORIZED
        target["confidence"] = bulk_upload.CONFIDENCE_HIGH
        target["reason"] = "Manually confirmed by the customer."
        target["warning"] = ""
    _audit(
        "bulk_upload_correct",
        target_type="bulk_upload",
        target_id=bulk_id,
        details=f"{filename} -> {new_rt or '(cleared)'}",
    )
    flash("Report type updated.", "success")
    return redirect(url_for("bulk_upload_review", bulk_id=bulk_id))


@app.route("/upload/bulk/<bulk_id>/append", methods=["POST"])
@login_required
def bulk_upload_append(bulk_id):
    """Append additional PCLaw CSV files to an existing bulk upload.

    Customers frequently realize after the initial bulk submission that
    they forgot a report (e.g. the trust listing, or last quarter's
    transaction history). Without this route they have to restart the
    workflow with a new firm / company name, which loses the existing
    review state. Append reuses the existing bulk record so the new
    files are categorized into the same workflow, the checklist /
    summary stays a single coherent thing, and any duplicate-import
    safeguards still fire because each file still lands as its own
    job in the per-file pipeline.

    Security & invariants:
      - The bulk record must belong to the current user's firm
        (enforced by `_bulk_or_404`).
      - Each appended file goes through the same classify + encrypt +
        persist pipeline as the initial submission, so duplicate
        protection, typed-import confirmation, and QBO posting
        safeguards continue to apply per-file. Nothing here imports
        to QuickBooks.
      - Non-CSV files are rejected with a clear reason just like the
        initial flow.
    """
    bulk, user = _bulk_or_404(bulk_id)
    files = request.files.getlist("ledger_files") or []
    files = [f for f in files if f and (f.filename or "").strip()]
    if not files:
        flash(
            "Pick one or more PCLaw CSV exports to add to this migration.",
            "error",
        )
        return redirect(url_for("bulk_upload_review", bulk_id=bulk_id))
    # Cap the *appended* batch at _BULK_MAX_FILES so the per-request
    # work stays bounded. The cumulative total across multiple appends
    # is intentionally unbounded — a firm with 20 reports should be
    # able to upload them in two batches without restarting.
    if len(files) > _BULK_MAX_FILES:
        flash(
            f"Bulk upload accepts up to {_BULK_MAX_FILES} files at a time. "
            "Upload the rest in another batch.",
            "error",
        )
        return redirect(url_for("bulk_upload_review", bulk_id=bulk_id))

    new_entries = _classify_and_process_files(
        files,
        company=bulk["company"],
        user_email=bulk.get("email") or user["email"],
        user=user,
    )

    # Merge into the bulk record. Duplicate-collision logic must run on
    # the *combined* set so a newly uploaded duplicate of a previously
    # uploaded file still gets flagged for review rather than silently
    # overwriting.
    bulk["results"].extend(new_entries)
    cr_objects = [bulk_upload.ClassificationResult(
        filename=e.get("filename") or "",
        report_type=e.get("report_type"),
        report_label=e.get("report_label") or "",
        confidence=e.get("confidence") or bulk_upload.CONFIDENCE_NONE,
        status=e.get("status") or bulk_upload.STATUS_NEEDS_REVIEW,
        reason=e.get("reason") or "",
        warning=e.get("warning") or "",
    ) for e in bulk["results"]]
    bulk_upload.resolve_collisions(cr_objects)
    for original, cr in zip(bulk["results"], cr_objects):
        original["status"] = cr.status
        original["warning"] = cr.warning
    bulk["summary"] = bulk_upload.summarize_bulk(cr_objects)

    added_categorized = sum(
        1 for e in new_entries
        if e.get("status") == bulk_upload.STATUS_CATEGORIZED
    )
    _audit(
        "bulk_upload_append",
        target_type="bulk_upload",
        target_id=bulk_id,
        details=(
            f"{bulk.get('company')} / +{len(files)} files / "
            f"{added_categorized} newly categorized"
        ),
    )
    flash(
        f"Added {len(files)} file(s) to this migration. "
        f"{added_categorized} were identified automatically. "
        "Review the updated summary below.",
        "success" if added_categorized else "info",
    )
    return redirect(url_for("bulk_upload_review", bulk_id=bulk_id))


@app.route("/upload/bulk/<bulk_id>/add", methods=["GET"])
@login_required
def bulk_upload_add(bulk_id):
    """GET-friendly redirect that scrolls the review page to the
    'Add more reports' form. Linked from the "Upload missing files"
    CTA on the bulk-upload review screen and the migration checklist.
    """
    _bulk_or_404(bulk_id)
    return redirect(url_for("bulk_upload_review", bulk_id=bulk_id) + "#add-more-reports")


@app.route("/jobs/<job_id>")
@login_required
def job_detail(job_id):
    job, _user = _job_or_403(job_id)
    qbo_conn = _get_qbo_connection(job_id) or {}
    job_history = history.get_history_for_job(job_id)

    # Surface counts the preflight panel renders. We compute these here
    # rather than on the job dict so they always reflect the current
    # mapping / connection state, even for jobs created before preflight
    # existed.
    preflight = job.get("preflight") or {}
    unmapped_count = len(job.get("unmapped_accounts") or [])
    qbo_connection_status = "connected" if job.get("qbo_connected") else "not_connected"
    qbo_env_status = (
        "production" if (QBO_ENVIRONMENT or "").lower() == "production" else "sandbox"
    )

    report_type = job.get("report_type") or REPORT_GENERAL_LEDGER
    return render_template(
        "job-detail.html",
        job=job,
        qbo_connection=qbo_conn,
        qbo_configured=QBO_CLIENT_ID != "your-client-id-here",
        qbo_real_import=QBO_REAL_IMPORT,
        job_history=job_history,
        preflight=preflight,
        unmapped_count=unmapped_count,
        qbo_connection_status=qbo_connection_status,
        qbo_env_status=qbo_env_status,
        report_type=report_type,
        report_label=REPORT_LABELS.get(report_type, REPORT_LABELS[REPORT_GENERAL_LEDGER]),
        qbo_behavior=REPORT_QBO_BEHAVIOR.get(report_type, "importable"),
        **_workflow_stepper_context(_user["firm_id"]),
    )


@app.route("/jobs/<job_id>/coa-preview")
@login_required
def coa_preview(job_id):
    """Render a non-destructive Chart of Accounts dry-run preview.

    Compares the parsed COA rows against the connected QuickBooks
    company's Account list and shows which accounts already exist
    (matched on AcctNum / Name) and which would be created. No QBO
    write endpoint is called.

    Available only for jobs uploaded as report_type=chart_of_accounts.
    Other report types redirect back to the job detail with a flash —
    GL has its own mapping/import flow, and Trial Balance / Trust
    Listing are read-only.
    """
    job, user = _job_or_403(job_id)
    if (job.get("report_type") or REPORT_GENERAL_LEDGER) != REPORT_CHART_OF_ACCOUNTS:
        flash(
            "The Chart of Accounts preview is only available for jobs "
            "uploaded as a Chart of Accounts report.",
            "info",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    coa_rows = job.get("parsed_coa")
    if not coa_rows:
        # Re-parse from the encrypted upload when the in-memory cache lost
        # it (job rehydrated from DB).
        coa_rows = _reparse_report_rows(job, REPORT_CHART_OF_ACCOUNTS)

    qbo, qbo_conn = _get_qbo_client(job_id, user)
    preview = None
    qbo_error = None
    if not qbo:
        qbo_error = (
            "Connect QuickBooks first to run the Chart of Accounts preview. "
            "Until then, the parsed COA is shown without any QBO comparison."
        )
        qbo_accounts = {"QueryResponse": {"Account": []}}
    else:
        try:
            qbo_accounts = qbo.get_accounts()
        except Exception as exc:  # noqa: BLE001
            qbo_error = (
                "Could not fetch the QuickBooks Chart of Accounts. "
                "The COA preview will retry on next page load."
            )
            qbo_accounts = {"QueryResponse": {"Account": []}}
            _audit(
                "coa_preview_qbo_query_failed",
                target_type="job", target_id=job_id, details=str(exc)[:200],
            )
    overrides = dict(job.get("coa_type_overrides") or {})
    # Apply overrides to the COA rows for preview so the operator sees the
    # corrected types reflected in the preview / type-mapping output.
    coa_rows_for_preview = []
    for r in (coa_rows or []):
        num = (r.get("account_number") or "").strip()
        nl = (r.get("account_name") or "").strip().lower()
        ov = overrides.get(num) or overrides.get(nl)
        if ov:
            coa_rows_for_preview.append({
                **r,
                "account_type": ov.get("account_type") or r.get("account_type"),
                "detail_type": ov.get("detail_type") or r.get("detail_type"),
            })
        else:
            coa_rows_for_preview.append(r)
    preview = build_coa_dry_run_preview(coa_rows_for_preview, qbo_accounts)
    hierarchy_plan = build_hierarchy_plan(coa_rows_for_preview, qbo_accounts)
    # Build the create plan so the preview page can render per-row decisions
    # (blocked / warn / ok) — this is what the manual-override form keys
    # off, and what surfaces the AR/AP misclassification block from
    # coa_apply.map_pclaw_account_to_qbo_type.
    create_plan = build_create_plan(coa_rows_for_preview, preview,
                                    type_overrides=overrides)
    _audit("coa_preview_view", target_type="job", target_id=job_id,
           details=(
               f"matched={preview['matched_count']} "
               f"would_create={preview['would_create_count']} "
               f"hierarchy_blocked={len(hierarchy_plan.blocked)} "
               f"blocked={len(create_plan.blocked)} "
               f"overrides={len(overrides)}"
           ))
    return render_template(
        "coa-preview.html",
        job=job,
        preview=preview,
        hierarchy=hierarchy_plan.to_dict(),
        plan=create_plan.to_dict(),
        coa_type_overrides=overrides,
        coa_override_account_types=COA_OVERRIDE_ACCOUNT_TYPES,
        qbo_error=qbo_error,
        qbo_connection=qbo_conn or {},
        report_label=REPORT_LABELS[REPORT_CHART_OF_ACCOUNTS],
    )


COA_CREATE_CONFIRMATION_PHRASE = "CREATE ACCOUNTS"


def _load_coa_state(job_id):
    """Return (job, user, coa_rows, qbo, qbo_conn) or short-circuit redirect.

    The COA confirm + apply routes share the same setup: verify the job
    is a COA job, re-parse the upload if the in-memory cache lost it,
    and require a QBO connection. Returns a tuple of (state_dict,
    redirect_response). Exactly one of those is non-None.
    """
    job, user = _job_or_403(job_id)
    if (job.get("report_type") or REPORT_GENERAL_LEDGER) != REPORT_CHART_OF_ACCOUNTS:
        flash(
            "Chart of Accounts creation is only available for jobs "
            "uploaded as a Chart of Accounts report.",
            "info",
        )
        return None, redirect(url_for("job_detail", job_id=job_id))

    coa_rows = job.get("parsed_coa")
    if not coa_rows:
        coa_rows = _reparse_report_rows(job, REPORT_CHART_OF_ACCOUNTS)
    if not coa_rows:
        flash(
            "Could not re-read the Chart of Accounts upload. Re-upload "
            "the file and try again.",
            "error",
        )
        return None, redirect(url_for("coa_preview", job_id=job_id))

    qbo, qbo_conn = _get_qbo_client(job_id, user)
    if not qbo:
        flash(
            "Connect QuickBooks first. Chart of Accounts creation needs "
            "a live connection so existing accounts can be detected before "
            "any writes happen.",
            "error",
        )
        return None, redirect(url_for("coa_preview", job_id=job_id))

    return {
        "job": job,
        "user": user,
        "coa_rows": coa_rows,
        "qbo": qbo,
        "qbo_conn": qbo_conn,
    }, None


def _build_coa_plan(coa_rows, qbo, type_overrides=None):
    """Run the read-only QBO query, build the preview, then the create plan.

    Also resolves the parent/sub-account hierarchy and folds any
    hierarchy-blocked rows (orphan parents, cycles) into the plan's
    blocked list so the confirmation page refuses to create them.
    Hierarchy creation order is annotated on the plan dict so the
    confirmation UI can render parent-first semantics.

    ``type_overrides`` are layered onto the COA rows before mapping so
    operator corrections (see /jobs/<id>/coa-override) reach the create
    plan as well as the preview.
    """
    qbo_accounts = qbo.get_accounts()
    overrides = type_overrides or {}
    # Layer overrides so preview + hierarchy reflect the same corrections
    # the create plan will use. Keep the lookup tolerant of name vs number.
    coa_rows_eff = []
    for r in coa_rows:
        num = (r.get("account_number") or "").strip()
        nl = (r.get("account_name") or "").strip().lower()
        ov = overrides.get(num) or overrides.get(nl)
        if ov:
            coa_rows_eff.append({
                **r,
                "account_type": ov.get("account_type") or r.get("account_type"),
                "detail_type": ov.get("detail_type") or r.get("detail_type"),
            })
        else:
            coa_rows_eff.append(r)
    preview = build_coa_dry_run_preview(coa_rows_eff, qbo_accounts)
    plan = build_create_plan(coa_rows_eff, preview, type_overrides=overrides)
    hierarchy_plan = build_hierarchy_plan(coa_rows_eff, qbo_accounts)
    if hierarchy_plan.has_blockers:
        # Promote hierarchy blockers (orphan parent, cycle) into the
        # CreatePlan.blocked list so plan.has_blockers gates the apply.
        blocked_keys = {
            (n.account_number, n.account_name) for n in hierarchy_plan.blocked
        }
        from coa_apply import CreatePlanEntry
        moved: list = []
        kept = []
        for entry in plan.to_create:
            if (entry.account_number, entry.account_name) in blocked_keys:
                node = next(
                    (n for n in hierarchy_plan.blocked
                     if n.account_number == entry.account_number
                     and n.account_name == entry.account_name),
                    None,
                )
                entry.decision = "blocked"
                entry.blocked_reason = (
                    node.blocker if node and node.blocker else
                    "Parent/sub-account hierarchy could not be resolved."
                )
                moved.append(entry)
            else:
                kept.append(entry)
        plan.to_create = kept
        plan.blocked = plan.blocked + moved
    return preview, plan, hierarchy_plan


@app.route("/jobs/<job_id>/coa-confirm", methods=["GET", "POST"])
@login_required
def coa_confirm(job_id):
    """Render the typed-confirmation page for creating QBO Accounts.

    Read-only on GET. On POST without the confirmation phrase it
    re-renders with an error; on POST with the phrase it forwards to
    the apply route which performs the writes.
    """
    state, bail = _load_coa_state(job_id)
    if bail is not None:
        return bail
    job = state["job"]
    qbo = state["qbo"]
    qbo_conn = state["qbo_conn"]
    coa_rows = state["coa_rows"]

    try:
        preview, plan, hierarchy_plan = _build_coa_plan(
            coa_rows, qbo,
            type_overrides=job.get("coa_type_overrides") or {},
        )
    except QBOError as e:
        _audit(
            "coa_create_qbo_query_failed",
            target_type="job", target_id=job_id,
            details=_audit_details_with_tid(str(e)[:200], e.intuit_tid),
        )
        flash(
            "Could not fetch the QuickBooks Chart of Accounts to build the "
            "create plan. Try again in a moment."
            + (f" (Intuit support reference: {e.intuit_tid})" if e.intuit_tid else ""),
            "error",
        )
        return redirect(url_for("coa_preview", job_id=job_id))
    except Exception as e:  # noqa: BLE001
        _audit(
            "coa_create_qbo_query_failed",
            target_type="job", target_id=job_id, details=str(e)[:200],
        )
        flash(
            "Could not fetch the QuickBooks Chart of Accounts. "
            "Re-open the preview and try again.",
            "error",
        )
        return redirect(url_for("coa_preview", job_id=job_id))

    confirmation_error = None
    if request.method == "POST":
        # CSRF is enforced by the global before_request hook. We only need to
        # validate the typed confirmation phrase here.
        phrase = (request.form.get("confirm_create") or "").strip().upper()
        if phrase != COA_CREATE_CONFIRMATION_PHRASE:
            confirmation_error = (
                f"Type {COA_CREATE_CONFIRMATION_PHRASE} exactly to confirm. "
                "This is a safety check — no QuickBooks accounts have been "
                "created."
            )
            _audit(
                "coa_create_confirmation_failed",
                target_type="job", target_id=job_id,
                details=f"phrase={phrase!r}",
            )
        elif plan.has_blockers:
            confirmation_error = (
                "Cannot proceed: some rows are blocked from auto-creation. "
                "Resolve the blocked rows below before confirming."
            )
            _audit(
                "coa_create_confirmation_blocked",
                target_type="job", target_id=job_id,
                details=f"blocked_count={len(plan.blocked)}",
            )
        else:
            # Forward to apply with method=POST. We re-build the plan
            # there from scratch (don't trust a hidden form field) so a
            # tampered form can't smuggle in extra rows.
            return redirect(url_for("coa_apply_route", job_id=job_id), code=307)

    _audit(
        "coa_create_confirmation_shown",
        target_type="job", target_id=job_id,
        details=(
            f"to_create={len(plan.to_create)} blocked={len(plan.blocked)} "
            f"matched={len(plan.matched)}"
        ),
    )
    return render_template(
        "coa-confirm.html",
        job=job,
        preview=preview,
        plan=plan.to_dict(),
        hierarchy=hierarchy_plan.to_dict(),
        qbo_connection=qbo_conn or {},
        report_label=REPORT_LABELS[REPORT_CHART_OF_ACCOUNTS],
        confirmation_phrase=COA_CREATE_CONFIRMATION_PHRASE,
        confirmation_error=confirmation_error,
        qbo_env_status=(
            "production" if (QBO_ENVIRONMENT or "").lower() == "production"
            else "sandbox"
        ),
    )


@app.route("/jobs/<job_id>/coa-apply", methods=["POST"])
@login_required
def coa_apply_route(job_id):
    """Execute the COA create plan. POST-only; requires typed confirmation."""
    state, bail = _load_coa_state(job_id)
    if bail is not None:
        return bail
    job = state["job"]
    qbo = state["qbo"]
    qbo_conn = state["qbo_conn"]
    coa_rows = state["coa_rows"]

    phrase = (request.form.get("confirm_create") or "").strip().upper()
    if phrase != COA_CREATE_CONFIRMATION_PHRASE:
        _audit(
            "coa_create_blocked_no_confirmation",
            target_type="job", target_id=job_id,
            details="apply route reached without confirmation phrase",
        )
        flash(
            "Chart of Accounts creation requires explicit typed "
            f"confirmation ({COA_CREATE_CONFIRMATION_PHRASE}). Nothing was "
            "created in QuickBooks.",
            "error",
        )
        return redirect(url_for("coa_confirm", job_id=job_id))

    try:
        preview, plan, hierarchy_plan = _build_coa_plan(
            coa_rows, qbo,
            type_overrides=job.get("coa_type_overrides") or {},
        )
    except Exception as e:  # noqa: BLE001
        tid = getattr(e, "intuit_tid", None)
        _audit(
            "coa_create_qbo_query_failed",
            target_type="job", target_id=job_id,
            details=_audit_details_with_tid(str(e)[:200], tid),
        )
        flash(
            "Could not refresh the QuickBooks account list before creating. "
            "Nothing was created. Try again in a moment."
            + (f" (Intuit support reference: {tid})" if tid else ""),
            "error",
        )
        return redirect(url_for("coa_confirm", job_id=job_id))

    if plan.has_blockers:
        _audit(
            "coa_create_blocked",
            target_type="job", target_id=job_id,
            details=f"blocked_count={len(plan.blocked)}",
        )
        flash(
            "Cannot create accounts: some rows are blocked from "
            "auto-creation. Resolve them and re-confirm.",
            "error",
        )
        return redirect(url_for("coa_confirm", job_id=job_id))

    if not plan.to_create:
        _audit(
            "coa_create_noop",
            target_type="job", target_id=job_id,
            details="every account already exists in QBO",
        )
        flash(
            "Every account in the Chart of Accounts already exists in "
            "QuickBooks. Nothing to create.",
            "info",
        )
        return redirect(url_for("coa_preview", job_id=job_id))

    _audit(
        "coa_create_started",
        target_type="job", target_id=job_id,
        details=(
            f"to_create={len(plan.to_create)} "
            f"realm={qbo_conn.get('realm_id')} "
            f"company={qbo_conn.get('company_name') or ''}"
        ),
    )
    result = apply_create_plan(qbo, plan)

    created = result["created"]
    failed = result["failed"]
    intuit_tids = result["intuit_tids"]

    # Persist the outcome on the job so the checklist + audit trail can
    # show "previewed / created / completed" without re-running the QBO
    # query on every page render.
    coa_history = job.get("coa_create_history") or []
    coa_history.append({
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "realm_id": qbo_conn.get("realm_id"),
        "company_name": qbo_conn.get("company_name"),
        "created_count": len(created),
        "failed_count": len(failed),
        "created": created,
        "failed": failed,
        "intuit_tids": intuit_tids,
    })
    job["coa_create_history"] = coa_history
    if failed:
        job["status"] = (
            f"COA: {len(created)} created, {len(failed)} failed"
        )
    else:
        job["status"] = f"COA: {len(created)} accounts created in QuickBooks"
    _save_job(job_id)

    _audit(
        "coa_create_completed",
        target_type="job", target_id=job_id,
        details=_audit_details_with_tid(
            f"created={len(created)} failed={len(failed)}",
            intuit_tids[0] if intuit_tids else None,
        ),
    )
    if failed:
        flash(
            f"Created {len(created)} QuickBooks account(s); "
            f"{len(failed)} failed. Review the per-row errors below.",
            "warning" if created else "error",
        )
    else:
        flash(
            f"Created {len(created)} QuickBooks account(s). "
            "Continue with the opening trial balance step in the migration "
            "checklist.",
            "success",
        )
    return render_template(
        "coa-result.html",
        job=job,
        plan=plan.to_dict(),
        created=created,
        failed=failed,
        intuit_tids=intuit_tids,
        qbo_connection=qbo_conn or {},
        report_label=REPORT_LABELS[REPORT_CHART_OF_ACCOUNTS],
    )


def _reparse_report_rows(job: dict, report_type: str):
    """Re-decrypt the upload and re-run the parser. Returns [] on error."""
    enc_name = job.get("encrypted_file")
    if not enc_name:
        return []
    enc_path = UPLOAD_DIR / enc_name
    if not enc_path.exists():
        return []
    temp_path = UPLOAD_DIR / f"reparse_{secrets.token_urlsafe(8)}.csv"
    try:
        decrypt_file(enc_path, temp_path)
        if report_type == REPORT_CHART_OF_ACCOUNTS:
            rows, _fn, _missing = parse_chart_of_accounts(temp_path)
        elif report_type == REPORT_TRIAL_BALANCE:
            rows, _fn, _missing = parse_trial_balance(temp_path)
        elif report_type == REPORT_TRUST_LISTING:
            rows, _fn, _missing = parse_trust_listing(temp_path)
        else:
            rows = []
        return rows
    except Exception:  # noqa: BLE001
        return []
    finally:
        temp_path.unlink(missing_ok=True)


def _job_trial_balance_rows(job: dict) -> list[dict]:
    rows = job.get("parsed_trial_balance")
    if not rows:
        rows = _reparse_report_rows(job, REPORT_TRIAL_BALANCE)
    return rows or []


def _job_trust_listing_rows(job: dict) -> list[dict]:
    rows = job.get("parsed_trust_listing")
    if not rows:
        rows = _reparse_report_rows(job, REPORT_TRUST_LISTING)
    return rows or []


def _firm_latest_jobs_by_type(firm_id: int, report_type: str, limit: int = 20) -> list[dict]:
    """Return firm jobs of a given report_type, newest first."""
    all_jobs = db.list_jobs_for_firm(firm_id, limit=limit) or []
    return [j for j in all_jobs if (j.get("report_type") or "general_ledger") == report_type]


def _latest_other_job_report(firm_id: int, report_type: str, exclude_job_id: str) -> list[dict]:
    """Find the most recent successful job of a given report_type for this
    firm, *excluding* the current job, and return its parsed rows (from
    the in-memory jobs cache or by reparsing). Returns [] when nothing
    suitable is on file.
    """
    candidates = [
        j for j in _firm_latest_jobs_by_type(firm_id, report_type)
        if j.get("id") != exclude_job_id
    ]
    for j in candidates:
        live = jobs.get(j["id"])
        if report_type == REPORT_TRIAL_BALANCE:
            rows = (live or {}).get("parsed_trial_balance")
            if not rows:
                rows = _reparse_report_rows(live or j, REPORT_TRIAL_BALANCE)
            if rows:
                return rows
        elif report_type == REPORT_TRUST_LISTING:
            rows = (live or {}).get("parsed_trust_listing")
            if not rows:
                rows = _reparse_report_rows(live or j, REPORT_TRUST_LISTING)
            if rows:
                return rows
        elif report_type == REPORT_GENERAL_LEDGER:
            # GL doesn't have parsed_* cached on the job dict; reparse via
            # the dedicated pipeline.
            live_job = live or j
            try:
                enc_name = live_job.get("encrypted_file")
                if not enc_name:
                    continue
                enc_path = UPLOAD_DIR / enc_name
                if not enc_path.exists():
                    continue
                temp_path = UPLOAD_DIR / f"reparse_gl_{secrets.token_urlsafe(8)}.csv"
                decrypt_file(enc_path, temp_path)
                try:
                    rows = load_general_ledger_csv(temp_path)
                    if rows:
                        return rows
                finally:
                    temp_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                continue
    return []


# ---------------------------------------------------------------------------
# Chart of Accounts → Trial Balance cross-validation helpers.
#
# The helper email called out that the Trial Balance step must rely on a
# finalized Chart of Accounts, with operator-corrected account types where
# the parser was uncertain. These helpers + the /coa-override route below
# are the COA-first plumbing the opening-balance route consumes.
# ---------------------------------------------------------------------------


def _firm_latest_coa_state(firm_id: int) -> dict:
    """Collect the firm's latest Chart-of-Accounts artifacts in one place.

    Returns a dict with:
      * ``coa_rows``: parsed COA rows from the newest COA job (possibly
        re-parsed from the encrypted upload), or an empty list if the
        firm has not uploaded a COA yet.
      * ``coa_job``: the underlying job row (for audit references) or
        None.
      * ``coa_type_overrides``: operator-set type corrections, keyed
        by account number then lowercased name (see
        /jobs/<id>/coa-override).
      * ``coa_create_history``: aggregated history of QBO accounts
        created across all of the firm's COA jobs (used to surface the
        "Created in QBO" badge on the TB step).
    """
    coa_jobs = _firm_latest_jobs_by_type(firm_id, REPORT_CHART_OF_ACCOUNTS)
    coa_job = None
    coa_rows: list[dict] = []
    for j in coa_jobs:
        live = jobs.get(j["id"])
        rows = (live or {}).get("parsed_coa")
        if not rows:
            rows = _reparse_report_rows(live or j, REPORT_CHART_OF_ACCOUNTS)
        if rows:
            coa_rows = rows
            coa_job = live or j
            break

    overrides: dict = {}
    create_history: list[dict] = []
    for j in coa_jobs:
        live = jobs.get(j["id"]) or j
        ov = live.get("coa_type_overrides") or {}
        for k, v in ov.items():
            overrides.setdefault(k, v)
        for h in (live.get("coa_create_history") or []):
            create_history.append(h)

    return {
        "coa_rows": coa_rows,
        "coa_job": coa_job,
        "coa_type_overrides": overrides,
        "coa_create_history": create_history,
    }


# Allowed QBO AccountType values the override form accepts. We deliberately
# keep this short and curated rather than letting the operator type any
# string — the helper email specifically guarded against AR/AP/Trust
# mis-classification, and the create-plan validator already enforces
# AccountType/DetailType validity downstream.
COA_OVERRIDE_ACCOUNT_TYPES = (
    "Bank",
    "Accounts Receivable",
    "Other Current Asset",
    "Fixed Asset",
    "Other Asset",
    "Accounts Payable",
    "Credit Card",
    "Other Current Liability",
    "Long Term Liability",
    "Equity",
    "Income",
    "Other Income",
    "Cost of Goods Sold",
    "Expense",
    "Other Expense",
)


@app.route("/jobs/<job_id>/coa-override", methods=["POST"])
@login_required
def coa_override_route(job_id):
    """Operator-supplied account-type correction for a single COA row.

    Form fields:
      * ``account_number`` (required) — the COA row to override.
        Optional, but rows without a number key on the lowercased name.
      * ``account_name`` (optional fallback when no number).
      * ``account_type`` (required) — one of ``COA_OVERRIDE_ACCOUNT_TYPES``.
      * ``detail_type`` (optional) — free-text QBO AccountSubType. Empty
        is allowed; the type-mapper will fall back to the default
        sub-type for the chosen account type.
      * ``clear`` — when set, removes any existing override for the
        keyed row instead of writing a new one.

    Persists the override on the COA job dict under
    ``coa_type_overrides`` and reloads the preview page so the operator
    sees the correction applied. The route is POST-only so a stray GET
    can't mutate state.
    """
    job, _user = _job_or_403(job_id)
    if (job.get("report_type") or REPORT_GENERAL_LEDGER) != REPORT_CHART_OF_ACCOUNTS:
        flash(
            "Account-type overrides are only available on Chart of "
            "Accounts jobs.",
            "info",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    account_number = (request.form.get("account_number") or "").strip()
    account_name = (request.form.get("account_name") or "").strip()
    account_type = (request.form.get("account_type") or "").strip()
    detail_type = (request.form.get("detail_type") or "").strip()
    clear = bool(request.form.get("clear"))

    if not account_number and not account_name:
        flash(
            "Account-type override needs at least an account number or "
            "an account name.",
            "error",
        )
        return redirect(url_for("coa_preview", job_id=job_id))

    if not clear and account_type not in COA_OVERRIDE_ACCOUNT_TYPES:
        flash(
            "Pick a QuickBooks AccountType from the dropdown — free-text "
            "values are not allowed.",
            "error",
        )
        return redirect(url_for("coa_preview", job_id=job_id))

    key = account_number or account_name.lower()
    overrides = dict(job.get("coa_type_overrides") or {})
    if clear:
        overrides.pop(key, None)
        _audit(
            "coa_type_override_cleared",
            target_type="job", target_id=job_id,
            details=f"key={key!r}",
        )
        flash("Cleared the manual account-type override for that row.", "info")
    else:
        overrides[key] = {
            "account_type": account_type,
            "detail_type": detail_type,
            "account_number": account_number,
            "account_name": account_name,
        }
        _audit(
            "coa_type_override_set",
            target_type="job", target_id=job_id,
            details=(
                f"key={key!r} account_type={account_type!r} "
                f"detail_type={detail_type!r}"
            ),
        )
        flash(
            f"Saved manual account type '{account_type}' for "
            f"{account_number or account_name}.",
            "success",
        )
    job["coa_type_overrides"] = overrides
    return redirect(url_for("coa_preview", job_id=job_id))


@app.route("/jobs/<job_id>/opening-balance", methods=["GET", "POST"])
@login_required
def opening_balance_preview(job_id):
    """Opening Trial Balance -> opening balance JournalEntry preview.

    GET (or POST without the confirmation phrase) shows the plan: every
    TB row mapped to a QBO account, totals, balance check, and per-row
    blockers. POST with ``confirm_post=POST OPENING BALANCE`` and a
    fully-resolved plan posts a single balancing JournalEntry to QBO.

    Refuses to post when:
      * the TB is unbalanced (no auto-balance to suspense),
      * any TB row can't resolve to a QBO account,
      * QBO isn't connected,
      * the confirmation phrase wasn't typed.
    """
    job, user = _job_or_403(job_id)
    if (job.get("report_type") or REPORT_GENERAL_LEDGER) != REPORT_TRIAL_BALANCE:
        flash(
            "Opening balance posting is only available for jobs uploaded "
            "as a Trial Balance report.",
            "info",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    tb_rows = _job_trial_balance_rows(job)
    if not tb_rows:
        flash("Could not read the Trial Balance upload. Re-upload and try again.", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    qbo, qbo_conn = _get_qbo_client(job_id, user)
    qbo_error: Optional[str] = None
    if not qbo:
        qbo_error = (
            "Connect QuickBooks to resolve TB accounts and (eventually) "
            "post the opening journal entry. The plan below is shown "
            "without QBO account resolution until you connect."
        )
        qbo_accounts = {"QueryResponse": {"Account": []}}
        account_mappings = []
    else:
        try:
            qbo_accounts = qbo.get_accounts()
        except Exception as exc:  # noqa: BLE001
            qbo_error = (
                "Could not fetch the QuickBooks Chart of Accounts. The "
                "opening balance plan will retry on next page load."
            )
            qbo_accounts = {"QueryResponse": {"Account": []}}
            _audit(
                "opening_balance_qbo_query_failed",
                target_type="job", target_id=job_id,
                details=str(exc)[:200],
            )
        account_mappings = db.list_account_mappings(
            user["firm_id"], qbo_conn["realm_id"]
        ) if qbo_conn else []

    cutover = db.get_cutover_settings(user["firm_id"]) or {}
    plan = build_opening_balance_plan(
        tb_rows,
        qbo_accounts,
        as_of_date=cutover.get("opening_balance_date"),
        account_mappings=account_mappings,
    )

    # COA-first validation: cross-check every TB account against the
    # firm's latest Chart of Accounts (parsed + operator overrides +
    # QBO accounts + create-history). The helper email's central ask:
    # the TB step must not be allowed to proceed when the COA isn't
    # finalized, an account type is blank, or an AR/AP mismatch hasn't
    # been resolved.
    coa_state = _firm_latest_coa_state(user["firm_id"])
    tb_coa_validation = validate_tb_against_coa(
        tb_rows,
        coa_state["coa_rows"],
        qbo_accounts,
        account_mappings=account_mappings,
        coa_create_history=coa_state["coa_create_history"],
        coa_type_overrides=coa_state["coa_type_overrides"],
    )

    confirmation_error: Optional[str] = None
    if request.method == "POST":
        phrase = (request.form.get("confirm_post") or "").strip().upper()
        if phrase != OPENING_BALANCE_CONFIRMATION_PHRASE:
            confirmation_error = (
                f"Type {OPENING_BALANCE_CONFIRMATION_PHRASE} exactly to "
                "confirm. Nothing was posted."
            )
            _audit(
                "opening_balance_confirmation_failed",
                target_type="job", target_id=job_id,
                details=f"phrase={phrase!r}",
            )
        elif plan.has_blockers:
            confirmation_error = (
                "Cannot post: the plan has blockers (unbalanced TB or "
                "rows that don't resolve to a QBO account). Fix them "
                "before confirming."
            )
            _audit(
                "opening_balance_confirmation_blocked",
                target_type="job", target_id=job_id,
                details=f"blocker_count={len(plan.blockers)}",
            )
        elif not tb_coa_validation.ready:
            # COA-first gate. The blockers list will contain a one-line
            # rollup per category (missing-from-COA, needs-type, type-
            # mismatch) so the operator sees the right next action.
            confirmation_error = (
                "Cannot post: the Chart of Accounts is not ready. "
                + " ".join(tb_coa_validation.blockers)
                + " Resolve on the Chart of Accounts step, then return "
                "to the starting-balances step."
            )
            _audit(
                "opening_balance_blocked_by_coa",
                target_type="job", target_id=job_id,
                details=(
                    f"counts={tb_coa_validation.counts} "
                    f"has_coa={tb_coa_validation.has_coa}"
                ),
            )
        elif not qbo:
            confirmation_error = (
                "Connect QuickBooks before confirming. Nothing was posted."
            )
        else:
            return _opening_balance_post(job, user, qbo, qbo_conn, plan)

    return render_template(
        "opening-balance.html",
        job=job,
        plan=plan.to_dict(),
        tb_coa_validation=tb_coa_validation.to_dict(),
        coa_job_id=(coa_state["coa_job"] or {}).get("id"),
        qbo_connection=qbo_conn or {},
        qbo_error=qbo_error,
        confirmation_phrase=OPENING_BALANCE_CONFIRMATION_PHRASE,
        confirmation_error=confirmation_error,
        report_label=REPORT_LABELS[REPORT_TRIAL_BALANCE],
        qbo_env_status=(
            "production" if (QBO_ENVIRONMENT or "").lower() == "production"
            else "sandbox"
        ),
    )


def _opening_balance_post(job, user, qbo, qbo_conn, plan):
    job_id = job["id"]
    if QBO_ENVIRONMENT == "production":
        confirmation = (request.form.get("confirm_post") or "").strip().upper()
        if confirmation != OPENING_BALANCE_CONFIRMATION_PHRASE:
            flash(
                "Production safety check: type "
                f"{OPENING_BALANCE_CONFIRMATION_PHRASE} to confirm.",
                "error",
            )
            return redirect(url_for("opening_balance_preview", job_id=job_id))

    if not QBO_REAL_IMPORT:
        job["status"] = "Opening balance JE (demo mode)"
        history_entry = {
            "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "demo_mode": True,
            "as_of_date": plan.as_of_date,
            "total_debit": plan.total_debit,
            "total_credit": plan.total_credit,
            "line_count": len(plan.postable_lines),
            "qbo_je_id": None,
        }
        job.setdefault("opening_balance_history", []).append(history_entry)
        _save_job(job_id)
        _audit("opening_balance_demo", target_type="job", target_id=job_id,
               details=f"lines={len(plan.postable_lines)} as_of={plan.as_of_date}")
        flash(
            "Demo mode: no journal entry was posted to QuickBooks. Set "
            "QBO_REAL_IMPORT=1 and reconnect QBO to post a real opening "
            "balance JE.",
            "info",
        )
        return redirect(url_for("opening_balance_preview", job_id=job_id))

    payload = build_opening_je_payload(plan)
    try:
        response = qbo.create_journal_entry(payload)
    except QBOError as e:
        _audit(
            "opening_balance_post_failed",
            target_type="job", target_id=job_id,
            details=_audit_details_with_tid(str(e)[:300], e.intuit_tid),
        )
        flash(
            "Could not post the opening balance JE to QuickBooks. Nothing "
            f"was created.{' Intuit ref: ' + e.intuit_tid if e.intuit_tid else ''}",
            "error",
        )
        return redirect(url_for("opening_balance_preview", job_id=job_id))
    except Exception as e:  # noqa: BLE001
        _audit(
            "opening_balance_post_failed",
            target_type="job", target_id=job_id,
            details=str(e)[:300],
        )
        flash("Could not post the opening balance JE. Nothing was created.", "error")
        return redirect(url_for("opening_balance_preview", job_id=job_id))

    je = (response or {}).get("JournalEntry") or {}
    je_id = str(je.get("Id") or "")
    history_entry = {
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "as_of_date": plan.as_of_date,
        "total_debit": plan.total_debit,
        "total_credit": plan.total_credit,
        "line_count": len(plan.postable_lines),
        "qbo_je_id": je_id,
        "realm_id": qbo_conn.get("realm_id"),
        "company_name": qbo_conn.get("company_name"),
    }
    job.setdefault("opening_balance_history", []).append(history_entry)
    job["status"] = f"Opening balance JE posted (#{je_id})"
    _save_job(job_id)
    _audit(
        "opening_balance_posted",
        target_type="job", target_id=job_id,
        details=f"je_id={je_id} as_of={plan.as_of_date} total={plan.total_debit}",
    )
    flash(
        f"Opening balance JournalEntry #{je_id} posted to QuickBooks. "
        "Use the ending trial balance step to verify QBO matches PCLaw.",
        "success",
    )
    return redirect(url_for("opening_balance_preview", job_id=job_id))


@app.route("/jobs/<job_id>/ending-tb-reconciliation")
@login_required
def ending_tb_reconciliation_view(job_id):
    """Compare an uploaded ending TB against opening TB + parsed GL."""
    job, user = _job_or_403(job_id)
    if (job.get("report_type") or REPORT_GENERAL_LEDGER) != REPORT_TRIAL_BALANCE:
        flash(
            "Ending TB reconciliation is only available for Trial Balance "
            "report jobs.",
            "info",
        )
        return redirect(url_for("job_detail", job_id=job_id))
    ending_rows = _job_trial_balance_rows(job)
    if not ending_rows:
        flash("Could not read the Trial Balance upload. Re-upload and try again.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    opening_rows = _latest_other_job_report(user["firm_id"], REPORT_TRIAL_BALANCE, job_id)
    gl_rows = _latest_other_job_report(user["firm_id"], REPORT_GENERAL_LEDGER, job_id)
    report = build_ending_tb_reconciliation(ending_rows, opening_rows, gl_rows)
    job["ending_tb_reconciliation"] = {
        "built_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "summary": report["summary"],
    }
    _save_job(job_id)
    _audit(
        "ending_tb_reconciliation_view",
        target_type="job", target_id=job_id,
        details=(
            f"matched={report['summary']['matched_count']} "
            f"diff={report['summary']['diff_count']} "
            f"unexpected={report['summary']['unexpected_count']} "
            f"missing={report['summary']['missing_count']}"
        ),
    )
    return render_template(
        "ending-tb-reconciliation.html",
        job=job,
        report=report,
        opening_available=bool(opening_rows),
        gl_available=bool(gl_rows),
    )


@app.route("/jobs/<job_id>/ending-tb-reconciliation.csv")
@login_required
def ending_tb_reconciliation_csv(job_id):
    from tb_reconciliation import render_ending_tb_reconciliation_csv
    job, user = _job_or_403(job_id)
    if (job.get("report_type") or REPORT_GENERAL_LEDGER) != REPORT_TRIAL_BALANCE:
        return ("Not a Trial Balance job", 400)
    ending_rows = _job_trial_balance_rows(job)
    opening_rows = _latest_other_job_report(user["firm_id"], REPORT_TRIAL_BALANCE, job_id)
    gl_rows = _latest_other_job_report(user["firm_id"], REPORT_GENERAL_LEDGER, job_id)
    report = build_ending_tb_reconciliation(ending_rows, opening_rows, gl_rows)
    csv_text = render_ending_tb_reconciliation_csv(report)
    _audit("ending_tb_reconciliation_download", target_type="job", target_id=job_id)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="ending_tb_reconciliation_{job_id}.csv"'
            ),
        },
    )


@app.route("/jobs/<job_id>/trust-reconciliation")
@login_required
def trust_reconciliation_view(job_id):
    job, user = _job_or_403(job_id)
    if (job.get("report_type") or REPORT_GENERAL_LEDGER) != REPORT_TRUST_LISTING:
        flash(
            "Trust reconciliation is only available for Trust Listing "
            "report jobs.",
            "info",
        )
        return redirect(url_for("job_detail", job_id=job_id))
    trust_rows = _job_trust_listing_rows(job)
    if not trust_rows:
        flash("Could not read the Trust Listing upload. Re-upload and try again.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    tb_rows = _latest_other_job_report(user["firm_id"], REPORT_TRIAL_BALANCE, job_id)
    report = build_trust_listing_reconciliation(trust_rows, tb_rows)
    job["trust_reconciliation"] = {
        "built_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "summary": report["summary"],
    }
    _save_job(job_id)
    _audit(
        "trust_reconciliation_view",
        target_type="job", target_id=job_id,
        details=(
            f"clients={report['summary']['client_count']} "
            f"matters={report['summary']['matter_count']} "
            f"negatives={report['summary']['negative_row_count']} "
            f"liability_match={report['summary']['liability_match']}"
        ),
    )
    return render_template(
        "trust-reconciliation.html",
        job=job,
        report=report,
        tb_available=bool(tb_rows),
    )


@app.route("/jobs/<job_id>/trust-reconciliation.csv")
@login_required
def trust_reconciliation_csv(job_id):
    from trust_reconciliation import render_trust_reconciliation_csv
    job, user = _job_or_403(job_id)
    if (job.get("report_type") or REPORT_GENERAL_LEDGER) != REPORT_TRUST_LISTING:
        return ("Not a Trust Listing job", 400)
    trust_rows = _job_trust_listing_rows(job)
    tb_rows = _latest_other_job_report(user["firm_id"], REPORT_TRIAL_BALANCE, job_id)
    report = build_trust_listing_reconciliation(trust_rows, tb_rows)
    csv_text = render_trust_reconciliation_csv(report)
    _audit("trust_reconciliation_download", target_type="job", target_id=job_id)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="trust_reconciliation_{job_id}.csv"'
            ),
        },
    )


@app.route("/jobs/<job_id>/connect-qbo")
@login_required
def connect_qbo(job_id):
    job, _user = _job_or_403(job_id)

    if QBO_CLIENT_ID == "your-client-id-here":
        flash(
            "QuickBooks OAuth not configured. Set QBO_CLIENT_ID, QBO_CLIENT_SECRET, and QBO_REDIRECT_URI environment variables.",
            "error",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    # Production-mode safety gate. Prevents the operator from sending a
    # real customer through Intuit's consent screen against a half-
    # configured production deploy (e.g. http:// callback, missing
    # SUPPORT_EMAIL, QBO_REAL_IMPORT off). Sandbox bypasses this on
    # purpose so beta testing keeps working.
    blockers = _qbo_production_blockers()
    if blockers:
        _audit(
            "qbo_connect_blocked",
            target_type="job", target_id=job_id,
            details="; ".join(blockers),
        )
        flash(
            "Cannot connect to QuickBooks: this production deploy is not "
            "fully configured to receive real customer data yet. "
            + " ".join(b + "." for b in blockers)
            + " Open the readiness page to fix the remaining items, then try again."
            + _support_suffix(),
            "error",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    # Bind the OAuth state to a fresh per-attempt random nonce stored in
    # the session. The job_id alone is a predictable timestamp string, so
    # without a nonce an attacker who learns or guesses a victim's job_id
    # could craft a /oauth/callback?state=<job_id>&code=<their_code> URL
    # and trick the victim into linking their own QuickBooks company to
    # the attacker's job. The session-bound nonce defeats that: only a
    # callback whose state matches the value we just minted (in the same
    # browser session) is accepted.
    nonce = secrets.token_urlsafe(32)
    session["pending_job_id"] = job_id
    session["pending_oauth_state"] = f"{job_id}:{nonce}"
    auth_url = qbo_auth.get_authorization_url(state=session["pending_oauth_state"])
    return redirect(auth_url)


def _support_suffix():
    """Append a "contact <support email>" sentence when a real one is
    configured. Suppressed for the deploy-default placeholder so beta
    testers never see "support@your-domain.example"."""
    addr = (branding.SUPPORT_EMAIL or "").strip()
    if not addr or branding.is_placeholder_email(addr):
        return ""
    return f" If this keeps happening, contact {addr}."


def _sandbox_hint():
    """One-sentence reminder that sandbox builds require Intuit's sandbox
    company, not a real QuickBooks login. Returned as a leading sentence
    when QBO_ENVIRONMENT=sandbox so beta testers stop seeing Intuit's
    generic "didn't connect" page without context."""
    if QBO_ENVIRONMENT == "sandbox":
        return (
            "This deploy is in QuickBooks Sandbox Testing Mode. You must "
            "sign in with the Intuit sandbox company we provided — a real "
            "QuickBooks Online company will not connect until production "
            "credentials are approved by Intuit. "
        )
    return ""


@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    realm_id = request.args.get("realmId")
    error = request.args.get("error")
    error_description = (request.args.get("error_description") or "").strip()

    user = current_user()
    if not user:
        # Session expired (or the browser dropped the session cookie on
        # the cross-site redirect back from Intuit). Try to extract the
        # originating job_id from the OAuth state so we can return the
        # user *back to the job page* after they log in, instead of
        # dumping them on the generic dashboard. The state format minted
        # by /jobs/<id>/connect-qbo is "<job_id>:<nonce>".
        #
        # We deliberately do NOT honor the OAuth `code` or trigger a
        # token exchange in this branch — without a verified session
        # there is no firm to attach the connection to. The next-URL is
        # only used to drive the post-login redirect; the OAuth flow
        # has to be restarted from the job page so a fresh
        # pending_oauth_state nonce is minted in the new session.
        next_url = None
        return_job_id = ""
        if state:
            return_job_id = state.split(":", 1)[0] if ":" in state else state
            # url_for is the only thing we trust to build the path; if
            # the job_id has any unusual characters the redirect_for_login
            # validator below will still strip them out.
            try:
                next_url = url_for("job_detail", job_id=return_job_id)
            except Exception:
                next_url = None
        flash(
            "Your session timed out during the QuickBooks redirect. "
            "Please log in again, then re-click "
            "Connect to QuickBooks on the job page."
            + _support_suffix(),
            "error",
        )
        # `next` is sanitized by the login view (only same-origin paths
        # are honored) so we cannot be used as an open redirect here.
        if next_url:
            return redirect(url_for("login", next=next_url))
        return redirect(url_for("login"))

    if error:
        # Intuit's hosted error page only says "didn't connect"; map the
        # OAuth `error` query param to something the tester can act on.
        # `access_denied` = user clicked Cancel; everything else is some
        # form of credential/scope/realm rejection from Intuit.
        db.audit(
            action="oauth_callback_error",
            firm_id=user["firm_id"], user_id=user["id"],
            target_type="job", target_id=state or "",
            # Audit only records the OAuth error code, not any URL params
            # that could carry a token or PII.
            details=f"error={error}",
        )
        if error == "access_denied":
            flash(
                _sandbox_hint()
                + "QuickBooks connection cancelled. No data was changed. "
                "Click Connect to QuickBooks again when you're ready."
                + _support_suffix(),
                "error",
            )
        else:
            desc = f" Details from Intuit: {error_description}." if error_description else ""
            flash(
                _sandbox_hint()
                + "QuickBooks did not approve this connection ("
                + str(error)
                + ")." + desc
                + " This usually means the wrong QuickBooks company was "
                "picked, or this build is not yet approved by Intuit for "
                "live customer companies."
                + _support_suffix(),
                "error",
            )
        return redirect(url_for("dashboard"))

    if not code or not realm_id:
        # No `error=` and no `code` either — Intuit's hosted "Uh oh, there's
        # a connection problem" page redirects here without a code. Give
        # the tester a clear next step instead of a generic message.
        db.audit(
            action="oauth_callback_missing_params",
            firm_id=user["firm_id"], user_id=user["id"],
            target_type="job", target_id=state or "",
            details=f"have_code={bool(code)} have_realm={bool(realm_id)}",
        )
        flash(
            _sandbox_hint()
            + "QuickBooks did not return an authorization for this "
            "connection. If Intuit showed an \"Uh oh, there's a connection "
            "problem\" page, that means your QuickBooks login is not "
            "compatible with this build's credentials. Go back to the job "
            "page and click Connect to QuickBooks again."
            + _support_suffix(),
            "error",
        )
        return redirect(url_for("dashboard"))

    # Verify the OAuth `state` parameter against the per-session nonce we
    # minted in /jobs/<id>/connect-qbo. Without this check, the only thing
    # tying the callback to the user's session is the trailing firm_id
    # comparison below; a state mismatch should be a hard stop with no
    # token exchange. We tolerate older sessions (no pending_oauth_state)
    # by falling back to the previous behavior so an in-flight upgrade
    # doesn't break a connect that started before the deploy. New connects
    # always set pending_oauth_state.
    expected_state = session.pop("pending_oauth_state", None)
    pending_job_id = session.pop("pending_job_id", None)
    if expected_state is not None:
        if not state or not secrets.compare_digest(str(expected_state), str(state)):
            db.audit(
                action="oauth_callback_state_mismatch",
                firm_id=user["firm_id"], user_id=user["id"],
                target_type="job", target_id=pending_job_id or "",
            )
            flash(
                "QuickBooks connection rejected: the security token from "
                "Intuit did not match the one this session issued. No data "
                "was changed. Please open the job and click Connect to "
                "QuickBooks again." + _support_suffix(),
                "error",
            )
            return redirect(url_for("dashboard"))
        # state has the form "<job_id>:<nonce>"; trust the prefix here
        # because we just verified it matches our own session value.
        job_id = pending_job_id or expected_state.split(":", 1)[0]
    else:
        # Legacy fallback: state is the bare job_id.
        job_id = pending_job_id or state
    job = jobs.get(job_id)
    if not job:
        flash(
            "We could not match this QuickBooks connection back to a "
            "migration job. Please open the job and click Connect to "
            "QuickBooks again."
            + _support_suffix(),
            "error",
        )
        return redirect(url_for("dashboard"))
    if job.get("firm_id") != user["firm_id"]:
        # Should not happen unless the OAuth state was tampered with.
        db.audit(
            action="oauth_callback_firm_mismatch",
            firm_id=user["firm_id"], user_id=user["id"],
            target_type="job", target_id=job_id,
        )
        flash(
            "We could not match this QuickBooks connection back to a "
            "migration job in your firm. Please open the job and click "
            "Connect to QuickBooks again."
            + _support_suffix(),
            "error",
        )
        return redirect(url_for("dashboard"))

    try:
        token_data = qbo_auth.get_bearer_token(code)
        # The intuit_tid from the token-exchange response — useful when an
        # operator needs to ask Intuit support which request they saw.
        token_exchange_tid = token_data.get("intuit_tid")

        encrypted_access = encrypt_token(token_data["access_token"])
        encrypted_refresh = encrypt_token(token_data["refresh_token"])

        qbo_connections[job_id] = {
            "realm_id": realm_id,
            "access_token_enc": encrypted_access,
            "refresh_token_enc": encrypted_refresh,
            "expires_at": token_data["expires_at"],
            "connected_at": datetime.utcnow().isoformat(),
            "company_name": None,
        }

        # Best-effort: fetch the company name so the user can confirm they
        # picked the right sandbox. A failure here does NOT block the connect
        # flow — the user can still proceed with realmId alone.
        try:
            qbo = QBOClient(
                access_token=token_data["access_token"],
                realm_id=realm_id,
                environment=QBO_ENVIRONMENT,
            )
            info = qbo.get_company_info()
            ci = info.get("CompanyInfo", {})
            qbo_connections[job_id]["company_name"] = ci.get("CompanyName")
            qbo_connections[job_id]["legal_name"] = ci.get("LegalName")
            qbo_connections[job_id]["country"] = ci.get("Country")
        except Exception as ci_err:  # noqa: BLE001
            qbo_connections[job_id]["company_info_error"] = str(ci_err)

        jobs[job_id]["qbo_connected"] = True
        company_label = qbo_connections[job_id].get("company_name") or f"realmId {realm_id}"
        jobs[job_id]["status"] = f"QuickBooks connected to {company_label}"
        db.upsert_qbo_connection(
            job_id=job_id, firm_id=user["firm_id"], realm_id=realm_id,
            access_token_enc=encrypted_access,
            refresh_token_enc=encrypted_refresh,
            company_name=qbo_connections[job_id].get("company_name"),
            legal_name=qbo_connections[job_id].get("legal_name"),
            country=qbo_connections[job_id].get("country"),
            expires_at=token_data.get("expires_at"),
            company_info_error=qbo_connections[job_id].get("company_info_error"),
        )
        _save_job(job_id)
        # Include the Intuit transaction id in the audit row so operators
        # can correlate this connect event with Intuit's logs if support
        # ever needs to look it up. The tid is an opaque request id, safe
        # to log alongside firm/user metadata.
        connect_details = f"realmId={realm_id} company={company_label}"
        if token_exchange_tid:
            connect_details = f"{connect_details} intuit_tid={token_exchange_tid}"
        _audit("qbo_connected", target_type="job", target_id=job_id,
               details=connect_details)

        flash(
            f"Connected to QuickBooks: {company_label} (realmId {realm_id}). "
            "If this is the wrong company, click Disconnect QuickBooks and reconnect.",
            "success",
        )
        return redirect(url_for("job_detail", job_id=job_id))
    except Exception as e:  # noqa: BLE001
        # Intuit returns 400/401 here when the build's credentials don't
        # match the QBO company the user picked (the common beta failure
        # mode). The raw exception body can include client_id, so we log
        # the truncated string for ops but show the user a friendly
        # explanation with no secrets.
        raw = str(e)
        # Pull the Intuit transaction id off the exception (when our
        # QBOAuthError raises it) or off the handler's last_intuit_tid
        # fallback. Either way, the tid is an opaque request id with no
        # token material — safe to record in audit and surface to support.
        tid = getattr(e, "intuit_tid", None) or getattr(qbo_auth, "last_intuit_tid", None)
        ops_detail = raw[:200]
        if tid:
            ops_detail = f"{ops_detail} intuit_tid={tid}"
        db.audit(
            action="oauth_token_exchange_failed",
            firm_id=user["firm_id"], user_id=user["id"],
            target_type="job", target_id=job_id,
            details=ops_detail,
        )
        # Append the Intuit transaction id (opaque, no secret material) so
        # the user can quote it to support. This is the same id ops will
        # have in the audit row, which lets us match user reports to logs.
        tid_suffix = f" Intuit support reference: {tid}." if tid else ""
        flash(
            _sandbox_hint()
            + "QuickBooks accepted your sign-in but rejected this app's "
            "credentials when finishing the connection. This usually "
            "means the QuickBooks company you picked is not the sandbox "
            "company tied to this build, or this build is not yet "
            "approved by Intuit for production companies. No journal "
            "entries were posted. Open the job and click Connect to "
            "QuickBooks again, picking the sandbox company we provided."
            + tid_suffix
            + _support_suffix(),
            "error",
        )
        return redirect(url_for("job_detail", job_id=job_id))


def _revoke_and_delete_qbo_connection(job_id, qbo_conn, user, *, source):
    """Best-effort revoke at Intuit, then drop encrypted tokens for one job.

    Always deletes the local row, even if the Intuit revoke call failed —
    once the encrypted refresh token is gone we can't reconnect with it,
    and the user is guaranteed the local app no longer holds credentials
    for that QuickBooks company.
    """
    revoke_attempted = False
    revoke_ok = False
    intuit_tid = None
    if qbo_conn and qbo_conn.get("refresh_token_enc"):
        try:
            refresh_plain = decrypt_token(qbo_conn["refresh_token_enc"])
            revoke_attempted = True
            revoke_ok = qbo_auth.revoke_token(refresh_plain)
            intuit_tid = getattr(qbo_auth, "last_intuit_tid", None)
        except Exception:  # noqa: BLE001
            revoke_ok = False

    qbo_connections.pop(job_id, None)
    if session.get("pending_job_id") == job_id:
        session.pop("pending_job_id", None)
    db.delete_qbo_connection(job_id)

    job = jobs.get(job_id)
    if job:
        job["qbo_connected"] = False
        if not job.get("status", "").startswith("Imported"):
            job["status"] = "Ready for QBO connection"
        job["qbo_results"] = None
        job["import_summary"] = None
        job["verification"] = None
        _save_job(job_id)

    details = f"source={source} revoke_attempted={revoke_attempted} revoke_ok={revoke_ok}"
    _audit(
        "qbo_disconnected",
        target_type="job", target_id=job_id,
        details=_audit_details_with_tid(details, intuit_tid),
    )
    return revoke_attempted, revoke_ok


@app.route("/disconnect", methods=["GET", "POST"])
@app.route("/quickbooks/disconnect", methods=["GET", "POST"])
def public_disconnect():
    """Public Disconnect page registered with Intuit as the Disconnect URL.

    Behavior:
      * Anyone (logged out): renders an explanation of how to disconnect
        from QuickBooks, including the in-app path and the manual
        QuickBooks app-settings path. No data is required to render this.
      * Logged in with active QBO connections: renders the same
        explanation plus a list of the firm's connected QuickBooks
        companies and a confirmation form to revoke + remove tokens.
        Submitting the form triggers a server-side revoke call to Intuit
        for each connection, then deletes the encrypted token rows.

    Tokens are NEVER rendered. Only the realmId, company name, and
    connected_at timestamp are shown.
    """
    user = current_user()
    connections = []
    if user:
        connections = db.list_qbo_connections_for_firm(user["firm_id"])

    if request.method == "POST":
        if not user:
            flash("Please log in first to disconnect QuickBooks for your firm.", "error")
            return redirect(url_for("login", next=url_for("public_disconnect")))
        confirmation = (request.form.get("confirm_disconnect") or "").strip().upper()
        if confirmation != "DISCONNECT":
            flash(
                "Disconnect not confirmed. Type DISCONNECT in the confirmation "
                "box and try again.",
                "error",
            )
            return redirect(url_for("public_disconnect"))

        revoked = 0
        attempted = 0
        for row in connections:
            qbo_conn = _get_qbo_connection(row["job_id"])
            if not qbo_conn:
                continue
            attempted_one, revoked_one = _revoke_and_delete_qbo_connection(
                row["job_id"], qbo_conn, user, source="public_disconnect",
            )
            if attempted_one:
                attempted += 1
                if revoked_one:
                    revoked += 1

        # Defensive fallback: if any rows survived (e.g. a connection was
        # created between the list and the loop), wipe them now. This
        # guarantees the post-condition: no QBO tokens remain for this firm.
        db.delete_qbo_connections_for_firm(user["firm_id"])
        _audit(
            "qbo_disconnect_all",
            details=f"attempted={attempted} revoked={revoked}",
        )
        if attempted == 0:
            flash(
                "No active QuickBooks connections to disconnect for this firm.",
                "info",
            )
        elif revoked == attempted:
            flash(
                f"Disconnected {attempted} QuickBooks connection(s). "
                "Tokens have been revoked at Intuit and removed from this app.",
                "success",
            )
        else:
            flash(
                f"Disconnected {attempted} QuickBooks connection(s) locally. "
                f"Intuit's revoke endpoint accepted {revoked} of {attempted} requests; "
                "any unrevoked refresh tokens are now deleted from this app and "
                "can also be revoked manually in QuickBooks → Apps → Connected apps.",
                "success",
            )
        return redirect(url_for("public_disconnect"))

    return render_template(
        "disconnect.html",
        connections=connections,
        is_logged_in=bool(user),
    )


@app.route("/quickbooks", methods=["GET"])
@login_required
def quickbooks_manage():
    """Per-firm dashboard for managing QuickBooks connections.

    Lists every job in this firm that currently has stored QBO tokens,
    with realmId, company name, connected_at, and links to reconnect
    (re-run OAuth for that job) or disconnect (revoke + drop tokens).
    Tokens are never rendered.
    """
    user = current_user()
    rows = db.list_qbo_connections_for_firm(user["firm_id"])
    # Annotate each connection with the parent job's company so the page
    # is readable even when CompanyInfo failed at connect time.
    firm_jobs = {j["id"]: j for j in db.list_jobs_for_firm(user["firm_id"], limit=500)}
    for r in rows:
        parent = firm_jobs.get(r["job_id"]) or {}
        r["job_company"] = parent.get("company")
        r["job_status"] = parent.get("status")
    return render_template(
        "quickbooks-manage.html",
        connections=rows,
        production_blockers=_qbo_production_blockers(),
    )


@app.route("/jobs/<job_id>/disconnect-qbo", methods=["POST"])
@login_required
def disconnect_qbo(job_id):
    job, user = _job_or_403(job_id)

    qbo_conn = _get_qbo_connection(job_id)
    _revoke_and_delete_qbo_connection(
        job_id, qbo_conn, user, source="job_detail",
    )

    flash(
        "Disconnected QuickBooks. Click Connect to QuickBooks and choose the "
        "company where you created the matching accounts.",
        "success",
    )
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/<job_id>/import-to-qbo", methods=["POST"])
@login_required
def import_to_qbo(job_id):
    job, _user = _job_or_403(job_id)
    qbo_conn = _get_qbo_connection(job_id)

    # Multi-report safety gate. Trial Balance and Trust Listing are
    # validation/reconciliation artifacts and must never auto-post to QBO
    # from this route. Chart of Accounts uses its own /coa-preview flow.
    # We deliberately fail closed here so a future UI bug that surfaces
    # the GL "Import to QBO" button on a non-GL job cannot post anything.
    report_type = job.get("report_type") or REPORT_GENERAL_LEDGER
    if report_type != REPORT_GENERAL_LEDGER:
        flash(
            f"Import to QuickBooks is not available for {REPORT_LABELS.get(report_type, report_type)}. "
            "This report type is parsed for validation and reconciliation only.",
            "error",
        )
        _audit(
            "import_blocked_report_type",
            target_type="job", target_id=job_id,
            details=f"report_type={report_type}",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    if not qbo_conn:
        flash("QBO connection not found. Connect to QuickBooks first.", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    if not QBO_REAL_IMPORT:
        job["status"] = "Import to QBO initiated (demo mode)"
        _save_job(job_id)
        _audit("import_demo", target_type="job", target_id=job_id)
        flash(
            "Demo mode: no journal entries were sent to QuickBooks. "
            "Set QBO_REAL_IMPORT=1 in the environment and restart to perform a real sandbox import.",
            "info",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    # Production-mode final confirmation. The job-detail page surfaces a
    # two-step flow: the first POST (no confirm_import) lands on the
    # confirmation card showing connected company + file summary; the
    # user must re-submit with confirm_import=IMPORT to actually post.
    # Sandbox-mode imports skip this so existing beta flows are unchanged.
    if QBO_ENVIRONMENT == "production":
        confirmation = (request.form.get("confirm_import") or "").strip().upper()
        if confirmation != "IMPORT":
            job["pending_production_confirm"] = True
            _save_job(job_id)
            _audit(
                "import_confirmation_shown",
                target_type="job", target_id=job_id,
                details=f"realm={qbo_conn.get('realm_id')} company={qbo_conn.get('company_name') or ''}",
            )
            flash(
                "Production safety check: this will post real journal entries to "
                f"QuickBooks Online company '"
                f"{qbo_conn.get('company_name') or qbo_conn.get('realm_id')}'. "
                "Review the import summary and type IMPORT in the confirmation "
                "box to proceed.",
                "info",
            )
            return redirect(url_for("job_detail", job_id=job_id))
        # Clear the pending flag once confirmed.
        if job.get("pending_production_confirm"):
            job["pending_production_confirm"] = False
            _save_job(job_id)

    user = current_user()
    try:
        qbo, qbo_conn = _get_qbo_client(job_id, user)
    except QBOAuthExpired as e:
        _audit("qbo_token_refresh_failed", target_type="job", target_id=job_id, details=str(e))
        flash("QuickBooks connection expired. Please reconnect.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    realm_id = qbo_conn["realm_id"]

    # Decrypt the original uploaded PCLaw CSV. We use the source file (not the
    # flat QBO-import CSV) because that's where transaction_id grouping lives.
    encrypted_in = UPLOAD_DIR / job["encrypted_file"]
    temp_csv = UPLOAD_DIR / f"temp_import_{job_id}.csv"
    decrypt_file(encrypted_in, temp_csv)

    try:
        with temp_csv.open("r", newline="", encoding="utf-8-sig") as f:
            sample_reader = _csv.DictReader(f)
            fieldnames = sample_reader.fieldnames or []

        # Always fetch QBO accounts first so we can either map or fall back.
        try:
            qbo_accounts = qbo.get_accounts()
        except requests.HTTPError as e:
            flash(
                f"Could not query QBO accounts ({e.response.status_code}). "
                "The access token may have expired — reconnect and try again.",
                "error",
            )
            return redirect(url_for("job_detail", job_id=job_id))

        if is_gl_format(fieldnames):
            rows = load_general_ledger_csv(temp_csv)

            # === Duplicate-import prevention =================================
            # Block if this exact file content has already been imported into
            # this exact realm successfully. We also check transaction_ids in
            # case the user re-exported the same period to a slightly
            # different file but with the same JEs.
            file_sha = job.get("file_sha256")
            if file_sha:
                prior = history.has_completed_import(file_sha, realm_id)
                if prior:
                    job["status"] = "Duplicate blocked"
                    _save_job(job_id)
                    _audit("import_blocked", target_type="job", target_id=job_id,
                           details=f"file_sha256 already imported (#{prior['id']})")
                    flash(
                        f"Duplicate import blocked: this exact file was already imported "
                        f"to this QuickBooks company on {prior['created_at'][:19].replace('T', ' ')} UTC "
                        f"(import #{prior['id']}, {prior['transaction_count']} JEs). "
                        "Delete the prior import in QBO if you really want to re-post, "
                        "or use a different ledger file.",
                        "error",
                    )
                    return redirect(url_for("job_detail", job_id=job_id))

            grouped_for_check = group_rows_by_transaction(rows)
            already = history.has_completed_transactions(
                grouped_for_check.keys(), realm_id
            )
            if already:
                job["status"] = "Duplicate blocked"
                _save_job(job_id)
                _audit("import_blocked", target_type="job", target_id=job_id,
                       details=f"transaction_id overlap: {sorted(already)}")
                flash(
                    "Duplicate import blocked: these transaction_id values already "
                    f"exist in a prior successful import to this company: {sorted(already)}. "
                    "Remove them from the CSV (or delete the prior JEs in QBO) and retry.",
                    "error",
                )
                return redirect(url_for("job_detail", job_id=job_id))

            # Build the QBO auto-match mappings, then overlay any saved
            # account mappings the user configured for this firm+realm. Saved
            # entries take priority over auto-match.
            auto_by_number = build_account_mapping_from_numbers(qbo_accounts)
            auto_by_name = build_account_mapping_from_names(qbo_accounts)
            saved_mappings = db.list_account_mappings(user["firm_id"], realm_id)

            # Decide the lookup mode: numbers are preferred when any exist,
            # because they're stable across name renames.
            if auto_by_number or any(m.get("pclaw_account_number") for m in saved_mappings):
                mapping = dict(auto_by_number)
                mapping_mode = "number"
                for m in saved_mappings:
                    if m.get("pclaw_account_number"):
                        mapping[str(m["pclaw_account_number"])] = m["qbo_account_id"]
            else:
                mapping = dict(auto_by_name)
                mapping_mode = "name"
                for m in saved_mappings:
                    if m.get("pclaw_account_name"):
                        mapping[m["pclaw_account_name"]] = m["qbo_account_id"]

            unmapped = find_unmapped_accounts(rows, mapping, mapping_mode)
            if unmapped:
                # Beginner-safe: don't silently fake success. The import
                # block is correct safety behavior — we never create or
                # silently remap QBO accounts here — but the raw "Cannot
                # import" flash assumes the operator knows what to do.
                # Build a context-aware CTA based on whether the firm
                # has uploaded their Account List yet and whether they've
                # already mirrored it into QuickBooks.
                coa_ctx = _firm_latest_coa_state(user["firm_id"])
                guidance = classify_unmapped_accounts(
                    unmapped_keys=unmapped,
                    mapping_mode=mapping_mode,
                    coa_rows=coa_ctx.get("coa_rows") or [],
                    coa_create_history=coa_ctx.get("coa_create_history") or [],
                    job_id=job_id,
                    company_name=qbo_conn.get("company_name"),
                    environment=QBO_ENVIRONMENT,
                )
                job["status"] = "Import blocked: unmapped accounts"
                _audit("import_blocked", target_type="job", target_id=job_id,
                       details=(
                           f"unmapped accounts: {sorted(unmapped)} "
                           f"action={guidance.action}"
                       ))
                # Stash both the raw list (back-compat for older banners /
                # API consumers) and the structured guidance the
                # job-detail page renders.
                job["unmapped_accounts"] = sorted(unmapped)
                job["unmapped_account_guidance"] = guidance.to_dict()
                _save_job(job_id)
                accounts_display = "; ".join(a.display for a in guidance.accounts)
                flash(
                    f"{guidance.headline} Accounts missing in "
                    f"{guidance.company_label}: {accounts_display}.",
                    "error",
                )
                return redirect(url_for("job_detail", job_id=job_id))

            type_index = build_account_type_index(qbo_accounts)
            payloads = build_journal_entries_from_gl(
                rows, mapping, mapping_mode=mapping_mode, account_type_index=type_index
            )

            # QBO requires Entity (Customer/Vendor) on A/R and A/P lines.
            # Resolve and inject before posting; create missing entities.
            try:
                new_entities = _resolve_entity_hints(qbo, payloads)
            except QBOError as e:
                job["status"] = "Import failed (entity setup)"
                job["last_error"] = qbo_error_hint.parse(str(e), intuit_tid=e.intuit_tid)
                _save_job(job_id)
                _audit(
                    "import_failed",
                    target_type="job",
                    target_id=job_id,
                    details=_audit_details_with_tid(f"entity setup: {e}", e.intuit_tid),
                )
                tid_suffix = f" (Intuit support reference: {e.intuit_tid})" if e.intuit_tid else ""
                flash(
                    "Could not set up the Customer/Vendor required by QBO for "
                    f"Accounts Receivable / Accounts Payable lines: {e}{tid_suffix}",
                    "error",
                )
                return redirect(url_for("job_detail", job_id=job_id))

            # Compute source totals (used for both verification and history).
            source_debit = sum(money(r["debit"]) for r in rows)
            source_credit = sum(money(r["credit"]) for r in rows)

            # Pair each payload with its PCLaw transaction_id in deterministic
            # order so we can match created JE IDs back to source rows.
            txn_ids = list(grouped_for_check.keys())
            created = []
            created_transactions = []
            for txn_id, payload in zip(txn_ids, payloads):
                resp = qbo.create_journal_entry(payload)
                je = resp.get("JournalEntry", {})
                created.append({
                    "Id": je.get("Id"),
                    "DocNumber": je.get("DocNumber"),
                    "TxnDate": je.get("TxnDate"),
                    "transaction_id": txn_id,
                })
                created_transactions.append({
                    "transaction_id": txn_id,
                    "qbo_je_id": je.get("Id"),
                    "doc_number": je.get("DocNumber"),
                    "txn_date": je.get("TxnDate"),
                })

            # Record the import in history immediately. Even if verification
            # below fails, the JEs ARE in QBO and we want a permanent record
            # so the duplicate guard works on the next attempt.
            import_id = history.record_import(
                job_id=job_id,
                realm_id=realm_id,
                file_sha256=job.get("file_sha256", ""),
                company_name=qbo_conn.get("company_name"),
                transaction_count=len(created),
                debit_total=source_debit,
                credit_total=source_credit,
                status="success",
                created_transactions=created_transactions,
                created_entities=new_entities,
            )
            job["last_import_id"] = import_id

            job["status"] = f"Imported {len(created)} journal entries to QuickBooks"
            job["unmapped_accounts"] = None
            job["unmapped_account_guidance"] = None
            job["last_error"] = None
            _save_job(job_id)
            _audit("import_success", target_type="job", target_id=job_id,
                   details=f"{len(created)} JEs, debit=${source_debit}, credit=${source_credit}")
            job["qbo_results"] = created
            job["import_summary"] = {
                "source_transaction_count": len(txn_ids),
                "qbo_je_count": len(created),
                "source_debit_total": str(source_debit),
                "source_credit_total": str(source_credit),
                "balanced": source_debit == source_credit,
            }
            entity_msg = ""
            if new_entities:
                names = ", ".join(f"{k} '{n}'" for k, n, _ in new_entities)
                entity_msg = f" Also created in QBO: {names}."
            flash(
                f"Created {len(created)} JournalEntry record(s) in QuickBooks Online.{entity_msg}",
                "success",
            )

            # Run verification automatically (best-effort). Failure does not
            # roll back; the import already happened.
            try:
                _verify_import(job, qbo)
            except Exception as ve:  # noqa: BLE001
                job["verification"] = {
                    "status": "error",
                    "error": str(ve),
                }
            # Persist the full success snapshot (status + qbo_results +
            # import_summary + verification) so the job survives a restart.
            _save_job(job_id)
        else:
            # Fallback for the simple flat sample CSV (no transaction_id):
            # post a single tiny test JournalEntry so the user can confirm the
            # real write path works end-to-end. We do NOT pretend the full
            # ledger imported.
            txn_date = datetime.utcnow().strftime("%Y-%m-%d")
            payload = build_test_journal_entry(qbo_accounts, txn_date=txn_date)
            resp = qbo.create_journal_entry(payload)
            je = resp.get("JournalEntry", {})
            job["status"] = "Imported 1 test JournalEntry (CSV lacked transaction_id grouping)"
            job["qbo_results"] = [{"Id": je.get("Id"), "DocNumber": je.get("DocNumber"), "TxnDate": je.get("TxnDate")}]
            _save_job(job_id)
            flash(
                "Your CSV doesn't include a transaction_id column, so a single $1.00 "
                "test JournalEntry was created in QuickBooks to verify the connection. "
                "Upload a GL with columns "
                f"{', '.join(GL_REQUIRED_COLUMNS)} to import the real ledger.",
                "info",
            )

    except QBOError as e:
        job["status"] = "Import failed (QBO error)"
        job["last_error"] = qbo_error_hint.parse(str(e), intuit_tid=e.intuit_tid)
        _save_job(job_id)
        _audit(
            "import_failed",
            target_type="job",
            target_id=job_id,
            details=_audit_details_with_tid(str(e), e.intuit_tid),
        )
        hint = job["last_error"]
        msg = hint["summary"]
        if hint.get("action"):
            msg = f"{msg} {hint['action']}"
        if e.intuit_tid:
            msg = f"{msg} (Intuit support reference: {e.intuit_tid})"
        flash(msg, "error")
    except ValueError as e:
        job["status"] = "Import failed (validation)"
        job["last_error"] = {
            "summary": str(e),
            "action": None,
            "technical_detail": str(e),
            "status_code": None,
            "intuit_tid": None,
        }
        _save_job(job_id)
        _audit("import_failed", target_type="job", target_id=job_id, details=str(e))
        flash(f"Import failed: {e}", "error")
    except Exception as e:  # noqa: BLE001
        job["status"] = "Import failed"
        job["last_error"] = {
            "summary": "Unexpected error during import. The full message is in the technical details below.",
            "action": "Try again, and if the problem persists, contact support with the job ID.",
            "technical_detail": str(e),
            "status_code": None,
            "intuit_tid": None,
        }
        _save_job(job_id)
        _audit("import_failed", target_type="job", target_id=job_id, details=str(e))
        flash(f"Import failed: {e}", "error")
    finally:
        temp_csv.unlink(missing_ok=True)

    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/<job_id>/verify", methods=["POST"])
@login_required
def verify_import(job_id):
    job, _user = _job_or_403(job_id)
    qbo_conn = _get_qbo_connection(job_id)
    if not qbo_conn:
        flash("QBO connection not found. Connect to QuickBooks first.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    if not job.get("qbo_results"):
        flash("Nothing to verify yet — run the import first.", "info")
        return redirect(url_for("job_detail", job_id=job_id))

    user = current_user()
    try:
        qbo, _conn = _get_qbo_client(job_id, user)
    except QBOAuthExpired as e:
        _audit("qbo_token_refresh_failed", target_type="job", target_id=job_id, details=str(e))
        flash("QuickBooks connection expired. Please reconnect.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    try:
        _verify_import(job, qbo)
    except QBOError as e:
        tid_suffix = f" (Intuit support reference: {e.intuit_tid})" if e.intuit_tid else ""
        flash(f"Verification failed (QBO error): {e}{tid_suffix}", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    except Exception as e:  # noqa: BLE001
        flash(f"Verification failed: {e}", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    v = job["verification"]
    _save_job(job_id)
    _audit("verify", target_type="job", target_id=job_id, details=v.get("status"))
    if v["status"] == "ok":
        flash(
            f"Verification OK: {v['qbo_je_count']} JournalEntry record(s) confirmed in QBO; "
            f"debits ${v['qbo_debit_total']} = source ${v['source_debit_total']}.",
            "success",
        )
    else:
        flash(
            "Verification mismatch — see Verification result panel for details.",
            "error",
        )
    return redirect(url_for("job_detail", job_id=job_id))


def _load_job_gl_rows(job):
    """Decrypt the job's source CSV and return (rows, fieldnames).

    Returns ``(None, [])`` if the file is not in the GL format. The caller
    is responsible for telling the user.
    """
    encrypted_in = UPLOAD_DIR / job["encrypted_file"]
    temp_csv = UPLOAD_DIR / f"temp_preview_{job['id']}.csv"
    decrypt_file(encrypted_in, temp_csv)
    try:
        with temp_csv.open("r", newline="", encoding="utf-8-sig") as f:
            sample = _csv.DictReader(f)
            fieldnames = sample.fieldnames or []
        if not is_gl_format(fieldnames):
            return None, fieldnames
        rows = load_general_ledger_csv(temp_csv)
        return rows, fieldnames
    finally:
        temp_csv.unlink(missing_ok=True)


@app.route("/jobs/<job_id>/preview-import")
@login_required
def preview_import(job_id):
    """Dry-run preview: show exactly what would be posted to QuickBooks.

    Beginner-safe and **non-destructive**: this view never calls a QBO
    write/create endpoint. It uses the same parsing + mapping logic the
    importer does, but stops short of POSTing. The user can review the
    counts, totals, and unmapped accounts here before clicking the real
    import button on the job detail page.
    """
    job, user = _job_or_403(job_id)
    qbo_conn = _get_qbo_connection(job_id) or {}

    rows, fieldnames = (None, [])
    qbo_accounts = {"QueryResponse": {"Account": []}}
    preview = None
    preview_error = None
    saved_mappings = []

    try:
        rows, fieldnames = _load_job_gl_rows(job)
    except ValueError as e:
        preview_error = str(e)
    except FileNotFoundError:
        preview_error = "The uploaded ledger file could not be found on disk."
    except Exception as e:  # noqa: BLE001
        preview_error = f"Could not read the uploaded ledger: {e}"

    if rows is None and not preview_error:
        preview_error = (
            "This CSV is not in the General Ledger format expected for QBO import. "
            "Re-upload with columns: " + ", ".join(GL_REQUIRED_COLUMNS) + "."
        )

    if rows is not None and qbo_conn:
        # READ-ONLY: get_accounts is a SELECT against the QBO query API. It
        # does NOT mutate anything in the customer's QuickBooks company.
        try:
            qbo, _conn = _get_qbo_client(job_id, user)
            qbo_accounts = qbo.get_accounts()
            saved_mappings = db.list_account_mappings(user["firm_id"], qbo_conn["realm_id"])
        except QBOAuthExpired:
            preview_error = (
                "QuickBooks connection expired while loading the chart of accounts. "
                "Reconnect to QuickBooks and try again."
            )
        except Exception as e:  # noqa: BLE001
            preview_error = f"Could not query QuickBooks chart of accounts: {e}"

    if rows is not None and preview_error is None:
        preview = build_dry_run_preview(rows, qbo_accounts, saved_mappings)

    _audit("import_preview", target_type="job", target_id=job_id,
           details=(f"je={preview['journal_entry_count']} unmapped={preview['unmapped_account_count']}"
                    if preview else preview_error or "no preview"))

    return render_template(
        "preview-import.html",
        job=job,
        qbo_connection=qbo_conn,
        preview=preview,
        preview_error=preview_error,
        qbo_real_import=QBO_REAL_IMPORT,
    )


@app.route("/jobs/<job_id>/validation-report.csv")
@login_required
def validation_report_csv(job_id):
    """Download a per-job validation report as CSV.

    Auth + firm scoping enforced via ``_job_or_403``. Every cell is sent
    through ``csv_safety.sanitize_csv_cell`` so a malicious description
    field cannot turn the report into a spreadsheet formula.
    """
    job, user = _job_or_403(job_id)

    preflight = job.get("preflight") or {}
    qbo_conn = _get_qbo_connection(job_id)
    preview = None

    # Best-effort: include the mapping preview when we can talk to QBO,
    # but never let that fail the download. The validation report is the
    # one thing the user should be able to grab even when QBO is down.
    if qbo_conn:
        try:
            rows, _fn = _load_job_gl_rows(job)
            if rows is not None:
                qbo, _conn = _get_qbo_client(job_id, user)
                qbo_accounts = qbo.get_accounts()
                saved = db.list_account_mappings(user["firm_id"], qbo_conn["realm_id"])
                preview = build_dry_run_preview(rows, qbo_accounts, saved)
        except Exception:  # noqa: BLE001
            preview = None

    body = render_validation_csv(job, preflight, preview)
    _audit("validation_report_download", target_type="job", target_id=job_id)
    filename = f"validation-{job_id}.csv"
    return Response(
        body,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/jobs/<job_id>/reconciliation-report.csv")
@login_required
def reconciliation_report_csv(job_id):
    """Download a post-import reconciliation report as CSV.

    Returns 404 if no successful import exists for this job — we don't
    want to hand out an empty report and confuse the user.
    """
    job, _user = _job_or_403(job_id)
    import_rec = history.get_latest_completed_import_for_job(job_id)
    if not import_rec:
        flash(
            "No completed import yet for this job — run the import first, "
            "then download the reconciliation report.",
            "info",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    reversal = history.get_reversal_for_import(import_rec["id"])
    report = build_reconciliation_report(
        job, import_rec, verification=job.get("verification"), reversal=reversal,
    )
    body = render_reconciliation_csv(report)
    _audit("reconciliation_report_download", target_type="job", target_id=job_id,
           details=f"import_id={import_rec['id']}")
    filename = f"reconciliation-{job_id}.csv"
    return Response(
        body,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/jobs/<job_id>/account-mapping", methods=["GET", "POST"])
@login_required
def account_mapping(job_id):
    """List PCLaw accounts in this job's CSV alongside QBO accounts and let
    the user save (firm_id, realm_id, pclaw_*, qbo_account_id) mappings.

    The mapping is by PCLaw account_number when present, otherwise by
    account_name. Saved mappings then override the auto-match in the
    import flow.

    Resilience notes (production polish):
      - All expected failure modes (no QBO connection, expired tokens,
        missing/corrupt encrypted upload, QBO API down, transient form
        re-submits via browser back) flash a friendly message and redirect
        to the job page rather than 500-ing.
      - POST is idempotent: save_account_mapping is an upsert keyed on
        (firm_id, realm_id, pclaw_*); resubmitting the same form is safe.
      - If the user clicks "Map accounts" again after a previous save,
        their saved selections render as "Saved" and remain editable.
    """
    job, user = _job_or_403(job_id)
    qbo_conn = _get_qbo_connection(job_id)
    if not qbo_conn:
        flash("Connect this job to QuickBooks first.", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    # Refresh tokens if needed; show a clean error if refresh fails.
    try:
        qbo, qbo_conn = _get_qbo_client(job_id, user)
    except QBOAuthExpired as e:
        _audit("qbo_token_refresh_failed", target_type="job", target_id=job_id, details=str(e))
        flash("QuickBooks connection expired. Please reconnect.", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    realm_id = qbo_conn["realm_id"]

    # Fetch QBO accounts (the dropdown source of truth). Any QBO error here
    # — flaky network, throttle, brief Intuit outage — should send the user
    # back to the job page with a friendly message instead of a traceback.
    try:
        qbo_accounts_resp = qbo.get_accounts()
    except QBOError as e:
        tid_suffix = f" (Intuit support reference: {e.intuit_tid})" if e.intuit_tid else ""
        flash(f"Could not query QBO accounts: {e}{tid_suffix}", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    except Exception as e:  # noqa: BLE001 — last-resort net for unexpected client errors
        _audit("account_mapping_qbo_error", target_type="job", target_id=job_id, details=str(e))
        flash(
            "Could not load QuickBooks accounts right now. Please try again "
            "in a moment, or reconnect QuickBooks if the problem persists.",
            "error",
        )
        return redirect(url_for("job_detail", job_id=job_id))
    qbo_accounts = qbo_accounts_resp.get("QueryResponse", {}).get("Account", [])

    if request.method == "POST":
        # Form posts pclaw rows as `mapping[<index>]_*` fields. Anything blank
        # means "skip" / leave unmapped. The save is upsert so re-submission
        # (e.g. browser back + retry) is safe.
        saved = 0
        skipped = 0
        try:
            for key, qbo_acct_id in request.form.items(multi=False):
                if not key.startswith("mapping[") or not key.endswith("]"):
                    continue
                qbo_acct_id = (qbo_acct_id or "").strip()
                if not qbo_acct_id:
                    continue
                row_idx = key[len("mapping["):-1]
                pclaw_num = (request.form.get(f"pclaw_num[{row_idx}]") or "").strip() or None
                pclaw_name = (request.form.get(f"pclaw_name[{row_idx}]") or "").strip() or None
                if not pclaw_num and not pclaw_name:
                    # Empty/garbage row — likely a stale form re-submitted
                    # after the underlying CSV no longer has this account.
                    skipped += 1
                    continue
                qbo_match = next((a for a in qbo_accounts if a.get("Id") == qbo_acct_id), None)
                db.save_account_mapping(
                    firm_id=user["firm_id"],
                    realm_id=realm_id,
                    pclaw_account_number=pclaw_num,
                    pclaw_account_name=pclaw_name,
                    qbo_account_id=qbo_acct_id,
                    qbo_account_name=qbo_match.get("Name") if qbo_match else None,
                    qbo_account_type=qbo_match.get("AccountType") if qbo_match else None,
                )
                saved += 1
        except Exception as e:  # noqa: BLE001
            _audit("account_mapping_save_error", target_type="job", target_id=job_id, details=str(e))
            flash(
                "Something went wrong while saving your mappings. Please "
                "reload the page and try again.",
                "error",
            )
            return redirect(url_for("account_mapping", job_id=job_id))
        _audit("account_mapping_saved", target_type="job", target_id=job_id,
               details=f"saved {saved} mapping(s) skipped {skipped}")
        if saved:
            flash(f"Saved {saved} account mapping(s). Click Import to retry.", "success")
        else:
            flash(
                "No account mappings were changed. Pick a QuickBooks account "
                "for at least one row and click Save mappings.",
                "info",
            )
        return redirect(url_for("account_mapping", job_id=job_id))

    # Build the list of unique PCLaw accounts in this job's source CSV.
    # Defensive paths cover: encrypted file deleted (e.g. job purged
    # between visits), corrupt ciphertext (key rotated since upload),
    # CSV decode errors (encoding edge cases). All redirect cleanly.
    encrypted_in = UPLOAD_DIR / job["encrypted_file"]
    temp_csv = UPLOAD_DIR / f"temp_mapping_{job_id}.csv"
    if not encrypted_in.exists():
        _audit("account_mapping_missing_file", target_type="job", target_id=job_id)
        flash(
            "The original upload for this job is no longer available. "
            "Re-upload the PCLaw export to continue.",
            "error",
        )
        return redirect(url_for("job_detail", job_id=job_id))
    try:
        decrypt_file(encrypted_in, temp_csv)
    except Exception as e:  # noqa: BLE001
        _audit("account_mapping_decrypt_error", target_type="job", target_id=job_id, details=str(e))
        flash(
            "We could not read the saved upload for this job. Please "
            "re-upload the PCLaw export to continue.",
            "error",
        )
        # Best-effort cleanup of any partial output.
        temp_csv.unlink(missing_ok=True)
        return redirect(url_for("job_detail", job_id=job_id))
    try:
        try:
            with temp_csv.open("r", newline="", encoding="utf-8-sig") as f:
                reader = _csv.DictReader(f)
                if not is_gl_format(reader.fieldnames or []):
                    flash(
                        "Account mapping is only available for the rich PCLaw GL "
                        "format (with transaction_id and account_number columns).",
                        "info",
                    )
                    return redirect(url_for("job_detail", job_id=job_id))
                seen = {}
                for r in reader:
                    num = (r.get("account_number") or "").strip() or None
                    name = (r.get("account_name") or "").strip() or None
                    key = (num, name)
                    if key in seen:
                        continue
                    seen[key] = {"number": num, "name": name}
            pclaw_accounts = list(seen.values())
        except (UnicodeDecodeError, _csv.Error) as e:
            _audit("account_mapping_csv_error", target_type="job", target_id=job_id, details=str(e))
            flash(
                "The saved upload looks corrupt or is in an unexpected format. "
                "Please re-upload the PCLaw export to continue.",
                "error",
            )
            return redirect(url_for("job_detail", job_id=job_id))
    finally:
        temp_csv.unlink(missing_ok=True)

    # Existing saved mappings keyed for fast template lookup.
    saved_mappings = db.list_account_mappings(user["firm_id"], realm_id)
    saved_by_key = {(m["pclaw_account_number"], m["pclaw_account_name"]): m for m in saved_mappings}

    # Build auto-match suggestions: prefer AcctNum, fall back to Name.
    auto_by_number = {str(a.get("AcctNum")): a for a in qbo_accounts if a.get("AcctNum")}
    auto_by_name = {a.get("Name"): a for a in qbo_accounts if a.get("Name")}

    rows = []
    for idx, pa in enumerate(pclaw_accounts):
        saved = saved_by_key.get((pa["number"], pa["name"]))
        suggestion = None
        if not saved:
            if pa["number"] and pa["number"] in auto_by_number:
                suggestion = auto_by_number[pa["number"]]
            elif pa["name"] and pa["name"] in auto_by_name:
                suggestion = auto_by_name[pa["name"]]
        rows.append({
            "idx": idx,
            "pclaw_number": pa["number"],
            "pclaw_name": pa["name"],
            "current_qbo_id": (saved or {}).get("qbo_account_id") or (suggestion or {}).get("Id"),
            "current_qbo_name": (saved or {}).get("qbo_account_name") or (suggestion or {}).get("Name"),
            "is_saved": bool(saved),
            "is_suggestion": bool(suggestion and not saved),
        })

    return render_template(
        "account-mapping.html",
        job=job,
        qbo_connection=qbo_conn,
        rows=rows,
        qbo_accounts=sorted(
            qbo_accounts,
            key=lambda a: (a.get("AccountType") or "", a.get("Name") or ""),
        ),
    )


def _build_reversal_payload(
    original_je, original_je_id, job_id, reversal_date,
    import_id=None, transaction_id=None,
):
    """Build a JournalEntry payload that reverses `original_je`.

    Each line keeps the same `Amount` and `AccountRef`, swaps `PostingType`
    Debit/Credit, and preserves any `Entity` block (required by QBO for
    A/R and A/P lines).

    To make reversal entries obvious in the QBO Journal report:
      - Every line `Description` is prefixed with "REVERSAL - " followed by
        the original line description (or a synthetic label) and a tail
        that names the original JournalEntry Id and PCLaw transaction_id.
      - `DocNumber` is set to "REV-<original_je_id>" (capped at QBO's 21-char
        limit) so it shows up next to the entry in the Journal report.
      - `PrivateNote` records the import and original JE for audit.
    """
    txn_suffix_parts = [f"orig JE {original_je_id}"]
    if transaction_id:
        txn_suffix_parts.append(f"PCLaw {transaction_id}")
    txn_suffix = " | ".join(txn_suffix_parts)

    new_lines = []
    for line in original_je.get("Line", []) or []:
        detail = line.get("JournalEntryLineDetail")
        if not detail:
            continue
        flipped = dict(detail)
        if flipped.get("PostingType") == "Debit":
            flipped["PostingType"] = "Credit"
        elif flipped.get("PostingType") == "Credit":
            flipped["PostingType"] = "Debit"
        original_desc = (line.get("Description") or "").strip()
        if original_desc:
            base = f"REVERSAL - {original_desc} ({txn_suffix})"
        else:
            base = f"REVERSAL - {txn_suffix}"
        if len(base) > 1000:
            base = base[:997] + "..."
        new_lines.append({
            "Description": base,
            "Amount": line.get("Amount"),
            "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": flipped,
        })

    doc_number = f"REV-{original_je_id}"
    if len(doc_number) > 21:
        doc_number = doc_number[:21]

    private_note = (
        f"REVERSAL of PCLaw import (job {job_id}"
        + (f", import #{import_id}" if import_id is not None else "")
        + f"); original QBO JournalEntry Id={original_je_id}"
        + (f"; PCLaw transaction_id={transaction_id}" if transaction_id else "")
    )

    return {
        "TxnDate": reversal_date,
        "DocNumber": doc_number,
        "PrivateNote": private_note,
        "Line": new_lines,
    }


@app.route("/jobs/<job_id>/reverse-import", methods=["POST"])
@login_required
def reverse_import(job_id):
    """Reverse a completed import by posting offsetting JournalEntries.

    This is an *accounting* reversal. The original QBO records remain
    visible — auditors can see both sides. We never call delete on the
    original JEs.

    Idempotent: refuses to reverse the same import twice. The
    `confirm_reverse` form field must be exactly the string `REVERSE`
    so a stray click can't undo work.
    """
    job, user = _job_or_403(job_id)

    if (request.form.get("confirm_reverse") or "").strip() != "REVERSE":
        flash(
            "Reversal not confirmed. Type REVERSE in the confirmation box and try again.",
            "error",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    last_import = history.get_latest_completed_import_for_job(job_id)
    if not last_import:
        _audit("import_reversal_blocked", target_type="job", target_id=job_id,
               details="no completed import")
        flash("Nothing to reverse: this job has no successful import.", "info")
        return redirect(url_for("job_detail", job_id=job_id))

    existing = history.get_reversal_for_import(last_import["id"])
    if existing:
        _audit("import_reversal_blocked", target_type="job", target_id=job_id,
               details=f"already reversed (reversal #{existing['id']})")
        flash(
            f"This import was already reversed on {existing['reversed_at'][:19].replace('T', ' ')} UTC. "
            "QuickBooks already has the offsetting journal entries.",
            "info",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    if not QBO_REAL_IMPORT:
        _audit("import_reversal_blocked", target_type="job", target_id=job_id,
               details="QBO_REAL_IMPORT off")
        flash(
            "Demo mode: no journal entries were sent to QuickBooks. "
            "Set QBO_REAL_IMPORT=1 in the environment to perform a real reversal.",
            "info",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    try:
        qbo, qbo_conn = _get_qbo_client(job_id, user)
    except QBOAuthExpired as e:
        _audit("qbo_token_refresh_failed", target_type="job", target_id=job_id, details=str(e))
        flash("QuickBooks connection expired. Please reconnect.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    if not qbo_conn:
        flash("QBO connection not found. Connect to QuickBooks first.", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    _audit("import_reversal_started", target_type="job", target_id=job_id,
           details=f"import #{last_import['id']}, {len(last_import['transactions'])} JEs")

    reversal_date = datetime.utcnow().strftime("%Y-%m-%d")
    reversal_rows = []
    try:
        for tx in last_import["transactions"]:
            original_id = tx["qbo_je_id"]
            if not original_id:
                # Skipped during the original import (rare). Record but don't post.
                reversal_rows.append({
                    "transaction_id": tx["transaction_id"],
                    "original_qbo_je_id": None,
                    "reversal_qbo_je_id": None,
                    "reversal_doc_number": None,
                    "reversal_txn_date": None,
                })
                continue
            original_je = qbo.get_journal_entry(original_id)
            if not original_je:
                raise QBOError(
                    f"QBO has no JournalEntry with Id={original_id}; "
                    "it may have been deleted manually. Aborting reversal."
                )
            payload = _build_reversal_payload(
                original_je=original_je,
                original_je_id=original_id,
                job_id=job_id,
                reversal_date=reversal_date,
                import_id=last_import["id"],
                transaction_id=tx.get("transaction_id"),
            )
            resp = qbo.create_journal_entry(payload)
            new_je = resp.get("JournalEntry", {})
            reversal_rows.append({
                "transaction_id": tx["transaction_id"],
                "original_qbo_je_id": original_id,
                "reversal_qbo_je_id": new_je.get("Id"),
                "reversal_doc_number": new_je.get("DocNumber"),
                "reversal_txn_date": new_je.get("TxnDate"),
            })
    except QBOError as e:
        # Best-effort: persist what we managed to reverse so the user can
        # see partial state and clean up manually.
        err_with_tid = f"{e} (intuit_tid={e.intuit_tid})" if e.intuit_tid else str(e)
        try:
            history.record_reversal(
                import_id=last_import["id"],
                job_id=job_id,
                firm_id=user["firm_id"],
                realm_id=qbo_conn["realm_id"],
                status="failed",
                created_by_user_id=user["id"],
                reversed_transactions=reversal_rows,
                error=err_with_tid,
            )
        except ValueError:
            pass  # already recorded; nothing to do
        _audit(
            "import_reversal_failed",
            target_type="job",
            target_id=job_id,
            details=_audit_details_with_tid(str(e), e.intuit_tid),
        )
        tid_suffix = f" (Intuit support reference: {e.intuit_tid})" if e.intuit_tid else ""
        flash(
            f"Reversal failed after creating {sum(1 for r in reversal_rows if r['reversal_qbo_je_id'])} "
            f"of {len(last_import['transactions'])} reversal entries: {e}{tid_suffix}",
            "error",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    history.record_reversal(
        import_id=last_import["id"],
        job_id=job_id,
        firm_id=user["firm_id"],
        realm_id=qbo_conn["realm_id"],
        status="success",
        created_by_user_id=user["id"],
        reversed_transactions=reversal_rows,
    )
    job["status"] = (
        f"Reversed {sum(1 for r in reversal_rows if r['reversal_qbo_je_id'])} "
        f"journal entries (original import #{last_import['id']} stays visible in QBO)"
    )
    _save_job(job_id)
    _audit("import_reversal_success", target_type="job", target_id=job_id,
           details=f"import #{last_import['id']}")
    flash(
        f"Reversal complete. {len(reversal_rows)} offsetting JournalEntry record(s) "
        "were created in QuickBooks. The original entries remain visible for audit. "
        "In QuickBooks, reversals appear as separate journal entries (DocNumber "
        "starting with 'REV-' and line descriptions prefixed 'REVERSAL'); the "
        "Journal report will list them alongside the originals rather than marking "
        "the originals as voided.",
        "success",
    )
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/<job_id>/delete", methods=["POST"])
@login_required
def delete_job(job_id):
    """Purge local app data for a job.

    Deletes the encrypted source/output files, the in-memory and DB job
    row, and the QBO connection record (encrypted tokens). Does NOT
    delete or reverse any JournalEntry records that were posted to
    QuickBooks; those stay in the firm's QBO company until explicitly
    reversed via /jobs/<id>/reverse-import.

    Duplicate-import protection is preserved: the `imports` table in the
    separate import_history database is intentionally kept so a future
    upload of the same file content into the same realm is still
    blocked. Audit log rows are preserved for the same reason.

    Requires the user to type "DELETE" in `confirm_delete` so a stray
    click on the danger button cannot wipe a job's local record.
    """
    job, user = _job_or_403(job_id)

    if (request.form.get("confirm_delete") or "").strip().upper() != "DELETE":
        flash(
            "Deletion not confirmed. Type DELETE in the confirmation box and try again.",
            "error",
        )
        return redirect(url_for("job_detail", job_id=job_id))

    had_qbo_results = bool(job.get("qbo_results"))
    last_import_id = job.get("last_import_id")

    try:
        if "encrypted_file" in job:
            (UPLOAD_DIR / job["encrypted_file"]).unlink(missing_ok=True)
        if "encrypted_output" in job:
            (OUTPUT_DIR / job["encrypted_output"]).unlink(missing_ok=True)

        if job_id in qbo_connections:
            del qbo_connections[job_id]
        jobs.pop(job_id, None)
        db.delete_job(job_id)
        _audit(
            "delete_job",
            target_type="job",
            target_id=job_id,
            details=(
                f"company={job.get('company')}"
                + (f" had_qbo_import={had_qbo_results}" if had_qbo_results else "")
                + (f" last_import_id={last_import_id}" if last_import_id else "")
                + " (QBO records preserved; import_history row preserved for duplicate guard)"
            ),
        )

        if had_qbo_results:
            flash(
                "Local job data deleted. Note: this does NOT remove any journal "
                "entries already posted to QuickBooks. To remove those, use "
                "Reverse this import on the job page before deleting next time, "
                "or void / delete them manually in QuickBooks.",
                "success",
            )
        else:
            flash(
                "Local job data deleted (encrypted file, QuickBooks tokens, job row).",
                "success",
            )
    except Exception as e:
        _audit("delete_job_failed", target_type="job", target_id=job_id, details=str(e))
        flash(f"Deletion error: {str(e)}", "error")

    return redirect(url_for("dashboard"))


@app.route("/api/jobs/<job_id>")
@login_required
def job_api(job_id):
    job, _user = _job_or_403(job_id)
    return jsonify(job)


# ---------------------------------------------------------------------------
# Operator / admin panel
#
# Gating: env var OPERATOR_EMAILS lists who is allowed in. The role column
# is per-firm and every signup creates an 'admin' for *that* firm — it is
# not a global app role and must not be used to gate this panel.
#
# The panel is read-only in v1: no triggering imports/reversals/disconnects.
# All mutation routes still require firm-scoped login.
# ---------------------------------------------------------------------------

def _is_operator():
    return operator_panel.is_operator_user(current_user())


def operator_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        if not operator_panel.is_operator_user(user):
            # 404 rather than 403 so we don't confirm the panel exists for
            # non-operators. This matches the cross-firm 404 convention
            # used elsewhere (see _job_or_403).
            abort(404)
        return view(*args, **kwargs)
    return wrapper


@app.context_processor
def _inject_operator_flag():
    """Make `is_operator` available to every template so the nav can
    conditionally show the Operator link only for allowed emails.

    Also injects ``demo_mode_enabled`` (deploy-level flag) and
    ``demo_visible`` (per-request flag combining deploy + operator
    status) so the nav can show a Demo link without leaking the
    affordance to normal production customers.
    """
    user = current_user()
    is_op = _is_operator()
    return {
        "is_operator": is_op,
        "demo_mode_enabled": demo_mode.is_demo_mode_enabled(),
        "demo_visible": demo_mode.demo_visible_for_user(user, is_op),
    }


def _workflow_stepper_context(firm_id):
    """Compute the 6-stage customer-facing workflow stepper for a firm.

    Returns a dict that callers spread into render_template() so the
    `_workflow_stepper.html` partial gets everything it needs. Keeping
    this out of the global context processor means we only pay for the
    checklist computation on the handful of pages that actually render
    the stepper (dashboard, migration checklist, cutover setup, job
    detail) — not on auth / static pages.
    """
    _cutover, items, _next = _build_firm_checklist(firm_id)
    firm_jobs = db.list_jobs_for_firm(firm_id, limit=1)
    stages = customer_workflow.build_customer_stages(
        items,
        url_for=url_for,
        has_jobs=bool(firm_jobs),
    )
    current = customer_workflow.current_stage(stages)
    return {
        "workflow_stages": [s.to_dict() for s in stages],
        "workflow_current": current.to_dict() if current else None,
        "workflow_progress": customer_workflow.progress_percent(stages),
        "workflow_completed": customer_workflow.completed_count(stages),
        "workflow_terms": customer_workflow.FRIENDLY_TERMS,
    }


@app.route("/operator")
@operator_required
def operator_dashboard():
    metrics = operator_panel.collect_metrics(db, history)
    firms = operator_panel.list_firms_overview(db, history)
    imports = operator_panel.recent_imports(history, limit=25)
    errors = operator_panel.recent_errors(db, limit=25)
    return render_template(
        "operator-dashboard.html",
        metrics=metrics,
        firms=firms,
        recent_imports=imports,
        recent_errors=errors,
        operator_emails_count=len(operator_panel.get_operator_emails()),
        qbo_environment=QBO_ENVIRONMENT,
        qbo_real_import=QBO_REAL_IMPORT,
        app_env=APP_ENV,
    )


@app.route("/operator/firm/<int:firm_id>")
@operator_required
def operator_firm_detail(firm_id):
    detail = operator_panel.firm_detail(db, history, firm_id)
    if not detail:
        abort(404)
    return render_template("operator-firm.html", **detail)


# ---------------------------------------------------------------------------
# Demo mode
#
# A dedicated "Demo workspace" page that exposes:
#
#   - A "Start new demo" reset that archives prior demo jobs for this firm
#     so the dashboard renders a clean slate. Does NOT touch QuickBooks.
#   - Downloads of internally-balanced, run-id-salted sample reports that
#     can be uploaded into the normal flow without colliding with prior
#     demo runs against the same QBO sandbox/demo company.
#
# Everything below is hidden (route 404s, nav link absent) unless either
# DEMO_MODE=true on this deploy OR the logged-in user is an operator.
# That way normal production customers never see demo controls.
# ---------------------------------------------------------------------------


def _demo_required(view):
    """Same shape as login_required + operator_required but for demo mode.

    When the deploy itself is a demo deploy (DEMO_MODE=true), an
    unauthenticated visitor is redirected to /login?next=/demo — the
    deploy is openly a demo deploy and the login redirect makes the
    "you need to sign in first" path obvious to the demo operator.

    Otherwise (production-config'd deploy where only operators see the
    demo affordance) we 404 unauthenticated visitors so we don't
    confirm the workspace exists.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            if demo_mode.is_demo_mode_enabled():
                flash("Please log in to access the demo workspace.", "info")
                return redirect(url_for("login", next=request.path))
            # Production deploy: don't reveal that /demo exists.
            abort(404)
        if not demo_mode.demo_visible_for_user(user, _is_operator()):
            abort(404)
        return view(*args, **kwargs)
    return wrapper


def _current_demo_run_id(firm_id: int) -> Optional[str]:
    """Return the most recent demo run id stored in the firm's session, or
    None if no demo has been started in this session. We persist this in
    the user's Flask session rather than on the firm row to keep the demo
    feature side-effect-free against the existing schema.
    """
    runs = session.get("_demo_runs") or {}
    return runs.get(str(firm_id))


def _set_demo_run_id(firm_id: int, run_id: str) -> None:
    runs = session.get("_demo_runs") or {}
    runs[str(firm_id)] = run_id
    session["_demo_runs"] = runs


@app.route("/demo")
@_demo_required
def demo_workspace():
    """Demo control panel. Visible only when DEMO_MODE=true or the
    logged-in user is an operator.
    """
    user = current_user()
    firm = db.get_firm(user["firm_id"])
    run_id = _current_demo_run_id(user["firm_id"])

    # Pull the most-recent QBO connection for any of this firm's jobs so
    # we can surface the connected realm prominently. Without this the
    # demo operator can't easily tell which QBO company is wired up.
    firm_jobs = db.list_jobs_for_firm(user["firm_id"], limit=10)
    qbo_company_name = None
    qbo_realm_id = None
    for j in firm_jobs:
        conn = db.get_qbo_connection(j["id"])
        if conn:
            qbo_company_name = conn.get("company_name")
            qbo_realm_id = conn.get("realm_id")
            break

    return render_template(
        "demo-workspace.html",
        firm=firm,
        run_id=run_id,
        qbo_company_name=qbo_company_name,
        qbo_realm_id=qbo_realm_id,
        qbo_environment=QBO_ENVIRONMENT,
        demo_mode_enabled=demo_mode.is_demo_mode_enabled(),
    )


@app.route("/demo/start", methods=["POST"])
@_demo_required
def demo_start_new():
    """Reset the firm's app-side demo workspace and mint a new run id.

    Side effects:
      - Archives every job for the firm (status -> "Archived (demo reset
        <run-id>)") so the dashboard / migration-checklist render a fresh
        state. Real audit/import history is preserved.
      - Writes an audit row.

    Explicitly NOT side effects:
      - No QuickBooks Online records are deleted, voided, or modified.
      - No firm/user/QBO-connection rows are deleted.
    """
    user = current_user()
    run_id = demo_mode.new_demo_run_id()
    result = demo_mode.reset_demo_workspace(db, user["firm_id"], run_id)
    _set_demo_run_id(user["firm_id"], run_id)
    _audit(
        "demo_workspace_reset",
        target_type="firm",
        target_id=str(user["firm_id"]),
        details=f"run_id={run_id} archived_jobs={result['archived_jobs']}",
    )
    flash(
        f"New demo started (run id {run_id}). "
        f"{result['archived_jobs']} prior job(s) archived in the app. "
        "Nothing was deleted from QuickBooks.",
        "success",
    )
    return redirect(url_for("demo_workspace"))


# Map a short report-type slug to (filename, MIME, builder-callable). The
# callable takes the current demo run id (may be None for the COA which
# does not need salting) and returns the CSV body as a string.
_DEMO_SAMPLE_REPORTS = {
    "chart-of-accounts": (
        "demo_chart_of_accounts.csv",
        lambda _run: demo_mode.render_chart_of_accounts_csv(),
    ),
    "trial-balance": (
        "demo_trial_balance.csv",
        lambda _run: demo_mode.render_trial_balance_csv(),
    ),
    "general-ledger": (
        "demo_general_ledger.csv",
        lambda run: demo_mode.render_general_ledger_csv(run),
    ),
    "trust-listing": (
        "demo_trust_listing.csv",
        lambda run: demo_mode.render_trust_listing_csv(run),
    ),
}


@app.route("/demo/sample/<report>.csv")
@_demo_required
def demo_sample_csv(report):
    """Download one of the bundled demo report files for the current run.

    GL / trust-listing CSVs embed the current demo run id so each run is
    duplicate-safe against the same QBO company. COA / trial-balance
    keep stable account numbers so QBO doesn't accumulate duplicate
    accounts across demos.
    """
    entry = _DEMO_SAMPLE_REPORTS.get(report)
    if not entry:
        abort(404)
    filename, builder = entry
    user = current_user()
    run_id = _current_demo_run_id(user["firm_id"]) or demo_mode.new_demo_run_id()
    body = builder(run_id)
    return Response(
        body,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "no-store",
        },
    )


if __name__ == "__main__":
    # Never enable Werkzeug's debugger by default. The debugger pin is a known
    # RCE vector if exposed (CVE-2024-34069), and any operator who runs
    # `python app.py` against a real APP_DB shouldn't be auto-opted-in to it.
    # Set FLASK_DEBUG=1 explicitly when you want the local debugger.
    _debug = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes", "on")
    if _debug and IS_PRODUCTION:
        raise RuntimeError("FLASK_DEBUG must not be set when APP_ENV=production")
    app.run(debug=_debug)