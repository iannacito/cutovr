from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort
from werkzeug.utils import secure_filename
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from functools import wraps
import os, secrets
import requests

from app_db import AppDB
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
import csv as _csv

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
    """Make `user`, `firm`, and `now_year` available to every template."""
    user = current_user()
    firm = db.get_firm(user["firm_id"]) if user else None
    return {"user": user, "firm": firm, "now_year": datetime.utcnow().year}


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
        raise QBOAuthExpired(str(e)) from e

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


@app.route("/healthz")
def healthz():
    """Lightweight health probe. Reports presence (not values) of critical
    config so Render and humans can confirm the deploy is healthy without
    leaking secrets."""
    return jsonify({
        "status": "ok",
        "app_env": APP_ENV,
        "qbo_environment": QBO_ENVIRONMENT,
        "qbo_real_import": QBO_REAL_IMPORT,
        "secret_key_set": bool(os.environ.get("SECRET_KEY") or os.environ.get("APP_SECRET")),
        "encryption_key_set": bool(os.environ.get("ENCRYPTION_KEY")),
        "qbo_client_id_set": QBO_CLIENT_ID != "your-client-id-here" and bool(QBO_CLIENT_ID),
        "qbo_redirect_uri_set": bool(QBO_REDIRECT_URI) and not QBO_REDIRECT_URI.startswith("http://localhost"),
    }), 200


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
            _is_gl = is_gl_format(_reader.fieldnames)
            _row_count = sum(1 for _ in _reader)

        if _is_gl:
            jobs[job_id]["status"] = "Ready for QBO connection"
            jobs[job_id]["summary"] = {"row_count": _row_count, "format": "GL (transaction_id)"}
            flash(
                "PCLaw GL file accepted. Connect to QuickBooks to complete the import.",
                "success",
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
        jobs[job_id]["status"] = f"Error: {str(e)}"
        flash(f"Processing error: {str(e)}", "error")
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
    return render_template(
        "job-detail.html",
        job=job,
        qbo_connection=qbo_conn,
        qbo_configured=QBO_CLIENT_ID != "your-client-id-here",
        qbo_real_import=QBO_REAL_IMPORT,
        job_history=job_history,
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

    session["pending_job_id"] = job_id
    auth_url = qbo_auth.get_authorization_url(state=job_id)
    return redirect(auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    realm_id = request.args.get("realmId")
    error = request.args.get("error")

    user = current_user()
    if not user:
        flash(
            "Your session expired during the QuickBooks redirect. Please log "
            "in again, then re-click Connect to QuickBooks on the job page.",
            "error",
        )
        return redirect(url_for("login"))

    if error:
        flash(f"QuickBooks authorization failed: {error}", "error")
        return redirect(url_for("dashboard"))

    if not code or not realm_id:
        flash("Invalid OAuth callback parameters", "error")
        return redirect(url_for("dashboard"))

    job_id = session.pop("pending_job_id", state)
    job = jobs.get(job_id)
    if not job:
        flash("Job not found", "error")
        return redirect(url_for("dashboard"))
    if job.get("firm_id") != user["firm_id"]:
        # Should not happen unless the OAuth state was tampered with.
        db.audit(
            action="oauth_callback_firm_mismatch",
            firm_id=user["firm_id"], user_id=user["id"],
            target_type="job", target_id=job_id,
        )
        flash("Job not found", "error")
        return redirect(url_for("dashboard"))

    try:
        token_data = qbo_auth.get_bearer_token(code)

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
        _audit("qbo_connected", target_type="job", target_id=job_id,
               details=f"realmId={realm_id} company={company_label}")

        flash(
            f"Connected to QuickBooks: {company_label} (realmId {realm_id}). "
            "If this is the wrong company, click Disconnect QuickBooks and reconnect.",
            "success",
        )
        return redirect(url_for("job_detail", job_id=job_id))
    except Exception as e:
        flash(f"Token exchange failed: {str(e)}", "error")
        return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/<job_id>/disconnect-qbo", methods=["POST"])
@login_required
def disconnect_qbo(job_id):
    job, _user = _job_or_403(job_id)

    # Tokens live only in the in-memory qbo_connections dict (already
    # Fernet-encrypted at rest there). Remove them and any pending session
    # marker. There are no token files on disk to delete for this job.
    qbo_connections.pop(job_id, None)
    if session.get("pending_job_id") == job_id:
        session.pop("pending_job_id", None)

    job["qbo_connected"] = False
    job["status"] = "Ready for QBO connection"
    job["qbo_results"] = None
    job["import_summary"] = None
    job["verification"] = None
    db.delete_qbo_connection(job_id)
    _save_job(job_id)
    _audit("qbo_disconnected", target_type="job", target_id=job_id)

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
                flash(
                    "Could not set up the Customer/Vendor required by QBO for "
                    f"Accounts Receivable / Accounts Payable lines: {e}",
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
        _save_job(job_id)
        _audit("import_failed", target_type="job", target_id=job_id, details=str(e))
        flash(f"QuickBooks rejected the import: {e}", "error")
    except ValueError as e:
        job["status"] = "Import failed (validation)"
        _save_job(job_id)
        _audit("import_failed", target_type="job", target_id=job_id, details=str(e))
        flash(f"Import failed: {e}", "error")
    except Exception as e:  # noqa: BLE001
        job["status"] = "Import failed"
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
        flash(f"Verification failed (QBO error): {e}", "error")
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
        flash(f"Could not query QBO accounts: {e}", "error")
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
        try:
            history.record_reversal(
                import_id=last_import["id"],
                job_id=job_id,
                firm_id=user["firm_id"],
                realm_id=qbo_conn["realm_id"],
                status="failed",
                created_by_user_id=user["id"],
                reversed_transactions=reversal_rows,
                error=str(e),
            )
        except ValueError:
            pass  # already recorded; nothing to do
        _audit("import_reversal_failed", target_type="job", target_id=job_id,
               details=str(e))
        flash(
            f"Reversal failed after creating {sum(1 for r in reversal_rows if r['reversal_qbo_je_id'])} "
            f"of {len(last_import['transactions'])} reversal entries: {e}",
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
    job, _user = _job_or_403(job_id)

    try:
        if "encrypted_file" in job:
            (UPLOAD_DIR / job["encrypted_file"]).unlink(missing_ok=True)
        if "encrypted_output" in job:
            (OUTPUT_DIR / job["encrypted_output"]).unlink(missing_ok=True)

        if job_id in qbo_connections:
            del qbo_connections[job_id]
        del jobs[job_id]
        db.delete_job(job_id)
        _audit("delete_job", target_type="job", target_id=job_id)

        flash("Job and associated files deleted", "success")
    except Exception as e:
        flash(f"Deletion error: {str(e)}", "error")

    return redirect(url_for("dashboard"))


@app.route("/api/jobs/<job_id>")
@login_required
def job_api(job_id):
    job, _user = _job_or_403(job_id)
    return jsonify(job)


if __name__ == "__main__":
    app.run(debug=True)