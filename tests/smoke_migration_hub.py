"""Smoke tests: Migration Hub per-GL projection (Roadblock 5).

A firm uploading several monthly general ledgers needs to see each one as
its own card with its own status and blockers, so a single stuck ledger
never hides the rest. ``migration_hub.build_hub`` is a pure projection over
hydrated job dicts — no DB, no QBO — so the status logic is unit testable.

Covered
-------
  H1  A blocked GL and a ready GL both appear; one does not hide the other.
  H2  Posting one GL (import_summary present) does not change another GL's
      status — each card is classified independently.
  H3  Concrete blockers surface: unmapped accounts and missing entity names.
  H4  Status precedence: imported > superseded/failed > blocked > ready.
  H5  Superseded GLs are dropped from the board by default but still counted.
  H6  Sort order puts the ledgers that need a human first (blocked, ready).
  H7  ready requires QBO connected + account mappings; otherwise validated.

Run from project root::

    python3 tests/smoke_migration_hub.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import migration_hub as mh  # noqa: E402


def _gl(job_id, **kw):
    """Build a minimal GL job dict with sensible defaults."""
    job = {
        "id": job_id,
        "company": kw.pop("company", f"Firm {job_id}"),
        "report_type": "general_ledger",
        "created_at": kw.pop("created_at", "2026-01-01T00:00:00"),
        "updated_at": kw.pop("updated_at", "2026-01-01T00:00:00"),
    }
    job.update(kw)
    return job


def _card_by_id(hub, job_id):
    for c in hub["cards"]:
        if c.job_id == job_id:
            return c
    return None


def h1_blocked_and_ready_coexist():
    blocked = _gl("b1", preflight={"line_count": 10},
                  unmapped_accounts=["1200 Trust"])
    ready = _gl("r1", preflight={"line_count": 5})
    hub = mh.build_hub([blocked, ready], has_qbo_connection=True,
                       account_mapping_count=3)
    ids = {c.job_id for c in hub["cards"]}
    assert ids == {"b1", "r1"}, ids
    assert _card_by_id(hub, "b1").status == mh.STATUS_BLOCKED
    assert _card_by_id(hub, "r1").status == mh.STATUS_READY
    print("H1 OK: a blocked GL and a ready GL both show; neither hides the other")


def h2_posting_one_does_not_affect_another():
    imported = _gl("i1", import_summary={"qbo_je_count": 42},
                   preflight={"line_count": 10})
    pending = _gl("p1", preflight={"line_count": 8})
    hub = mh.build_hub([imported, pending], has_qbo_connection=True,
                       account_mapping_count=2)
    assert _card_by_id(hub, "i1").status == mh.STATUS_IMPORTED
    # The still-pending GL is unaffected by the other's posting.
    assert _card_by_id(hub, "p1").status == mh.STATUS_READY
    assert _card_by_id(hub, "i1").je_count == 42
    print("H2 OK: posting one GL leaves the others' status unchanged")


def h3_concrete_blockers_surface():
    j = _gl("j1", preflight={"line_count": 4},
            unmapped_accounts=["1200 A", "1300 B"],
            entity_name_blockers={"kind": "Customer",
                                  "offenders": ["row 7 (A/R)", "row 9 (A/R)"]})
    hub = mh.build_hub([j], has_qbo_connection=True, account_mapping_count=1)
    card = _card_by_id(hub, "j1")
    assert card.status == mh.STATUS_BLOCKED, card.status
    joined = " | ".join(card.blockers)
    assert "not matched" in joined, joined
    assert "name(s) missing" in joined, joined
    assert "row 7 (A/R)" in card.entity_needs, card.entity_needs
    print("H3 OK: unmapped accounts and missing entity names both surface as blockers")


def h4_status_precedence():
    # imported wins even if a stale blocker snapshot is present.
    imported = _gl("p1", import_summary={"qbo_je_count": 1},
                   unmapped_accounts=["stale"])
    hub = mh.build_hub([imported], has_qbo_connection=False,
                       account_mapping_count=0)
    assert _card_by_id(hub, "p1").status == mh.STATUS_IMPORTED
    print("H4 OK: imported status takes precedence over a stale blocker snapshot")


def h5_superseded_dropped_but_counted():
    sup = _gl("s1", status="Superseded (replaced by newer upload of the same type)")
    active = _gl("a1", preflight={"line_count": 3})
    hub = mh.build_hub([sup, active], has_qbo_connection=True,
                       account_mapping_count=1)
    ids = {c.job_id for c in hub["cards"]}
    assert ids == {"a1"}, ids
    assert hub["counts"].get(mh.STATUS_SUPERSEDED) == 1, hub["counts"]
    # And it can be shown explicitly when asked.
    hub2 = mh.build_hub([sup, active], has_qbo_connection=True,
                        account_mapping_count=1, include_superseded=True)
    assert {c.job_id for c in hub2["cards"]} == {"a1", "s1"}
    print("H5 OK: superseded GLs are hidden by default but still counted")


def h6_sort_blocked_first():
    ready = _gl("r1", preflight={"line_count": 1}, updated_at="2026-03-01T00:00:00")
    blocked = _gl("b1", preflight={"line_count": 1},
                  unmapped_accounts=["x"], updated_at="2026-02-01T00:00:00")
    imported = _gl("i1", import_summary={"qbo_je_count": 1},
                   updated_at="2026-04-01T00:00:00")
    hub = mh.build_hub([imported, ready, blocked], has_qbo_connection=True,
                       account_mapping_count=1)
    order = [c.job_id for c in hub["cards"]]
    assert order[0] == "b1", order  # blocked first regardless of recency
    assert order.index("r1") < order.index("i1"), order
    print("H6 OK: cards sort blocked-first, then ready, then imported")


def h7_ready_requires_qbo_and_mappings():
    j = _gl("j1", preflight={"line_count": 5})
    # No QBO connection -> validated (checked but setup incomplete), not ready.
    hub_no_qbo = mh.build_hub([j], has_qbo_connection=False, account_mapping_count=5)
    assert _card_by_id(hub_no_qbo, "j1").status == mh.STATUS_VALIDATED
    # QBO connected but no mappings -> still validated.
    hub_no_map = mh.build_hub([j], has_qbo_connection=True, account_mapping_count=0)
    assert _card_by_id(hub_no_map, "j1").status == mh.STATUS_VALIDATED
    # No preflight at all -> uploaded.
    raw = _gl("j2")
    hub_raw = mh.build_hub([raw], has_qbo_connection=True, account_mapping_count=5)
    assert _card_by_id(hub_raw, "j2").status == mh.STATUS_UPLOADED
    print("H7 OK: ready needs QBO + mappings; otherwise validated/uploaded")


def main():
    h1_blocked_and_ready_coexist()
    h2_posting_one_does_not_affect_another()
    h3_concrete_blockers_surface()
    h4_status_precedence()
    h5_superseded_dropped_but_counted()
    h6_sort_blocked_first()
    h7_ready_requires_qbo_and_mappings()
    print("\nAll migration-hub smoke tests passed.")


if __name__ == "__main__":
    main()
