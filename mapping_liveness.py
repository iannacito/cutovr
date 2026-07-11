"""Validate saved account mappings against the LIVE QBO Chart of Accounts.

A mapping row stores a QBO account Id captured at Step-3 save time. That Id
can die later: sandbox companies get reseeded (realm survives, accounts
don't), operators delete or deactivate accounts in QBO, or a create-missing
plan records an Id that never committed. Posting a JournalEntry that
references a dead Id fails with QBO 400 code 2500 ("Invalid Reference Id"),
potentially mid-batch — a silent partial post.

This module is pure (no Flask, no QBO HTTP): callers pass the referenced
ids and the live accounts list; it returns the dead ones, classified.

Statuses:
  * "missing"  — Id not in the live COA at all (deleted, reseeded away, or
                 never committed). Action: re-map in Step 3.
  * "inactive" — Id exists but Active is false. Action: reactivate the
                 account in QuickBooks, or re-map in Step 3.
"""

from __future__ import annotations

from typing import Iterable, Optional


def collect_referenced_account_ids(
    mapping_rows: Iterable[dict],
    extra_ids: Optional[Iterable[str]] = None,
) -> dict:
    """{qbo_account_id: context} for every account a post could reference.

    mapping_rows: the realm-scoped saved mappings (dicts with
    qbo_account_id / qbo_account_name / pclaw_account_number /
    pclaw_account_name — extra keys ignored).
    extra_ids: ids referenced outside the mapping table (e.g. the
    auto-balance bank / expense-offset accounts), context-free.
    """
    referenced: dict = {}
    for m in mapping_rows or []:
        qid = str(m.get("qbo_account_id") or "").strip()
        if not qid:
            continue
        referenced.setdefault(qid, {
            "qbo_account_name": (m.get("qbo_account_name") or "").strip(),
            "pclaw_account_number": (m.get("pclaw_account_number") or "").strip(),
            "pclaw_account_name": (m.get("pclaw_account_name") or "").strip(),
        })
    for qid in extra_ids or []:
        qid = str(qid or "").strip()
        if qid:
            referenced.setdefault(qid, {
                "qbo_account_name": "",
                "pclaw_account_number": "",
                "pclaw_account_name": "",
            })
    return referenced


def find_dead_mappings(
    referenced: dict,
    live_accounts: Iterable[dict],
) -> list[dict]:
    """Return one row per referenced Id that is missing or inactive.

    live_accounts: QBO Account objects (need Id, Name, Active). MUST come
    from a query that includes inactive accounts — a default QBO query
    returns active only, which would misreport 'inactive' as 'missing'.
    """
    by_id: dict = {}
    for a in live_accounts or []:
        aid = str(a.get("Id") or "").strip()
        if aid:
            by_id[aid] = a
    dead: list[dict] = []
    for qid, ctx in sorted(referenced.items(), key=lambda kv: kv[0]):
        live = by_id.get(qid)
        if live is None:
            status = "missing"
            live_name = ""
        elif not live.get("Active", True):
            status = "inactive"
            live_name = live.get("Name") or ""
        else:
            continue
        dead.append({
            "qbo_account_id": qid,
            "status": status,
            "qbo_account_name": ctx.get("qbo_account_name") or live_name,
            "pclaw_account_number": ctx.get("pclaw_account_number", ""),
            "pclaw_account_name": ctx.get("pclaw_account_name", ""),
            "action": (
                "Re-map this account in Step 3 — the QuickBooks account it "
                "pointed to no longer exists in this company."
                if status == "missing" else
                "Reactivate this account in QuickBooks, or re-map it in "
                "Step 3."
            ),
        })
    return dead
