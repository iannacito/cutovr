"""Smoke tests for account-mapping recovery when the source upload is gone.

Background
----------
Render's free/starter tiers wipe the project tree on every redeploy.
Before this fix, that meant ``uploads/*.enc`` disappeared while the job
metadata (kept on the persistent disk via APP_DB=/var/data/...) survived,
and clicking "Match accounts" flashed:

    "The original upload for this job is no longer available.
     Re-upload the PCLaw export to continue."

…and bounced the user back to the job page with no clear way forward.

This sprint:

  1. Persists the unique (account_number, account_name) list to a new
     ``jobs.pclaw_accounts_json`` column at upload time, so account
     matching can proceed without the raw CSV.
  2. Adds an in-template recovery state (category=missing_source) with a
     precise re-upload CTA, instead of bouncing with a generic flash, for
     the residual case where neither the snapshot nor the CSV is on file
     (legacy jobs uploaded before the column existed).
  3. Allows UPLOAD_DIR / OUTPUT_DIR to be overridden via env so the
     Render deploy can point them at /var/data — the durable disk.

Covers
------
  M1  ``_extract_pclaw_accounts_from_gl_rows`` returns unique pairs in
      first-seen order and tolerates missing / blank columns.
  M2  ``AppDB.save_job_state`` round-trips ``pclaw_accounts`` via
      ``hydrate_job`` so the snapshot survives a restart.
  M3  ``/jobs/<id>/account-mapping`` succeeds when the encrypted CSV is
      gone but the persisted snapshot exists — no redirect, no error.
  M4  When both the file AND the snapshot are missing (legacy job), the
      page renders the precise re-upload recovery state with a CTA, NOT
      the old "dead-end" generic flash that bounced to the job page.
  M5  When the snapshot is absent but the encrypted CSV is present, the
      route reparses the CSV, renders the matching table, AND backfills
      the snapshot so subsequent visits no longer depend on the file.
  M6  UPLOAD_DIR honors the ``UPLOAD_DIR`` env var so the Render deploy
      can keep encrypted CSVs on the persistent disk.

Run from project root:

    python3 tests/smoke_account_mapping_missing_upload.py
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Use a dedicated upload dir per run so we don't pollute the repo's
# uploads/ folder and can blow it away cleanly.
_UPLOAD_DIR = tempfile.mkdtemp(prefix="pclaw_uploads_")
os.environ["UPLOAD_DIR"] = _UPLOAD_DIR
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_")

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-account-mapping-missing-upload")

import app as appmod  # noqa: E402
from app_db import AppDB  # noqa: E402
from encryption import encrypt_file  # noqa: E402


def m1_extract_unique_accounts():
    extract = appmod._extract_pclaw_accounts_from_gl_rows
    rows = [
        {"account_number": "1000", "account_name": "Cash"},
        {"account_number": "1000", "account_name": "Cash"},  # dup
        {"account_number": "4000", "account_name": "Fees"},
        {"account_number": "", "account_name": "Trust"},
        {"account_number": "  ", "account_name": "  "},  # blank → skip
        {"account_number": "2000", "account_name": ""},  # number-only OK
    ]
    out = extract(rows)
    assert out == [
        {"number": "1000", "name": "Cash"},
        {"number": "4000", "name": "Fees"},
        {"number": None, "name": "Trust"},
        {"number": "2000", "name": None},
    ], out
    assert extract([]) == []
    assert extract(None) == []
    print("M1 OK: _extract_pclaw_accounts_from_gl_rows uniques + tolerates blanks")


def m2_save_and_hydrate_pclaw_accounts():
    db_path = tempfile.mktemp(suffix=".sqlite3")
    db = AppDB(db_path)
    db.create_firm_and_admin("F", "m2@example.test", "passw0rd!1234")
    user = db.get_user_by_email("m2@example.test")
    job_id = "job_m2_persist"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company="M2 Co", source_file="x.csv",
        encrypted_file="missing.enc", file_sha256="0" * 64,
        status="uploaded",
    )

    snapshot = [
        {"number": "1000", "name": "Cash"},
        {"number": "4000", "name": "Fees"},
    ]
    db.save_job_state(job_id, {"status": "uploaded", "pclaw_accounts": snapshot})

    hydrated = db.hydrate_job(job_id)
    assert hydrated is not None
    assert hydrated.get("pclaw_accounts") == snapshot, hydrated.get("pclaw_accounts")

    # Round-trip with None clears it (writes NULL).
    db.save_job_state(job_id, {"status": "uploaded", "pclaw_accounts": None})
    hydrated = db.hydrate_job(job_id)
    assert "pclaw_accounts" not in hydrated or not hydrated.get("pclaw_accounts")
    print("M2 OK: AppDB persists + hydrates pclaw_accounts snapshot")


def _signup_and_login(client, email, firm):
    pwd = "passw0rd!1234"
    r = client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={"email": email, "password": pwd},
                    follow_redirects=False)


def _make_job_with_qbo(client, email, firm, *, encrypted_file_name):
    """Sign up a firm, create a job, wire up a fake QBO connection."""
    _signup_and_login(client, email, firm)
    db = appmod.db
    user = db.get_user_by_email(email)
    job_id = f"job_{firm.replace(' ', '_').lower()}"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company=firm, source_file="x.csv",
        encrypted_file=encrypted_file_name, file_sha256="0" * 64,
        status="uploaded",
    )
    appmod.qbo_connections[job_id] = {
        "realm_id": "R-TEST",
        "access_token_enc": appmod.encrypt_token("fake-access"),
        "refresh_token_enc": appmod.encrypt_token("fake-refresh"),
        "company_name": firm,
        "legal_name": firm,
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }
    # Drop any prior in-memory copy so the route hydrates from DB on the
    # first request — this is what the production restart path does.
    appmod.jobs.pop(job_id, None)
    return job_id, user


class _FakeQBO:
    """Stand-in for QBOClient returning a small QBO chart of accounts."""
    def get_accounts(self):
        return {
            "QueryResponse": {
                "Account": [
                    {"Id": "10", "Name": "Cash", "AcctNum": "1000", "AccountType": "Bank"},
                    {"Id": "40", "Name": "Fees", "AcctNum": "4000", "AccountType": "Income"},
                ]
            }
        }


def m3_route_uses_snapshot_when_file_missing():
    client = appmod.app.test_client()
    job_id, user = _make_job_with_qbo(
        client, "m3@example.test", "M3 LLP",
        encrypted_file_name="m3_does_not_exist.enc",
    )
    # Persist the snapshot — this is what _process_uploaded_csv would have
    # written at upload time on a Render deploy with the fix in place.
    snapshot = [
        {"number": "1000", "name": "Cash"},
        {"number": "4000", "name": "Fees"},
    ]
    appmod.db.save_job_state(job_id, {"status": "uploaded", "pclaw_accounts": snapshot})

    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(_FakeQBO(), appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping", follow_redirects=False)

    assert r.status_code == 200, f"expected 200, got {r.status_code} -> {r.headers.get('Location')}"
    body = r.get_data(as_text=True)
    assert "original upload for this job is no longer available" not in body, \
        "regression: dead-end flash leaked into rendered page"
    assert 'data-testid="account-mapping-error"' not in body, \
        "regression: error state rendered when snapshot was usable"
    # The rendered matching table should contain both PCLaw rows.
    assert "1000" in body and "Cash" in body
    assert "4000" in body and "Fees" in body
    print("M3 OK: account-mapping renders from persisted snapshot when .enc is gone")


def m4_route_renders_precise_recovery_when_truly_missing():
    client = appmod.app.test_client()
    job_id, user = _make_job_with_qbo(
        client, "m4@example.test", "M4 LLP",
        encrypted_file_name="m4_does_not_exist.enc",
    )
    # No snapshot saved AND no encrypted file on disk → unrecoverable.
    # The fix renders a precise re-upload CTA on the Match-accounts screen
    # itself rather than bouncing with a generic flash.
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(_FakeQBO(), appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping", follow_redirects=False)

    assert r.status_code == 200, \
        f"expected the page to render with recovery CTA, got {r.status_code}"
    body = r.get_data(as_text=True)
    assert 'data-testid="account-mapping-error"' in body, \
        "expected the in-place error card, got something else"
    assert 'data-testid="reupload-cta"' in body
    assert "Re-upload PCLaw export" in body
    assert "no longer on file for this job" in body
    # Old dead-end flash must NOT appear.
    assert "original upload for this job is no longer available" not in body
    print("M4 OK: no snapshot + no file → in-place re-upload CTA (not dead-end flash)")


def m5_route_reparses_and_backfills_snapshot_from_file():
    client = appmod.app.test_client()

    # Write a valid GL CSV, encrypt it, and stash it under UPLOAD_DIR with
    # the encrypted_file name we register in the job row.
    csv_text = (
        "transaction_id,account_number,account_name,date,debit,credit,memo\n"
        "T1,1000,Cash,2024-01-01,100.00,0.00,test\n"
        "T1,4000,Fees,2024-01-01,0.00,100.00,test\n"
        "T2,1000,Cash,2024-01-02,50.00,0.00,another\n"
        "T2,4000,Fees,2024-01-02,0.00,50.00,another\n"
    )
    upload_dir = Path(os.environ["UPLOAD_DIR"])
    raw = upload_dir / "m5_raw.csv"
    raw.write_text(csv_text, encoding="utf-8")
    enc_name = "m5_legacy.enc"
    encrypt_file(raw, upload_dir / enc_name)
    raw.unlink()

    job_id, user = _make_job_with_qbo(
        client, "m5@example.test", "M5 LLP",
        encrypted_file_name=enc_name,
    )
    # NO snapshot persisted — simulates a legacy job uploaded before this
    # fix landed but whose .enc still lives on the persistent disk.

    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(_FakeQBO(), appmod.qbo_connections[job_id]),
    ):
        r = client.get(f"/jobs/{job_id}/account-mapping", follow_redirects=False)

    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "1000" in body and "Cash" in body, "expected Cash row from reparsed CSV"
    assert "4000" in body and "Fees" in body, "expected Fees row from reparsed CSV"

    # And the snapshot should now be on the row, so a future cold start
    # where the .enc has been wiped still works.
    hydrated = appmod.db.hydrate_job(job_id)
    assert hydrated is not None
    snap = hydrated.get("pclaw_accounts") or []
    snap_pairs = {(a.get("number"), a.get("name")) for a in snap}
    assert ("1000", "Cash") in snap_pairs, snap
    assert ("4000", "Fees") in snap_pairs, snap
    print("M5 OK: reparse legacy .enc + backfill snapshot for future restart resilience")


def m6_upload_dir_honors_env_override():
    # Importing app already snapped UPLOAD_DIR at module load; just assert
    # the value reflects the env var, so Render's blueprint /var/data
    # override actually takes effect.
    assert str(appmod.UPLOAD_DIR) == os.environ["UPLOAD_DIR"], (
        appmod.UPLOAD_DIR, os.environ["UPLOAD_DIR"],
    )
    assert str(appmod.OUTPUT_DIR) == os.environ["OUTPUT_DIR"]
    # And both must exist (mkdir(parents=True) at import).
    assert appmod.UPLOAD_DIR.is_dir()
    assert appmod.OUTPUT_DIR.is_dir()
    print("M6 OK: UPLOAD_DIR / OUTPUT_DIR honor env (durable disk on Render)")


def main():
    m1_extract_unique_accounts()
    m2_save_and_hydrate_pclaw_accounts()
    m3_route_uses_snapshot_when_file_missing()
    m4_route_renders_precise_recovery_when_truly_missing()
    m5_route_reparses_and_backfills_snapshot_from_file()
    m6_upload_dir_honors_env_override()
    print("\nALL ACCOUNT-MAPPING MISSING-UPLOAD SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
