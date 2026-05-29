"""Smoke tests for QBO transient-failure retry posture.

Run from project root:

    python3 tests/smoke_qbo_retry_backoff.py

Covers:
  T1  create_journal_entry retries on 503 then succeeds.
  T2  create_journal_entry retries on 429 honoring Retry-After (capped).
  T3  create_journal_entry does NOT retry on 400 (caller bug, not transient).
  T4  create_journal_entry raises QBOError after exhausting retries on 5xx.
  T5  qbo_auth._post_token retries on 503 then succeeds — a transient
      Intuit blip should not force a full re-OAuth.
  T6  qbo_auth._post_token does NOT retry on 400 (invalid_grant) — that's
      a real "please reconnect" signal.
  T7  import_history.has_completed_transactions treats partial imports
      as already-imported (duplicate guard covers mid-batch failures).
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Make all sleeps no-ops so tests run fast.
import qbo_client as qc  # noqa: E402
import qbo_auth as qa  # noqa: E402

qc._sleep = lambda s: None  # type: ignore[assignment]

import time as _time  # noqa: E402
_orig_sleep = _time.sleep
_time.sleep = lambda s: None  # type: ignore[assignment]


def _resp(status=200, body=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.text = body or "{}"
    r.json.return_value = {"JournalEntry": {"Id": "1", "DocNumber": "JE-1", "TxnDate": "2026-01-01"}}
    r.headers = headers or {"intuit_tid": "abc-123"}
    return r


def t1_create_je_retries_503_then_succeeds():
    client = qc.QBOClient(access_token="t", realm_id="r", environment="sandbox")
    responses = [_resp(503), _resp(503), _resp(200)]
    with patch("qbo_client.requests.post", side_effect=responses) as mock_post:
        out = client.create_journal_entry({})
    assert mock_post.call_count == 3
    assert out["JournalEntry"]["Id"] == "1"
    print("T1 OK: create_journal_entry retries 503 and recovers")


def t2_create_je_retries_429_honoring_retry_after():
    client = qc.QBOClient(access_token="t", realm_id="r", environment="sandbox")
    responses = [
        _resp(429, headers={"intuit_tid": "tid-1", "Retry-After": "2"}),
        _resp(200),
    ]
    with patch("qbo_client.requests.post", side_effect=responses) as mock_post:
        out = client.create_journal_entry({})
    assert mock_post.call_count == 2
    assert out["JournalEntry"]["Id"] == "1"
    print("T2 OK: create_journal_entry retries 429 with Retry-After")


def t3_create_je_does_not_retry_400():
    client = qc.QBOClient(access_token="t", realm_id="r", environment="sandbox")
    responses = [_resp(400, body='{"Fault":{"Error":[{"Message":"Bad request"}]}}')]
    with patch("qbo_client.requests.post", side_effect=responses) as mock_post:
        try:
            client.create_journal_entry({})
            raise AssertionError("expected QBOError on 400")
        except qc.QBOError as e:
            assert e.status_code == 400
    assert mock_post.call_count == 1
    print("T3 OK: create_journal_entry does NOT retry 400 (fails fast)")


def t4_create_je_exhausts_retries():
    client = qc.QBOClient(access_token="t", realm_id="r", environment="sandbox")
    responses = [_resp(503), _resp(503), _resp(503), _resp(503)]
    with patch("qbo_client.requests.post", side_effect=responses) as mock_post:
        try:
            client.create_journal_entry({})
            raise AssertionError("expected QBOError after exhausted retries")
        except qc.QBOError as e:
            assert e.status_code == 503
    # 1 initial + 3 retries = 4 total
    assert mock_post.call_count == 4
    print("T4 OK: create_journal_entry surfaces final 503 after retries exhausted")


def _token_resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.text = body or "{}"
    r.json.return_value = {
        "access_token": "atk",
        "refresh_token": "rtk",
        "expires_in": 3600,
        "token_type": "bearer",
    }
    r.headers = {"intuit_tid": "tid-token"}
    return r


def t5_token_refresh_retries_503_then_succeeds():
    handler = qa.QBOAuthHandler(client_id="c", client_secret="s",
                                redirect_uri="http://localhost",
                                environment="sandbox")
    responses = [_token_resp(503), _token_resp(200)]
    with patch("qbo_auth.requests.post", side_effect=responses) as mock_post:
        out = handler.refresh_access_token("old-refresh")
    assert mock_post.call_count == 2
    assert out["access_token"] == "atk"
    print("T5 OK: token refresh retries 503 and recovers without forcing re-OAuth")


def t6_token_refresh_fails_fast_on_400():
    handler = qa.QBOAuthHandler(client_id="c", client_secret="s",
                                redirect_uri="http://localhost",
                                environment="sandbox")
    responses = [_token_resp(400, '{"error":"invalid_grant"}')]
    with patch("qbo_auth.requests.post", side_effect=responses) as mock_post:
        try:
            handler.refresh_access_token("expired-refresh")
            raise AssertionError("expected QBOAuthError on 400")
        except qa.QBOAuthError as e:
            assert e.status_code == 400
    assert mock_post.call_count == 1
    print("T6 OK: token refresh fails fast on 400 (invalid_grant)")


def t7_history_partial_status_blocks_duplicates():
    # Standalone test: use a temp ImportHistory DB.
    from import_history import ImportHistory
    db_path = tempfile.mktemp(suffix=".sqlite3")
    try:
        history = ImportHistory(db_path)
        # Partial import recorded mid-batch failure.
        history.record_import(
            job_id="job-a",
            realm_id="realm-1",
            file_sha256="hash-a",
            company_name="Test Co",
            transaction_count=2,
            debit_total="100.00",
            credit_total="100.00",
            status="partial",
            created_transactions=[
                {"transaction_id": "TX-1", "qbo_je_id": "JE-1",
                 "doc_number": "1", "txn_date": "2026-01-01"},
                {"transaction_id": "TX-2", "qbo_je_id": "JE-2",
                 "doc_number": "2", "txn_date": "2026-01-01"},
            ],
        )
        already = history.has_completed_transactions(
            ["TX-1", "TX-2", "TX-3"], "realm-1",
        )
        assert already == {"TX-1", "TX-2"}, \
            f"partial imports must show up in dup-guard, got {already}"
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass
    print("T7 OK: partial imports block duplicate transaction_ids on retry")


if __name__ == "__main__":
    try:
        t1_create_je_retries_503_then_succeeds()
        t2_create_je_retries_429_honoring_retry_after()
        t3_create_je_does_not_retry_400()
        t4_create_je_exhausts_retries()
        t5_token_refresh_retries_503_then_succeeds()
        t6_token_refresh_fails_fast_on_400()
        t7_history_partial_status_blocks_duplicates()
        print("\nALL QBO RETRY/BACKOFF SMOKE TESTS PASSED")
    finally:
        _time.sleep = _orig_sleep
