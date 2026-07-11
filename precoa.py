"""Check if a GL batch is ready to import directly from the Hub.

A batch is ready when:
- All GL accounts are resolved to QBO accounts (checkpoint >= matched)
- At least one firm GL batch is already completed (COA done via inline mapping)
- Vendors have been uploaded
- Clients have been uploaded

If any of these are missing, the batch must go through the stepper.
"""

from __future__ import annotations


def is_ready_to_import(job_id: str, firm_id: str, db) -> bool:
    """Return True if this GL batch can be imported directly from the Hub."""
    all_jobs = db.list_jobs_for_firm(firm_id, limit=50)

    # This job must be at least at 'matched' checkpoint
    this_job = next((j for j in all_jobs if j["id"] == job_id), None)
    if not this_job:
        return False
    checkpoint = this_job.get("checkpoint") or "uploaded"
    # Only offer the direct-import shortcut once the user has explicitly
    # reviewed the preview (checkpoint "reviewed"). A job at "matched" has
    # completed account-mapping but hasn't confirmed the import plan yet —
    # send it through the stepper so Step 4 (preview_import) is not skipped.
    if checkpoint != "reviewed":
        return False

    # Firm needs at least one completed GL batch (proves accounts are set up)
    # OR a completed chart_of_accounts job
    coa_ok = any(
        (
            (j.get("report_type") or "general_ledger") == "general_ledger"
            and j.get("checkpoint") == "completed"
            and j["id"] != job_id
        )
        or (
            j.get("report_type") == "chart_of_accounts"
            and j.get("checkpoint") == "completed"
        )
        for j in all_jobs
    )
    if not coa_ok:
        return False

    # Vendors and clients must be uploaded
    vendor_ok = any(j.get("report_type") == "vendor_list" for j in all_jobs)
    client_ok = any(j.get("report_type") == "customer_list" for j in all_jobs)

    return vendor_ok and client_ok


# ── COA Consolidation Utilities ───────────────────────────────────────────────
# Auto-detection of PCLaw account structure from uploaded files.
# Implements GLCheckr gate A: every GL account must resolve to a confirmed
# QBO mapping before any JEs can post (RECOMMENDATIONS.md §A).

import re as _re


ACCOUNT_CONTINUED_RE = _re.compile(
    r"\s*[-(]\s*continued\s*\)?\s*$", _re.IGNORECASE
)


def normalize_account_name(name: str) -> str:
    """Strip '-continued' / '(continued)' variants and surrounding whitespace.

    PCLaw exports split long account names across rows with a '-continued'
    suffix. This normalises all variants so they consolidate to the canonical
    name. See GLCheckr RECOMMENDATIONS.md §A and parsing gotchas.
    """
    if name is None or name == "":
        return ""
    return ACCOUNT_CONTINUED_RE.sub("", str(name)).strip()


def normalize_gl_number(gl_value: str) -> str:
    """Canonicalise GL numbers from PCLaw.

    - Remove trailing '.0' (Excel import artifact).
    - Preserve departmental suffixes (e.g., '4000.BGS').
    - Strip whitespace.
    """
    s = str(gl_value).strip()
    if _re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s


def is_equity_account(account_name: str) -> bool:
    """Return True for Retained Earnings or Net Income accounts."""
    name_lower = str(account_name).casefold()
    return "retained earnings" in name_lower or "net income" in name_lower


def get_coa_consolidation_status(firm_id: int, db) -> dict:
    """Assess COA consolidation status for the firm.

    Scans all COA and GL jobs for the firm, extracts unique PCLaw accounts,
    normalises names and GL numbers, and returns a consolidation summary.

    Returns:
        {
            "status": "ready" | "needs_consolidation" | "needs_mapping",
            "pclaw_accounts": [
                {
                    "gl_number": str,
                    "account_name": str,
                    "raw_names": list[str],
                    "is_consolidated": bool,
                    "is_equity": bool,
                },
                ...
            ],
            "consolidated_count": int,  # rows merged (had "-continued" variants)
            "unmapped_count": int,       # accounts needing QBO type assignment
        }
    """
    import json as _json
    import logging as _log_m

    _dbg = _log_m.getLogger(__name__)

    all_jobs = db.list_jobs_for_firm(firm_id, limit=200)
    _dbg.debug("[precoa] get_coa_consolidation_status: %d jobs for firm %s",
               len(all_jobs), firm_id)

    # key: (gl_number, normalised_name) → account dict
    pclaw_accounts: dict[tuple, dict] = {}

    for job in all_jobs:
        report_type = job.get("report_type") or "general_ledger"
        checkpoint  = job.get("checkpoint") or "uploaded"

        if report_type not in ("chart_of_accounts", "general_ledger"):
            continue
        if checkpoint not in ("uploaded", "parsed", "matched", "reviewed", "completed"):
            continue

        try:
            if report_type == "chart_of_accounts" and job.get("summary_json"):
                raw = job.get("summary_json", "[]")
                summary = _json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(summary, dict):
                    parsed_coa = (
                        summary.get("rows")
                        or summary.get("accounts")
                        or summary.get("parsed")
                        or []
                    )
                else:
                    parsed_coa = summary if isinstance(summary, list) else []

                _dbg.debug("[precoa] COA job %s: %d rows", job.get("id"), len(parsed_coa))

                for row in parsed_coa:
                    gl       = normalize_gl_number(row.get("account_number", ""))
                    raw_name = row.get("account_name", "")
                    norm     = normalize_account_name(raw_name)
                    if not (gl and norm):
                        continue
                    key = (gl, norm)
                    if key not in pclaw_accounts:
                        pclaw_accounts[key] = {
                            "gl_number":      gl,
                            "account_name":   norm,
                            "raw_names":      [raw_name],
                            "is_consolidated": False,
                            "is_equity":      is_equity_account(norm),
                        }
                    elif raw_name not in pclaw_accounts[key]["raw_names"]:
                        pclaw_accounts[key]["raw_names"].append(raw_name)
                        if raw_name != norm:
                            pclaw_accounts[key]["is_consolidated"] = True

            elif report_type == "general_ledger" and job.get("pclaw_accounts_json"):
                # list_jobs_for_firm returns raw DB rows; pclaw_accounts_json is
                # the correct column (stored by save_job_state at app_db.py:836).
                # Format: [{"number": str|None, "name": str|None}]
                raw_pa = job.get("pclaw_accounts_json", "[]")
                pa_list = _json.loads(raw_pa) if isinstance(raw_pa, str) else (raw_pa or [])
                for acct in pa_list:
                    gl       = normalize_gl_number(acct.get("number", ""))
                    raw_name = acct.get("name", "")
                    norm     = normalize_account_name(raw_name)
                    if not (gl and norm):
                        continue
                    key = (gl, norm)
                    if key not in pclaw_accounts:
                        pclaw_accounts[key] = {
                            "gl_number":      gl,
                            "account_name":   norm,
                            "raw_names":      [raw_name],
                            "is_consolidated": False,
                            "is_equity":      is_equity_account(norm),
                        }
                    elif raw_name not in pclaw_accounts[key]["raw_names"]:
                        pclaw_accounts[key]["raw_names"].append(raw_name)
                        if raw_name != norm:
                            pclaw_accounts[key]["is_consolidated"] = True

        except (ValueError, KeyError, TypeError, AttributeError):
            pass

    # Sort by GL number (numeric prefix) then name
    def _sort_key(a: dict) -> tuple:
        m = _re.match(r"(\d+)", a["gl_number"])
        return (int(m.group(1)) if m else 999999, a["account_name"])

    account_list = sorted(pclaw_accounts.values(), key=_sort_key)
    consolidated_count = sum(1 for a in account_list if a["is_consolidated"])
    unmapped_count     = len(account_list)

    if not account_list:
        status = "ready"
    elif consolidated_count > 0:
        status = "needs_consolidation"
    else:
        status = "needs_mapping"

    return {
        "status":            status,
        "pclaw_accounts":    account_list,
        "consolidated_count": consolidated_count,
        "unmapped_count":    unmapped_count,
    }
