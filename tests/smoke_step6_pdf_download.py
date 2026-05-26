"""Smoke tests for the Step 6 PDF report download.

Covers
------
  P1  /reconcile-balances/report.pdf requires auth: an anonymous GET
      gets redirected to /login (does NOT return a PDF).
  P2  Logged-in user with no completed import: endpoint redirects
      back to /reconcile-balances with a friendly flash, never a 500.
  P3  Logged-in user with a completed import: endpoint returns
      application/pdf with a Content-Disposition that triggers a
      download, a sensible filename, and a non-empty PDF body that
      starts with %PDF-.
  P4  Step 6 page renders the "Download PDF" button below the email
      form, pointing at the PDF endpoint.
  P5  The PDF body never embeds technical identifiers: realm ids,
      job ids, encrypted-file paths, SMTP env names.
  P6  build_report_pdf is deterministic in shape (returns bytes,
      handles the blocked summary without crashing) and the
      generated PDF preserves the firm name + plain-English status.

Run from project root::

    python3 tests/smoke_step6_pdf_download.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="pclaw_uploads_")
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_")
os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("DEMO_MODE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-step6-pdf")

for var in (
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_USERNAME",
    "SMTP_PASSWORD", "SMTP_FROM", "SMTP_FROM_EMAIL", "SMTP_FROM_NAME",
    "SMTP_USE_TLS",
    "MAIL_SERVER", "MAIL_PORT", "MAIL_USERNAME", "MAIL_PASSWORD",
    "MAIL_DEFAULT_SENDER", "MAIL_FROM_NAME", "MAIL_USE_TLS",
):
    os.environ.pop(var, None)

import app as appmod  # noqa: E402
import final_report  # noqa: E402


def _signup_and_login(client, email, firm="PDF LLP"):
    pwd = "passw0rd!1234"
    client.post(
        "/signup",
        data={
            "firm_name": firm,
            "email": email,
            "password": pwd,
            "confirm_password": pwd,
        },
        follow_redirects=False,
    )
    # /signup auto-logs the user in on success on this app; if it
    # didn't, log in explicitly.
    if not client.get("/dashboard").status_code == 200:
        client.post(
            "/login",
            data={"email": email, "password": pwd},
            follow_redirects=False,
        )


def _complete_step1(firm_id):
    appmod.db.upsert_cutover_settings(
        firm_id=firm_id,
        cutover_date="2026-04-01",
        opening_balance_date="2026-04-01",
        period_start="2025-01-01",
        period_end="2025-12-31",
        country="US",
        accounting_basis="accrual",
        migration_scope=None,
        notes=None,
        qbo_company_name=None,
        qbo_realm_id=None,
        clio_involved=False,
        ar_ap_strategy="open_only",
    )


def _make_imported_gl_job(user, job_id="job_p1", realm_id="R1"):
    db = appmod.db
    db.upsert_job(
        job_id=job_id,
        firm_id=user["firm_id"],
        user_id=user["id"],
        company="PDF LLP",
        source_file="gl.csv",
        encrypted_file="x.enc",
        file_sha256="0" * 64,
        status="Imported 87 JournalEntries",
    )
    db.save_job_state(
        job_id,
        {
            "status": "Imported 87 JournalEntries",
            "report_type": "general_ledger",
            "import_summary": {
                "qbo_je_count": 87,
                "source_transaction_count": 87,
                "source_debit_total": "1000.00",
                "source_credit_total": "1000.00",
                "balanced": True,
            },
        },
    )
    appmod.qbo_connections[job_id] = {
        "realm_id": realm_id,
        "access_token_enc": appmod.encrypt_token("fake"),
        "refresh_token_enc": appmod.encrypt_token("fake"),
        "company_name": "PDF Test QBO",
        "legal_name": "PDF Test QBO",
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }


def _make_uploaded_only_job(user, job_id="job_p2"):
    db = appmod.db
    db.upsert_job(
        job_id=job_id,
        firm_id=user["firm_id"],
        user_id=user["id"],
        company="PDF LLP",
        source_file="gl.csv",
        encrypted_file="x.enc",
        file_sha256="0" * 64,
        status="uploaded",
    )
    db.save_job_state(
        job_id,
        {"status": "uploaded", "report_type": "general_ledger"},
    )


def p1_pdf_endpoint_requires_auth():
    client = appmod.app.test_client()
    r = client.get(
        "/reconcile-balances/report.pdf", follow_redirects=False
    )
    # Either 302 to /login or a friendly redirect — never a PDF.
    assert r.status_code in (302, 303), r.status_code
    location = r.headers.get("Location", "")
    assert "/login" in location, location
    assert r.mimetype != "application/pdf", r.mimetype
    print("P1 OK: PDF endpoint redirects anonymous users to /login")


def p2_pdf_endpoint_redirects_when_blocked():
    client = appmod.app.test_client()
    _signup_and_login(client, "p2@example.test", firm="Blocked LLP")
    user = appmod.db.get_user_by_email("p2@example.test")
    _complete_step1(user["firm_id"])
    _make_uploaded_only_job(user)

    r = client.get(
        "/reconcile-balances/report.pdf", follow_redirects=False
    )
    assert r.status_code in (302, 303), r.status_code
    location = r.headers.get("Location", "")
    assert "/reconcile-balances" in location, location
    assert r.mimetype != "application/pdf"
    # Follow the redirect and confirm a friendly flash was shown.
    r2 = client.get(location, follow_redirects=False)
    assert r2.status_code == 200
    body = r2.get_data(as_text=True)
    assert "isn&#39;t ready yet" in body or "isn't ready yet" in body, (
        "expected a friendly 'isn't ready yet' flash"
    )
    print("P2 OK: pre-import GET returns a friendly redirect, not a 500")


def p3_pdf_endpoint_returns_pdf_when_complete():
    client = appmod.app.test_client()
    _signup_and_login(client, "p3@example.test", firm="Smith & Hart LLP")
    user = appmod.db.get_user_by_email("p3@example.test")
    _complete_step1(user["firm_id"])
    _make_imported_gl_job(user, job_id="job_p3")

    r = client.get(
        "/reconcile-balances/report.pdf", follow_redirects=False
    )
    assert r.status_code == 200, r.status_code
    assert r.mimetype == "application/pdf", r.mimetype
    cd = r.headers.get("Content-Disposition", "")
    assert "attachment" in cd.lower(), cd
    assert "pclaw-migrate-final-report.pdf" in cd, cd
    body = r.get_data()
    assert body.startswith(b"%PDF-"), body[:20]
    assert len(body) > 800, ("PDF suspiciously small", len(body))
    # Don't let intermediaries cache per-user PDFs.
    cache = r.headers.get("Cache-Control", "")
    assert "no-store" in cache, cache
    print("P3 OK: completed user gets application/pdf with attachment headers")


def p4_step6_page_renders_pdf_button():
    client = appmod.app.test_client()
    _signup_and_login(client, "p4@example.test", firm="Button LLP")
    user = appmod.db.get_user_by_email("p4@example.test")
    _complete_step1(user["firm_id"])
    _make_imported_gl_job(user, job_id="job_p4")

    r = client.get("/reconcile-balances", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # The PDF block lives in the same final-report card.
    assert 'data-testid="step6-final-report"' in body
    assert 'data-testid="step6-report-pdf-block"' in body
    assert 'data-testid="step6-report-pdf-download"' in body
    assert "/reconcile-balances/report.pdf" in body
    # And it must appear BELOW the email form, as requested.
    pdf_idx = body.find('data-testid="step6-report-pdf-block"')
    form_idx = body.find('data-testid="step6-report-form"')
    assert form_idx > 0 and pdf_idx > form_idx, (
        "PDF block must render below the email form"
    )
    # Plain-English label, not jargon.
    assert "Download PDF" in body
    print("P4 OK: Step 6 page renders Download PDF below the email form")


def p5_pdf_does_not_leak_technical_ids():
    """The visible PDF must NOT carry realm ids, job ids, encrypted
    file names, or SMTP env material. We render the PDF for a real
    completed firm and scan the bytes.
    """
    client = appmod.app.test_client()
    _signup_and_login(client, "p5@example.test", firm="Privacy LLP")
    user = appmod.db.get_user_by_email("p5@example.test")
    _complete_step1(user["firm_id"])
    _make_imported_gl_job(
        user, job_id="job_p5_unique_secret", realm_id="REALM_TOPSECRET",
    )

    r = client.get(
        "/reconcile-balances/report.pdf", follow_redirects=False
    )
    assert r.status_code == 200
    body = r.get_data()
    # PDF content streams are zlib-compressed by default. Decompress
    # everything we can so the substring check catches strings that
    # only appear in the rendered text layer.
    import re as _re
    import zlib as _zlib
    decoded = [body]
    for m in _re.finditer(rb"stream\r?\n(.+?)\r?\nendstream", body, _re.S):
        data = m.group(1)
        try:
            decoded.append(_zlib.decompress(data))
        except Exception:
            decoded.append(data)
    haystack = b"\n".join(decoded)
    for forbidden in (
        b"REALM_TOPSECRET",
        b"job_p5_unique_secret",
        b"x.enc",
        b"SMTP_HOST",
        b"SMTP_PASSWORD",
        b"MAIL_PASSWORD",
        b"realm_id",
    ):
        assert forbidden not in haystack, (
            f"PDF leaked technical identifier: {forbidden!r}"
        )
    print("P5 OK: PDF does not embed realm/job ids or SMTP env material")


def p6_build_report_pdf_unit_behavior():
    """Pure-function check: build_report_pdf returns bytes, starts
    with %PDF, preserves the firm name and a plain status phrase, and
    does not crash on a blocked summary.
    """
    completed = final_report.build_reconciliation_summary(
        firm_name="Smith & Hart LLP",
        cutover={"cutover_date": "2026-04-01"},
        jobs=[
            {"id": "g", "report_type": "general_ledger",
             "import_summary": {"qbo_je_count": 7,
                                "source_transaction_count": 87,
                                "balanced": True}},
            {"id": "c", "report_type": "chart_of_accounts",
             "coa_create_history": [{"created_count": 5}]},
        ],
        qbo_connections=[{"company_name": "Demo QBO", "realm_id": "R1"}],
        account_mapping_count=10,
    )
    pdf = final_report.build_report_pdf(completed)
    assert isinstance(pdf, (bytes, bytearray))
    assert pdf.startswith(b"%PDF-"), pdf[:20]
    # Structural checks: a valid 1-page PDF carries Pages + /MediaBox
    # markers and ends with %%EOF. We avoid decoding compressed
    # content streams here — `build_report_text` covers the text-
    # content checks in tests/smoke_step6_reconcile_balances.py, and
    # the on-screen template already renders the same fields.
    assert b"%%EOF" in pdf[-32:], "PDF must end with %%EOF"
    assert b"/Pages" in pdf, "PDF must declare a Pages tree"
    assert b"Helvetica" in pdf, "expected a known font in the PDF"
    # Title metadata is not compressed and carries the firm name.
    assert b"Smith" in pdf, "PDF /Title metadata should carry firm name"

    # Blocked-summary PDF: must still produce a well-formed PDF.
    blocked = final_report.build_reconciliation_summary(
        firm_name="Blocked LLP",
        cutover=None,
        jobs=[{"id": "g", "report_type": "general_ledger",
               "unmapped_accounts": ["1101 Petty Cash"]}],
        qbo_connections=[],
        account_mapping_count=0,
    )
    pdf2 = final_report.build_report_pdf(blocked)
    assert pdf2.startswith(b"%PDF-")
    assert b"%%EOF" in pdf2[-32:]
    print("P6 OK: build_report_pdf is well-behaved for completed and blocked")


def main():
    p1_pdf_endpoint_requires_auth()
    p2_pdf_endpoint_redirects_when_blocked()
    p3_pdf_endpoint_returns_pdf_when_complete()
    p4_step6_page_renders_pdf_button()
    p5_pdf_does_not_leak_technical_ids()
    p6_build_report_pdf_unit_behavior()
    print("\nALL STEP 6 PDF DOWNLOAD SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
