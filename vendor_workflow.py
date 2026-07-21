"""Vendor list 2-stage workflow stepper.

Mirrors trial balance workflow but for vendor supplier details.
Vendor jobs have just two steps: Upload vendor file, then push to QBO.

Stages:
  1. Upload      — vendor list file uploaded (job exists + parsed)
  2. Push        — vendors pushed to QuickBooks
"""

from __future__ import annotations
from typing import Optional, Callable, List
from customer_workflow import (
    WorkflowStage,
    STAGE_STATUS_COMPLETE, STAGE_STATUS_CURRENT, STAGE_STATUS_UPCOMING,
    progress_percent, completed_count, current_stage,
)

STAGE_UPLOAD = "upload"
STAGE_PUSH   = "push"

_STAGES = [
    (STAGE_UPLOAD, "Upload vendor list",        "Upload"),
    (STAGE_PUSH,   "Push to QuickBooks",        "Push"),
]


def build_vendor_stages(
    vendor_job: dict,
    *,
    url_for: Optional[Callable[..., str]] = None,
    connected: bool = False,
) -> List[WorkflowStage]:
    """Build the 2 vendor workflow stages from the vendor job state.

    Args:
        vendor_job: the job dict (from db / jobs cache), must have report_type==vendor_list.
        url_for: Flask url_for, optional (for unit tests).
        connected: True if the firm has ≥1 QBO connection (auto-detected or via OAuth).
    """
    job_id = vendor_job.get("id") or vendor_job.get("job_id", "")

    def u(endpoint: str, fallback: str, **kw) -> str:
        if url_for is None:
            return fallback
        try:
            return url_for(endpoint, **kw)
        except Exception:
            return fallback

    # Detect file upload: job exists + has parsed_vendor_list or encrypted_file
    uploaded = bool(job_id) and bool(vendor_job.get("parsed_vendor_list") or vendor_job.get("encrypted_file"))
    # Detect push completion
    pushed = bool(vendor_job.get("vendor_details_pushed"))

    # Compute raw status per stage
    raw = {
        STAGE_UPLOAD: STAGE_STATUS_COMPLETE if uploaded else STAGE_STATUS_UPCOMING,
        STAGE_PUSH:   STAGE_STATUS_COMPLETE if pushed   else STAGE_STATUS_UPCOMING,
    }

    # Sequential gating: once a non-complete stage is hit, all later ones become upcoming
    blocked = False
    for key, _, _ in _STAGES:
        if blocked:
            raw[key] = STAGE_STATUS_UPCOMING
        elif raw[key] != STAGE_STATUS_COMPLETE:
            blocked = True

    # First non-complete stage becomes "current"
    current_key = next(
        (k for k, _, _ in _STAGES if raw[k] != STAGE_STATUS_COMPLETE),
        None,
    )

    # Nav URLs (for completed + current stages only)
    nav_urls = {
        STAGE_UPLOAD: u("job_detail", f"/jobs/{job_id}", job_id=job_id),
        STAGE_PUSH:   u("job_detail", f"/jobs/{job_id}", job_id=job_id),
    }

    # CTA for the current stage
    # Step 2 CTA changes based on connection: if connected → "Push vendor details",
    # else → "Connect to QuickBooks" (connect_qbo route)
    cta_map = {
        STAGE_UPLOAD: ("Step 2: Push vendor details", u("start_vendor_push", f"/jobs/{job_id}/vendor-push", job_id=job_id)),
        STAGE_PUSH:   (
            ("Push vendor details", u("start_vendor_push", f"/jobs/{job_id}/vendor-push", job_id=job_id))
            if connected
            else ("Connect to QuickBooks", u("connect_qbo", f"/connect-qbo?job_id={job_id}"))
        ),
    }

    # Back label (vendor stepper is simple: just "Back to dashboard")
    back_map = {
        STAGE_UPLOAD: ("Back to dashboard", u("migration_nexus", "/migration")),
        STAGE_PUSH:   ("Back to dashboard", u("migration_nexus", "/migration")),
    }

    stages: List[WorkflowStage] = []
    total = len(_STAGES)
    for i, (key, label, short) in enumerate(_STAGES, start=1):
        status = raw[key]
        is_current = (key == current_key)
        if is_current:
            status = STAGE_STATUS_CURRENT

        cta_label, cta_url = cta_map.get(key, ("", "")) if is_current else ("", "")
        back_label, back_url = back_map.get(key, ("", "")) if is_current else ("", "")
        nav_url = nav_urls.get(key, "") if status in (STAGE_STATUS_COMPLETE, STAGE_STATUS_CURRENT) else ""

        stages.append(WorkflowStage(
            key=key,
            label=label,
            short_label=short,
            description="",
            status=status,
            index=i,
            total=total,
            cta_label=cta_label,
            cta_url=cta_url,
            back_label=back_label,
            back_url=back_url,
            nav_url=nav_url,
        ))
    return stages


def vendor_stages_context(stages: List[WorkflowStage]) -> dict:
    """Return the workflow_* template context keys for _workflow_stepper.html."""
    current = current_stage(stages)
    return {
        "workflow_stages": [s.to_dict() for s in stages],
        "workflow_current": current.to_dict() if current else None,
        "workflow_progress": progress_percent(stages),
        "workflow_completed": completed_count(stages),
    }
