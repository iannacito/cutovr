"""Tests for the Beginning Trial Balance posting-date selector.

The starting-balance card defaults its posting date from the firm's Step 1
(Setup) answers so a lawyer never retypes a date they already gave us, and
lets them change it. These cover the pure date-resolution precedence plus
the opening JE payload picking up the resolved date as the QuickBooks
TxnDate.
"""

import opening_balance as ob


def test_override_takes_precedence():
    cutover = {"opening_balance_date": "2026-01-01", "cutover_date": "2026-04-01"}
    assert ob.resolve_opening_balance_date(cutover, override="2026-03-31") == "2026-03-31"


def test_defaults_to_step1_opening_balance_date():
    cutover = {"opening_balance_date": "2026-01-01", "cutover_date": "2026-04-01"}
    assert ob.resolve_opening_balance_date(cutover) == "2026-01-01"


def test_falls_back_to_cutover_date():
    cutover = {"cutover_date": "2026-04-01"}
    assert ob.resolve_opening_balance_date(cutover) == "2026-04-01"


def test_falls_back_to_tb_row_date():
    assert ob.resolve_opening_balance_date({}, tb_rows=[{"as_of_date": "2026-02-28"}]) == "2026-02-28"


def test_empty_when_nothing_available():
    assert ob.resolve_opening_balance_date({}) == ""


def test_malformed_override_is_ignored():
    cutover = {"cutover_date": "2026-04-01"}
    # A free-text override never silently becomes the posting date.
    assert ob.resolve_opening_balance_date(cutover, override="next tuesday") == "2026-04-01"


def test_malformed_step1_dates_are_skipped():
    cutover = {"opening_balance_date": "Jan 1 2026", "cutover_date": "2026-04-01"}
    assert ob.resolve_opening_balance_date(cutover) == "2026-04-01"


def test_is_iso_date():
    assert ob.is_iso_date("2026-03-31")
    assert not ob.is_iso_date("3/31/2026")
    assert not ob.is_iso_date("")
    assert not ob.is_iso_date(None)


def test_resolved_date_flows_into_je_payload():
    """The opening JE uses the resolved date as its QuickBooks TxnDate."""
    cutover = {"cutover_date": "2026-04-01"}
    as_of = ob.resolve_opening_balance_date(cutover, override="2026-03-31")
    qbo = {"QueryResponse": {"Account": [
        {"Id": "10", "AcctNum": "1000", "Name": "Bank", "AccountType": "Bank"},
        {"Id": "20", "AcctNum": "3000", "Name": "Equity", "AccountType": "Equity"},
    ]}}
    tb = [
        {"account_number": "1000", "account_name": "Bank", "debit_balance": "100.00", "credit_balance": "0"},
        {"account_number": "3000", "account_name": "Equity", "debit_balance": "0", "credit_balance": "100.00"},
    ]
    plan = ob.build_opening_balance_plan(tb, qbo, as_of_date=as_of)
    assert plan.balanced
    assert not plan.has_blockers
    payload = ob.build_opening_je_payload(plan)
    assert payload["TxnDate"] == "2026-03-31"
