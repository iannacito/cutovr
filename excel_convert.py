"""Convert PCLaw Excel exports (.xlsx) to CSV bytes.

PCLaw reports often come out of the firm's machine as Excel workbooks
rather than CSV — a lawyer hits "Export" and gets an .xlsx. Rather than
making them re-save every report, we accept .xlsx and flatten the first
worksheet to CSV so the existing detection + parsing pipeline (which is
resilient to preamble, BOM, and footer rows) handles it unchanged.

Scope:
  * ``.xlsx`` / ``.xlsm`` — converted here with openpyxl (pure-Python, no
    system libraries, already used elsewhere in the project).
  * ``.xls`` — the legacy binary format needs a separate reader we do not
    ship. We raise a friendly, specific error telling the firm to re-save
    as .xlsx or .csv, never a generic "couldn't read the ledger" message.

The module is pure: it works on bytes and returns bytes, with no Flask or
filesystem coupling, so it is trivially testable.
"""

from __future__ import annotations

import csv
import datetime
import io


EXCEL_EXTENSIONS = (".xlsx", ".xlsm")
LEGACY_EXCEL_EXTENSIONS = (".xls",)


class ExcelConversionError(Exception):
    """Raised with a lawyer-friendly message when conversion can't proceed."""


def is_excel_filename(filename: str) -> bool:
    """True for any Excel extension we recognise (.xlsx/.xlsm/.xls)."""
    lower = (filename or "").lower()
    return lower.endswith(EXCEL_EXTENSIONS) or lower.endswith(LEGACY_EXCEL_EXTENSIONS)


def is_legacy_excel_filename(filename: str) -> bool:
    """True for the old binary .xls format we can't read directly."""
    return (filename or "").lower().endswith(LEGACY_EXCEL_EXTENSIONS)


def csv_filename_for(filename: str) -> str:
    """Return ``<stem>.csv`` for an Excel filename (keeps the report name)."""
    name = filename or "upload"
    for ext in EXCEL_EXTENSIONS + LEGACY_EXCEL_EXTENSIONS:
        if name.lower().endswith(ext):
            return name[: -len(ext)] + ".csv"
    return name + ".csv"


def _cell_to_text(value) -> str:
    """Render one Excel cell as the plain text a CSV parser expects."""
    if value is None:
        return ""
    if isinstance(value, bool):
        # Excel TRUE/FALSE — keep as words rather than 1/0.
        return "TRUE" if value else "FALSE"
    if isinstance(value, (datetime.datetime, datetime.date)):
        # PCLaw dates: keep the date part as ISO so downstream date parsing
        # (e.g. trial-balance as-of dates) reads them cleanly.
        if isinstance(value, datetime.datetime) and (
            value.hour or value.minute or value.second
        ):
            return value.isoformat(sep=" ")
        return value.date().isoformat() if isinstance(value, datetime.datetime) else value.isoformat()
    if isinstance(value, float):
        # Excel stores all numbers as floats; render whole numbers without a
        # trailing ".0" so account numbers like 1000 don't become "1000.0".
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


def excel_bytes_to_csv_bytes(data: bytes, filename: str = "") -> bytes:
    """Convert the first worksheet of an Excel workbook to CSV bytes.

    Raises ``ExcelConversionError`` with a specific, friendly message for
    legacy ``.xls`` files, empty workbooks, and unreadable/corrupt files.
    """
    if is_legacy_excel_filename(filename):
        raise ExcelConversionError(
            "This looks like an older Excel file (.xls). In Excel, open it "
            "and choose File → Save As, then pick “Excel Workbook "
            "(.xlsx)” or “CSV (Comma delimited)” and upload "
            "that file."
        )

    try:
        import openpyxl  # imported lazily so the app boots even without it
    except ImportError as exc:  # pragma: no cover - dependency always present
        raise ExcelConversionError(
            "We couldn't open this Excel file. Please re-save the report as "
            "CSV (File → Save As → CSV) and upload that instead."
        ) from exc

    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True
        )
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many types
        raise ExcelConversionError(
            "We couldn't read this Excel file — it may be password "
            "protected or damaged. Try re-saving the report as CSV (File "
            "→ Save As → CSV) and upload that instead."
        ) from exc

    try:
        worksheet = workbook.worksheets[0] if workbook.worksheets else None
        if worksheet is None:
            raise ExcelConversionError(
                "This Excel file has no worksheets. Re-export the report "
                "from PCLaw and try again."
            )

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        wrote_any = False
        for row in worksheet.iter_rows(values_only=True):
            cells = [_cell_to_text(v) for v in row]
            # Drop fully-empty rows so spreadsheet padding doesn't become
            # blank CSV lines (the parser tolerates them, but this is tidier).
            if not any(c.strip() for c in cells):
                continue
            writer.writerow(cells)
            wrote_any = True
    finally:
        workbook.close()

    if not wrote_any:
        raise ExcelConversionError(
            "This Excel file has no data rows. Re-export the report from "
            "PCLaw and try again."
        )

    return buffer.getvalue().encode("utf-8")
