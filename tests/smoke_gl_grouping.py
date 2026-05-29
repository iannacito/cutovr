"""Smoke tests for source-journal grouping (gl_grouping.py).

Background — Cesar's QA 2026-05-29
----------------------------------
The validator was hard-blocking three PCLaw transaction references that
were individually unbalanced but whose combined debit/credit totals
matched ($43,334.29 each). The fix is in ``gl_grouping`` — group
unbalanced references by their source-journal token (the first word of
the memo, e.g. ``GB``) and only rescue groups that balance to the cent.

Tests cover:

  T1 Cesar's exact payload (refs 259730 / 259733 / 259736 under "GB")
     balances as a group and is rescued.

  T2 Mixed bag: GB rescues; an unrelated still-unbalanced reference
     stays blocked.

  T3 Negative path: when the combined total still doesn't balance, no
     rescue happens and the references stay blocked.

  T4 Balanced individual references aren't merged — they keep their
     own PCLaw reference number on the QBO side so the firm can trace
     each entry.

  T5 Cross-token offsets (CER short by $46.05, GB long by $46.05)
     are surfaced as an explanatory pair but NOT merged across
     source journals.

  T6 The import pipeline plan returns (payloads, posted_ids) with one
     GROUP- entry per rescued bucket and refuses to plan if anything
     remains unbalanced.
"""

import os
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Pure-module tests — no Flask app, no DB. Set env vars for safety so
# if a downstream import ever touches them they get clean values.
os.environ.setdefault("APP_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("IMPORT_HISTORY_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("SECRET_KEY", "smoke-secret")

from gl_grouping import (  # noqa: E402
    build_source_journal_groups,
    cross_token_offsets,
    plan_posting_groups,
    source_journal_token,
    split_balanced_and_unbalanced,
)
from pclaw_pipeline import (  # noqa: E402
    build_account_mapping_from_numbers,
    build_account_type_index,
    group_rows_by_transaction,
    plan_balanced_payloads,
)


# --- helpers -----------------------------------------------------------------

def _row(txn, account_num, account_name, debit, credit, memo="GB Payroll", date="2021-01-15"):
    return {
        "transaction_id": txn,
        "date": date,
        "account_number": account_num,
        "account_name": account_name,
        "debit": debit,
        "credit": credit,
        "memo": memo,
        "description": memo,
        "vendor_name": "Payroll",
    }


def _cesar_payroll_rows():
    """Reproduce the exact ref/amount layout from Cesar's 2026-05-29 email.

    refs 259730 (GB Payroll), 259733 (GB 401K), 259736 (GB Payroll).
    Combined: debits=43334.29, credits=43334.29.
    """
    return [
        _row("259730", "1012", "NFCU Operating", "0",        "25024.66", memo="GB Payroll"),
        _row("259730", "5110", "Gross Salaries-Prof", "28591.67", "0",   memo="GB Payroll"),
        _row("259730", "5130", "Gross Salaries-Supp", "3650.00", "0",    memo="GB Payroll"),
        _row("259730", "5130", "Gross Salaries-Supp", "4812.51", "0",    memo="GB Payroll"),
        _row("259730", "5140", "Salaries Jonathan",   "2996.22", "0",    memo="GB Payroll"),
        _row("259730", "5150", "Health Insurance",    "0",       "1507.91", memo="GB Payroll"),
        _row("259733", "1012", "NFCU Operating",      "0",       "2773.92", memo="GB 401K"),
        _row("259736", "1012", "NFCU Operating",      "0",       "14027.80", memo="GB Payroll"),
        _row("259736", "5111", "Payroll Taxes",       "3283.89", "0",    memo="GB Payroll"),
    ]


# --- T1 ----------------------------------------------------------------------

def t1_cesar_payroll_rescued_by_grouping():
    rows = _cesar_payroll_rows()
    grouped = group_rows_by_transaction(rows)
    plan = plan_posting_groups(grouped)
    assert plan["would_post_via_grouping"] is True
    # Every reference Cesar listed is rescued.
    assert set(plan["rescued_transaction_ids"]) == {"259730", "259733", "259736"}
    assert len(plan["merged_groups"]) == 1
    g = plan["merged_groups"][0]
    assert g["token"] == "GB"
    # Both sides must equal $43,334.29 — the file total in the email.
    assert g["debits"] == "43334.29", g
    assert g["credits"] == "43334.29", g
    # Nothing stays blocked.
    assert plan["still_blocked"] == []
    print("T1 OK: payroll batch rescued by GB grouping (debits=credits=43334.29)")


# --- T2 ----------------------------------------------------------------------

def t2_mixed_rescue_and_still_blocked():
    """Add an unrelated 1-line GJ row that doesn't balance; it stays blocked."""
    rows = _cesar_payroll_rows() + [
        _row("259999", "5200", "Misc", "75.00", "0", memo="GJ Adjustment"),
    ]
    grouped = group_rows_by_transaction(rows)
    plan = plan_posting_groups(grouped)
    rescued = set(plan["rescued_transaction_ids"])
    assert {"259730", "259733", "259736"} <= rescued
    still = {b["transaction_id"] for b in plan["still_blocked"]}
    assert "259999" in still, plan["still_blocked"]
    # The blocker description is plain English-ish (no internal traceback).
    blocker_text = plan["still_blocked"][0]["reasons"][0].lower()
    assert "fewer than 2 posting lines" in blocker_text or "unbalanced" in blocker_text
    print("T2 OK: GB group rescued; unrelated 1-line GJ row stays blocked")


# --- T3 ----------------------------------------------------------------------

def t3_combined_total_still_unbalanced_stays_blocked():
    """If the combined total *also* doesn't balance, the group is NOT rescued."""
    rows = [
        _row("A1", "1012", "Bank", "100.00", "0", memo="GB X"),
        _row("A2", "1012", "Bank", "0", "50.00", memo="GB X"),
    ]
    grouped = group_rows_by_transaction(rows)
    plan = plan_posting_groups(grouped)
    assert plan["would_post_via_grouping"] is False
    assert plan["merged_groups"] == []
    blocked_ids = {b["transaction_id"] for b in plan["still_blocked"]}
    assert blocked_ids == {"A1", "A2"}, blocked_ids
    print("T3 OK: unbalanced GB group not rescued; both refs stay blocked")


# --- T4 ----------------------------------------------------------------------

def t4_balanced_individual_refs_are_not_merged():
    rows = [
        _row("B1", "1000", "Cash", "100.00", "0", memo="GB X"),
        _row("B1", "5000", "Rent", "0", "100.00", memo="GB X"),
        _row("B2", "1000", "Cash", "200.00", "0", memo="GB X"),
        _row("B2", "5000", "Rent", "0", "200.00", memo="GB X"),
    ]
    grouped = group_rows_by_transaction(rows)
    plan = plan_posting_groups(grouped)
    # Both B1 and B2 are individually balanced — no grouping needed.
    assert plan["merged_groups"] == []
    assert set(plan["balanced_transactions"].keys()) == {"B1", "B2"}
    print("T4 OK: balanced individual refs keep their own PCLaw reference")


# --- T5 ----------------------------------------------------------------------

def t5_cross_token_offsets_are_surfaced_not_merged():
    """CER is short $46.05; GB is long $46.05 — surface the pair, don't merge."""
    rows = [
        _row("C1", "1000", "Cash", "1407.99", "0", memo="CER 9001"),
        _row("C1", "5000", "Acc",  "0", "1454.04", memo="CER 9001"),
        _row("G1", "1000", "Cash", "179205.56", "0", memo="GB X"),
        _row("G1", "5000", "Acc",  "0", "179159.51", memo="GB X"),
    ]
    grouped = group_rows_by_transaction(rows)
    plan = plan_posting_groups(grouped)
    # Each token is individually unbalanced — no rescue.
    assert plan["merged_groups"] == [], plan["merged_groups"]
    # But cross_token_offsets reports the cancelling pair.
    offsets = plan["cross_token_offsets"]
    tokens_seen = {(o["left_token"], o["right_token"]) for o in offsets}
    assert ("CER", "GB") in tokens_seen, offsets
    amts = {o["amount"] for o in offsets}
    assert "46.05" in amts, offsets
    print("T5 OK: CER/GB offsetting pair surfaced; never merged across journals")


# --- T6 ----------------------------------------------------------------------

def t6_plan_balanced_payloads_uses_grouping():
    """The payload planner emits one GROUP- payload per rescued bucket."""
    rows = _cesar_payroll_rows()
    # Add one trivially-balanced reference so we exercise both paths.
    rows += [
        _row("Z1", "1000", "Cash", "10.00", "0", memo="GJ X"),
        _row("Z1", "5000", "Acc",  "0", "10.00", memo="GJ X"),
    ]
    # Fake QBO accounts covering everything by AcctNum.
    accounts = {"QueryResponse": {"Account": [
        {"Id": "100", "AcctNum": "1000", "Name": "Cash", "AccountType": "Bank"},
        {"Id": "101", "AcctNum": "1012", "Name": "NFCU", "AccountType": "Bank"},
        {"Id": "102", "AcctNum": "5000", "Name": "Acc", "AccountType": "Expense"},
        {"Id": "103", "AcctNum": "5110", "Name": "Gross Salaries-Prof", "AccountType": "Expense"},
        {"Id": "104", "AcctNum": "5130", "Name": "Gross Salaries-Supp", "AccountType": "Expense"},
        {"Id": "105", "AcctNum": "5140", "Name": "Salaries Jonathan", "AccountType": "Expense"},
        {"Id": "106", "AcctNum": "5150", "Name": "Health Insurance", "AccountType": "Expense"},
        {"Id": "107", "AcctNum": "5111", "Name": "Payroll Taxes", "AccountType": "Expense"},
    ]}}
    mapping = build_account_mapping_from_numbers(accounts)
    type_index = build_account_type_index(accounts)

    payloads, posted_ids = plan_balanced_payloads(rows, mapping, "number", type_index)

    # One payload for Z1 + one merged GROUP-GB payload = 2 total.
    assert len(payloads) == 2, [p.get("TxnDate") for p in payloads]
    assert len(posted_ids) == 2, posted_ids
    assert "Z1" in posted_ids
    assert any(pid.startswith("GROUP-GB") for pid in posted_ids), posted_ids

    # The GROUP payload itself balances when summed up (QBO won't accept
    # anything else).
    group_payload = next(
        p for p, pid in zip(payloads, posted_ids) if pid.startswith("GROUP-")
    )
    debit_total = sum(
        ln["Amount"] for ln in group_payload["Line"]
        if ln["JournalEntryLineDetail"]["PostingType"] == "Debit"
    )
    credit_total = sum(
        ln["Amount"] for ln in group_payload["Line"]
        if ln["JournalEntryLineDetail"]["PostingType"] == "Credit"
    )
    assert round(debit_total, 2) == round(credit_total, 2) == 43334.29, (
        debit_total, credit_total,
    )
    print("T6 OK: plan_balanced_payloads emits GROUP-* + balanced JEs, refuses unbalanced")


# --- T7 ----------------------------------------------------------------------

def t7_planner_refuses_unbalanced_left_over():
    """If a still_blocked txn survives the grouping pass, the planner raises."""
    rows = [
        _row("U1", "1000", "Cash", "100.00", "0", memo="GB Bad"),
    ]
    accounts = {"QueryResponse": {"Account": [
        {"Id": "100", "AcctNum": "1000", "Name": "Cash", "AccountType": "Bank"},
    ]}}
    mapping = build_account_mapping_from_numbers(accounts)
    type_index = build_account_type_index(accounts)
    try:
        plan_balanced_payloads(rows, mapping, "number", type_index)
    except ValueError as e:
        msg = str(e)
        assert "U1" in msg, msg
        # Plain-English hint: tell user how to fix.
        assert "Fix the CSV" in msg or "source-journal" in msg, msg
        print("T7 OK: planner refuses to post anything when an unbalanced ref survives grouping")
        return
    raise AssertionError("Expected ValueError for unbalanced leftover")


# --- T8 ----------------------------------------------------------------------

def t8_source_journal_token_priority():
    assert source_journal_token({"memo": "GB Payroll"}) == "GB"
    assert source_journal_token({"description": "CER, 12345"}) == "CER"
    assert source_journal_token({"reference": "GJ entry"}) == "GJ"
    # Empty / missing all three -> None
    assert source_journal_token({}) is None
    assert source_journal_token({"memo": " ", "description": "", "reference": None}) is None
    print("T8 OK: source_journal_token reads memo > description > reference")


if __name__ == "__main__":
    t1_cesar_payroll_rescued_by_grouping()
    t2_mixed_rescue_and_still_blocked()
    t3_combined_total_still_unbalanced_stays_blocked()
    t4_balanced_individual_refs_are_not_merged()
    t5_cross_token_offsets_are_surfaced_not_merged()
    t6_plan_balanced_payloads_uses_grouping()
    t7_planner_refuses_unbalanced_left_over()
    t8_source_journal_token_priority()
    print()
    print("ALL gl_grouping SMOKE TESTS PASSED")
