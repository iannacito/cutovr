"""Smoke tests for Intuit transaction id (`intuit_tid`) capture.

Run from project root:

    python3 tests/smoke_intuit_tid.py

Covers:
  T1 QBOClient.create_journal_entry pulls intuit_tid from a successful
     response's headers and stores it on `last_intuit_tid`. Failure case
     also captures it and surfaces it on the raised QBOError.
  T2 QBOClient.query / get_company_info / get_accounts capture tid on
     success.
  T3 QBOAuthHandler.get_bearer_token returns the tid in its dict and
     stashes it on `last_intuit_tid`. Failure raises QBOAuthError with
     the tid attached, and the message does NOT include the upstream
     response body (which can echo client identifiers).
  T4 qbo_error_hint.parse() round-trips intuit_tid into the rendered
     error dict so the job-detail UI can display it.
  T5 No client_secret / access_token / refresh_token / authorization
     code substring leaks into our QBOError / QBOAuthError messages or
     str() representations.

Pure unit-style mocks; no network and no Flask app boot.
"""

import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# These imports do NOT require the Flask app, so we can skip the env setup
# the larger smoke tests need.
from qbo_client import QBOClient, QBOError, extract_intuit_tid  # noqa: E402
from qbo_auth import QBOAuthHandler, QBOAuthError  # noqa: E402
import qbo_error_hint  # noqa: E402


SECRETS = {
    "client_secret": "CS-zzz-must-not-leak",
    "access_token": "AT-zzz-must-not-leak",
    "refresh_token": "RT-zzz-must-not-leak",
    "auth_code": "CODE-zzz-must-not-leak",
}


class FakeResponse:
    """Minimal stand-in for requests.Response used by the QBO SDK code."""

    def __init__(self, status_code=200, json_body=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text or ""
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}", response=self)


def _assert_no_secret_leaks(blob):
    """Fail the test if any of the marker secrets appears in `blob`."""
    text = str(blob)
    for label, marker in SECRETS.items():
        assert marker not in text, f"{label} leaked into: {text[:300]}"


def t1_create_journal_entry_captures_tid_success_and_failure():
    qbo = QBOClient(access_token=SECRETS["access_token"], realm_id="R1", environment="sandbox")

    # --- success path ---
    ok_headers = {"intuit_tid": "1-abc-tid-success"}
    ok_resp = FakeResponse(
        status_code=200,
        json_body={"JournalEntry": {"Id": "42", "DocNumber": "D1", "TxnDate": "2026-05-06"}},
        headers=ok_headers,
    )
    with mock.patch("qbo_client.requests.post", return_value=ok_resp):
        result = qbo.create_journal_entry({"Line": []})
    assert result["JournalEntry"]["Id"] == "42"
    assert qbo.last_intuit_tid == "1-abc-tid-success", qbo.last_intuit_tid

    # --- failure path ---
    err_headers = {"Intuit-TID": "1-abc-tid-failure"}
    err_resp = FakeResponse(
        status_code=400,
        text='{"Fault":{"Error":[{"Message":"Account is inactive"}]}}',
        headers=err_headers,
    )
    with mock.patch("qbo_client.requests.post", return_value=err_resp):
        try:
            qbo.create_journal_entry({"Line": []})
        except QBOError as e:
            assert e.status_code == 400
            assert e.intuit_tid == "1-abc-tid-failure", e.intuit_tid
            assert qbo.last_intuit_tid == "1-abc-tid-failure"
            _assert_no_secret_leaks(e)
        else:
            raise AssertionError("expected QBOError")
    print("T1 OK: create_journal_entry captures intuit_tid on success and failure")


def t2_query_and_company_info_capture_tid():
    qbo = QBOClient(access_token=SECRETS["access_token"], realm_id="R1", environment="sandbox")

    q_resp = FakeResponse(
        status_code=200,
        json_body={"QueryResponse": {"Account": []}},
        headers={"intuit_tid": "1-q-tid"},
    )
    with mock.patch("qbo_client.requests.get", return_value=q_resp):
        qbo.query("SELECT Id FROM Account")
    assert qbo.last_intuit_tid == "1-q-tid"

    ci_resp = FakeResponse(
        status_code=200,
        json_body={"CompanyInfo": {"CompanyName": "Sandbox", "Country": "US"}},
        headers={"intuit_tid": "1-ci-tid"},
    )
    with mock.patch("qbo_client.requests.get", return_value=ci_resp):
        qbo.get_company_info()
    assert qbo.last_intuit_tid == "1-ci-tid"

    # get_accounts goes through query() — same headers should win.
    accts_resp = FakeResponse(
        status_code=200,
        json_body={"QueryResponse": {"Account": []}},
        headers={"intuit_tid": "1-accts-tid"},
    )
    with mock.patch("qbo_client.requests.get", return_value=accts_resp):
        qbo.get_accounts()
    assert qbo.last_intuit_tid == "1-accts-tid"

    # No header → tid stays None (extracted, not carried over).
    plain_resp = FakeResponse(
        status_code=200,
        json_body={"QueryResponse": {"Account": []}},
        headers={},
    )
    with mock.patch("qbo_client.requests.get", return_value=plain_resp):
        qbo.query("SELECT Id FROM Account")
    assert qbo.last_intuit_tid is None
    print("T2 OK: query / company_info / get_accounts capture intuit_tid")


def t3_token_exchange_and_refresh_capture_tid():
    auth = QBOAuthHandler(
        client_id="CLIENT-ID", client_secret=SECRETS["client_secret"],
        redirect_uri="https://example.test/cb", environment="sandbox",
    )

    # --- success ---
    ok_resp = FakeResponse(
        status_code=200,
        json_body={
            "access_token": SECRETS["access_token"],
            "refresh_token": SECRETS["refresh_token"],
            "expires_in": 3600,
            "token_type": "bearer",
        },
        headers={"intuit_tid": "1-token-ok"},
    )
    with mock.patch("qbo_auth.requests.post", return_value=ok_resp):
        out = auth.get_bearer_token(SECRETS["auth_code"])
    assert out["intuit_tid"] == "1-token-ok"
    assert auth.last_intuit_tid == "1-token-ok"
    # The auth code we passed in is not echoed back into out.
    assert SECRETS["auth_code"] not in str(out.get("token_type", ""))

    # --- refresh success ---
    refresh_resp = FakeResponse(
        status_code=200,
        json_body={
            "access_token": "AT2", "refresh_token": "RT2",
            "expires_in": 3600, "token_type": "bearer",
        },
        headers={"intuit_tid": "1-token-refresh"},
    )
    with mock.patch("qbo_auth.requests.post", return_value=refresh_resp):
        out2 = auth.refresh_access_token(SECRETS["refresh_token"])
    assert out2["intuit_tid"] == "1-token-refresh"
    assert auth.last_intuit_tid == "1-token-refresh"

    # --- failure: tid captured, body NOT in message ---
    leaky_body = (
        '{"error":"invalid_grant","error_description":"client_secret '
        + SECRETS["client_secret"] + ' is wrong"}'
    )
    err_resp = FakeResponse(
        status_code=400,
        text=leaky_body,
        headers={"intuit_tid": "1-token-fail"},
    )
    with mock.patch("qbo_auth.requests.post", return_value=err_resp):
        try:
            auth.get_bearer_token(SECRETS["auth_code"])
        except QBOAuthError as e:
            assert e.status_code == 400
            assert e.intuit_tid == "1-token-fail"
            assert auth.last_intuit_tid == "1-token-fail"
            # The error message must NOT echo the client_secret / response body.
            _assert_no_secret_leaks(e)
            _assert_no_secret_leaks(str(e))
        else:
            raise AssertionError("expected QBOAuthError on 400")

    print("T3 OK: token exchange + refresh capture tid; failure does not leak body")


def t4_error_hint_parse_round_trips_tid():
    raw = "QBO returned 400: " + (
        '{"Fault":{"Error":[{"Message":"Account is inactive","code":"6000"}]}}'
    )
    out = qbo_error_hint.parse(raw, intuit_tid="1-hint-tid")
    assert out["intuit_tid"] == "1-hint-tid"
    assert out["status_code"] == 400
    assert "inactive" in out["summary"].lower(), out["summary"]
    # Original behaviour preserved when no tid is supplied.
    out2 = qbo_error_hint.parse(raw)
    assert out2["intuit_tid"] is None
    assert out2["status_code"] == 400
    print("T4 OK: qbo_error_hint.parse round-trips intuit_tid")


def t5_no_secret_leaks_in_error_paths():
    # extract_intuit_tid returns None for missing/empty headers.
    assert extract_intuit_tid(None) is None
    assert extract_intuit_tid(FakeResponse(headers={})) is None
    assert extract_intuit_tid(FakeResponse(headers={"intuit_tid": "  "})) is None

    # QBOError str() never carries a token.
    e = QBOError("QBO returned 401: token expired", status_code=401, body="token expired",
                 intuit_tid="1-x")
    _assert_no_secret_leaks(e)
    _assert_no_secret_leaks(repr(e))
    assert e.intuit_tid == "1-x"

    # QBOAuthError carries the tid but not arbitrary upstream body content.
    ae = QBOAuthError("Intuit token endpoint returned 400", status_code=400, intuit_tid="1-y")
    _assert_no_secret_leaks(ae)
    _assert_no_secret_leaks(str(ae))
    assert "Intuit token endpoint returned 400" in str(ae)
    assert ae.intuit_tid == "1-y"
    print("T5 OK: error paths do not leak secrets; tids are preserved")


if __name__ == "__main__":
    t1_create_journal_entry_captures_tid_success_and_failure()
    t2_query_and_company_info_capture_tid()
    t3_token_exchange_and_refresh_capture_tid()
    t4_error_hint_parse_round_trips_tid()
    t5_no_secret_leaks_in_error_paths()
    print("ALL INTUIT_TID SMOKE TESTS PASSED")
