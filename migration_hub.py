"""Migration Hub: GL-by-GL processing overview.

The original stepper assumes a firm posts *one* general ledger in a
single pass. Real migrations upload several monthly general ledgers, and
each one surfaces its own edge cases — an unmatched account here, a
missing customer name there. When every GL shares one monolithic stepper,
a single blocked GL hides the status of all the others and the operator
can't tell which file needs what.

The Migration Hub is a per-GL board. Each active GL upload becomes a card
with a clear status, the specific blockers holding it back, and a single
"open this GL" action. Operators work the list top to bottom, resolving
edge cases one ledger at a time, while the stepper still exists for the
common single-GL path.

This module is a **pure projection** over already-hydrated job dicts and a
handful of firm-level facts (QBO connected, account-mapping count). It
performs no I/O and no QuickBooks writes, so the status logic is unit
testable without a database or a live QBO connection.

Status vocabulary (per GL):

  uploaded   file accepted, not yet validated / no preflight yet
  validated  preflight ran and the file parses, but it isn't import-ready
             (e.g. accounts not matched, or QBO not connected yet)
  ready      validated, accounts matched, QBO connected — safe to post
  blocked    something concrete must be fixed first (unmapped accounts,
             missing entity names, preflight problem rows)
  imported   posted to QuickBooks (has an import_summary)
  failed     the upload itself errored (couldn't parse)
  superseded replaced by a newer upload of the same type
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import demo_mode

STATUS_UPLOADED = "uploaded"
STATUS_VALIDATED = "validated"
STATUS_READY = "ready"
STATUS_BLOCKED = "blocked"
STATUS_IMPORTED = "imported"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"

# Plain-English label + a one-line, lawyer-friendly explanation for each
# status. Operators get the technical blocker list separately.
STATUS_LABELS: Dict[str, str] = {
    STATUS_UPLOADED: "Uploaded",
    STATUS_VALIDATED: "Checked",
    STATUS_READY: "Ready to send",
    STATUS_BLOCKED: "Needs attention",
    STATUS_IMPORTED: "Sent to QuickBooks",
    STATUS_FAILED: "Couldn't read file",
    STATUS_SUPERSEDED: "Replaced",
}

STATUS_BLURBS: Dict[str, str] = {
    STATUS_UPLOADED: "Received — we'll check it next.",
    STATUS_VALIDATED: "Checked and parsed. A few setup steps remain before it can post.",
    STATUS_READY: "Everything checks out. This ledger is ready to send to QuickBooks.",
    STATUS_BLOCKED: "One or two things need a fix before this ledger can post.",
    STATUS_IMPORTED: "This ledger's transactions are in QuickBooks.",
    STATUS_FAILED: "We couldn't read this file. Re-export it from PCLaw and upload again.",
    STATUS_SUPERSEDED: "A newer upload replaced this file. No action needed.",
}


@dataclass
class GLCard:
    """One general-ledger upload as shown on the Migration Hub board."""

    job_id: str
    company: Optional[str]
    status: str
    status_label: str
    status_blurb: str
    blockers: List[str] = field(default_factory=list)
    entity_needs: List[str] = field(default_factory=list)
    line_count: int = 0
    je_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    action_label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "company": self.company,
            "status": self.status,
            "status_label": self.status_label,
            "status_blurb": self.status_blurb,
            "blockers": list(self.blockers),
            "entity_needs": list(self.entity_needs),
            "line_count": self.line_count,
            "je_count": self.je_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "action_label": self.action_label,
        }


def _gl_status_and_blockers(job: dict, *, has_qbo_connection: bool,
                            account_mapping_count: int):
    """Classify a single GL job. Returns (status, blockers, entity_needs)."""
    blockers: List[str] = []
    entity_needs: List[str] = []

    if demo_mode.is_superseded_job(job):
        return STATUS_SUPERSEDED, blockers, entity_needs
    if demo_mode.is_failed_job(job):
        return STATUS_FAILED, blockers, entity_needs
    if job.get("import_summary"):
        return STATUS_IMPORTED, blockers, entity_needs

    # Concrete blockers take precedence — these are the edge cases the hub
    # exists to make visible per GL.
    unmapped = job.get("unmapped_accounts") or []
    if unmapped:
        blockers.append(
            f"{len(unmapped)} account(s) not matched to QuickBooks: "
            + "; ".join(unmapped[:5])
            + ("" if len(unmapped) <= 5 else f" (+{len(unmapped) - 5} more)")
        )

    entity_blockers = (job.get("entity_name_blockers") or {}).get("offenders") or []
    if entity_blockers:
        entity_needs.extend(entity_blockers)
        blockers.append(
            f"{len(entity_blockers)} customer/vendor name(s) missing: "
            + "; ".join(entity_blockers[:5])
            + ("" if len(entity_blockers) <= 5 else f" (+{len(entity_blockers) - 5} more)")
        )

    preflight = job.get("preflight") or {}
    if preflight:
        if preflight.get("beginning_balance_row_count"):
            blockers.append(
                "Contains beginning-balance rows — move these to the opening "
                "trial balance, then re-upload."
            )
        if (
            preflight.get("problem_rows")
            or preflight.get("rows_unparseable_date")
            or preflight.get("rows_missing_date")
            or preflight.get("rows_missing_account")
        ):
            blockers.append("Some rows need a fix before posting (see review step).")
        if preflight.get("line_count") and not preflight.get("balanced", True):
            preview = job.get("preview") or {}
            if not preview.get("balanced", False):
                blockers.append("Debits and credits don't balance.")

    if blockers:
        return STATUS_BLOCKED, blockers, entity_needs

    # No concrete blockers. Distinguish "ready to send" from "checked but
    # setup incomplete" (no QBO connection / no account matches yet).
    has_preflight = bool(preflight)
    if not has_preflight:
        return STATUS_UPLOADED, blockers, entity_needs
    if has_qbo_connection and account_mapping_count > 0:
        return STATUS_READY, blockers, entity_needs
    if not has_qbo_connection:
        blockers.append("Connect QuickBooks to send this ledger.")
    elif account_mapping_count <= 0:
        blockers.append("Match your PCLaw accounts to QuickBooks first.")
    return STATUS_VALIDATED, blockers, entity_needs


def _action_label(status: str) -> str:
    return {
        STATUS_UPLOADED: "Open & check",
        STATUS_VALIDATED: "Open & finish setup",
        STATUS_READY: "Open & send",
        STATUS_BLOCKED: "Open & resolve",
        STATUS_IMPORTED: "View details",
        STATUS_FAILED: "View details",
        STATUS_SUPERSEDED: "View details",
    }.get(status, "Open")


def build_gl_card(job: dict, *, has_qbo_connection: bool,
                  account_mapping_count: int) -> GLCard:
    status, blockers, entity_needs = _gl_status_and_blockers(
        job,
        has_qbo_connection=has_qbo_connection,
        account_mapping_count=account_mapping_count,
    )
    preflight = job.get("preflight") or {}
    import_summary = job.get("import_summary") or {}
    return GLCard(
        job_id=job.get("id"),
        company=job.get("company"),
        status=status,
        status_label=STATUS_LABELS.get(status, status.title()),
        status_blurb=STATUS_BLURBS.get(status, ""),
        blockers=blockers,
        entity_needs=entity_needs,
        line_count=int(preflight.get("line_count") or 0),
        je_count=int(import_summary.get("qbo_je_count") or 0),
        created_at=job.get("created_at"),
        updated_at=job.get("updated_at"),
        action_label=_action_label(status),
    )


def build_hub(jobs: List[dict], *, has_qbo_connection: bool,
              account_mapping_count: int,
              include_superseded: bool = False) -> Dict[str, Any]:
    """Build the Migration Hub view model from a firm's GL jobs.

    ``jobs`` should be the firm's general-ledger jobs (any report_type
    filtering is the caller's job, but non-GL rows are ignored defensively).
    By default superseded GLs are dropped from the board so a re-upload
    doesn't clutter the list, but they are still counted so the operator
    can see history if needed.

    Returns a dict with the ordered cards plus per-status counts so the
    template can render a status summary without re-deriving anything.
    """
    cards: List[GLCard] = []
    counts: Dict[str, int] = {k: 0 for k in STATUS_LABELS}
    for job in jobs:
        if (job.get("report_type") or "general_ledger") != "general_ledger":
            continue
        card = build_gl_card(
            job,
            has_qbo_connection=has_qbo_connection,
            account_mapping_count=account_mapping_count,
        )
        counts[card.status] = counts.get(card.status, 0) + 1
        if card.status == STATUS_SUPERSEDED and not include_superseded:
            continue
        cards.append(card)

    # Sort so the GLs that need a human come first (blocked, then ready,
    # then everything else), newest within each bucket. This is what makes
    # edge cases discoverable rather than buried.
    priority = {
        STATUS_BLOCKED: 0,
        STATUS_READY: 1,
        STATUS_VALIDATED: 2,
        STATUS_UPLOADED: 3,
        STATUS_FAILED: 4,
        STATUS_IMPORTED: 5,
        STATUS_SUPERSEDED: 6,
    }
    cards.sort(
        key=lambda c: (priority.get(c.status, 9), c.updated_at or "", c.created_at or ""),
    )

    active = [c for c in cards if c.status not in (STATUS_SUPERSEDED, STATUS_FAILED)]
    return {
        "cards": cards,
        "counts": counts,
        "total_gls": len(active),
        "blocked_count": counts.get(STATUS_BLOCKED, 0),
        "ready_count": counts.get(STATUS_READY, 0),
        "imported_count": counts.get(STATUS_IMPORTED, 0),
    }
