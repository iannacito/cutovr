"""Smoke tests for the product-hardening reliability pass.

Run from project root::

    python3 tests/smoke_product_hardening.py

Covers the deterministic, framework-light pieces of the hardening work so
the safety guarantees are pinned by tests without needing a live QBO:

  G1  preflight.evaluate_import_gate passes a clean balanced ledger.
  G2  evaluate_import_gate blocks an unbalanced ledger with plain-English
      guidance (no row contents leaked).
  G3  evaluate_import_gate blocks missing-date / missing-account rows.
  C1  job_checkpoints.advance never moves a job backwards.
  C2  job_checkpoints: any stage can drop to needs_attention and recover.
  C3  job_checkpoints.resume_step maps each checkpoint to a workflow step.
  D1  pclaw_pipeline.idempotency_doc_number is stable and <= 21 chars.
  D2  build_journal_entry_payload stamps a deterministic DocNumber.
  Q1  QBOClient.find_journal_entry_by_doc_number returns the match.
  Q2  find_journal_entry_by_doc_number returns None when absent.
  R1  data_retention honors UPLOAD_RETENTION_DAYS (preferred over RETENTION_DAYS).
  F1  final_report marks validation passed when no GL job needs attention.
  F2  final_report counts open validation items and renders them in text.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import preflight  # noqa: E402
import job_checkpoints as jc  # noqa: E402
import pclaw_pipeline as pp  # noqa: E402
import qbo_client as qc  # noqa: E402


# ---- helpers ---------------------------------------------------------------

def _gl_row(txn, acct, name, debit="0", credit="0", date="2026-01-01"):
    return {
        "transaction_id": txn,
        "date": date,
        "account_number": acct,
        "account_name": name,
        "debit": debit,
        "credit": credit,
        "description": "test",
    }


def _balanced_rows():
    return [
        _gl_row("JE-1", "1000", "Cash", debit="100.00"),
        _gl_row("JE-1", "4000", "Revenue", credit="100.00"),
    ]


# ---- G: validation gate ----------------------------------------------------

def g1_gate_passes_clean_ledger():
    ok, blockers = preflight.evaluate_import_gate(_balanced_rows())
    assert ok is True, blockers
    assert blockers == []
    print("G1 OK: balanced ledger passes the import gate")


def g2_gate_blocks_unbalanced():
    rows = [
        _gl_row("JE-1", "1000", "Cash", debit="100.00"),
        _gl_row("JE-1", "4000", "Revenue", credit="90.00"),
    ]
    ok, blockers = preflight.evaluate_import_gate(rows)
    assert ok is False
    assert any("don't match" in b["headline"] for b in blockers), blockers
    # No row contents / amounts leaked into the customer-facing text.
    joined = " ".join(b["headline"] + b["action"] for b in blockers)
    assert "100.00" not in joined and "90.00" not in joined
    print("G2 OK: unbalanced ledger blocked with plain-English guidance")


def g3_gate_blocks_missing_date_and_account():
    rows = [
        _gl_row("JE-1", "", "", debit="50.00", date=""),
        _gl_row("JE-1", "4000", "Revenue", credit="50.00", date=""),
    ]
    ok, blockers = preflight.evaluate_import_gate(rows)
    assert ok is False
    heads = " ".join(b["headline"] for b in blockers)
    assert "account" in heads and "date" in heads, blockers
    print("G3 OK: missing-date / missing-account rows blocked")


# ---- C: checkpoints --------------------------------------------------------

def c1_advance_never_goes_backwards():
    assert jc.advance(jc.COMPLETED, jc.PARSED) == jc.COMPLETED
    assert jc.advance(jc.MATCHED, jc.REVIEWED) == jc.REVIEWED
    assert jc.advance(None, jc.UPLOADED) == jc.UPLOADED
    print("C1 OK: advance never moves a job backwards")


def c2_needs_attention_is_a_side_state():
    # Any stage can drop into needs_attention.
    assert jc.advance(jc.IMPORTING, jc.NEEDS_ATTENTION) == jc.NEEDS_ATTENTION
    # And recover forward once the blocker clears.
    assert jc.advance(jc.NEEDS_ATTENTION, jc.IMPORTING) == jc.IMPORTING
    print("C2 OK: needs_attention drops in and recovers")


def c3_resume_step_mapping():
    assert jc.resume_step(jc.PARSED) == 3
    assert jc.resume_step(jc.COMPLETED) == 6
    assert jc.resume_step(jc.NEEDS_ATTENTION) == 4
    print("C3 OK: resume_step maps checkpoints to workflow steps")


# ---- D: deterministic DocNumber -------------------------------------------

def d1_doc_number_stable_and_bounded():
    a = pp.idempotency_doc_number("JE-0003")
    b = pp.idempotency_doc_number("JE-0003")
    c = pp.idempotency_doc_number("GROUP-GB-2026-01")
    assert a == b, "doc number must be deterministic"
    assert a != c, "distinct references must not collide"
    for v in (a, c, pp.idempotency_doc_number("x" * 100)):
        assert len(v) <= 21, f"DocNumber too long: {v}"
    print("D1 OK: idempotency_doc_number stable, distinct, <= 21 chars")


def d2_payload_stamps_doc_number():
    rows = _balanced_rows()
    mapping = {"1000": "11", "4000": "44"}
    payload = pp.build_journal_entry_payload(
        "JE-1", rows, mapping, mapping_mode="number"
    )
    assert payload["DocNumber"] == pp.idempotency_doc_number("JE-1")
    print("D2 OK: build_journal_entry_payload stamps deterministic DocNumber")


# ---- Q: QBO idempotency probe ---------------------------------------------

def _q_resp(items):
    r = MagicMock()
    r.status_code = 200
    r.headers = {"intuit_tid": "tid"}
    r.json.return_value = {"QueryResponse": {"JournalEntry": items}}
    return r


def q1_find_je_returns_match():
    client = qc.QBOClient(access_token="t", realm_id="r", environment="sandbox")
    hit = {"Id": "42", "DocNumber": "JE1-ABCD1234", "TxnDate": "2026-01-01"}
    with patch("qbo_client.requests.get", return_value=_q_resp([hit])):
        out = client.find_journal_entry_by_doc_number("JE1-ABCD1234")
    assert out and out["Id"] == "42"
    print("Q1 OK: find_journal_entry_by_doc_number returns the match")


def q2_find_je_returns_none_when_absent():
    client = qc.QBOClient(access_token="t", realm_id="r", environment="sandbox")
    with patch("qbo_client.requests.get", return_value=_q_resp([])):
        out = client.find_journal_entry_by_doc_number("NOPE-00000000")
    assert out is None
    assert client.find_journal_entry_by_doc_number("") is None
    print("Q2 OK: find_journal_entry_by_doc_number returns None when absent")


# ---- R: retention env alias ------------------------------------------------

def r1_upload_retention_days_alias():
    import importlib
    import data_retention as dr

    saved = dict(os.environ)
    try:
        os.environ.pop("RETENTION_DAYS", None)
        os.environ["UPLOAD_RETENTION_DAYS"] = "30"
        importlib.reload(dr)
        assert dr._retention_days() == 30
        # UPLOAD_RETENTION_DAYS wins over RETENTION_DAYS when both present.
        os.environ["RETENTION_DAYS"] = "5"
        assert dr._retention_days() == 30
    finally:
        os.environ.clear()
        os.environ.update(saved)
        importlib.reload(dr)
    print("R1 OK: UPLOAD_RETENTION_DAYS honored (and preferred)")


# ---- F: final report validation summary -----------------------------------

def _final_report_summary(jobs):
    import final_report as fr
    return fr.build_reconciliation_summary(
        firm_name="Acme Law",
        cutover={"cutover_date": "2026-01-01"},
        jobs=jobs,
        qbo_connections=[{"company_name": "Acme QBO", "realm_id": "r1"}],
        account_mapping_count=3,
    )


def f1_validation_passed_when_clean():
    jobs = [{
        "report_type": "general_ledger",
        "checkpoint": "completed",
        "import_summary": {"qbo_je_count": 2, "source_transaction_count": 2, "balanced": True},
    }]
    summary = _final_report_summary(jobs)
    assert summary.validation_passed is True
    assert summary.validation_open_items == 0
    import final_report as fr
    assert "Pre-import checks: all passed" in fr.build_report_text(summary)
    print("F1 OK: final report marks validation passed when clean")


def f2_validation_open_items_counted():
    jobs = [{
        "report_type": "general_ledger",
        "checkpoint": "needs_attention",
        "import_gate_blockers": [{"headline": "x", "action": "y"}],
    }]
    summary = _final_report_summary(jobs)
    assert summary.validation_passed is False
    assert summary.validation_open_items == 1
    import final_report as fr
    assert "still need a look" in fr.build_report_text(summary)
    print("F2 OK: final report counts open validation items")


def main():
    g1_gate_passes_clean_ledger()
    g2_gate_blocks_unbalanced()
    g3_gate_blocks_missing_date_and_account()
    c1_advance_never_goes_backwards()
    c2_needs_attention_is_a_side_state()
    c3_resume_step_mapping()
    d1_doc_number_stable_and_bounded()
    d2_payload_stamps_doc_number()
    q1_find_je_returns_match()
    q2_find_je_returns_none_when_absent()
    r1_upload_retention_days_alias()
    f1_validation_passed_when_clean()
    f2_validation_open_items_counted()
    print("\nALL PRODUCT-HARDENING SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
