#!/usr/bin/env python3
"""Smoke tests for GL parser column mapping and validation."""

from qbo_client import _build_gl_column_map, _validate_gl_columns

def test_column_map():
    """Test column-map building with QBO GL header variations."""
    header = ["Tx Date", "Txn Type", "Doc Num", "Name", "Memo", "Debt Amt", "Credit Amt"]
    col_map = _build_gl_column_map(header)
    assert col_map == {
        "tx_date": 0,
        "txn_type": 1,
        "doc_num": 2,
        "name": 3,
        "memo": 4,
        "debt_amt": 5,
        "credit_amt": 6,
    }, f"Column map mismatch: {col_map}"
    print("[OK] Column-map OK")


def test_validate_columns():
    """Test column validation hard-fail on missing columns."""
    valid_map = {
        "tx_date": 0,
        "txn_type": 1,
        "doc_num": 2,
        "name": 3,
        "memo": 4,
        "debt_amt": 5,
        "credit_amt": 6,
    }
    # Should not raise
    _validate_gl_columns(valid_map)
    print("[OK] Validation OK for complete map")

    # Should raise on missing debt_amt (hard requirement, never falls back to Amount)
    incomplete_map = valid_map.copy()
    del incomplete_map["debt_amt"]
    try:
        _validate_gl_columns(incomplete_map)
        raise AssertionError("Should have raised ValueError for missing debt_amt")
    except ValueError as e:
        assert "missing required columns" in str(e).lower()
        print("[OK] Hard-fail on missing debt_amt OK")


def test_suffix_aware_account_regex():
    """Test that GL regex handles decimal suffixes in account numbers."""
    from qbo_client import _GL_ACCT_NUM_RE
    assert _GL_ACCT_NUM_RE.match("1000")
    assert _GL_ACCT_NUM_RE.match("1000.0")
    assert _GL_ACCT_NUM_RE.match("1 000")
    assert not _GL_ACCT_NUM_RE.match("ABC123")
    print("[OK] Suffix-aware regex OK")


if __name__ == "__main__":
    test_column_map()
    test_validate_columns()
    test_suffix_aware_account_regex()
    print("\nsmoke OK — 4 key functions verified")
