"""Smoke tests for fixed-asset / liability inference from an uploaded
account list.

The user's accounting colleague reported that even with the PCLaw
account list uploaded, the COA flow only auto-matched "Office
Equipment" to a Fixed Asset and left related fixed-asset rows
("Computers", "Furniture & Fixtures", "Leasehold Improvements",
"Office Construction") unmatched. The fix: when the account list has a
high-level category like "Fixed Asset" and the account name is one of
the common law-firm fixed-asset / liability names, resolve to a
specific QBO sub-type instead of bailing out.

Covers:
  F1  Each of the five reported fixed-asset names resolves to a
      specific Fixed Asset sub-type when the account list provides
      either no category or the generic "Fixed Asset" bucket.
  F2  Common law-firm liability names ("Loan From John Smith",
      "Shareholder Loan") resolve to NotesPayable variants.
  F3  An explicit operator-supplied sub-type still wins over the new
      inference (operator override is sacred).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("UPLOAD_DIR", tempfile.mkdtemp(prefix="pclaw_uploads_fa_"))
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="pclaw_outputs_fa_"))
os.environ.setdefault("APP_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("IMPORT_HISTORY_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-fixed-asset")

from coa_apply import map_pclaw_account_to_qbo_type  # noqa: E402


def f1_fixed_asset_names_resolve_to_specific_subtypes():
    """All five reported fixed-asset names get a specific QBO sub-type."""
    expected = {
        "Computers": ("Fixed Asset", "MachineryAndEquipment"),
        "Furniture & Fixtures": ("Fixed Asset", "FurnitureAndFixtures"),
        "Leasehold Improvements": ("Fixed Asset", "LeaseholdImprovements"),
        "Office Construction": ("Fixed Asset", "LeaseholdImprovements"),
        "Office Equipment": ("Fixed Asset", "MachineryAndEquipment"),
    }
    # Test both shapes: (a) account list only carries the bare name,
    # and (b) account list carries the generic "Fixed Asset" bucket.
    for shape in ("name_only", "with_generic_bucket"):
        for name, (want_type, want_detail) in expected.items():
            row = {"account_name": name}
            if shape == "with_generic_bucket":
                row["account_type"] = "Fixed Asset"
            result = map_pclaw_account_to_qbo_type(row)
            assert result["decision"] in ("ok", "warn"), (
                f"[{shape}] {name!r} unexpectedly blocked: {result}"
            )
            assert result["account_type"] == want_type, (
                f"[{shape}] {name!r} got {result['account_type']!r}, "
                f"want {want_type!r}"
            )
            assert result["detail_type"] == want_detail, (
                f"[{shape}] {name!r} got {result['detail_type']!r}, "
                f"want {want_detail!r}"
            )
    print(
        "F1 OK: all 5 fixed-asset names resolve to specific QBO sub-types "
        "from the account list, with or without the generic bucket"
    )


def f2_law_firm_liabilities_resolve_safely():
    """Common law-firm liability names map to NotesPayable variants."""
    cases = [
        ("Loan From John Smith", "Long Term Liability", "NotesPayable"),
        ("Shareholder Loan", "Long Term Liability", "ShareholderNotesPayable"),
        ("Partner Loan", "Long Term Liability", "NotesPayable"),
        ("Notes Payable", "Long Term Liability", "NotesPayable"),
    ]
    for name, want_type, want_detail in cases:
        result = map_pclaw_account_to_qbo_type({"account_name": name})
        assert result["decision"] in ("ok", "warn"), (
            f"{name!r} unexpectedly blocked: {result}"
        )
        assert result["account_type"] == want_type, (name, result)
        assert result["detail_type"] == want_detail, (name, result)
    print(
        "F2 OK: common law-firm liability names resolve to NotesPayable / "
        "ShareholderNotesPayable from the account list"
    )


def f3_explicit_subtype_overrides_inference():
    """If the account list supplies an explicit detail_type, that wins."""
    # "Computers" with explicit detail_type "Buildings" — operator wins.
    result = map_pclaw_account_to_qbo_type({
        "account_name": "Computers",
        "account_type": "Fixed Asset",
        "detail_type": "Buildings",
    })
    assert result["decision"] in ("ok", "warn"), result
    assert result["account_type"] == "Fixed Asset", result
    assert result["detail_type"] == "Buildings", result
    assert result["match_hint"] == "detail_type", result
    print(
        "F3 OK: explicit operator-supplied detail_type wins over the new "
        "fixed-asset inference (operator override is preserved)"
    )


def main():
    f1_fixed_asset_names_resolve_to_specific_subtypes()
    f2_law_firm_liabilities_resolve_safely()
    f3_explicit_subtype_overrides_inference()
    print("\nALL FIXED-ASSET ACCOUNT-LIST INFERENCE SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
