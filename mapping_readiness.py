"""Single source of truth for account mapping readiness.

Evaluates whether account mappings are complete and valid across Steps 3/4/5.
All three stepper stages call evaluate() with the same arguments, so their
verdicts are identical by construction — preventing the stepper bounce where
Step 3 says "complete" but Step 5 disagrees and redirects backward.

This module is pure Python — no Flask, no QBO HTTP, no DB access. Callers
fetch data and pass it in; evaluate() returns a verdict dict.
"""

from __future__ import annotations
import re
import logging
from typing import Any

from mapping_liveness import find_dead_mappings
from pclaw_pipeline import (
    build_account_mapping_from_numbers,
    build_account_mapping_from_names,
)

_log = logging.getLogger(__name__)

CONTINUED_RE = re.compile(r"\s*[-(]\s*continued\s*\)?\s*$", re.IGNORECASE)


def normalize_account_name(name: Any) -> str:
    """Strip PCLaw's page-break '-Continued' suffix and surrounding whitespace.

    Variants seen in real exports: '- Continued', '-continued', '(continued)',
    case differences, trailing spaces. All become the base account name.
    """
    return CONTINUED_RE.sub("", str(name or "")).strip()


def account_key(number: Any, name: Any, mode: str) -> str:
    """Canonical lookup key for one pclaw account, matching the import path."""
    num = str(number or "").strip()
    if num.endswith(".0"):
        num = num[:-2]
    if mode == "number" and num:
        return num
    return normalize_account_name(name).casefold()


def _choose_mode(pclaw_accounts: list[dict]) -> tuple[str, str]:
    """Returns (mapping_mode, mode_reason).

    Mode rule is derived from the PCLAW side, not from QBO.
    "number" if ANY pclaw account has a non-empty number field.
    Do NOT fall back to "name" because auto_by_number came back empty —
    that fallback is what caused the 63-phantom-unmapped incident on 2026-07-11.
    """
    for acct in pclaw_accounts:
        num = str(acct.get("number", "") or acct.get("account_number", "") or "").strip()
        if num.endswith(".0"):
            num = num[:-2]
        if num:
            return "number", "pclaw_has_numbers"
    return "name", "pclaw_no_numbers"


def _consolidate(pclaw_accounts: list[dict], mode: str) -> list[dict]:
    """Return de-duplicated pclaw accounts (one per canonical key).

    A '-Continued' variant and its base are the SAME account. The base
    name variant takes priority if both appear; otherwise the only form found.
    This ensures counts.total is the actual number of distinct GL accounts,
    not inflated by page-break duplicates.
    """
    seen: dict[str, dict] = {}
    for acct in pclaw_accounts:
        key = account_key(
            acct.get("number") or acct.get("account_number"),
            acct.get("name"),
            mode,
        )
        if key not in seen:
            seen[key] = acct
        else:
            # Prefer the variant without a '-Continued' suffix (the base name)
            existing_name = str(seen[key].get("name") or "")
            this_name = str(acct.get("name") or "")
            if CONTINUED_RE.search(existing_name) and not CONTINUED_RE.search(this_name):
                seen[key] = acct
    return list(seen.values())


def evaluate(
    pclaw_accounts: list[dict],
    saved_mappings: list[dict],
    live_accounts: list[dict],
) -> dict:
    """Single source of truth for 'is account mapping done for this job?'

    Args:
        pclaw_accounts: distinct account list in the shape _load_pclaw_accounts_for_mapping
                        returns — keys: "number" (str|None) and "name" (str|None).
        saved_mappings: rows from db.list_account_mappings(firm_id, realm_id) —
                        keys: pclaw_account_number, pclaw_account_name, qbo_account_id.
        live_accounts:  QBO Chart of Accounts INCLUDING inactive accounts so
                        that stale saved rows (pointing at deactivated QBO
                        accounts) are classified as stale rather than silently
                        winning the overlay.

    Returns dict with keys:
        mapping_mode   "number" | "name"
        mode_reason    why mode was chosen (for logging/debugging)
        mapping        {account_key: qbo_account_id}  — live ACTIVE ids only
        resolved       [{account, via}]  via = "auto" | "saved"
        unmatched      [account]         need create-missing or manual pick
        stale          [saved_mapping_row]  saved row whose qbo_account_id is
                       dead or inactive in live_accounts
        counts         {total, resolved, unmatched, stale}
        ready          bool — True when unmatched == [] and stale == []
    """
    # 1. Choose mode from PCLAW side (never from QBO)
    mapping_mode, mode_reason = _choose_mode(pclaw_accounts)

    # 2. Consolidate '-Continued' duplicates
    consolidated = _consolidate(pclaw_accounts, mapping_mode)

    # 3. Partition live_accounts into active and inactive id sets
    active_ids: set[str] = {
        str(a["Id"]) for a in live_accounts
        if a.get("Active", True)  # treat missing Active as True
    }

    # 4. Build auto-matches using ACTIVE accounts only
    active_accounts = [a for a in live_accounts if str(a.get("Id")) in active_ids]

    # Call the import path's own builders to ensure readiness matches import behavior
    auto_by_number = build_account_mapping_from_numbers(
        {"QueryResponse": {"Account": active_accounts}}
    )
    auto_by_name = build_account_mapping_from_names(
        {"QueryResponse": {"Account": active_accounts}}
    )
    auto_map = auto_by_number if mapping_mode == "number" else auto_by_name

    # 5. Overlay saved_mappings — only rows whose qbo_account_id is live-active
    stale: list[dict] = []
    saved_overlay: dict[str, str] = {}  # key → qbo_account_id
    for row in saved_mappings:
        qbo_id = str(row.get("qbo_account_id") or "").strip()
        # Saved rows use pclaw_account_number and pclaw_account_name as the key
        pclaw_num = row.get("pclaw_account_number")
        pclaw_name = row.get("pclaw_account_name")
        key = account_key(pclaw_num, pclaw_name, mapping_mode)
        if not key or not qbo_id:
            continue
        if qbo_id in active_ids:
            saved_overlay[key] = qbo_id
        else:
            stale.append(row)

    # 6. Determine resolved vs unmatched for each consolidated account
    mapping: dict[str, str] = {}
    resolved: list[dict] = []
    unmatched: list[dict] = []

    for acct in consolidated:
        key = account_key(
            acct.get("number") or acct.get("account_number"),
            acct.get("name"),
            mapping_mode,
        )
        if key in saved_overlay:
            qbo_id = saved_overlay[key]
            mapping[key] = qbo_id
            resolved.append({"account": acct, "via": "saved"})
        elif key in auto_map:
            qbo_id = auto_map[key]
            mapping[key] = qbo_id
            resolved.append({"account": acct, "via": "auto"})
        else:
            unmatched.append(acct)

    _log.info(
        "mapping_readiness.evaluate: mode=%s reason=%s total=%d resolved=%d "
        "unmatched=%d stale=%d ready=%s",
        mapping_mode,
        mode_reason,
        len(consolidated),
        len(resolved),
        len(unmatched),
        len(stale),
        len(unmatched) == 0 and len(stale) == 0,
    )

    ready = len(unmatched) == 0 and len(stale) == 0
    return {
        "mapping_mode": mapping_mode,
        "mode_reason": mode_reason,
        "mapping": mapping,
        "resolved": resolved,
        "unmatched": unmatched,
        "stale": stale,
        "counts": {
            "total": len(consolidated),
            "resolved": len(resolved),
            "unmatched": len(unmatched),
            "stale": len(stale),
        },
        "ready": ready,
    }
