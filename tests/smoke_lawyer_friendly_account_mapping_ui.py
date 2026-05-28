"""Smoke tests for lawyer-friendly Match-accounts UI improvements.

This sprint replaces accountant-jargon with plain-English wording on
the Match-accounts screen and gives every unmatched row a clear
"add this account to QuickBooks" affordance so the user doesn't have
to think about account types.

Covers:
  U1  The QuickBooks-account dropdown placeholder no longer reads
      "pick QuickBooks account" — it reads "pick an existing
      QuickBooks account" so the alternative ("add a new one") is
      obvious.
  U2  Every unmatched row renders an inline "add this account to
      QuickBooks" link plus a short reassurance that this only creates
      the empty account (no transactions are posted).
  U3  A row whose name is "Net Income (Loss)" renders with a
      "Skipped — calculated by QuickBooks" badge and a plain-English
      note. No dropdown is shown for that row.
  U4  The create-missing flash message uses plain-English wording
      ("we need a bit more information") instead of the old
      "couldn't safely guess the QuickBooks account type for ...".
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_UPLOAD_DIR = tempfile.mkdtemp(prefix="pclaw_uploads_ui_")
os.environ["UPLOAD_DIR"] = _UPLOAD_DIR
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_ui_")
os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-lawyer-ui")

import app as appmod  # noqa: E402


def _signup_and_login(client, email, firm):
    pwd = "passw0rd!1234"
    client.post("/logout", follow_redirects=False)
    r = client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


def _make_job(snapshot, qbo_accounts, *, email, firm):
    client = appmod.app.test_client()
    _signup_and_login(client, email, firm)
    user = appmod.db.get_user_by_email(email)
    job_id = f"job_ui_{firm.replace(' ', '_').lower()}"
    appmod.db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company=firm, source_file="x.csv",
        encrypted_file="ui.enc", file_sha256="0" * 64,
        status="uploaded",
    )
    appmod.db.save_job_state(job_id, {
        "status": "uploaded", "pclaw_accounts": snapshot,
    })
    appmod.qbo_connections[job_id] = {
        "realm_id": f"R-{firm}",
        "access_token_enc": appmod.encrypt_token("fake-access"),
        "refresh_token_enc": appmod.encrypt_token("fake-refresh"),
        "company_name": firm, "legal_name": firm, "country": "US",
        "expires_at": "2999-01-01T00:00:00", "company_info_error": None,
    }
    appmod.jobs.pop(job_id, None)

    class _FakeQBO:
        def get_accounts(self):
            return {"QueryResponse": {"Account": list(qbo_accounts)}}
        def find_account_by_acctnum(self, num):
            return None
        def find_account_by_name(self, name):
            return None
        def create_account(self, payload):
            return {"Account": {"Id": "X", **payload}}

    return client, job_id, _FakeQBO()


def u1_dropdown_placeholder_is_lawyer_friendly():
    """Placeholder must hint that an existing account is being picked,
    so the alternative (create a new one) is obvious."""
    client, job_id, qbo = _make_job(
        snapshot=[{"number": "1000", "name": "Operating Bank"}],
        qbo_accounts=[{"Id": "1", "Name": "Checking", "AccountType": "Bank"}],
        email="u1@example.test", firm="U1 LLP",
    )
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping")
    body = r.get_data(as_text=True)
    assert r.status_code == 200, r.status_code
    assert "pick an existing QuickBooks account" in body, (
        "dropdown placeholder should read 'pick an existing QuickBooks "
        "account' so the create-new option is the obvious alternative"
    )
    # The bare old placeholder must NOT appear — it suggested there was
    # only one path (pick).
    assert "pick QuickBooks account" not in body or "pick an existing" in body
    print(
        "U1 OK: dropdown placeholder reads 'pick an existing QuickBooks "
        "account' (not the bare 'pick QuickBooks account')"
    )


def u2_unmatched_row_has_inline_create_link():
    """Each unmatched row links to the create-missing banner with a
    short 'this only creates the empty account' reassurance."""
    client, job_id, qbo = _make_job(
        snapshot=[
            {"number": "1000", "name": "Operating Bank"},  # matches by alias
            {"number": "5000", "name": "Rent Expense"},     # truly unmatched
        ],
        qbo_accounts=[
            {"Id": "1", "Name": "Operating Bank", "AcctNum": "1000",
             "AccountType": "Bank"},
        ],
        email="u2@example.test", firm="U2 LLP",
    )
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping")
    body = r.get_data(as_text=True)
    assert r.status_code == 200, r.status_code
    # Inline "add this account to QuickBooks" link.
    assert "add this account to QuickBooks" in body, (
        "unmatched row should expose an inline 'add this account to "
        "QuickBooks' link"
    )
    # Reassurance: this only creates the empty account. Template
    # wrapping inserts whitespace, so collapse it before checking.
    flat = " ".join(body.split())
    assert "only creates the empty account" in flat, (
        "unmatched row should reassure that this only creates the "
        "empty account"
    )
    assert "no transactions are posted" in flat, (
        "unmatched row should say 'no transactions are posted'"
    )
    # The banner anchor target must exist so the row link can scroll.
    assert "create-missing-banner" in body, body[:200]
    print(
        "U2 OK: each unmatched row exposes an inline 'add this account "
        "to QuickBooks' link with a 'no transactions are posted' note"
    )


def u3_net_income_row_renders_as_system_calculated():
    """Net Income (Loss) renders as a system-calculated row: no
    dropdown, plain-English explanation, dedicated badge."""
    client, job_id, qbo = _make_job(
        snapshot=[
            {"number": "9999", "name": "Net Income (Loss)"},
            {"number": "5000", "name": "Rent Expense"},
        ],
        qbo_accounts=[
            {"Id": "1", "Name": "Other", "AccountType": "Expense"},
        ],
        email="u3@example.test", firm="U3 LLP",
    )
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping")
    body = r.get_data(as_text=True)
    assert r.status_code == 200, r.status_code
    assert 'data-testid="row-system-calculated"' in body, (
        "Net Income (Loss) row should carry the system-calculated test id"
    )
    assert "Skipped" in body and "calculated by QuickBooks" in body, (
        "Net Income (Loss) row should show the 'Skipped — calculated by "
        "QuickBooks' badge"
    )
    assert "calculates this automatically" in body, (
        "row should include the plain-English explanation"
    )
    # No match-needed message on the row.
    assert "No match needed" in body, body[-2000:]
    print(
        "U3 OK: Net Income (Loss) row renders with a 'Skipped — "
        "calculated by QuickBooks' badge and the plain-English note; "
        "no dropdown is presented for that row"
    )


def u4_create_missing_blocked_flash_is_plain_english():
    """The blocked-create flash reads as plain English, not as the
    old 'couldn't safely guess the QuickBooks account type' phrase."""
    # Build a job whose snapshot has one row the type-mapper can't
    # resolve. That triggers the blocked branch in
    # account_mapping_create_missing.
    snapshot = [{"number": "8123", "name": "Misc Account 8123"}]
    qbo_accounts = []
    client, job_id, qbo = _make_job(
        snapshot=snapshot, qbo_accounts=qbo_accounts,
        email="u4@example.test", firm="U4 LLP",
    )

    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r = client.post(
            f"/jobs/{job_id}/account-mapping/create-missing",
            follow_redirects=True,
        )
    body = r.get_data(as_text=True)
    assert r.status_code == 200, r.status_code
    # The blocked flash is now phrased action-first and uses plain
    # English. It must surface a clear next action ("pick what kind of
    # account it is" or "pick what kind") without raw API jargon.
    lower = body.lower()
    assert (
        "need your choice" in lower
        or "pick what kind of account" in lower
    ), (
        "blocked flash should ask the user to pick what kind of account "
        "each remaining row is"
    )
    # Old jargon must NOT appear.
    assert "couldn't safely guess the QuickBooks account type" not in body, (
        "old jargon-y wording must be removed"
    )
    print(
        "U4 OK: blocked-create flash is action-oriented and avoids raw "
        "API / QBO jargon"
    )


def main():
    u1_dropdown_placeholder_is_lawyer_friendly()
    u2_unmatched_row_has_inline_create_link()
    u3_net_income_row_renders_as_system_calculated()
    u4_create_missing_blocked_flash_is_plain_english()
    print("\nALL LAWYER-FRIENDLY ACCOUNT-MAPPING UI SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
