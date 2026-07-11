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


def _norm_num(v) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def build_heal_plan(dead: list, live_accounts: list, saved_mappings: list) -> list:
    """For each dead mapping, decide how to repair it against the live COA.

    Returns one action dict per dead row:
      {"mapping": <the saved mapping row>,
       "action": "relink" | "delete",
       "new_qbo_account_id": str | None,
       "new_qbo_account_name": str | None,
       "matched_by": "acctnum" | "name" | None}

    Relink precedence:
      1. AcctNum — the live account whose AcctNum equals the mapping's
         pclaw_account_number (numbers survive renames; this is why the
         import prefers number-mode).
      2. Exact name (casefold) — live account whose Name equals the
         mapping's qbo_account_name or pclaw_account_name.
    Anything unmatched → "delete": removing the row lets the account fall
    back to live auto-match or the create-missing flow, both of which are
    already gated downstream. Only ACTIVE live accounts are heal targets.
    """
    dead_ids = {d["qbo_account_id"] for d in dead}
    by_acctnum: dict = {}
    by_name: dict = {}
    for a in live_accounts or []:
        if not a.get("Active", True):
            continue
        num = _norm_num(a.get("AcctNum"))
        if num and num not in by_acctnum:
            by_acctnum[num] = a
        nm = str(a.get("Name") or "").strip().casefold()
        if nm and nm not in by_name:
            by_name[nm] = a

    plan: list = []
    for m in saved_mappings or []:
        qid = str(m.get("qbo_account_id") or "").strip()
        if qid not in dead_ids:
            continue
        target = by_acctnum.get(_norm_num(m.get("pclaw_account_number")))
        matched_by = "acctnum" if target else None
        if target is None:
            for key in ("qbo_account_name", "pclaw_account_name"):
                nm = str(m.get(key) or "").strip().casefold()
                if nm and nm in by_name:
                    target = by_name[nm]
                    matched_by = "name"
                    break
        if target is not None:
            plan.append({
                "mapping": m,
                "action": "relink",
                "new_qbo_account_id": str(target.get("Id")),
                "new_qbo_account_name": target.get("Name") or "",
                "matched_by": matched_by,
            })
        else:
            plan.append({
                "mapping": m,
                "action": "delete",
                "new_qbo_account_id": None,
                "new_qbo_account_name": None,
                "matched_by": None,
            })
    return plan
