from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort
from werkzeug.utils import secure_filename
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from functools import wraps
import os, secrets
import requests

from app_db import AppDB
import branding
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
import qbo_error_hint
import csv as _csv
from io import StringIO
from flask import Response

app = Flask(__name__)

# Production-vs-local environment switch. Anything other than "local"/"dev"
# means we expect the operator to provide a real SECRET_KEY and serve over
# HTTPS. Set APP_ENV=production on Render.
APP_ENV = os.environ.get("APP_ENV", "local").lower()
IS_PRODUCTION = APP_ENV not in ("local", "dev", "development", "test")

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
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
)

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


def _audit_details_with_tid(details, intuit_tid):
    """Append the Intuit transaction id to an audit detail string, when one
    is present. The tid is opaque (no token / secret material), so it's
    safe to include alongside the existing detail text.
    """
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
        details=details,
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
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
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
                 target_type="firm", target_id=str(firm_id), details=email)
        flash(f"Welcome to {firm_name}!", "success")
        return redirect(url_for("dashboard"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = db.authenticate(email, password)
        if not user:
            db.audit(action="login_failed", details=email)
            flash("Invalid email or password.", "error")
            return render_template("login.html", email=email)
        session.clear()
        session["user_id"] = user["id"]
        session["firm_id"] = user["firm_id"]
        db.audit(action="login", firm_id=user["firm_id"], user_id=user["id"], details=email)
        next_url = request.args.get("next") or request.form.get("next")
        if next_url and next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    user = current_user()
    if user:
        db.audit(action="logout", firm_id=user["firm_id"], user_id=user["id"])
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    firm_jobs = db.list_jobs_for_firm(user["firm_id"], limit=20)
    return render_template(
        "dashboard.html",
        firm_jobs=firm_jobs,
        qbo_configured=QBO_CLIENT_ID != "your-client-id-here",
        recent_audit=db.recent_audit_for_firm(user["firm_id"], limit=10),
    )


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
    if current_user():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


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


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    user = current_user()
    company = request.form.get("company_name", "").strip()
    user_email = request.form.get("email", "").strip() or user["email"]
    file = request.files.get("ledger_file")

    if not company or not file:
        flash("Company name and PCLaw export file are required.", "error")
        return redirect(url_for("dashboard"))

    safe_name = secure_filename(file.filename)
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    job_id = f"job_{timestamp}"

    # Save and encrypt uploaded file
    upload_path = UPLOAD_DIR / f"{timestamp}_{safe_name}"
    file.save(upload_path)
    file_sha256 = sha256_of_file(upload_path)
    encrypted_path = UPLOAD_DIR / f"{timestamp}_{safe_name}.enc"
    encrypt_file(upload_path, encrypted_path)
    upload_path.unlink()  # remove unencrypted file

    jobs[job_id] = {
        "id": job_id,
        "firm_id": user["firm_id"],
        "user_id": user["id"],
        "company": company,
        "email": user_email,
        "source_file": f"{timestamp}_{safe_name}",
        "encrypted_file": encrypted_path.name,
        "file_sha256": file_sha256,
        "status": "File uploaded (encrypted)",
        "created_at": datetime.utcnow().isoformat(),
        "summary": {},
        "qbo_connected": False,
    }
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company=company, source_file=f"{timestamp}_{safe_name}",
        encrypted_file=encrypted_path.name, file_sha256=file_sha256,
        status="File uploaded (encrypted)",
    )
    _audit("upload", target_type="job", target_id=job_id,
           details=f"{company} / {safe_name}")

    # Decrypt for processing
    temp_path = UPLOAD_DIR / f"{timestamp}_temp.csv"
    decrypt_file(encrypted_path, temp_path)

    try:
        # The simple flat parser (Date/Account/Description/Debit/Credit) is
        # used for the legacy CSV format. The richer PCLaw GL format with
        # transaction_id is handled directly by the import pipeline, so we
        # don't need to convert it to the flat QBO export here.
        with temp_path.open("r", newline="", encoding="utf-8-sig") as _f:
            _reader = _csv.DictReader(_f)
            _fieldnames = list(_reader.fieldnames or [])
            _is_gl = is_gl_format(_fieldnames)
            _gl_rows = list(_reader) if _is_gl else []
            _row_count = len(_gl_rows) if _is_gl else sum(
                1 for _ in _csv.DictReader(temp_path.open("r", newline="", encoding="utf-8-sig"))
            )

        if _is_gl:
            preflight = build_preflight_summary(_gl_rows, _fieldnames)
            jobs[job_id]["status"] = "Ready for QBO connection"
            jobs[job_id]["summary"] = {
                "row_count": _row_count,
                "format": "GL (transaction_id)",
                "balanced": preflight["balanced"],
            }
            jobs[job_id]["preflight"] = preflight
            if preflight["ready"]:
                flash(
                    "PCLaw GL file accepted. Review the preflight checklist, "
                    "then connect QuickBooks to continue.",
                    "success",
                )
            else:
                flash(
                    "PCLaw GL file uploaded with warnings. Review the "
                    "preflight checklist on the job page before connecting "
                    "QuickBooks.",
                    "error",
                )
        else:
            rows = parse_pclaw_csv(temp_path)
            out_path = OUTPUT_DIR / f"{timestamp}_qbo_import.csv"
            summary = export_qbo_csv(rows, out_path)

            encrypted_out = OUTPUT_DIR / f"{timestamp}_qbo_import.csv.enc"
            encrypt_file(out_path, encrypted_out)
            out_path.unlink()

            jobs[job_id]["status"] = "Ready for QBO connection"
            jobs[job_id]["summary"] = summary
            jobs[job_id]["output_file"] = f"{timestamp}_qbo_import.csv"
            jobs[job_id]["encrypted_output"] = encrypted_out.name

            flash("Migration package prepared successfully. Connect to QuickBooks to complete.", "success")
    except Exception as e:
        headline, action = friendly_validation_message(e)
        jobs[job_id]["status"] = f"Error: {headline}"
        jobs[job_id]["last_validation_error"] = {
            "headline": headline,
            "action": action,
        }
        flash(f"{headline} {action}", "error")
    finally:
        temp_path.unlink(missing_ok=True)

    # Persist the parsed-state snapshot (summary, output_file, etc.) so the
    # job survives a restart between upload and import.
    _save_job(job_id)
    return redirect(url_for("job_detail", job_id=job_id))


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

    session["pending_job_id"] = job_id
    auth_url = qbo_auth.get_authorization_url(state=job_id)
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
        flash(
            "Your session expired during the QuickBooks redirect. Please log "
            "in again, then re-click Connect to QuickBooks on the job page."
            + _support_suffix(),
            "error",
        )
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

    job_id = session.pop("pending_job_id", state)
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
                # Beginner-safe: don't silently fake success. Send the user
                # to the mapping page so they can resolve it in one click.
                job["status"] = "Import blocked: unmapped accounts"
                _save_job(job_id)
                _audit("import_blocked", target_type="job", target_id=job_id,
                       details=f"unmapped accounts: {sorted(unmapped)}")
                # Stash the unmapped list on the job so the detail page can
                # render a one-click link to the mapping UI.
                job["unmapped_accounts"] = sorted(unmapped)
                _save_job(job_id)
                flash(
                    "Cannot import: these PCLaw accounts have no match in your QBO "
                    f"sandbox (matching by {mapping_mode}): "
                    + "; ".join(sorted(unmapped))
                    + ". Open the account mapping page (button below) to pick a "
                      "matching QBO account for each.",
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


@app.route("/jobs/<job_id>/account-mapping", methods=["GET", "POST"])
@login_required
def account_mapping(job_id):
    """List PCLaw accounts in this job's CSV alongside QBO accounts and let
    the user save (firm_id, realm_id, pclaw_*, qbo_account_id) mappings.

    The mapping is by PCLaw account_number when present, otherwise by
    account_name. Saved mappings then override the auto-match in the
    import flow.
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

    # Fetch QBO accounts (the dropdown source of truth).
    try:
        qbo_accounts_resp = qbo.get_accounts()
    except QBOError as e:
        tid_suffix = f" (Intuit support reference: {e.intuit_tid})" if e.intuit_tid else ""
        flash(f"Could not query QBO accounts: {e}{tid_suffix}", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    qbo_accounts = qbo_accounts_resp.get("QueryResponse", {}).get("Account", [])

    if request.method == "POST":
        # Form posts pclaw rows as `mapping[<index>]_*` fields. Anything blank
        # means "skip" / leave unmapped.
        saved = 0
        for key, qbo_acct_id in request.form.items(multi=False):
            if not key.startswith("mapping[") or not key.endswith("]"):
                continue
            if not qbo_acct_id:
                continue
            row_idx = key[len("mapping["):-1]
            pclaw_num = (request.form.get(f"pclaw_num[{row_idx}]") or "").strip() or None
            pclaw_name = (request.form.get(f"pclaw_name[{row_idx}]") or "").strip() or None
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
        _audit("account_mapping_saved", target_type="job", target_id=job_id,
               details=f"saved {saved} mapping(s)")
        flash(f"Saved {saved} account mapping(s). Click Import to retry.", "success")
        return redirect(url_for("account_mapping", job_id=job_id))

    # Build the list of unique PCLaw accounts in this job's source CSV.
    encrypted_in = UPLOAD_DIR / job["encrypted_file"]
    temp_csv = UPLOAD_DIR / f"temp_mapping_{job_id}.csv"
    decrypt_file(encrypted_in, temp_csv)
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


if __name__ == "__main__":
    app.run(debug=True)