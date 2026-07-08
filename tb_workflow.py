"""TB-specific 5-stage workflow stepper.

Mirrors customer_workflow.py but built for a single Trial Balance job.
GL uses a multi-job checklist; TB is one job → one opening balance entry.

Stages:
  1. Setup           — firm cutover setup done
  2. Upload          — TB file uploaded (job exists)
  3. Match accounts  — account mappings saved for this TB job's realm
  4. Review          — OB plan has 0 blockers AND plan is balanced
  5. Post & Verify   — opening_balance_history is non-empty (JE posted); reconcile_ob shown when posted
"""

from __future__ import annotations
from typing import Optional, Callable, List
from customer_workflow import (
    WorkflowStage,
    STAGE_STATUS_COMPLETE, STAGE_STATUS_CURRENT, STAGE_STATUS_UPCOMING,
)

STAGE_SETUP     = "setup"
STAGE_UPLOAD    = "upload"
STAGE_MATCH     = "match"
STAGE_REVIEW    = "review"
STAGE_POST      = "post"
STAGE_RECONCILE = "reconcile"

_STAGES = [
    (STAGE_SETUP,  "Set up your migration",               "Setup"),
    (STAGE_UPLOAD, "Upload your Trial Balance",            "Upload"),
    (STAGE_MATCH,  "Match accounts",                       "Match"),
    (STAGE_REVIEW, "Review before posting",                "Review"),
    (STAGE_POST,   "Post & Verify Opening Balance",        "Import"),
]

def build_tb_stages(
    tb_job: dict,
    *,
    url_for: Optional[Callable[..., str]] = None,
    mapping_saved: bool = False,
    plan_ready: bool = False,
    cutover_done: bool = True,
) -> List[WorkflowStage]:
    """Build the 5 TB workflow stages from the TB job state.

    Args:
        tb_job: the job dict (from db / jobs cache), must have report_type==trial_balance.
        url_for: Flask url_for, optional (for unit tests).
        mapping_saved: True if the firm has ≥1 saved account mapping for this realm.
        plan_ready: True if the OB plan has 0 blockers and is balanced.
        cutover_done: True if firm cutover setup has been completed.
    """
    job_id = tb_job.get("id") or tb_job.get("job_id", "")

    def u(endpoint: str, fallback: str, **kw) -> str:
        if url_for is None:
            return fallback
        try:
            return url_for(endpoint, **kw)
        except Exception:
            return fallback

    ob_posted = bool(tb_job.get("opening_balance_history"))

    # Compute raw status per stage
    raw = {
        STAGE_SETUP:   STAGE_STATUS_COMPLETE if cutover_done else STAGE_STATUS_UPCOMING,
        STAGE_UPLOAD:  STAGE_STATUS_COMPLETE if job_id else STAGE_STATUS_UPCOMING,
        STAGE_MATCH:   STAGE_STATUS_COMPLETE if mapping_saved else STAGE_STATUS_UPCOMING,
        STAGE_REVIEW:  STAGE_STATUS_COMPLETE if plan_ready else STAGE_STATUS_UPCOMING,
        STAGE_POST:    STAGE_STATUS_COMPLETE if ob_posted else STAGE_STATUS_UPCOMING,
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
        STAGE_SETUP:   u("cutover_setup", "/cutover"),
        STAGE_UPLOAD:  u("job_detail", f"/jobs/{job_id}", job_id=job_id),
        STAGE_MATCH:   u("ob_account_mapping", f"/jobs/{job_id}/ob-account-mapping", job_id=job_id),
        STAGE_REVIEW:  u("opening_balance_preview", f"/jobs/{job_id}/opening-balance", job_id=job_id),
        STAGE_POST: u("post_ob", f"/jobs/{job_id}/post-ob", job_id=job_id),
    }

    # CTA for the current stage
    cta_map = {
        STAGE_SETUP:   ("Step 2: Upload Trial Balance", u("dashboard", "/dashboard") + "#intake"),
        STAGE_UPLOAD:  ("Match accounts", u("ob_account_mapping", f"/jobs/{job_id}/ob-account-mapping", job_id=job_id)),
        STAGE_MATCH:   ("Review Opening Balance", u("opening_balance_preview", f"/jobs/{job_id}/opening-balance", job_id=job_id)),
        STAGE_REVIEW:  ("Step 5: Post Opening Trial Balance", u("post_ob", f"/jobs/{job_id}/post-ob", job_id=job_id)),
        STAGE_POST:    ("Post Opening Trial Balance", u("post_ob", f"/jobs/{job_id}/post-ob", job_id=job_id) + "#post-ob-card"),
    }

    # Back labels for the current stage
    back_map = {
        STAGE_UPLOAD:  ("Back to Step 1: Setup", u("cutover_setup", "/cutover")),
        STAGE_MATCH:   ("Back to Step 2: Upload reports", u("job_detail", f"/jobs/{job_id}", job_id=job_id)),
        STAGE_REVIEW:  ("Back to Step 3: Match accounts", u("ob_account_mapping", f"/jobs/{job_id}/ob-account-mapping", job_id=job_id)),
        STAGE_POST:    ("Back to Step 3: Match accounts", u("ob_account_mapping", f"/jobs/{job_id}/ob-account-mapping", job_id=job_id)),
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


def tb_stages_context(stages: List[WorkflowStage]) -> dict:
    """Return the workflow_* template context keys for _workflow_stepper.html."""
    from customer_workflow import progress_percent, completed_count, current_stage
    current = current_stage(stages)
    return {
        "workflow_stages": [s.to_dict() for s in stages],
        "workflow_current": current.to_dict() if current else None,
        "workflow_progress": progress_percent(stages),
        "workflow_completed": completed_count(stages),
    }
