"""Comprehensive test suite for mapping_readiness.evaluate()

Run: python test_mapping_readiness.py
"""
from mapping_readiness import evaluate, normalize_account_name
from app import _extract_pclaw_accounts_from_gl_rows


def test_case_1_all_auto_matched_by_number():
    """All auto-matched by number — no saved rows."""
    pclaw_accounts = [
        {"number": "1001", "name": "Bank"},
        {"number": "1002", "name": "AR"},
        {"number": "1003", "name": "Expenses"},
    ]
    live_accounts = [
        {"Id": "1", "Name": "Bank", "AcctNum": "1001", "Active": True},
        {"Id": "2", "Name": "AR", "AcctNum": "1002", "Active": True},
        {"Id": "3", "Name": "Expenses", "AcctNum": "1003", "Active": True},
    ]
    saved_mappings = []

    result = evaluate(pclaw_accounts, saved_mappings, live_accounts)

    assert result["mapping_mode"] == "number", f"Got {result['mapping_mode']}"
    assert result["ready"] is True, f"ready={result['ready']}"
    assert result["counts"]["resolved"] == 3, f"resolved={result['counts']['resolved']}"
    assert result["counts"]["unmatched"] == 0, f"unmatched={result['counts']['unmatched']}"
    assert all(r["via"] == "auto" for r in result["resolved"]), "Not all via=auto"
    print("[OK] Case 1: All auto-matched by number")


def test_case_2_bounce_case_stale_saved_row():
    """Saved row points at dead QBO id (inactive or missing)."""
    pclaw_accounts = [
        {"number": "1001", "name": "Bank"},
        {"number": "1002", "name": "AP"},
    ]
    live_accounts = [
        {"Id": "1", "Name": "Bank", "AcctNum": "1001", "Active": True},
        # 1002 is INACTIVE in QBO
        {"Id": "2", "Name": "AP", "AcctNum": "1002", "Active": False},
    ]
    saved_mappings = [
        {
            "pclaw_account_number": "1002",
            "pclaw_account_name": "AP",
            "qbo_account_id": "2",
            "qbo_account_name": "AP",
        },
    ]

    result = evaluate(pclaw_accounts, saved_mappings, live_accounts)

    assert result["ready"] is False, f"ready={result['ready']}"
    assert len(result["stale"]) == 1, f"stale count={len(result['stale'])}"
    assert result["counts"]["unmatched"] == 1, f"unmatched={result['counts']['unmatched']}"
    print("[OK] Case 2: Stale saved row detected (inactive account)")


def test_case_3_truly_unmatched_account():
    """One account has no auto match and no saved mapping."""
    pclaw_accounts = [
        {"number": "1001", "name": "Bank"},
        {"number": "1002", "name": "NewExpense"},
    ]
    live_accounts = [
        {"Id": "1", "Name": "Bank", "AcctNum": "1001", "Active": True},
        # 1002 does not exist in QBO at all
    ]
    saved_mappings = []

    result = evaluate(pclaw_accounts, saved_mappings, live_accounts)

    assert result["ready"] is False, f"ready={result['ready']}"
    assert len(result["unmatched"]) == 1, f"unmatched count={len(result['unmatched'])}"
    assert result["unmatched"][0]["number"] == "1002", f"Got {result['unmatched'][0]}"
    print("[OK] Case 3: Truly unmatched account detected")


def test_case_4_saved_overrides_auto():
    """Saved mapping overrides auto match when qbo_id is live-active."""
    pclaw_accounts = [
        {"number": "1001", "name": "Operating Bank"},
    ]
    live_accounts = [
        {"Id": "1", "Name": "Bank", "AcctNum": "1001", "Active": True},
        {"Id": "99", "Name": "Some Other Bank", "AcctNum": "", "Active": True},
    ]
    saved_mappings = [
        {
            "pclaw_account_number": "1001",
            "pclaw_account_name": "Operating Bank",
            "qbo_account_id": "99",  # Saved points at the other bank, not auto's choice
            "qbo_account_name": "Some Other Bank",
        },
    ]

    result = evaluate(pclaw_accounts, saved_mappings, live_accounts)

    assert result["mapping"]["1001"] == "99", f"Got {result['mapping'].get('1001')}"
    assert result["resolved"][0]["via"] == "saved", f"Got {result['resolved'][0]['via']}"
    print("[OK] Case 4: Saved mapping overrides auto")


def test_case_5_mode_number_even_when_auto_empty():
    """Mode is 'number' even if auto_by_number is empty (PCLaw has numbers).

    This is the regression case from 2026-07-11 (63 phantom unmapped).
    Never fall back to 'name' mode because auto result is empty.
    """
    pclaw_accounts = [
        {"number": "1001", "name": "Bank"},
        {"number": "1002", "name": "Expenses"},
    ]
    live_accounts = [
        # No AcctNum field — so auto_by_number will return {}
        {"Id": "1", "Name": "Bank", "Active": True},
        {"Id": "2", "Name": "Expenses", "Active": True},
    ]
    saved_mappings = []

    result = evaluate(pclaw_accounts, saved_mappings, live_accounts)

    assert result["mapping_mode"] == "number", f"Got {result['mapping_mode']}"
    assert result["mode_reason"] == "pclaw_has_numbers", f"Got {result['mode_reason']}"
    assert result["ready"] is False, f"ready={result['ready']}"
    assert result["counts"]["unmatched"] == 2, f"unmatched={result['counts']['unmatched']}"
    assert result["counts"]["total"] == 2, f"total={result['counts']['total']}"
    print("[OK] Case 5: Mode=number even with empty auto_by_number")


def test_case_6_continued_consolidation():
    """'-Continued' duplicates consolidate to one account."""
    pclaw_accounts = [
        {"number": "1012", "name": "NFCU - 0025 - Operating Acct."},
        {"number": "1012", "name": "NFCU - 0025 - Operating Acct. - Continued"},
        {"number": "1013", "name": "Trust Account"},
        {"number": "1013", "name": "Trust Account (continued)"},
    ]
    live_accounts = [
        {"Id": "1", "Name": "NFCU", "AcctNum": "1012", "Active": True},
        {"Id": "2", "Name": "Trust", "AcctNum": "1013", "Active": True},
    ]
    saved_mappings = []

    result = evaluate(pclaw_accounts, saved_mappings, live_accounts)

    assert result["counts"]["total"] == 2, f"total={result['counts']['total']} (expected 2)"
    assert result["ready"] is True, f"ready={result['ready']}"
    assert result["counts"]["resolved"] == 2, f"resolved={result['counts']['resolved']}"
    # Check no '-continued' string in resolved account names
    for r in result["resolved"]:
        acct_name = str(r["account"].get("name") or "")
        assert "continued" not in acct_name.lower(), f"Continued leak: {acct_name}"
    print("[OK] Case 6: '-Continued' consolidation (4 rows --> 2 distinct)")


def test_symmetry_load_vs_extract():
    """Symmetry check: direct list = GL rows extracted.

    Both approaches should yield the same consolidated pclaw_accounts
    and the same readiness verdict.
    """
    # Approach A: Direct list (simulating _load_pclaw_accounts_for_mapping)
    pclaw_a = [
        {"number": "1001", "name": "Bank"},
        {"number": "1002", "name": "Expenses"},
    ]

    # Approach B: Build GL rows with '-Continued' variants, extract
    gl_rows = [
        {
            "account_number": "1001",
            "account_name": "Bank",
            "transaction_id": "100",
            "debit": "100",
            "credit": "0",
        },
        {
            "account_number": "1001",
            "account_name": "Bank - Continued",  # Page break variant
            "transaction_id": "101",
            "debit": "50",
            "credit": "0",
        },
        {
            "account_number": "1002",
            "account_name": "Expenses",
            "transaction_id": "102",
            "debit": "0",
            "credit": "100",
        },
    ]
    pclaw_b = _extract_pclaw_accounts_from_gl_rows(gl_rows)

    # Common fixtures for both evaluations
    live_accounts = [
        {"Id": "1", "Name": "Bank", "AcctNum": "1001", "Active": True},
        {"Id": "2", "Name": "Expenses", "AcctNum": "1002", "Active": True},
    ]
    saved_mappings = []

    result_a = evaluate(pclaw_a, saved_mappings, live_accounts)
    result_b = evaluate(pclaw_b, saved_mappings, live_accounts)

    assert result_a["counts"]["total"] == result_b["counts"]["total"], \
        f"Total mismatch: {result_a['counts']['total']} vs {result_b['counts']['total']}"
    assert set(
        k for r in result_a["resolved"]
        for k in (r["account"].get("number"), r["account"].get("name"))
        if k
    ) == set(
        k for r in result_b["resolved"]
        for k in (r["account"].get("number"), r["account"].get("name"))
        if k
    ), "Resolved accounts don't match"
    print("[OK] Symmetry check: Direct list = GL extraction")


def main():
    print("\n=== Mapping Readiness Tests (7 cases) ===\n")
    test_case_1_all_auto_matched_by_number()
    test_case_2_bounce_case_stale_saved_row()
    test_case_3_truly_unmatched_account()
    test_case_4_saved_overrides_auto()
    test_case_5_mode_number_even_when_auto_empty()
    test_case_6_continued_consolidation()
    test_symmetry_load_vs_extract()
    print("\n=== All 7 tests PASSED ===\n")


if __name__ == "__main__":
    main()
