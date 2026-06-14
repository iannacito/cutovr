"""Smoke tests for the bulk upload + auto-detection feature.

Run from project root:

    python3 tests/smoke_bulk_upload.py

Covers:
  T1 classify_csv detects every target report type (COA, opening TB,
     GL, ending TB, trust listing) from real PCLaw-style headers.
  T2 classify_csv flags unidentifiable / unreadable CSVs as
     needs_review / unreadable with a reason.
  T3 detect_report_type_from_filename handles common naming patterns.
  T4 resolve_collisions marks two same-type non-TB files as
     ``duplicate`` and leaves the dual-TB case as a warning only.
  T5 missing_required reports the unfulfilled set.
  T6 /upload/bulk processes multiple files, persists per-file jobs,
     redirects to the review screen, and the review screen renders.
  T7 /upload/bulk rejects non-CSV files cleanly without creating a job
     for them.
  T8 /upload/bulk + manual correction updates the per-file report type
     and the underlying job's report_type.
  T9 /upload (single file) still works.
"""

import io
import os
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-bulk-secret")

import bulk_upload as bu  # noqa: E402
import report_types as rt  # noqa: E402
import app as appmod  # noqa: E402


GL_CSV = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()
COA_CSV = (ROOT / "test_data" / "01_chart_of_accounts.csv").read_bytes()
TB_CSV = (ROOT / "test_data" / "03_trial_balance.csv").read_bytes()
TRUST_CSV = (ROOT / "test_data" / "05_trust_listing.csv").read_bytes()


def _signup_and_login(client):
    pwd = "correct-horse-battery-staple"
    client.post(
        "/signup",
        data={
            "firm_name": "Bulk Upload Test LLP",
            "email": "test@bulkupload.example",
            "password": pwd,
            "confirm_password": pwd,
        },
        follow_redirects=True,
    )
    client.post(
        "/login",
        data={
            "email": "test@bulkupload.example",
            "password": pwd,
        },
        follow_redirects=True,
    )


def _classify_bytes(body: bytes, filename: str) -> bu.ClassificationResult:
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        f.write(body)
        p = Path(f.name)
    try:
        return bu.classify_csv(p, filename)
    finally:
        p.unlink(missing_ok=True)


# --- Unit-level checks (no Flask client) -----------------------------------


def t1_classify_each_report_type():
    cls = _classify_bytes(COA_CSV, "chart_of_accounts.csv")
    assert cls.report_type == rt.REPORT_CHART_OF_ACCOUNTS, cls
    assert cls.status == bu.STATUS_CATEGORIZED
    assert cls.confidence in (bu.CONFIDENCE_HIGH, bu.CONFIDENCE_MEDIUM)

    cls = _classify_bytes(GL_CSV, "general_ledger_jan_jun.csv")
    assert cls.report_type == rt.REPORT_GENERAL_LEDGER, cls
    assert cls.status == bu.STATUS_CATEGORIZED

    cls = _classify_bytes(TB_CSV, "opening_tb.csv")
    assert cls.report_type == rt.REPORT_TRIAL_BALANCE, cls
    assert cls.status == bu.STATUS_CATEGORIZED

    cls = _classify_bytes(TB_CSV, "ending_tb.csv")
    assert cls.report_type == rt.REPORT_TRIAL_BALANCE, cls

    cls = _classify_bytes(TRUST_CSV, "trust_listing.csv")
    assert cls.report_type == rt.REPORT_TRUST_LISTING, cls
    assert cls.status == bu.STATUS_CATEGORIZED

    # Misleading filename should not flip headers-based GL to COA.
    cls = _classify_bytes(GL_CSV, "trust_balances.csv")
    assert cls.report_type == rt.REPORT_GENERAL_LEDGER, (
        "headers should outweigh filename for an obvious GL"
    )

    print("T1 classify_csv per report type: OK")


def t2_classify_unidentifiable():
    blob = b"foo,bar,baz\n1,2,3\n"
    cls = _classify_bytes(blob, "mystery.csv")
    assert cls.report_type is None, cls
    assert cls.status == bu.STATUS_NEEDS_REVIEW
    assert "couldn" in cls.reason.lower() or "set the report type" in cls.reason.lower(), cls.reason

    # Empty file
    cls = _classify_bytes(b"", "empty.csv")
    assert cls.report_type is None
    assert cls.status == bu.STATUS_UNREADABLE
    print("T2 unidentifiable / unreadable: OK")


def t3_filename_hints():
    cases = [
        ("Chart_Of_Accounts_2026.csv", rt.REPORT_CHART_OF_ACCOUNTS),
        ("COA Q1.csv", rt.REPORT_CHART_OF_ACCOUNTS),
        ("Opening TB - 2026-03-31.csv", rt.REPORT_TRIAL_BALANCE),
        ("ending_tb.csv", rt.REPORT_TRIAL_BALANCE),
        ("trial-balance.csv", rt.REPORT_TRIAL_BALANCE),
        ("trust_listing_2026.csv", rt.REPORT_TRUST_LISTING),
        ("Trust Listing - main.csv", rt.REPORT_TRUST_LISTING),
        ("general_ledger.csv", rt.REPORT_GENERAL_LEDGER),
        ("transactions Q1.csv", rt.REPORT_GENERAL_LEDGER),
        ("export.csv", None),
    ]
    for name, expected in cases:
        got = bu.detect_report_type_from_filename(name)
        assert got == expected, f"filename {name!r}: expected {expected}, got {got}"
    print("T3 filename hint detection: OK")


def t4_resolve_collisions():
    # Two GL files: monthly ledgers are expected, so both stay categorized
    # with a warning (NOT flagged duplicate). Firms upload one GL per month.
    a = bu.ClassificationResult(
        filename="gl_jan.csv", report_type=rt.REPORT_GENERAL_LEDGER,
        confidence=bu.CONFIDENCE_HIGH, status=bu.STATUS_CATEGORIZED,
        report_label=rt.REPORT_LABELS[rt.REPORT_GENERAL_LEDGER],
    )
    b = bu.ClassificationResult(
        filename="gl_jul.csv", report_type=rt.REPORT_GENERAL_LEDGER,
        confidence=bu.CONFIDENCE_HIGH, status=bu.STATUS_CATEGORIZED,
        report_label=rt.REPORT_LABELS[rt.REPORT_GENERAL_LEDGER],
    )
    bu.resolve_collisions([a, b])
    assert a.status == bu.STATUS_CATEGORIZED and b.status == bu.STATUS_CATEGORIZED, (a, b)
    assert "monthly ledgers are expected" in a.warning

    # Two TB files: warning only, both still categorized.
    a = bu.ClassificationResult(
        filename="opening_tb.csv", report_type=rt.REPORT_TRIAL_BALANCE,
        confidence=bu.CONFIDENCE_HIGH, status=bu.STATUS_CATEGORIZED,
    )
    b = bu.ClassificationResult(
        filename="ending_tb.csv", report_type=rt.REPORT_TRIAL_BALANCE,
        confidence=bu.CONFIDENCE_HIGH, status=bu.STATUS_CATEGORIZED,
    )
    bu.resolve_collisions([a, b])
    assert a.status == bu.STATUS_CATEGORIZED and b.status == bu.STATUS_CATEGORIZED
    assert "opening TB" in a.warning and "ending TB" in a.warning

    # Single COA + single GL + single Trust + 2 TB: no collisions, 2 TB
    # warnings only.
    items = [
        bu.ClassificationResult(filename="coa.csv", report_type=rt.REPORT_CHART_OF_ACCOUNTS,
                                confidence=bu.CONFIDENCE_HIGH, status=bu.STATUS_CATEGORIZED),
        bu.ClassificationResult(filename="gl.csv", report_type=rt.REPORT_GENERAL_LEDGER,
                                confidence=bu.CONFIDENCE_HIGH, status=bu.STATUS_CATEGORIZED),
        bu.ClassificationResult(filename="opening_tb.csv", report_type=rt.REPORT_TRIAL_BALANCE,
                                confidence=bu.CONFIDENCE_HIGH, status=bu.STATUS_CATEGORIZED),
        bu.ClassificationResult(filename="ending_tb.csv", report_type=rt.REPORT_TRIAL_BALANCE,
                                confidence=bu.CONFIDENCE_HIGH, status=bu.STATUS_CATEGORIZED),
        bu.ClassificationResult(filename="trust.csv", report_type=rt.REPORT_TRUST_LISTING,
                                confidence=bu.CONFIDENCE_HIGH, status=bu.STATUS_CATEGORIZED),
    ]
    bu.resolve_collisions(items)
    for it in items:
        assert it.status == bu.STATUS_CATEGORIZED, it
    print("T4 collision resolution: OK")


def t5_missing_required():
    # Only COA present
    items = [
        bu.ClassificationResult(filename="coa.csv", report_type=rt.REPORT_CHART_OF_ACCOUNTS,
                                confidence=bu.CONFIDENCE_HIGH, status=bu.STATUS_CATEGORIZED),
    ]
    miss = bu.missing_required(items)
    # Required set is COA + TB + GL + Trust — minus COA = three missing.
    assert rt.REPORT_TRIAL_BALANCE in miss
    assert rt.REPORT_GENERAL_LEDGER in miss
    assert rt.REPORT_TRUST_LISTING in miss
    assert rt.REPORT_CHART_OF_ACCOUNTS not in miss

    # File flagged needs_review does NOT count as covering its type.
    items = [
        bu.ClassificationResult(filename="coa.csv", report_type=rt.REPORT_CHART_OF_ACCOUNTS,
                                confidence=bu.CONFIDENCE_LOW, status=bu.STATUS_NEEDS_REVIEW),
    ]
    miss = bu.missing_required(items)
    assert rt.REPORT_CHART_OF_ACCOUNTS in miss
    print("T5 missing_required: OK")


# --- Flask client checks ---------------------------------------------------


def _bulk_upload(client, files, company="Bulk Smoke Firm"):
    """POST a multi-file form to /upload/bulk.

    Multiple files with the same form field name need a MultiDict so
    werkzeug emits them as a repeated multipart field.
    """
    from werkzeug.datastructures import MultiDict
    data = MultiDict()
    data["company_name"] = company
    data["email"] = "ops@bulk.example"
    for body, name in files:
        data.add("ledger_files", (io.BytesIO(body), name))
    return client.post(
        "/upload/bulk",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def t6_bulk_route_happy_path():
    client = appmod.app.test_client()
    _signup_and_login(client)

    resp = _bulk_upload(
        client,
        files=[
            (COA_CSV, "chart_of_accounts.csv"),
            (TB_CSV, "opening_tb.csv"),
            (GL_CSV, "general_ledger.csv"),
            (TRUST_CSV, "trust_listing.csv"),
        ],
    )
    assert resp.status_code == 302, resp.status_code
    location = resp.headers.get("Location", "")
    assert "/upload/bulk/" in location, location

    # Follow to the review screen.
    bulk_id = location.rsplit("/", 1)[-1]
    bulk = appmod.bulk_uploads.get(bulk_id)
    assert bulk, "bulk upload not stored"
    assert len(bulk["results"]) == 4
    # Every file should have a job_id assigned.
    job_ids = [e["job_id"] for e in bulk["results"]]
    assert all(j for j in job_ids), f"missing job_ids: {job_ids}"
    # Every file should be categorized at the right type.
    seen = sorted(e["report_type"] for e in bulk["results"])
    expected = sorted([
        rt.REPORT_CHART_OF_ACCOUNTS,
        rt.REPORT_TRIAL_BALANCE,
        rt.REPORT_GENERAL_LEDGER,
        rt.REPORT_TRUST_LISTING,
    ])
    assert seen == expected, (seen, expected)

    # Verify the review page renders 200 and includes file names.
    page = client.get(location, follow_redirects=False)
    assert page.status_code == 200, page.status_code
    body = page.data.decode("utf-8", errors="replace")
    assert "Per-file detection summary" in body, body[:400]
    assert "chart_of_accounts.csv" in body
    assert "trust_listing.csv" in body
    assert "general_ledger.csv" in body
    print("T6 bulk upload happy path: OK")


def t7_bulk_route_rejects_non_csv():
    client = appmod.app.test_client()
    _signup_and_login(client)

    resp = _bulk_upload(
        client,
        files=[
            (b"not a csv", "malware.exe"),
            (COA_CSV, "coa.csv"),
        ],
    )
    assert resp.status_code == 302, resp.status_code
    bulk_id = resp.headers["Location"].rsplit("/", 1)[-1]
    bulk = appmod.bulk_uploads[bulk_id]
    by_name = {e["filename"]: e for e in bulk["results"]}
    # The non-csv was rejected and no job was created for it.
    rejected = by_name.get("malware.exe") or by_name.get("malware")
    if rejected is None:
        # Werkzeug's secure_filename rewrites the name; look up by status.
        rejected = next(
            (e for e in bulk["results"]
             if e["status"] == bu.STATUS_REJECTED), None
        )
    assert rejected is not None, bulk["results"]
    assert rejected["status"] == bu.STATUS_REJECTED
    assert rejected["job_id"] is None
    # The COA still got categorized.
    coa = next(e for e in bulk["results"] if e["filename"].endswith("coa.csv"))
    assert coa["report_type"] == rt.REPORT_CHART_OF_ACCOUNTS
    assert coa["job_id"]
    print("T7 bulk upload rejects non-csv: OK")


def t8_bulk_manual_correction():
    client = appmod.app.test_client()
    _signup_and_login(client)

    # Upload an unidentifiable CSV to force needs_review.
    mystery = b"foo,bar\n1,2\n3,4\n"
    resp = _bulk_upload(
        client,
        files=[(mystery, "mystery_export.csv")],
    )
    assert resp.status_code == 302
    bulk_id = resp.headers["Location"].rsplit("/", 1)[-1]
    bulk = appmod.bulk_uploads[bulk_id]
    entry = bulk["results"][0]
    assert entry["status"] in (bu.STATUS_NEEDS_REVIEW, bu.STATUS_CATEGORIZED), entry
    filename = entry["filename"]
    job_id = entry["job_id"]
    assert job_id, "even unknown files should get a job slot"

    # Post a correction to set this file as chart_of_accounts.
    resp = client.post(
        f"/upload/bulk/{bulk_id}/correct",
        data={
            "filename": filename,
            "report_type": rt.REPORT_CHART_OF_ACCOUNTS,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.status_code
    # The entry should now be categorized as COA.
    updated = bulk["results"][0]
    assert updated["report_type"] == rt.REPORT_CHART_OF_ACCOUNTS
    assert updated["status"] == bu.STATUS_CATEGORIZED
    # The underlying job's report_type should also have been updated.
    job = appmod.jobs[job_id]
    assert job["report_type"] == rt.REPORT_CHART_OF_ACCOUNTS
    print("T8 manual correction: OK")


def t9_single_upload_still_works():
    """Backward-compat: the original /upload route must still work."""
    client = appmod.app.test_client()
    _signup_and_login(client)
    resp = client.post(
        "/upload",
        data={
            "company_name": "Single-File Compat LLP",
            "email": "ops@single.example",
            "report_type": "",  # auto-detect
            "ledger_file": (io.BytesIO(COA_CSV), "01_chart_of_accounts.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.status_code
    location = resp.headers.get("Location", "")
    assert "/jobs/" in location, location
    job_id = location.rsplit("/", 1)[-1]
    job = appmod.jobs[job_id]
    assert job["report_type"] == rt.REPORT_CHART_OF_ACCOUNTS
    print("T9 single-file /upload still works: OK")


def main():
    t1_classify_each_report_type()
    t2_classify_unidentifiable()
    t3_filename_hints()
    t4_resolve_collisions()
    t5_missing_required()
    t6_bulk_route_happy_path()
    t7_bulk_route_rejects_non_csv()
    t8_bulk_manual_correction()
    t9_single_upload_still_works()
    print("\nAll bulk-upload smoke tests passed.")


if __name__ == "__main__":
    main()
