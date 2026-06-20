"""Smoke tests: entity_name_blockers + opening_balance_history persistence.

Both of these are set on the in-memory job dict by the import / opening-
balance routes. Before this fix ``save_job_state`` had no column for them,
so they vanished on a restart/redeploy — the Migration Hub would stop
showing which ledger needs names, and the opening-balance retry banner
would disappear. These tests pin the DB round-trip.

Covered
-------
  P1  entity_name_blockers round-trips through save_job_state/hydrate_job.
  P2  opening_balance_history round-trips, preserving a failed (retryable)
      attempt.
  P3  Clearing entity_name_blockers (set to None) is persisted, so a
      ledger that later posts stops reporting a stale block.

Run from project root::

    python3 tests/smoke_job_state_persistence.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("SECRET_KEY", "smoke-job-state-persistence")

from app_db import AppDB  # noqa: E402

db = AppDB(os.environ["APP_DB"])


def _seed_job(job_id="job_p"):
    firm_id, user_id = db.create_firm_and_admin(
        "Persist Firm", f"{job_id}@example.test", "passw0rd!1234"
    )
    db.upsert_job(
        job_id=job_id,
        firm_id=firm_id,
        user_id=user_id,
        company="Persist Co",
        source_file="p.csv",
        encrypted_file="/tmp/p.csv.enc",
        file_sha256="sha-p",
        status="Uploaded",
    )
    return job_id


def p1_entity_blockers_round_trip():
    job_id = _seed_job("job_p1")
    blockers = {"kind": "Customer", "offenders": ["row 7 (A/R)", "row 9 (A/R)"]}
    db.save_job_state(job_id, {"status": "Import blocked: entity names missing",
                               "entity_name_blockers": blockers})
    hydrated = db.hydrate_job(job_id)
    assert hydrated["entity_name_blockers"] == blockers, hydrated.get("entity_name_blockers")
    print("P1 OK: entity_name_blockers survive a save/hydrate round-trip")


def p2_opening_balance_history_round_trip():
    job_id = _seed_job("job_p2")
    history = [
        {"status": "failed", "qbo_je_id": None, "error": "QBO 500 (retry)",
         "at": "2026-06-01T10:00:00"},
    ]
    db.save_job_state(job_id, {"status": "Opening balance posting failed (retryable)",
                               "opening_balance_history": history})
    hydrated = db.hydrate_job(job_id)
    assert hydrated["opening_balance_history"] == history, hydrated.get("opening_balance_history")
    last = hydrated["opening_balance_history"][-1]
    assert last["status"] == "failed" and last["qbo_je_id"] is None
    print("P2 OK: opening_balance_history survives, preserving the retryable failure")


def p3_clearing_blockers_persists():
    job_id = _seed_job("job_p3")
    db.save_job_state(job_id, {"status": "blocked",
                               "entity_name_blockers": {"kind": "Vendor", "offenders": ["x"]}})
    # A later successful post clears the block.
    db.save_job_state(job_id, {"status": "Imported", "entity_name_blockers": None})
    hydrated = db.hydrate_job(job_id)
    assert not hydrated.get("entity_name_blockers"), hydrated.get("entity_name_blockers")
    print("P3 OK: clearing entity_name_blockers is persisted (no stale block after posting)")


def main():
    p1_entity_blockers_round_trip()
    p2_opening_balance_history_round_trip()
    p3_clearing_blockers_persists()
    print("\nAll job-state persistence smoke tests passed.")


if __name__ == "__main__":
    main()
