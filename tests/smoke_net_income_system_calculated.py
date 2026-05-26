"""Smoke tests for Net Income / Net Income (Loss) handling.

QuickBooks calculates Net Income from posted activity — it is not a
real account on QBO's chart of accounts. PCLaw exports sometimes list
"Net Income (Loss)" or "Current Year Earnings" as a normal row, but
creating it in QBO would conflict with the auto-calculated total and
cause reconciliation problems.

This test verifies:
  N1  ``is_system_calculated_account`` matches the expected variants.
  N2  ``map_pclaw_account_to_qbo_type`` returns ``decision='skipped'``
      with a plain-English explanation, never ``ok`` or ``blocked``.
  N3  ``build_create_plan`` routes those rows into ``plan.skipped`` and
      keeps them OUT of ``plan.to_create`` and ``plan.blocked``.
  N4  ``find_unmapped_accounts`` excludes system-calculated rows so the
      GL import path never asks the user to "match" Net Income.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("UPLOAD_DIR", tempfile.mkdtemp(prefix="pclaw_uploads_ni_"))
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="pclaw_outputs_ni_"))
os.environ.setdefault("APP_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("IMPORT_HISTORY_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-net-income")

from coa_apply import (  # noqa: E402
    is_system_calculated_account,
    map_pclaw_account_to_qbo_type,
    build_create_plan,
    SYSTEM_CALCULATED_EXPLANATION,
)
from pclaw_pipeline import find_unmapped_accounts  # noqa: E402


def n1_detector_matches_expected_variants():
    """The detector catches the names PCLaw exports use."""
    for name in (
        "Net Income",
        "Net Income (Loss)",
        "Net Income / Loss",
        "Net Loss",
        "NET INCOME",
        "  Net Income  ",
        "Current Year Earnings",
        "Current Earnings",
    ):
        assert is_system_calculated_account({"account_name": name}), name
    # Plenty of real accounts contain the word "Income" — those must NOT
    # be flagged. The detector is name-token specific.
    for name in (
        "Legal Fees Income",
        "Income Tax Payable",
        "Other Income",
        "Service Fee Income",
        "Net Receivable from Affiliate",   # has "Net" but not "Net Income"
    ):
        assert not is_system_calculated_account({"account_name": name}), name
    print(
        "N1 OK: detector catches Net Income / Net Income (Loss) / Net "
        "Loss / Current Year Earnings; does not false-positive on real "
        "Income / Receivable accounts"
    )


def n2_mapper_returns_skipped_with_plain_english():
    """``decision='skipped'`` with the customer-facing explanation."""
    result = map_pclaw_account_to_qbo_type({"account_name": "Net Income (Loss)"})
    assert result["decision"] == "skipped", result
    assert result["account_type"] is None, result
    assert result["detail_type"] is None, result
    assert result["blocked_reason"] is None, result
    assert result["skip_reason"] == SYSTEM_CALCULATED_EXPLANATION, result
    # Plain-English: should be readable by a lawyer with no QBO/CSV jargon.
    assert "calculates this automatically" in result["skip_reason"].lower()
    assert "quickbooks" in result["skip_reason"].lower()
    print(
        "N2 OK: Net Income (Loss) maps to decision='skipped' with the "
        "plain-English 'QuickBooks calculates this automatically' note"
    )


def n3_create_plan_routes_to_skipped_bucket():
    """Net Income rows go into ``plan.skipped`` — never ``to_create``
    or ``blocked``."""
    coa_rows = [
        {"account_number": "1000", "account_name": "Operating Bank",
         "account_type": "Bank", "detail_type": "Checking"},
        {"account_number": "9999", "account_name": "Net Income (Loss)",
         "account_type": "Equity"},
        {"account_number": "9998", "account_name": "Net Income"},
        {"account_number": "9997", "account_name": "Current Year Earnings"},
    ]
    preview = {
        "matched": [],
        "conflicts": [],
        "would_create": [
            {"account_number": r["account_number"], "account_name": r["account_name"]}
            for r in coa_rows
        ],
    }
    plan = build_create_plan(coa_rows, preview)
    # Operating Bank is the only real to_create row.
    to_create_names = [e.account_name for e in plan.to_create]
    skipped_names = [e.account_name for e in plan.skipped]
    blocked_names = [e.account_name for e in plan.blocked]
    assert to_create_names == ["Operating Bank"], (
        f"only Operating Bank should be created, got {to_create_names}"
    )
    assert "Net Income (Loss)" in skipped_names, skipped_names
    assert "Net Income" in skipped_names, skipped_names
    assert "Current Year Earnings" in skipped_names, skipped_names
    assert not blocked_names, (
        f"system-calculated rows must not be blocked, got: {blocked_names}"
    )
    # Plan exposes the skipped count + reason for the UI.
    d = plan.to_dict()
    assert d["skipped_count"] == 3, d
    assert d["has_skipped"] is True, d
    for entry in d["skipped"]:
        assert entry["skip_reason"], entry
        assert entry["decision"] == "skipped", entry
    print(
        "N3 OK: build_create_plan routes Net Income / Current Year "
        "Earnings into plan.skipped; only real accounts land in to_create"
    )


def n4_unmapped_helper_excludes_system_calculated():
    """``find_unmapped_accounts`` must not flag Net Income as unmapped —
    that would put a 'match this account' blocker on a row QBO owns."""
    rows = [
        {"account_number": "1000", "account_name": "Operating Bank",
         "debit": "100", "credit": "0"},
        {"account_number": "9999", "account_name": "Net Income (Loss)",
         "debit": "0", "credit": "100"},
    ]
    # Map only has the operating bank, not Net Income — but Net Income
    # must not show up in the unmapped set.
    mapping = {"1000": "qbo-id-1"}
    unmapped = find_unmapped_accounts(rows, mapping, mapping_mode="number")
    assert not any("Net Income" in u for u in unmapped), (
        f"Net Income should not appear in unmapped set: {unmapped}"
    )
    # And a real missing account still surfaces.
    rows.append({"account_number": "5000", "account_name": "Rent Expense",
                 "debit": "50", "credit": "0"})
    unmapped = find_unmapped_accounts(rows, mapping, mapping_mode="number")
    assert any("Rent Expense" in u for u in unmapped), (
        f"Real missing account should still be flagged: {unmapped}"
    )
    print(
        "N4 OK: find_unmapped_accounts excludes Net Income (Loss) but "
        "still flags genuinely-missing accounts"
    )


def main():
    n1_detector_matches_expected_variants()
    n2_mapper_returns_skipped_with_plain_english()
    n3_create_plan_routes_to_skipped_bucket()
    n4_unmapped_helper_excludes_system_calculated()
    print("\nALL NET-INCOME SYSTEM-CALCULATED SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
