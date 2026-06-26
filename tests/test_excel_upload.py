"""Tests for PCLaw Excel (.xlsx) upload support.

Firms frequently export PCLaw reports as Excel rather than CSV. We accept
.xlsx by flattening the first worksheet to CSV up front, then run the
existing detection + parsing pipeline unchanged. Legacy .xls and unreadable
files get a specific, friendly message — never a generic ledger error.

Covers:
  * pure conversion (excel_convert) for happy path, padding rows, numbers,
    dates, legacy .xls, and corrupt workbooks,
  * the single-file /upload route accepting an .xlsx and categorizing it,
  * the /upload/bulk route accepting an .xlsx and rejecting a .xls with a
    friendly reason.
"""

import csv
import datetime
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("APP_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("IMPORT_HISTORY_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-excel-secret")

import openpyxl  # noqa: E402

import excel_convert as ec  # noqa: E402
import report_types as rt  # noqa: E402


def _xlsx_bytes(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _csv_to_xlsx_bytes(csv_bytes):
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    return _xlsx_bytes(rows)


# --- pure conversion -------------------------------------------------------


def test_is_excel_filename():
    assert ec.is_excel_filename("Report.xlsx")
    assert ec.is_excel_filename("report.XLSM")
    assert ec.is_excel_filename("old.xls")
    assert not ec.is_excel_filename("report.csv")
    assert ec.is_legacy_excel_filename("old.xls")
    assert not ec.is_legacy_excel_filename("new.xlsx")
    assert ec.csv_filename_for("My GL.xlsx") == "My GL.csv"


def test_convert_happy_path_numbers_and_padding():
    data = _xlsx_bytes([
        ["Account Number", "Account Name", "Debit", "Credit"],
        [1000, "Operating Bank", 100.0, 0],
        [3000, "Owners Equity", 0, 100.5],
        [None, None, None, None],  # spreadsheet padding — dropped
    ])
    out = ec.excel_bytes_to_csv_bytes(data, "coa.xlsx").decode("utf-8")
    lines = out.strip().splitlines()
    assert lines[0] == "Account Number,Account Name,Debit,Credit"
    # Whole numbers render without a trailing ".0".
    assert lines[1] == "1000,Operating Bank,100,0"
    assert lines[2] == "3000,Owners Equity,0,100.5"
    assert len(lines) == 3  # padding row dropped


def test_convert_dates_render_iso():
    data = _xlsx_bytes([
        ["As Of", "Account"],
        [datetime.datetime(2026, 3, 31), "Bank"],
    ])
    out = ec.excel_bytes_to_csv_bytes(data, "tb.xlsx").decode("utf-8")
    assert "2026-03-31,Bank" in out


def test_legacy_xls_friendly_error():
    try:
        ec.excel_bytes_to_csv_bytes(b"anything", "ledger.xls")
        assert False, "expected ExcelConversionError"
    except ec.ExcelConversionError as exc:
        msg = str(exc).lower()
        assert ".xlsx" in msg and "csv" in msg
        # Friendly, not a generic ledger parse error.
        assert "ledger" not in msg


def test_corrupt_xlsx_friendly_error():
    try:
        ec.excel_bytes_to_csv_bytes(b"this is not a zip", "bad.xlsx")
        assert False, "expected ExcelConversionError"
    except ec.ExcelConversionError as exc:
        assert "csv" in str(exc).lower()


def test_empty_xlsx_friendly_error():
    data = _xlsx_bytes([])
    try:
        ec.excel_bytes_to_csv_bytes(data, "empty.xlsx")
        assert False, "expected ExcelConversionError"
    except ec.ExcelConversionError as exc:
        assert "no data" in str(exc).lower() or "no worksheets" in str(exc).lower()


def test_converted_csv_is_detectable_as_coa():
    """A converted .xlsx flows through the normal report detector."""
    coa_csv = (ROOT / "test_data" / "01_chart_of_accounts.csv").read_bytes()
    xlsx = _csv_to_xlsx_bytes(coa_csv)
    out = ec.excel_bytes_to_csv_bytes(xlsx, "coa.xlsx")
    p = Path(tempfile.mktemp(suffix=".csv"))
    p.write_bytes(out)
    try:
        rows, fieldnames = rt._open_csv(p)
        assert rt.detect_report_type(fieldnames) == rt.REPORT_CHART_OF_ACCOUNTS
    finally:
        p.unlink(missing_ok=True)


# --- route-level integration ----------------------------------------------


def _signup_and_login(client, email):
    pwd = "correct-horse-battery-staple"
    client.post("/signup", data={
        "firm_name": "Excel Upload LLP", "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=True)
    client.post("/login", data={"email": email, "password": pwd},
                follow_redirects=True)


def test_single_upload_accepts_xlsx():
    import app as appmod
    coa_csv = (ROOT / "test_data" / "01_chart_of_accounts.csv").read_bytes()
    xlsx = _csv_to_xlsx_bytes(coa_csv)
    client = appmod.app.test_client()
    _signup_and_login(client, "single@excel.example")
    resp = client.post("/upload", data={
        "company_name": "Excel Single LLP",
        "email": "ops@excel.example",
        "report_type": "",
        "ledger_file": (io.BytesIO(xlsx), "chart_of_accounts.xlsx"),
    }, content_type="multipart/form-data", follow_redirects=False)
    assert resp.status_code == 302, resp.status_code
    job_id = resp.headers["Location"].rsplit("/", 1)[-1]
    job = appmod.jobs[job_id]
    assert job["report_type"] == rt.REPORT_CHART_OF_ACCOUNTS
    # The stored source file was normalised to .csv.
    assert job["source_file"].endswith(".csv")


def test_bulk_upload_accepts_xlsx_and_rejects_xls():
    import app as appmod
    import bulk_upload as bu
    from werkzeug.datastructures import MultiDict
    coa_csv = (ROOT / "test_data" / "01_chart_of_accounts.csv").read_bytes()
    xlsx = _csv_to_xlsx_bytes(coa_csv)
    client = appmod.app.test_client()
    _signup_and_login(client, "bulk@excel.example")

    data = MultiDict()
    data["company_name"] = "Excel Bulk LLP"
    data["email"] = "ops@excel.example"
    data.add("ledger_files", (io.BytesIO(xlsx), "chart_of_accounts.xlsx"))
    data.add("ledger_files", (io.BytesIO(b"old binary"), "legacy.xls"))
    resp = client.post("/upload/bulk", data=data,
                       content_type="multipart/form-data", follow_redirects=False)
    assert resp.status_code == 302, resp.status_code
    bulk_id = resp.headers["Location"].rsplit("/", 1)[-1]
    results = appmod.bulk_uploads[bulk_id]["results"]
    by_type = {e.get("report_type"): e for e in results}
    # The .xlsx was converted and categorized as COA with a job.
    assert rt.REPORT_CHART_OF_ACCOUNTS in by_type, results
    assert by_type[rt.REPORT_CHART_OF_ACCOUNTS]["job_id"]
    # The .xls was rejected with a friendly, specific reason (no job).
    xls_entry = next(e for e in results if e["status"] == bu.STATUS_REJECTED)
    assert xls_entry["job_id"] is None
    assert ".xlsx" in xls_entry["reason"].lower()
