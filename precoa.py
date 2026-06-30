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
    if checkpoint not in ("matched", "reviewed", "needs_attention"):
        return False
    if checkpoint == "needs_attention":
        return False  # something went wrong — send to stepper

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
