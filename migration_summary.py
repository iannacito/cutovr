"""Customer-facing migration summary / completion report.

This module produces the final "what just happened with your migration"
view: a single, plain-English projection over everything we've already
persisted about a firm's PCLaw → QuickBooks Online migration. It lives
at the end of the customer workflow (after Reconcile) and tells the
firm, in language a non-accountant can follow:

  * which PCLaw reports they sent us;
  * what we created in QuickBooks (accounts, journal entries, etc.);
  * which balance checks passed, which didn't run, which need review;
  * what still needs the firm's attention; and
  * what the recommended next step is.

Design notes:

- This is a *pure projection*. We never read the database directly,
  never call QBO, and never run heuristics that aren't already encoded
  somewhere upstream. The Flask handler hydrates jobs / cutover /
  imports / mappings / bulk uploads from their respective stores and
  hands them in. That keeps the page cheap to render and trivial to
  unit-test.

- We never echo secrets, raw tokens, refresh-token timestamps, file
  contents, or `intuit_tid` values into the customer view. The Flask
  route is the one place where any operator-only debug info could be
  threaded in behind an explicit disclosure (we keep that surface
  empty here).

- Counts come straight from `import_history` rows and the per-job
  blobs persisted via `app_db.save_job_state`. When a count is
  unknown (e.g. the firm hasn't run the corresponding step yet) we
  show "not run yet" rather than 0, so customers can tell the
  difference between "we did the check and it found nothing" and
  "we haven't done the check at all".

- The CSV export is built from the same `MigrationSummary` object the
  HTML view renders. It contains only status fields (counts,
  pass/fail/needs-attention labels, totals) — never row-level
  transaction data, file paths, encryption keys, or raw API responses.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional, Dict, Any

from csv_safety import sanitize_csv_row
from cutover_workflow import (
    ChecklistItem,
    STATUS_COMPLETE,
    STATUS_IN_PROGRESS,
    STATUS_NOT_STARTED,
    STEP_COA_UPLOAD,
    STEP_OPENING_TB,
    STEP_GL_UPLOAD,
    STEP_ENDING_TB,
    STEP_TRUST_LISTING,
    STEP_PROD_IMPORT,
    STEP_RECONCILIATION,
)


# Customer-facing status labels. Keep these stable — the template and
# the CSV export both key off them, and they're what shows up in the
# big status pill at the top of the page.
STATE_NOT_STARTED = "not_started"     # no upload, no run
STATE_RECEIVED = "received"            # uploaded but not yet processed
STATE_READY_FOR_REVIEW = "ready_for_review"
STATE_IMPORTED = "imported"            # posted to QBO
STATE_PASS = "pass"                    # a check passed
STATE_NOT_RUN = "not_run"              # a check that hasn't run yet
STATE_NEEDS_ATTENTION = "needs_attention"
STATE_COMPLETED = "completed"          # whole migration finished


# Friendly labels for the state pills. Keep them short — they show
# next to section headings, not in sentences.
STATE_LABELS = {
    STATE_NOT_STARTED:       "Not started",
    STATE_RECEIVED:          "Received",
    STATE_READY_FOR_REVIEW:  "Ready for review",
    STATE_IMPORTED:          "Imported",
    STATE_PASS:              "Looks good",
    STATE_NOT_RUN:           "Not run yet",
    STATE_NEEDS_ATTENTION:   "Needs attention",
    STATE_COMPLETED:         "Completed",
}


# Plain-English names for each PCLaw report type that we receive.
# Keys mirror `bulk_upload.REPORT_*` / `job["report_type"]`. We map
# accounting jargon to the calmer phrasing used everywhere else in the
# customer UI; the accounting term itself is kept as secondary copy in
# the template, not the primary heading.
REPORT_LABELS = {
    "chart_of_accounts":  ("Account list",         "Chart of Accounts"),
    "trial_balance":      ("Starting & final balances",
                           "Opening / Ending Trial Balance"),
    "general_ledger":     ("Transaction history",  "General Ledger"),
    "trust_listing":      ("Client trust balances", "Trust Listing"),
}


@dataclass
class FileSection:
    """One row in the "Files received" section.

    `report_type` is the canonical key (e.g. ``"general_ledger"``).
    `count` is how many uploads of this type we have on file. `state`
    drives the badge color in the template.
    """
    report_type: str
    label: str
    accounting_label: str
    count: int
    state: str
    detail: str = ""
    latest_company: str = ""
    latest_uploaded_at: str = ""

    def to_dict(self) -> dict:
        return {
            "report_type": self.report_type,
            "label": self.label,
            "accounting_label": self.accounting_label,
            "count": self.count,
            "state": self.state,
            "state_label": STATE_LABELS.get(self.state, self.state),
            "detail": self.detail,
            "latest_company": self.latest_company,
            "latest_uploaded_at": self.latest_uploaded_at,
        }


@dataclass
class QboActivity:
    accounts_created: int = 0
    journal_entries_created: int = 0
    journal_entries_reversed: int = 0
    customers_created: int = 0
    vendors_created: int = 0
    last_import_at: Optional[str] = None
    last_company_name: Optional[str] = None

    @property
    def any_activity(self) -> bool:
        return any([
            self.accounts_created,
            self.journal_entries_created,
            self.journal_entries_reversed,
            self.customers_created,
            self.vendors_created,
        ])

    def to_dict(self) -> dict:
        return {
            "accounts_created": self.accounts_created,
            "journal_entries_created": self.journal_entries_created,
            "journal_entries_reversed": self.journal_entries_reversed,
            "customers_created": self.customers_created,
            "vendors_created": self.vendors_created,
            "last_import_at": self.last_import_at,
            "last_company_name": self.last_company_name,
            "any_activity": self.any_activity,
        }


@dataclass
class BalanceCheck:
    """One of the balance reconciliations we run.

    `state` is one of STATE_PASS / STATE_NEEDS_ATTENTION / STATE_NOT_RUN.
    The template renders these as Looks good / Needs attention / Not
    run yet. We avoid the word "fail" in the customer-facing surface;
    a mismatch typically means a mapping was wrong, not that the user
    did something wrong.
    """
    key: str
    label: str
    accounting_label: str
    state: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "accounting_label": self.accounting_label,
            "state": self.state,
            "state_label": STATE_LABELS.get(self.state, self.state),
            "detail": self.detail,
        }


@dataclass
class AttentionItem:
    """Something the firm should look at before calling the migration
    finished. These show up in the "Items needing attention" card and
    are the things a human still has to decide on or correct.
    """
    key: str
    label: str
    detail: str = ""
    cta_label: str = ""
    cta_url: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "detail": self.detail,
            "cta_label": self.cta_label,
            "cta_url": self.cta_url,
        }


@dataclass
class MigrationSummary:
    firm_name: str
    overall_state: str
    headline: str
    subhead: str
    generated_at: str
    cutover_date: Optional[str] = None
    opening_balance_date: Optional[str] = None
    qbo_company_name: Optional[str] = None
    qbo_realm_id: Optional[str] = None
    files: List[FileSection] = field(default_factory=list)
    qbo: QboActivity = field(default_factory=QboActivity)
    balance_checks: List[BalanceCheck] = field(default_factory=list)
    attention: List[AttentionItem] = field(default_factory=list)
    next_step_label: str = ""
    next_step_detail: str = ""
    next_step_url: str = ""
    last_import_id: Optional[int] = None
    has_jobs: bool = False

    def to_dict(self) -> dict:
        return {
            "firm_name": self.firm_name,
            "overall_state": self.overall_state,
            "overall_state_label": STATE_LABELS.get(
                self.overall_state, self.overall_state
            ),
            "headline": self.headline,
            "subhead": self.subhead,
            "generated_at": self.generated_at,
            "cutover_date": self.cutover_date,
            "opening_balance_date": self.opening_balance_date,
            "qbo_company_name": self.qbo_company_name,
            "qbo_realm_id": self.qbo_realm_id,
            "files": [f.to_dict() for f in self.files],
            "qbo": self.qbo.to_dict(),
            "balance_checks": [b.to_dict() for b in self.balance_checks],
            "attention": [a.to_dict() for a in self.attention],
            "next_step_label": self.next_step_label,
            "next_step_detail": self.next_step_detail,
            "next_step_url": self.next_step_url,
            "last_import_id": self.last_import_id,
            "has_jobs": self.has_jobs,
        }


# ---------------------------------------------------------------------------
# Helpers — projection from raw job rows / import history into summary parts
# ---------------------------------------------------------------------------

def _safe_str(v) -> str:
    return "" if v is None else str(v)


def _short_date(v) -> str:
    """Trim an ISO timestamp down to YYYY-MM-DD for display. Returns the
    raw value if it doesn't look like an ISO date so we never silently
    truncate something unexpected."""
    s = _safe_str(v)
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return s


def _file_section(
    jobs: Iterable[dict],
    report_type: str,
) -> FileSection:
    """Roll up uploads of a given report_type into one FileSection."""
    matching = [
        j for j in jobs
        if (j.get("report_type") or
            ("general_ledger" if report_type == "general_ledger" else "")) == report_type
    ]
    # For GL the fallback is needed because legacy rows had no report_type
    # field; for everything else we don't want to over-attribute.
    if report_type != "general_ledger":
        matching = [j for j in jobs if (j.get("report_type") or "") == report_type]

    label, accounting_label = REPORT_LABELS.get(
        report_type, (report_type, report_type)
    )

    if not matching:
        return FileSection(
            report_type=report_type,
            label=label,
            accounting_label=accounting_label,
            count=0,
            state=STATE_NOT_STARTED,
            detail="No upload yet.",
        )

    latest = matching[0]
    latest_company = _safe_str(latest.get("company"))
    latest_uploaded_at = _short_date(latest.get("created_at"))

    # Was anything imported? GL is the canonical case; for everything
    # else we look at known per-type post markers.
    imported = False
    detail = ""

    if report_type == "general_ledger":
        imported = any(
            "imported" in (j.get("status") or "").lower() for j in matching
        )
        if imported:
            n = sum(
                1 for j in matching
                if "imported" in (j.get("status") or "").lower()
            )
            detail = f"{n} of {len(matching)} sent to QuickBooks."
        else:
            detail = f"{len(matching)} on file; not yet sent to QuickBooks."
    elif report_type == "chart_of_accounts":
        # Count `coa_create_history` entries summed across uploads.
        history = [
            h for j in matching for h in (j.get("coa_create_history") or [])
        ]
        created = sum(int(h.get("created_count") or 0) for h in history)
        if created:
            imported = True
            detail = f"{created} account(s) created in QuickBooks."
        else:
            detail = f"{len(matching)} on file; ready to review."
    elif report_type == "trial_balance":
        opening_je_posted = [
            h for j in matching for h in (j.get("opening_balance_history") or [])
            if not h.get("demo_mode") and h.get("qbo_je_id")
        ]
        ending_built = any(j.get("ending_tb_reconciliation") for j in matching)
        if opening_je_posted and ending_built:
            imported = True
            detail = "Starting balances posted; final balance check built."
        elif opening_je_posted:
            imported = True
            detail = "Starting balances posted to QuickBooks."
        elif ending_built:
            detail = "Final balance check built; starting balance not posted yet."
        else:
            detail = f"{len(matching)} on file; ready to review."
    elif report_type == "trust_listing":
        # Trust posting is deliberately not auto-built. Surface the
        # validation/reconciliation status instead.
        reconciled = any(j.get("trust_reconciliation") for j in matching)
        if reconciled:
            detail = "Trust listing reconciled. Posting trust balances " \
                     "to QuickBooks is a manual step we do with you."
        else:
            detail = f"{len(matching)} on file; ready to reconcile."
    else:
        detail = f"{len(matching)} on file."

    if imported:
        state = STATE_IMPORTED
    elif len(matching) > 0:
        state = STATE_READY_FOR_REVIEW
    else:
        state = STATE_RECEIVED

    return FileSection(
        report_type=report_type,
        label=label,
        accounting_label=accounting_label,
        count=len(matching),
        state=state,
        detail=detail,
        latest_company=latest_company,
        latest_uploaded_at=latest_uploaded_at,
    )


def _qbo_activity(jobs: Iterable[dict], imports: Iterable[dict]) -> QboActivity:
    """Aggregate QBO write activity across jobs + import_history."""
    accounts = 0
    je_created = 0
    je_reversed = 0
    customers = 0
    vendors = 0
    last_import_at: Optional[str] = None
    last_company: Optional[str] = None

    for j in jobs:
        for h in (j.get("coa_create_history") or []):
            accounts += int(h.get("created_count") or 0)
        for h in (j.get("opening_balance_history") or []):
            if not h.get("demo_mode") and h.get("qbo_je_id"):
                je_created += 1
        # qbo_results carries per-import entity creation tallies that
        # the upload pipeline writes back after a successful POST.
        qr = j.get("qbo_results") or {}
        customers += int(qr.get("customers_created") or 0)
        vendors += int(qr.get("vendors_created") or 0)

    for imp in imports:
        # Only successful imports add JEs; reversals are tracked
        # separately so we can show "X created, Y reversed".
        if imp.get("status") == "success":
            je_created += int(imp.get("transaction_count") or 0)
        rev = imp.get("reversal")
        if rev and rev.get("status") == "success":
            je_reversed += int(imp.get("transaction_count") or 0)
        ts = imp.get("created_at")
        if ts and (last_import_at is None or ts > last_import_at):
            last_import_at = ts
            last_company = imp.get("company_name") or last_company

    return QboActivity(
        accounts_created=accounts,
        journal_entries_created=je_created,
        journal_entries_reversed=je_reversed,
        customers_created=customers,
        vendors_created=vendors,
        last_import_at=_short_date(last_import_at) if last_import_at else None,
        last_company_name=last_company,
    )


def _balance_checks(jobs: Iterable[dict]) -> List[BalanceCheck]:
    """Roll up the balance / reconciliation checks we expose."""
    tb_jobs = [j for j in jobs if (j.get("report_type") or "") == "trial_balance"]
    trust_jobs = [j for j in jobs if (j.get("report_type") or "") == "trust_listing"]
    gl_jobs = [j for j in jobs
               if (j.get("report_type") or "general_ledger") == "general_ledger"]

    checks: List[BalanceCheck] = []

    # Starting balances — did we post an opening JE?
    opening_posted = [
        h for j in tb_jobs for h in (j.get("opening_balance_history") or [])
        if not h.get("demo_mode") and h.get("qbo_je_id")
    ]
    if opening_posted:
        checks.append(BalanceCheck(
            key="opening_balance",
            label="Starting balances",
            accounting_label="Opening Trial Balance",
            state=STATE_PASS,
            detail="Opening journal entry posted to QuickBooks.",
        ))
    elif tb_jobs:
        checks.append(BalanceCheck(
            key="opening_balance",
            label="Starting balances",
            accounting_label="Opening Trial Balance",
            state=STATE_NEEDS_ATTENTION,
            detail="Trial balance uploaded but the starting balance journal "
                   "entry has not been posted yet.",
        ))
    else:
        checks.append(BalanceCheck(
            key="opening_balance",
            label="Starting balances",
            accounting_label="Opening Trial Balance",
            state=STATE_NOT_RUN,
            detail="Upload your opening trial balance to run this check.",
        ))

    # Final balance check — ending TB reconciliation.
    ending_recon = [j for j in tb_jobs if j.get("ending_tb_reconciliation")]
    if ending_recon:
        # If the recon has been built we treat it as Looks good. The
        # recon module itself surfaces line-level differences; this
        # summary page just tells the customer that the check ran.
        # If any of the recon blobs reports a non-zero variance, flip
        # to needs_attention.
        has_variance = False
        for j in ending_recon:
            recon = j.get("ending_tb_reconciliation") or {}
            if recon.get("has_differences") or recon.get("variance_total"):
                has_variance = True
                break
        checks.append(BalanceCheck(
            key="ending_balance",
            label="Final balance check",
            accounting_label="Ending Trial Balance",
            state=STATE_NEEDS_ATTENTION if has_variance else STATE_PASS,
            detail="Differences found — open the reconciliation report."
                   if has_variance else
                   "Final balances reconcile with PCLaw.",
        ))
    elif len(tb_jobs) >= 2:
        checks.append(BalanceCheck(
            key="ending_balance",
            label="Final balance check",
            accounting_label="Ending Trial Balance",
            state=STATE_NEEDS_ATTENTION,
            detail="Final trial balance uploaded but the reconciliation "
                   "report has not been built yet.",
        ))
    else:
        checks.append(BalanceCheck(
            key="ending_balance",
            label="Final balance check",
            accounting_label="Ending Trial Balance",
            state=STATE_NOT_RUN,
            detail="Upload the final trial balance after import to run this "
                   "check.",
        ))

    # Client trust balances — trust listing reconciliation.
    trust_recon = [j for j in trust_jobs if j.get("trust_reconciliation")]
    if trust_recon:
        has_variance = False
        for j in trust_recon:
            recon = j.get("trust_reconciliation") or {}
            if recon.get("has_differences") or recon.get("variance_total"):
                has_variance = True
                break
        checks.append(BalanceCheck(
            key="trust_balances",
            label="Client trust balances",
            accounting_label="Trust Listing",
            state=STATE_NEEDS_ATTENTION if has_variance else STATE_PASS,
            detail="Differences found — open the trust reconciliation report."
                   if has_variance else
                   "Trust listing totals reconcile with the trust bank account.",
        ))
    elif trust_jobs:
        checks.append(BalanceCheck(
            key="trust_balances",
            label="Client trust balances",
            accounting_label="Trust Listing",
            state=STATE_NEEDS_ATTENTION,
            detail="Trust listing uploaded but the reconciliation has not "
                   "been built yet.",
        ))
    else:
        checks.append(BalanceCheck(
            key="trust_balances",
            label="Client trust balances",
            accounting_label="Trust Listing",
            state=STATE_NOT_RUN,
            detail="Upload your client trust listing to run this check.",
        ))

    # Per-import verification — `verification` blob on a GL job that's
    # been imported.
    verifications = [j.get("verification") for j in gl_jobs
                     if j.get("verification")]
    if verifications:
        ok = all((v or {}).get("status") == "ok" for v in verifications)
        checks.append(BalanceCheck(
            key="import_verification",
            label="Transaction totals match",
            accounting_label="Import verification",
            state=STATE_PASS if ok else STATE_NEEDS_ATTENTION,
            detail="Debits and credits in QuickBooks match what we sent."
                   if ok else
                   "QuickBooks totals differ from what we sent. Open the "
                   "verification report.",
        ))

    return checks


def _attention_items(
    jobs: List[dict],
    bulks: List[dict],
    checklist: List[ChecklistItem],
    *,
    checklist_url: str = "",
    imports_url: str = "",
    dashboard_url: str = "",
) -> List[AttentionItem]:
    """Items the firm needs to look at before calling it done.

    Sources:
      - per-job ``unmapped_accounts`` blobs (account mapping missing);
      - per-job ``last_error`` blobs (import failure);
      - bulk-upload classification status (unknown / duplicate);
      - checklist items still in progress / not started but flagged
        as required (e.g. missing files).
    """
    out: List[AttentionItem] = []

    # Unmatched accounts
    unmapped_total = 0
    for j in jobs:
        um = j.get("unmapped_accounts") or []
        if isinstance(um, list):
            unmapped_total += len(um)
        elif isinstance(um, dict):
            unmapped_total += int(um.get("count") or 0)
    if unmapped_total:
        out.append(AttentionItem(
            key="unmatched_accounts",
            label=f"{unmapped_total} unmatched account(s)",
            detail="Some PCLaw accounts aren't paired with a QuickBooks "
                   "account yet. Open the matching screen to fix this.",
            cta_label="Open account matching",
            cta_url=checklist_url,
        ))

    # Import errors
    errored = [j for j in jobs if j.get("last_error")]
    if errored:
        sample = errored[0].get("last_error") or {}
        msg = sample.get("message") or sample.get("code") or "Unknown error"
        out.append(AttentionItem(
            key="import_errors",
            label=f"{len(errored)} job(s) had an import error",
            detail=f"Most recent error: {msg}. Open the job to retry.",
            cta_label="Open imports",
            cta_url=imports_url,
        ))

    # Unknown files / duplicates from bulk uploads
    unknown = 0
    duplicates = 0
    for bulk in bulks:
        for r in (bulk.get("results") or []):
            status = r.get("status") or ""
            if status in ("needs_review", "unreadable"):
                unknown += 1
            elif status == "duplicate":
                duplicates += 1
    if unknown:
        out.append(AttentionItem(
            key="unknown_files",
            label=f"{unknown} uploaded file(s) we couldn't identify",
            detail="Open the bulk-upload review and tell us what each "
                   "file is, then re-upload.",
            cta_label="Open bulk upload",
            cta_url=dashboard_url,
        ))
    if duplicates:
        out.append(AttentionItem(
            key="duplicate_files",
            label=f"{duplicates} duplicate report(s) uploaded",
            detail="More than one file of the same type was sent in a "
                   "single batch. Keep the latest export of each report.",
            cta_label="Open bulk upload",
            cta_url=dashboard_url,
        ))

    # Missing files (required steps still at not_started). We only
    # surface this as an *attention* item once the firm has started —
    # otherwise the empty-state would look like "you have a problem"
    # rather than "you haven't started yet". "Started" here means
    # at least one job uploaded OR at least one bulk classified.
    if jobs or bulks:
        required_files = {
            STEP_COA_UPLOAD: "your account list",
            STEP_OPENING_TB: "your starting balances",
            STEP_GL_UPLOAD: "your transaction history",
        }
        missing = []
        by_key = {item.key: item for item in checklist}
        for step, friendly in required_files.items():
            item = by_key.get(step)
            if item and item.status == STATUS_NOT_STARTED:
                missing.append(friendly)
        if missing:
            out.append(AttentionItem(
                key="missing_files",
                label=f"{len(missing)} file(s) not uploaded yet",
                detail="Still missing: " + ", ".join(missing) + ".",
                cta_label="Go to upload",
                cta_url=dashboard_url,
            ))

    # Failed checks — surfaced from balance_checks if any are
    # needs_attention. The handler re-injects this if appropriate.

    return out


def _overall_state_and_headline(
    files: List[FileSection],
    qbo: QboActivity,
    balance_checks: List[BalanceCheck],
    attention: List[AttentionItem],
    checklist: List[ChecklistItem],
) -> tuple:
    """Pick the top-of-page status pill + headline + subhead.

    Priority order:
      1. Anything in ``attention`` → Needs attention.
      2. Reconciliation step complete in checklist → Completed.
      3. At least one successful import → Imported.
      4. Any uploads received → Ready for review.
      5. Otherwise → Not started.
    """
    if attention:
        return (
            STATE_NEEDS_ATTENTION,
            "A few things still need your attention",
            "Your migration is most of the way there. Resolve the items "
            "below to wrap up.",
        )

    by_key = {item.key: item for item in checklist}
    recon = by_key.get(STEP_RECONCILIATION)
    prod = by_key.get(STEP_PROD_IMPORT)
    if recon and recon.status == STATUS_COMPLETE:
        return (
            STATE_COMPLETED,
            "Your migration is complete",
            "Everything we can check has been checked. Save this summary "
            "for your records.",
        )

    if prod and prod.status == STATUS_COMPLETE or qbo.journal_entries_created:
        return (
            STATE_IMPORTED,
            "Your records are in QuickBooks",
            "We've posted your migration to QuickBooks. Take a look in "
            "QuickBooks to confirm everything reads correctly, then come "
            "back here to run the final balance check.",
        )

    received = [f for f in files if f.count > 0]
    if received:
        return (
            STATE_READY_FOR_REVIEW,
            "Your reports are with us",
            "We have your PCLaw exports and they're ready for review. "
            "Pick up where you left off when you're ready.",
        )

    return (
        STATE_NOT_STARTED,
        "Your migration hasn't started yet",
        "Upload your PCLaw reports to get going. We'll walk you through "
        "the rest from there.",
    )


def _next_step(
    overall_state: str,
    attention: List[AttentionItem],
    checklist_url: str,
    dashboard_url: str,
) -> tuple:
    """One dominant next-action for the page, in plain English."""
    if overall_state == STATE_COMPLETED:
        return (
            "Review the results in QuickBooks",
            "Open QuickBooks and confirm the balances, then save this "
            "summary or the audit CSV for your records.",
            checklist_url,
        )
    if attention:
        first = attention[0]
        return (
            first.cta_label or "Open the checklist",
            first.detail,
            first.cta_url or checklist_url,
        )
    if overall_state == STATE_IMPORTED:
        return (
            "Run the final balance check",
            "Upload your ending trial balance and confirm QuickBooks "
            "matches PCLaw.",
            checklist_url,
        )
    if overall_state == STATE_READY_FOR_REVIEW:
        return (
            "Open the migration checklist",
            "Pick up the next step from the checklist.",
            checklist_url,
        )
    return (
        "Upload your PCLaw reports",
        "Send us your PCLaw exports to get started.",
        dashboard_url,
    )


def build_migration_summary(
    *,
    firm: dict,
    cutover: Optional[dict],
    jobs: Iterable[dict],
    imports: Iterable[dict],
    bulks: Iterable[dict],
    qbo_connections: Iterable[dict],
    checklist: Iterable[ChecklistItem],
    checklist_url: str = "",
    imports_url: str = "",
    dashboard_url: str = "",
    now: Optional[datetime] = None,
) -> MigrationSummary:
    """Project everything we already know about a firm's migration into
    a single, customer-facing summary object.

    All inputs are *already-persisted* state — the caller hydrates them
    from `app_db`, `import_history`, and the in-memory bulk-upload
    cache and hands them in. This keeps the projection trivial to
    unit-test and means we never re-query state we already know.
    """
    jobs = list(jobs)
    imports = list(imports)
    bulks = list(bulks)
    checklist = list(checklist)
    conns = list(qbo_connections)

    file_sections = [
        _file_section(jobs, "chart_of_accounts"),
        _file_section(jobs, "trial_balance"),
        _file_section(jobs, "general_ledger"),
        _file_section(jobs, "trust_listing"),
    ]
    qbo = _qbo_activity(jobs, imports)
    balance_checks = _balance_checks(jobs)
    attention = _attention_items(
        jobs, bulks, checklist,
        checklist_url=checklist_url,
        imports_url=imports_url,
        dashboard_url=dashboard_url,
    )
    overall_state, headline, subhead = _overall_state_and_headline(
        file_sections, qbo, balance_checks, attention, checklist,
    )
    next_label, next_detail, next_url = _next_step(
        overall_state, attention, checklist_url, dashboard_url,
    )

    qbo_company = None
    qbo_realm_id = None
    if conns:
        # Prefer the most-recently-updated connection if multiple are
        # present. The DB layer returns them ordered already; we just
        # take the first as the canonical "current" QBO company.
        primary = conns[0]
        qbo_company = primary.get("company_name") or primary.get("legal_name")
        qbo_realm_id = primary.get("realm_id")

    last_import_id = None
    for imp in imports:
        if imp.get("status") == "success":
            last_import_id = imp.get("id")
            break  # imports come ordered newest-first

    return MigrationSummary(
        firm_name=firm.get("name") or "Your firm",
        overall_state=overall_state,
        headline=headline,
        subhead=subhead,
        generated_at=(now or datetime.utcnow()).strftime("%Y-%m-%d %H:%M UTC"),
        cutover_date=(cutover or {}).get("cutover_date"),
        opening_balance_date=(cutover or {}).get("opening_balance_date"),
        qbo_company_name=qbo_company,
        qbo_realm_id=qbo_realm_id,
        files=file_sections,
        qbo=qbo,
        balance_checks=balance_checks,
        attention=attention,
        next_step_label=next_label,
        next_step_detail=next_detail,
        next_step_url=next_url,
        last_import_id=last_import_id,
        has_jobs=bool(jobs),
    )


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def summary_to_csv(summary: MigrationSummary) -> str:
    """Render a customer-facing CSV of the summary.

    Only contains status fields (counts, pass/fail labels, dates).
    Never row-level transactions, file paths, or anything sensitive.
    Cells are sanitized with `csv_safety.sanitize_csv_row` so that
    spreadsheet apps don't interpret attacker-controlled text as a
    formula.
    """
    buf = io.StringIO()
    w = csv.writer(buf)

    def row(*values):
        w.writerow(sanitize_csv_row(list(values)))

    row("PCLaw Migrate — Migration summary")
    row("Generated at", summary.generated_at)
    row("Firm", summary.firm_name)
    row("Status", STATE_LABELS.get(summary.overall_state, summary.overall_state))
    if summary.cutover_date:
        row("Cutover date", summary.cutover_date)
    if summary.opening_balance_date:
        row("Opening balance date", summary.opening_balance_date)
    if summary.qbo_company_name:
        row("QuickBooks company", summary.qbo_company_name)
    row("")

    row("Files received")
    row("Report", "Accounting term", "Uploads", "Status", "Notes")
    for f in summary.files:
        row(f.label, f.accounting_label, f.count,
            STATE_LABELS.get(f.state, f.state), f.detail)
    row("")

    row("QuickBooks activity")
    row("Accounts created", summary.qbo.accounts_created)
    row("Journal entries created", summary.qbo.journal_entries_created)
    row("Journal entries reversed", summary.qbo.journal_entries_reversed)
    row("Customers created", summary.qbo.customers_created)
    row("Vendors created", summary.qbo.vendors_created)
    if summary.qbo.last_import_at:
        row("Most recent import", summary.qbo.last_import_at)
    row("")

    row("Balance checks")
    row("Check", "Accounting term", "Status", "Notes")
    for b in summary.balance_checks:
        row(b.label, b.accounting_label,
            STATE_LABELS.get(b.state, b.state), b.detail)
    row("")

    if summary.attention:
        row("Items needing attention")
        row("Item", "Detail")
        for a in summary.attention:
            row(a.label, a.detail)
        row("")

    row("Recommended next step")
    row("Action", summary.next_step_label)
    if summary.next_step_detail:
        row("Detail", summary.next_step_detail)

    return buf.getvalue()
