"""Tests for the Vendor List and Customer/Client List report types.

These listings are required documents (alongside COA, Trial Balance, GL and
Trust Listing). They let the importer match the people a firm pays and the
people who pay it to the right QuickBooks vendor / customer when posting a
cash-basis General Ledger, instead of leaning on whatever entity names happen
to appear inside the GL rows. The tests cover detection, parsing, preflight,
and that both types are surfaced as required in onboarding copy + bulk upload.
"""

import bulk_upload as bu
import onboarding_preview as op
import report_types as rt


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# --- detection -------------------------------------------------------------


def test_detect_vendor_list_from_headers():
    headers = ["vendor_id", "vendor_name", "email", "phone", "tax_id"]
    assert rt.detect_report_type(headers) == rt.REPORT_VENDOR_LIST


def test_detect_customer_list_from_headers():
    headers = ["customer_id", "customer_name", "matter_id", "email"]
    assert rt.detect_report_type(headers) == rt.REPORT_CUSTOMER_LIST


def test_gl_not_misdetected_as_vendor_or_customer():
    gl = ["transaction_id", "date", "account_number",
          "account_name", "debit", "credit"]
    assert rt.detect_report_type(gl) == rt.REPORT_GENERAL_LEDGER


def test_coa_not_misdetected_as_vendor_or_customer():
    coa = ["account_number", "account_name", "account_type", "active"]
    assert rt.detect_report_type(coa) == rt.REPORT_CHART_OF_ACCOUNTS


# --- vendor parsing + preflight -------------------------------------------


def test_parse_vendor_list_normalizes_rows(tmp_path):
    csv_text = (
        "Vendor No,Vendor Name,Email,Default Account\n"
        "V-001,Staples,ar@staples.com,6000\n"
        "V-002,Westlaw,billing@westlaw.com,6200\n"
    )
    path = _write(tmp_path, "vendors.csv", csv_text)
    rows, fieldnames, missing = rt.parse_vendor_list(path)
    assert missing == []
    assert len(rows) == 2
    assert rows[0]["vendor_name"] == "Staples"
    assert rows[0]["vendor_id"] == "V-001"
    assert rows[0]["default_account"] == "6000"

    pf = rt.build_vendor_list_preflight(rows, fieldnames, missing)
    assert pf["ready"] is True
    assert pf["vendor_count"] == 2
    assert pf["unique_vendor_count"] == 2
    assert pf["report_type"] == rt.REPORT_VENDOR_LIST


def test_vendor_preflight_flags_missing_name_column(tmp_path):
    csv_text = "Vendor No,Email\nV-001,a@b.com\n"
    path = _write(tmp_path, "vendors_bad.csv", csv_text)
    rows, fieldnames, missing = rt.parse_vendor_list(path)
    assert missing  # name column absent
    pf = rt.build_vendor_list_preflight(rows, fieldnames, missing)
    assert pf["ready"] is False
    assert pf["missing_required_columns"]


def test_vendor_preflight_reports_duplicates(tmp_path):
    csv_text = (
        "Vendor Name,Default Account\n"
        "Staples,6000\n"
        "staples,6000\n"
        "Westlaw,6200\n"
    )
    path = _write(tmp_path, "vendors_dup.csv", csv_text)
    rows, fieldnames, missing = rt.parse_vendor_list(path)
    pf = rt.build_vendor_list_preflight(rows, fieldnames, missing)
    assert "staples" in pf["duplicate_vendor_names"]
    assert pf["unique_vendor_count"] == 2


# --- customer parsing + preflight -----------------------------------------


def test_parse_customer_list_normalizes_rows(tmp_path):
    csv_text = (
        "Client ID,Client Name,Matter,Email\n"
        "C-001,Jane Doe,Smith v Jones,jane@example.com\n"
        "C-002,Acme Corp,Acme Estate,ap@acme.com\n"
    )
    path = _write(tmp_path, "clients.csv", csv_text)
    rows, fieldnames, missing = rt.parse_customer_list(path)
    assert missing == []
    assert len(rows) == 2
    assert rows[0]["customer_name"] == "Jane Doe"
    assert rows[0]["customer_id"] == "C-001"
    assert rows[0]["matter_name"] == "Smith v Jones"

    pf = rt.build_customer_list_preflight(rows, fieldnames, missing)
    assert pf["ready"] is True
    assert pf["customer_count"] == 2
    assert pf["report_type"] == rt.REPORT_CUSTOMER_LIST


def test_customer_preflight_flags_missing_name_column(tmp_path):
    csv_text = "Client ID,Email\nC-001,a@b.com\n"
    path = _write(tmp_path, "clients_bad.csv", csv_text)
    rows, fieldnames, missing = rt.parse_customer_list(path)
    assert missing
    pf = rt.build_customer_list_preflight(rows, fieldnames, missing)
    assert pf["ready"] is False


# --- required-document surfacing ------------------------------------------


def test_vendor_and_customer_are_required_in_bulk_upload():
    assert rt.REPORT_VENDOR_LIST in bu.REQUIRED_REPORTS
    assert rt.REPORT_CUSTOMER_LIST in bu.REQUIRED_REPORTS


def test_vendor_and_customer_in_onboarding_checklist_required():
    keys = {c["key"]: c for c in op.REPORTS_CHECKLIST}
    assert keys["vendor_list"]["required"] is True
    assert keys["customer_list"]["required"] is True
    # Copy must explain the cash-basis GL posting rationale.
    assert "cash basis" in keys["vendor_list"]["note"].lower()
    assert "cash basis" in keys["customer_list"]["note"].lower()


def test_reports_email_lists_vendor_and_customer():
    email = op.build_reports_email(firm_name="Test Firm")
    assert "Vendor List" in email
    assert "Customer / Client List" in email
    assert "cash basis" in email.lower()


def test_report_labels_use_quickbooks_friendly_names():
    assert rt.REPORT_LABELS[rt.REPORT_VENDOR_LIST] == "Vendor List"
    assert rt.REPORT_LABELS[rt.REPORT_CUSTOMER_LIST] == "Customer List"
