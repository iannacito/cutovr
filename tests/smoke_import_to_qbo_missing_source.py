"""Smoke tests for /jobs/<id>/import-to-qbo recovery when the source upload is gone.

Background
----------
After PR #37 fixed the Match-accounts step against ephemeral-storage loss
on Render, the very next step in the demo — clicking "Send to QuickBooks" —
still 500'd for the same reason. The import route called
``decrypt_file(UPLOAD_DIR / job["encrypted_file"], ...)`` unconditionally,
so a redeploy that wiped uploads/ wedged the job with a raw Internal
Server Error.

This sprint:

  1. Persists the parsed GL rows snapshot to a new ``jobs.gl_rows_json``
     column at upload time, so the importer can run without the raw CSV.
  2. Adds a friendly in-place recovery page when neither the snapshot nor
     the encrypted CSV is on file (legacy jobs from before this fix). In
     DEMO_MODE the primary CTA is "Start a fresh demo run"; in production
     it's "Re-upload PCLaw export".
  3. Adds a route-level safety net so any unexpected exception in the
     import path renders the recovery page rather than 500-ing.

Covers
------
  I1  ``_gl_rows_for_snapshot`` returns plain JSON-safe dicts.
  I2  ``AppDB.save_job_state`` + ``hydrate_job`` round-trip the
      ``gl_rows`` snapshot via ``jobs.gl_rows_json``.
  I3  Snapshot present + encrypted CSV missing: import proceeds via the
      mocked QBO and the success status is set.
  I4  Snapshot missing + encrypted CSV missing: route renders the
      ``import-recovery-error`` card (HTTP 200) with the production
      re-upload CTA — no 500, no raw traceback.
  I5  Demo mode: the recovery card primary CTA is "Start a fresh demo
      run", with the re-upload CTA demoted.
  I6  Snapshot missing but encrypted CSV present: route reparses the CSV
      and backfills the snapshot, so the NEXT redeploy that loses the
      file still succeeds.
  I7  Route-level safety net: an unexpected exception inside the
      importer surfaces the recovery card instead of a raw 500.
  I8  Happy path still works end-to-end when both snapshot AND file are
      present (regression guard).

Run from project root:

    python3 tests/smoke_import_to_qbo_missing_source.py
"""

import io
import os
import sys
import tempfile
import unittest.mock as mock
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_UPLOAD_DIR = tempfile.mkdtemp(prefix="pclaw_imp_uploads_")
_OUTPUT_DIR = tempfile.mkdtemp(prefix="pclaw_imp_outputs_")
os.environ["UPLOAD_DIR"] = _UPLOAD_DIR
os.environ["OUTPUT_DIR"] = _OUTPUT_DIR

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-import-missing-source")

import app as appmod  # noqa: E402
from app_db import AppDB  # noqa: E402
from encryption import encrypt_file  # noqa: E402


GL_CSV_TEXT = (
    "transaction_id,account_number,account_name,date,debit,credit,description\n"
    "T1,1000,Cash,2024-01-01,100.00,0.00,opening\n"
    "T1,4000,Fees,2024-01-01,0.00,100.00,opening\n"
    "T2,1000,Cash,2024-01-02,50.00,0.00,second\n"
    "T2,4000,Fees,2024-01-02,0.00,50.00,second\n"
)

FAKE_ACCOUNTS = {
    "QueryResponse": {
        "Account": [
            {"Id": "10", "Name": "Cash", "AcctNum": "1000",
             "AccountType": "Bank", "Active": True},
            {"Id": "40", "Name": "Fees", "AcctNum": "4000",
             "AccountType": "Income", "Active": True},
        ]
    }
}

GL_SNAPSHOT = [
    {"transaction_id": "T1", "account_number": "1000", "account_name": "Cash",
     "date": "2024-01-01", "debit": "100.00", "credit": "0.00",
     "description": "opening"},
    {"transaction_id": "T1", "account_number": "4000", "account_name": "Fees",
     "date": "2024-01-01", "debit": "0.00", "credit": "100.00",
     "description": "opening"},
    {"transaction_id": "T2", "account_number": "1000", "account_name": "Cash",
     "date": "2024-01-02", "debit": "50.00", "credit": "0.00",
     "description": "second"},
    {"transaction_id": "T2", "account_number": "4000", "account_name": "Fees",
     "date": "2024-01-02", "debit": "0.00", "credit": "50.00",
     "description": "second"},
]


class _FakeQBO:
    """Stand-in for QBOClient. Posts return synthetic JournalEntry IDs."""

    def __init__(self):
        self.posted = []

    def get_accounts(self):
        return FAKE_ACCOUNTS

    def create_journal_entry(self, payload):
        n = len(self.posted) + 1
        je = {
            "Id": str(900 + n),
            "DocNumber": f"D{n}",
            "TxnDate": payload["TxnDate"],
            "Line": list(payload["Line"]),
        }
        self.posted.append((je["Id"], payload))
        return {"JournalEntry": je}

    def query(self, sql):
        return {"QueryResponse": {}}


def _signup_and_login(client, email, firm):
    pwd = "passw0rd!1234"
    r = client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


def _make_job_with_qbo(
    client, email, firm, *,
    encrypted_file_name,
    seed_snapshot=False,
):
    """Set up a logged-in user + a job row + a fake QBO connection."""
    _signup_and_login(client, email, firm)
    db = appmod.db
    user = db.get_user_by_email(email)
    job_id = f"job_{firm.replace(' ', '_').lower()}"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company=firm, source_file="x.csv",
        encrypted_file=encrypted_file_name, file_sha256="sha_" + firm,
        status="Ready for import",
    )
    state = {"status": "Ready for import",
             "report_type": "general_ledger",
             "qbo_connected": True}
    if seed_snapshot:
        state["gl_rows"] = GL_SNAPSHOT
        state["pclaw_accounts"] = [
            {"number": "1000", "name": "Cash"},
            {"number": "4000", "name": "Fees"},
        ]
    db.save_job_state(job_id, state)
    appmod.qbo_connections[job_id] = {
        "realm_id": f"R-{firm[:4].upper()}",
        "access_token_enc": appmod.encrypt_token("fake-access"),
        "refresh_token_enc": appmod.encrypt_token("fake-refresh"),
        "company_name": firm,
        "legal_name": firm,
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }
    appmod.jobs.pop(job_id, None)  # force DB rehydrate on first request
    return job_id, user


def i1_gl_rows_for_snapshot():
    fn = appmod._gl_rows_for_snapshot
    rows = [
        {"transaction_id": "T1", "account_number": "1000",
         "account_name": "Cash", "date": "2024-01-01",
         "debit": "100.00", "credit": "0.00", "description": ""},
        {None: ["stray"], "transaction_id": "T2", "account_number": None,
         "account_name": "Trust", "date": "2024-01-02",
         "debit": 50, "credit": 0, "description": None},
    ]
    out = fn(rows)
    assert len(out) == 2
    assert out[0]["debit"] == "100.00"
    # None values coerce to empty string for JSON friendliness.
    assert out[1]["account_number"] == ""
    assert out[1]["description"] == ""
    # Numeric input is stringified so JSON round-trip is stable.
    assert out[1]["debit"] == "50"
    # Synthetic None-key dropped.
    assert None not in out[1]
    # Empty / falsy inputs yield empty list, never raise.
    assert fn([]) == []
    assert fn(None) == []
    print("I1 OK: _gl_rows_for_snapshot produces JSON-safe dicts")


def i2_save_and_hydrate_gl_rows():
    db_path = tempfile.mktemp(suffix=".sqlite3")
    db = AppDB(db_path)
    db.create_firm_and_admin("F", "i2@example.test", "passw0rd!1234")
    user = db.get_user_by_email("i2@example.test")
    job_id = "job_i2_gl"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company="I2 Co", source_file="x.csv",
        encrypted_file="missing.enc", file_sha256="0" * 64,
        status="uploaded",
    )
    db.save_job_state(job_id, {"status": "uploaded", "gl_rows": GL_SNAPSHOT})

    hydrated = db.hydrate_job(job_id)
    assert hydrated is not None
    assert hydrated.get("gl_rows") == GL_SNAPSHOT, hydrated.get("gl_rows")

    # Clearing the snapshot writes NULL and hydrates back as absent / None.
    db.save_job_state(job_id, {"status": "uploaded", "gl_rows": None})
    hydrated = db.hydrate_job(job_id)
    assert not hydrated.get("gl_rows")
    print("I2 OK: AppDB persists + hydrates gl_rows snapshot")


def i3_import_uses_snapshot_when_file_missing():
    appmod.QBO_REAL_IMPORT = True
    client = appmod.app.test_client()
    job_id, user = _make_job_with_qbo(
        client, "i3@example.test", "I3 LLP",
        encrypted_file_name="i3_does_not_exist.enc",
        seed_snapshot=True,
    )
    fake_qbo = _FakeQBO()
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(fake_qbo, appmod.qbo_connections[job_id]),
    ), mock.patch.object(appmod, "QBO_ENVIRONMENT", "sandbox"):
        r = client.post(
            f"/jobs/{job_id}/import-to-qbo", follow_redirects=False,
        )
    # Sandbox flow redirects back to job detail on success.
    assert r.status_code in (301, 302), \
        f"expected redirect after import, got {r.status_code} body={r.data[:200]}"
    job = appmod.jobs[job_id]
    assert "Imported" in job.get("status", ""), job.get("status")
    assert len(fake_qbo.posted) == 2, fake_qbo.posted  # two PCLaw txns
    print("I3 OK: import proceeds from snapshot when encrypted CSV is missing")


def i4_recovery_when_truly_missing_production():
    """No snapshot AND no encrypted file -> friendly recovery, NOT 500."""
    appmod.QBO_REAL_IMPORT = True
    client = appmod.app.test_client()
    job_id, _u = _make_job_with_qbo(
        client, "i4@example.test", "I4 LLP",
        encrypted_file_name="i4_gone.enc",
        seed_snapshot=False,
    )
    # Production deploy = demo affordances invisible.
    with mock.patch.object(
        appmod.demo_mode, "demo_visible_for_user", return_value=False,
    ), mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(_FakeQBO(), appmod.qbo_connections[job_id]),
    ), mock.patch.object(appmod, "QBO_ENVIRONMENT", "sandbox"):
        r = client.post(
            f"/jobs/{job_id}/import-to-qbo", follow_redirects=False,
        )
    assert r.status_code == 200, \
        f"expected the recovery page to render (200), got {r.status_code}"
    body = r.get_data(as_text=True)
    assert 'data-testid="import-recovery-error"' in body, \
        "recovery card missing"
    assert 'data-testid="reupload-cta"' in body, \
        "production recovery should include re-upload CTA"
    assert "Re-upload PCLaw export" in body
    # The route must NOT have posted anything to QBO.
    assert "Imported" not in (appmod.jobs.get(job_id) or {}).get("status", "")
    print("I4 OK: missing snapshot+file -> friendly recovery card (prod)")


def i5_recovery_demo_mode_offers_fresh_run():
    """Demo deploy -> primary CTA is 'Start a fresh demo run'."""
    appmod.QBO_REAL_IMPORT = True
    client = appmod.app.test_client()
    job_id, user = _make_job_with_qbo(
        client, "i5@example.test", "I5 LLP",
        encrypted_file_name="i5_gone.enc",
        seed_snapshot=False,
    )
    # Force demo visibility on for this user.
    with mock.patch.object(
        appmod.demo_mode, "demo_visible_for_user", return_value=True,
    ), mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(_FakeQBO(), appmod.qbo_connections[job_id]),
    ), mock.patch.object(appmod, "QBO_ENVIRONMENT", "sandbox"):
        r = client.post(
            f"/jobs/{job_id}/import-to-qbo", follow_redirects=False,
        )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'data-testid="demo-restart-cta"' in body, \
        "demo recovery should expose Start a fresh demo run"
    assert "Start a fresh demo run" in body
    # Re-upload CTA is still present, just as the secondary action.
    assert 'data-testid="reupload-cta"' in body
    print("I5 OK: demo mode recovery offers 'Start a fresh demo run'")


def i6_reparse_legacy_file_and_backfill_snapshot():
    """Legacy job: .enc on disk, no snapshot -> reparse + backfill."""
    appmod.QBO_REAL_IMPORT = True
    upload_dir = Path(os.environ["UPLOAD_DIR"])
    raw = upload_dir / "i6_raw.csv"
    raw.write_text(GL_CSV_TEXT, encoding="utf-8")
    enc_name = "i6_legacy.enc"
    encrypt_file(raw, upload_dir / enc_name)
    raw.unlink()

    client = appmod.app.test_client()
    job_id, _u = _make_job_with_qbo(
        client, "i6@example.test", "I6 LLP",
        encrypted_file_name=enc_name,
        seed_snapshot=False,  # legacy
    )
    fake_qbo = _FakeQBO()
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(fake_qbo, appmod.qbo_connections[job_id]),
    ), mock.patch.object(appmod, "QBO_ENVIRONMENT", "sandbox"):
        r = client.post(
            f"/jobs/{job_id}/import-to-qbo", follow_redirects=False,
        )
    assert r.status_code in (301, 302), \
        f"expected redirect after reparse import, got {r.status_code}"
    # Backfilled snapshot is now on the DB row.
    hydrated = appmod.db.hydrate_job(job_id)
    snap = hydrated.get("gl_rows") or []
    assert len(snap) == 4, snap
    txn_ids = {r["transaction_id"] for r in snap}
    assert txn_ids == {"T1", "T2"}, txn_ids
    print("I6 OK: legacy .enc reparsed + snapshot backfilled for next redeploy")


def i7_route_safety_net_catches_unhandled():
    """An unexpected exception in the importer -> recovery card, not 500."""
    appmod.QBO_REAL_IMPORT = True
    client = appmod.app.test_client()
    job_id, _u = _make_job_with_qbo(
        client, "i7@example.test", "I7 LLP",
        encrypted_file_name="i7.enc",
        seed_snapshot=True,
    )

    def boom(job_id):  # noqa: ARG001
        raise RuntimeError("simulated unexpected crash in import path")

    with mock.patch.object(appmod, "_import_to_qbo_impl", new=boom):
        r = client.post(
            f"/jobs/{job_id}/import-to-qbo", follow_redirects=False,
        )
    assert r.status_code == 200, \
        f"safety net should render recovery card (200), got {r.status_code}"
    body = r.get_data(as_text=True)
    assert 'data-testid="import-recovery-error"' in body, \
        "expected the recovery card from the safety net"
    print("I7 OK: route-level safety net renders recovery card, not a 500")


def i8_happy_path_regression():
    """Snapshot + file present: import succeeds as before."""
    appmod.QBO_REAL_IMPORT = True
    upload_dir = Path(os.environ["UPLOAD_DIR"])
    raw = upload_dir / "i8_raw.csv"
    raw.write_text(GL_CSV_TEXT, encoding="utf-8")
    enc_name = "i8_present.enc"
    encrypt_file(raw, upload_dir / enc_name)
    raw.unlink()

    client = appmod.app.test_client()
    job_id, _u = _make_job_with_qbo(
        client, "i8@example.test", "I8 LLP",
        encrypted_file_name=enc_name,
        seed_snapshot=True,
    )
    fake_qbo = _FakeQBO()
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(fake_qbo, appmod.qbo_connections[job_id]),
    ), mock.patch.object(appmod, "QBO_ENVIRONMENT", "sandbox"):
        r = client.post(
            f"/jobs/{job_id}/import-to-qbo", follow_redirects=False,
        )
    assert r.status_code in (301, 302), r.status_code
    assert len(fake_qbo.posted) == 2
    job = appmod.jobs[job_id]
    assert "Imported" in job["status"]
    print("I8 OK: happy path still imports with both snapshot AND file present")


def main():
    i1_gl_rows_for_snapshot()
    i2_save_and_hydrate_gl_rows()
    i3_import_uses_snapshot_when_file_missing()
    i4_recovery_when_truly_missing_production()
    i5_recovery_demo_mode_offers_fresh_run()
    i6_reparse_legacy_file_and_backfill_snapshot()
    i7_route_safety_net_catches_unhandled()
    i8_happy_path_regression()
    print("\nALL IMPORT-TO-QBO MISSING-SOURCE SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
