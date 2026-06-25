"""Tests for reserved "-PC Law" account naming.

A PCLaw trial balance can list accounts QuickBooks owns or computes
itself — Net Income, Retained Earnings, Accounts Receivable, Accounts
Payable. The migration posts those opening balances into clearly-labelled
"-PC Law" holding accounts instead, so QuickBooks' built-in totals stay
clean. These cover:

  * the pure name matcher in ``reserved_accounts``,
  * ``coa_apply`` treating the holding accounts as real, createable
    accounts (and never as system-calculated),
  * ``opening_balance`` routing reserved TB rows to the holding account
    and never to QuickBooks' built-in account.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import reserved_accounts as ra
import coa_apply
import opening_balance as ob


# --- name matcher ----------------------------------------------------------


def test_match_reserved_native_names():
    assert ra.match_reserved("Net Income").key == "net_income"
    assert ra.match_reserved("Net Income (Loss)").key == "net_income"
    assert ra.match_reserved("Current Year Earnings").key == "net_income"
    assert ra.match_reserved("Retained Earnings").key == "retained_earnings"
    assert ra.match_reserved("Accounts Receivable").key == "accounts_receivable"
    assert ra.match_reserved("A/R").key == "accounts_receivable"
    assert ra.match_reserved("Accounts Payable").key == "accounts_payable"
    assert ra.match_reserved("A/P").key == "accounts_payable"


def test_match_reserved_ignores_ordinary_and_holding_accounts():
    # Ordinary accounts that merely contain a reserved word must not match.
    for name in (
        "Income Tax Payable",
        "Legal Fees Income",
        "Trust Receivable",
        "Rent Expense",
        "Operating Bank",
    ):
        assert ra.match_reserved(name) is None, name
    # The holding accounts themselves must not re-match (no infinite routing).
    for name in ("Net Income-PC Law", "RE-PC Law", "AR-PC Law", "AP-PC Law"):
        assert ra.match_reserved(name) is None, name
        assert ra.is_reserved_pc_law_name(name), name


def test_pc_law_names_and_types():
    by_key = {r.key: r for r in ra.RESERVED_ACCOUNTS}
    assert by_key["net_income"].pc_law_name == "Net Income-PC Law"
    assert by_key["retained_earnings"].pc_law_name == "RE-PC Law"
    assert by_key["accounts_receivable"].pc_law_name == "AR-PC Law"
    assert by_key["accounts_receivable"].qbo_account_type == "Other Current Asset"
    assert by_key["accounts_payable"].pc_law_name == "AP-PC Law"
    assert by_key["accounts_payable"].qbo_account_type == "Other Current Liability"


# --- coa_apply integration -------------------------------------------------


def test_holding_accounts_are_creatable_not_system_calculated():
    for name, exp_type in (
        ("Net Income-PC Law", "Equity"),
        ("RE-PC Law", "Equity"),
        ("AR-PC Law", "Other Current Asset"),
        ("AP-PC Law", "Other Current Liability"),
    ):
        assert not coa_apply.is_system_calculated_account({"account_name": name}), name
        res = coa_apply.map_pclaw_account_to_qbo_type({"account_name": name})
        assert res["decision"] in ("ok", "warn"), (name, res)
        assert res["account_type"] == exp_type, (name, res)


def test_native_net_income_still_system_calculated():
    # The native QBO-owned name is still skipped — only the "-PC Law"
    # holding account is createable.
    assert coa_apply.is_system_calculated_account({"account_name": "Net Income"})
    assert coa_apply.is_system_calculated_account({"account_name": "Net Income (Loss)"})
    res = coa_apply.map_pclaw_account_to_qbo_type({"account_name": "Net Income"})
    assert res["decision"] == "skipped", res


def test_net_income_pc_law_lands_in_create_plan():
    coa_rows = [
        {"account_number": "9990", "account_name": "Net Income-PC Law"},
        {"account_number": "9999", "account_name": "Net Income"},
    ]
    preview = {"matched": [], "conflicts": [], "would_create": [
        {"account_number": r["account_number"], "account_name": r["account_name"]}
        for r in coa_rows
    ]}
    plan = coa_apply.build_create_plan(coa_rows, preview)
    to_create = [e.account_name for e in plan.to_create]
    skipped = [e.account_name for e in plan.skipped]
    assert "Net Income-PC Law" in to_create, plan.to_dict()
    assert "Net Income" in skipped, plan.to_dict()


# --- opening_balance routing ----------------------------------------------


def _qbo(*accounts):
    return {"QueryResponse": {"Account": list(accounts)}}


def test_opening_balance_routes_reserved_to_holding_account():
    tb = [
        {"account_number": "1000", "account_name": "Bank",
         "debit_balance": "100.00", "credit_balance": "0.00",
         "as_of_date": "2026-03-31"},
        {"account_number": "1200", "account_name": "Accounts Receivable",
         "debit_balance": "0.00", "credit_balance": "100.00"},
    ]
    qbo = _qbo(
        {"Id": "10", "Name": "Bank", "AcctNum": "1000", "AccountType": "Bank"},
        # Native AR exists, but we must NOT post into it.
        {"Id": "20", "Name": "Accounts Receivable", "AcctNum": "1200",
         "AccountType": "Accounts Receivable"},
        {"Id": "30", "Name": "AR-PC Law", "AccountType": "Other Current Asset"},
    )
    plan = ob.build_opening_balance_plan(tb, qbo)
    ar_line = next(l for l in plan.lines if l.account_name == "Accounts Receivable")
    assert ar_line.routed_to == "AR-PC Law"
    assert ar_line.qbo_account_id == "30"
    assert ar_line.qbo_account_name == "AR-PC Law"
    assert ar_line.blocker is None
    assert any("AR-PC Law" in w for w in plan.warnings)
    assert plan.balanced and not plan.has_blockers


def test_opening_balance_blocks_when_holding_account_missing():
    tb = [
        {"account_number": "1000", "account_name": "Bank",
         "debit_balance": "100.00", "credit_balance": "0.00"},
        {"account_number": "1200", "account_name": "Accounts Receivable",
         "debit_balance": "0.00", "credit_balance": "100.00"},
    ]
    # Native AR present, but the AR-PC Law holding account is NOT created.
    qbo = _qbo(
        {"Id": "10", "Name": "Bank", "AcctNum": "1000", "AccountType": "Bank"},
        {"Id": "20", "Name": "Accounts Receivable", "AcctNum": "1200",
         "AccountType": "Accounts Receivable"},
    )
    plan = ob.build_opening_balance_plan(tb, qbo)
    ar_line = next(l for l in plan.lines if l.account_name == "Accounts Receivable")
    # Must not silently post into QuickBooks' built-in AR.
    assert ar_line.qbo_account_id is None
    assert ar_line.routed_to == "AR-PC Law"
    assert ar_line.blocker and "AR-PC Law" in ar_line.blocker
    assert plan.has_blockers


def test_operator_mapping_overrides_reserved_routing():
    tb = [
        {"account_number": "1000", "account_name": "Bank",
         "debit_balance": "100.00", "credit_balance": "0.00"},
        {"account_number": "1200", "account_name": "Accounts Receivable",
         "debit_balance": "0.00", "credit_balance": "100.00"},
    ]
    qbo = _qbo(
        {"Id": "10", "Name": "Bank", "AcctNum": "1000", "AccountType": "Bank"},
        {"Id": "55", "Name": "Custom AR Holding",
         "AccountType": "Other Current Asset"},
        {"Id": "30", "Name": "AR-PC Law", "AccountType": "Other Current Asset"},
    )
    mappings = [{"pclaw_account_number": "1200", "qbo_account_id": "55"}]
    plan = ob.build_opening_balance_plan(tb, qbo, account_mappings=mappings)
    ar_line = next(l for l in plan.lines if l.account_name == "Accounts Receivable")
    # Explicit operator mapping wins over automatic -PC Law routing.
    assert ar_line.qbo_account_id == "55"
