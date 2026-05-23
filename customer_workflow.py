"""Customer-facing migration progress stepper.

This module collapses the detailed cutover_workflow checklist (which has
11+ accounting-flavored steps) into a clean 6-stage progress bar that
non-accountants can follow:

  1. Setup           — name the cutover date, country, accounting basis
  2. Upload Reports  — supply the PCLaw exports
  3. Match Accounts  — pair PCLaw accounts to QuickBooks accounts
  4. Review          — preview what would post (dry-run)
  5. Import          — post to QuickBooks
  6. Reconcile       — confirm QBO matches PCLaw, save the audit trail

Each stage exposes:
  - key, label, short_label  (UI strings)
  - description              (one-line customer-friendly summary)
  - status                   "complete" | "current" | "upcoming"
  - cta_label, cta_url       (next-action button — empty for non-current
                              stages)
  - friendly_terms           (dict mapping technical accounting terms to
                              plain-English helper text, used for the
                              "what's inside this stage" tooltip / list)

The mapping from the underlying checklist items to these six stages lives
here so the stepper stays stable even if cutover_workflow.STEP_* ids
change.

Nothing in this module performs QBO writes, reads the database, or
mutates state. It is a pure projection over (checklist_items, job_count,
imported_gl) and the Flask app's url_for callable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Dict, Any

from cutover_workflow import (
    STEP_CUTOVER_SETUP,
    STEP_COA_UPLOAD,
    STEP_OPENING_TB,
    STEP_GL_UPLOAD,
    STEP_ENDING_TB,
    STEP_TRUST_LISTING,
    STEP_QBO_CONNECT,
    STEP_ACCOUNT_MAPPING,
    STEP_DRY_RUN,
    STEP_PROD_IMPORT,
    STEP_RECONCILIATION,
    STATUS_COMPLETE,
    STATUS_IN_PROGRESS,
    STATUS_NOT_STARTED,
    ChecklistItem,
)


# Customer-facing stage keys. Keep stable — templates and tests key off these.
STAGE_SETUP = "setup"
STAGE_UPLOAD = "upload"
STAGE_MATCH = "match"
STAGE_REVIEW = "review"
STAGE_IMPORT = "import"
STAGE_RECONCILE = "reconcile"

STAGE_STATUS_COMPLETE = "complete"
STAGE_STATUS_CURRENT = "current"
STAGE_STATUS_UPCOMING = "upcoming"


# Plain-English explanations of accounting jargon used elsewhere in the app.
# Pair these with the original term wherever a customer might see it so
# the technical word stays accurate but isn't intimidating.
FRIENDLY_TERMS: Dict[str, str] = {
    "Chart of Accounts": "your firm's account list",
    "Opening Trial Balance": "starting balances on switchover day",
    "General Ledger": "your transaction history",
    "Ending Trial Balance": "the final balance check after import",
    "Trust Listing": "client trust balances",
    "A/R": "money clients owe you (accounts receivable)",
    "A/P": "money you owe vendors (accounts payable)",
    "Journal Entry": "a balanced accounting record posted to QuickBooks",
    "Cutover": "the day you switch from PCLaw to QuickBooks Online",
}


@dataclass
class WorkflowStage:
    key: str
    label: str               # full, polished label  ("Upload your reports")
    short_label: str         # used in compact stepper headers ("Upload")
    description: str         # one-sentence customer summary
    status: str              # complete | current | upcoming
    index: int               # 1-based position in the stepper
    total: int               # total number of stages
    cta_label: str = ""
    cta_url: str = ""
    # Back-to-previous-step button (mirror of cta_*). Populated only on
    # the current stage and only when a previous step exists — Step 1 has
    # nothing to go back to so both fields stay empty.
    back_label: str = ""
    back_url: str = ""
    friendly_terms: List[str] = field(default_factory=list)
    detail_items: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.status == STAGE_STATUS_COMPLETE

    @property
    def is_current(self) -> bool:
        return self.status == STAGE_STATUS_CURRENT

    @property
    def is_upcoming(self) -> bool:
        return self.status == STAGE_STATUS_UPCOMING

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "short_label": self.short_label,
            "description": self.description,
            "status": self.status,
            "index": self.index,
            "total": self.total,
            "cta_label": self.cta_label,
            "cta_url": self.cta_url,
            "back_label": self.back_label,
            "back_url": self.back_url,
            "friendly_terms": list(self.friendly_terms),
            "detail_items": list(self.detail_items),
        }


# Mapping from customer-facing stage to the underlying checklist step keys.
# A stage counts as "complete" iff every required step in its bundle is
# complete. A stage is "current" if it is the first non-complete stage.
_STAGE_BUNDLES = [
    (STAGE_SETUP,     "Set up your migration",  "Setup",
     "A few dates and choices so we can plan the rest of the move.",
     [STEP_CUTOVER_SETUP],
     ["Cutover"]),
    (STAGE_UPLOAD,    "Upload your reports",     "Upload",
     "Send your PCLaw exports — your account list, starting balances, "
     "transaction history, and client trust balances.",
     [STEP_COA_UPLOAD, STEP_OPENING_TB, STEP_GL_UPLOAD],
     ["Chart of Accounts", "Opening Trial Balance",
      "General Ledger", "Trust Listing"]),
    (STAGE_MATCH,     "Connect QuickBooks & match accounts", "Match",
     "Link your QuickBooks Online company and pair each PCLaw account "
     "to the right QuickBooks account.",
     [STEP_QBO_CONNECT, STEP_ACCOUNT_MAPPING],
     []),
    (STAGE_REVIEW,    "Review before posting",    "Review",
     "See exactly what will post. Nothing reaches QuickBooks yet.",
     [STEP_DRY_RUN],
     ["Journal Entry"]),
    (STAGE_IMPORT,    "Send to QuickBooks",      "Import",
     "Post the records to your QuickBooks Online company.",
     [STEP_PROD_IMPORT],
     []),
    (STAGE_RECONCILE, "Final balance check",     "Reconcile",
     "Confirm everything matches and save the audit report.",
     [STEP_RECONCILIATION, STEP_ENDING_TB],
     ["Ending Trial Balance"]),
]


def _stage_status(
    required_keys: Iterable[str],
    by_key: Dict[str, ChecklistItem],
    *,
    stage_key: Optional[str] = None,
) -> str:
    """Roll up the status of a stage from its underlying checklist items.

    A stage is `complete` only when every *required* item is complete.
    Items flagged `planned=True` (auto-posting not yet built) don't block
    completion as long as their upload status is at least in_progress —
    customers see "complete" for the work they can actually finish today.

    Special case for the Upload stage: the underlying STEP_GL_UPLOAD
    item only flips to STATUS_COMPLETE once the GL is *imported* to
    QuickBooks, which doesn't happen until Step 5. From the customer's
    perspective the Upload step is finished as soon as the files are
    on file — keeping the stepper stuck on Step 2 through the entire
    Match/Review/Import flow is exactly the "progress doesn't advance"
    bug we're fixing. For the stepper we therefore count Upload as
    complete when every required upload is at least IN_PROGRESS.
    """
    statuses = []
    upload_in_progress_ok = stage_key == STAGE_UPLOAD
    for k in required_keys:
        item = by_key.get(k)
        if item is None:
            statuses.append(STATUS_NOT_STARTED)
            continue
        # Planned-but-uploaded counts as "complete enough" for the stepper.
        if item.planned and item.status in (
            STATUS_IN_PROGRESS, STATUS_COMPLETE
        ):
            statuses.append(STATUS_COMPLETE)
        elif upload_in_progress_ok and item.status == STATUS_IN_PROGRESS:
            # For the Upload stage, treat "file uploaded but not yet
            # imported / posted" as complete. The actual posting work
            # is tracked under the Import stage.
            statuses.append(STATUS_COMPLETE)
        else:
            statuses.append(item.status)
    if all(s == STATUS_COMPLETE for s in statuses):
        return STAGE_STATUS_COMPLETE
    if any(s in (STATUS_IN_PROGRESS, STATUS_COMPLETE) for s in statuses):
        return STAGE_STATUS_CURRENT  # will be normalized below
    return STAGE_STATUS_UPCOMING


def _required_uploads_present(by_key: Dict[str, ChecklistItem]) -> bool:
    """True if the firm has uploaded the core reports Step 2 needs.

    "Uploaded" here means at least `in_progress` — the file is on file
    even if the downstream QBO posting hasn't happened yet. We need this
    distinction because the Upload stage doesn't roll up to `complete`
    until the QBO posting steps land, but the user can absolutely move
    on to Step 3 (Match accounts) as soon as the source files are in.

    Required for Step 3 to be useful: the chart of accounts (so there's
    something to map) plus the opening trial balance and the general
    ledger (so the matching applies to real records). Trust and ending
    TB are optional at this point and don't gate progress.
    """
    needed = (STEP_COA_UPLOAD, STEP_OPENING_TB, STEP_GL_UPLOAD)
    for key in needed:
        item = by_key.get(key)
        if item is None or item.status == STATUS_NOT_STARTED:
            return False
    return True


def _stage_cta(
    stage_key: str,
    url_for: Optional[Callable[..., str]],
    has_jobs: bool,
    ready_to_advance: bool = False,
) -> tuple:
    """Return (cta_label, cta_url) for the *current* stage.

    url_for is optional so this module is unit-testable without Flask.
    When unavailable we return relative paths that match the existing
    routes — these are not used in tests, only as a defensive fallback.

    `ready_to_advance` is consulted only for the Upload stage. When the
    user has all three core reports on file but the Upload stage hasn't
    rolled up to `complete` yet, we steer them to Step 3 instead of
    nagging them to upload more.
    """
    def u(endpoint: str, fallback: str) -> str:
        if url_for is None:
            return fallback
        try:
            return url_for(endpoint)
        except Exception:
            return fallback

    if stage_key == STAGE_SETUP:
        return ("Start setup", u("cutover_setup", "/cutover"))
    if stage_key == STAGE_UPLOAD:
        if ready_to_advance:
            return ("Next: Match accounts",
                    u("match_accounts_entry", "/match-accounts"))
        if has_jobs:
            return ("Upload another report", u("dashboard", "/dashboard") + "#intake")
        return ("Upload your reports", u("dashboard", "/dashboard") + "#intake")
    if stage_key == STAGE_MATCH:
        return ("Start Step 3: Match accounts",
                u("match_accounts_entry", "/match-accounts"))
    if stage_key == STAGE_REVIEW:
        return ("Review on the checklist", u("migration_checklist", "/migration-checklist"))
    if stage_key == STAGE_IMPORT:
        return ("Open the checklist", u("migration_checklist", "/migration-checklist"))
    if stage_key == STAGE_RECONCILE:
        return ("Open the checklist", u("migration_checklist", "/migration-checklist"))
    return ("", "")


def _stage_back_link(
    current_key: str,
    url_for: Optional[Callable[..., str]],
) -> tuple:
    """Return (back_label, back_url) — where the customer goes to revisit
    the *previous* step from the stage that's currently in progress.

    Customer-facing labels intentionally avoid accounting jargon: a lawyer
    sees "Back to Step 2: Upload reports", not "Back to General Ledger
    intake". Each previous step points at the canonical entry route for
    that stage so the button is never a dead anchor:

      Setup    -> /cutover            (cutover_setup)
      Upload   -> /dashboard#intake   (the upload landing area)
      Match    -> /match-accounts     (dispatch route from PR #33)
      Review   -> /migration-checklist
      Import   -> /migration-checklist

    Returns ("", "") for the first stage (no previous step exists) so
    callers/templates can hide the back button cleanly.
    """
    def u(endpoint: str, fallback: str) -> str:
        if url_for is None:
            return fallback
        try:
            return url_for(endpoint)
        except Exception:
            return fallback

    # Per-stage previous-step entry: (customer-facing label, route).
    # Keyed by the *current* stage; value describes the step before it.
    table = {
        STAGE_UPLOAD:    ("Back to Step 1: Setup",
                          u("cutover_setup", "/cutover")),
        STAGE_MATCH:     ("Back to Step 2: Upload reports",
                          u("dashboard", "/dashboard") + "#intake"),
        STAGE_REVIEW:    ("Back to Step 3: Match accounts",
                          u("match_accounts_entry", "/match-accounts")),
        STAGE_IMPORT:    ("Back to Step 4: Review",
                          u("migration_checklist", "/migration-checklist")),
        STAGE_RECONCILE: ("Back to Step 5: Send to QuickBooks",
                          u("migration_checklist", "/migration-checklist")),
    }
    return table.get(current_key, ("", ""))


def build_customer_stages(
    checklist_items: Iterable[ChecklistItem],
    *,
    url_for: Optional[Callable[..., str]] = None,
    has_jobs: bool = False,
) -> List[WorkflowStage]:
    """Project the detailed checklist into the 6-stage customer stepper.

    Args:
      checklist_items: output of cutover_workflow.build_checklist().
      url_for: Flask's url_for or any callable that resolves endpoint
        names. Optional — pass None in unit tests.
      has_jobs: whether the firm has uploaded at least one job (used to
        tune CTA wording, e.g. "Upload your first report" vs
        "Upload another report").

    Returns:
      A list of six WorkflowStage objects in display order. Exactly one
      stage has status="current" unless every stage is complete (in
      which case every stage is "complete").
    """
    items = list(checklist_items)
    by_key = {item.key: item for item in items}
    total = len(_STAGE_BUNDLES)

    raw_statuses = []
    stages: List[WorkflowStage] = []
    for i, (stage_key, label, short, desc, required, terms) in enumerate(
        _STAGE_BUNDLES, start=1
    ):
        raw_status = _stage_status(required, by_key, stage_key=stage_key)
        raw_statuses.append(raw_status)
        stages.append(WorkflowStage(
            key=stage_key,
            label=label,
            short_label=short,
            description=desc,
            status=raw_status,  # will be normalized below
            index=i,
            total=total,
            friendly_terms=[t for t in terms],
            detail_items=[
                {
                    "key": item.key,
                    "label": item.label,
                    "status": item.status,
                    "summary": item.summary,
                    "planned": item.planned,
                }
                for k in required
                for item in [by_key.get(k)]
                if item is not None
            ],
        ))

    # Normalize: exactly one "current" — the first non-complete stage.
    current_index = None
    for i, stage in enumerate(stages):
        if stage.status != STAGE_STATUS_COMPLETE:
            current_index = i
            break

    ready_to_advance = _required_uploads_present(by_key)

    for i, stage in enumerate(stages):
        if current_index is None:
            stage.status = STAGE_STATUS_COMPLETE
        elif i < current_index:
            stage.status = STAGE_STATUS_COMPLETE
        elif i == current_index:
            stage.status = STAGE_STATUS_CURRENT
            stage.cta_label, stage.cta_url = _stage_cta(
                stage.key, url_for, has_jobs=has_jobs,
                ready_to_advance=ready_to_advance,
            )
            stage.back_label, stage.back_url = _stage_back_link(
                stage.key, url_for,
            )
        else:
            stage.status = STAGE_STATUS_UPCOMING

    return stages


def upload_stage_missing_reports(
    checklist_items: Iterable[ChecklistItem],
) -> List[str]:
    """Return human labels for required reports still missing from Step 2.

    Used by the migration-checklist template to render a short
    "what's still needed" list next to the Step 2 CTA so users who
    haven't uploaded everything yet see a concrete next action instead
    of jargon.
    """
    by_key = {item.key: item for item in checklist_items}
    label_for = {
        STEP_COA_UPLOAD: "Account list (chart of accounts)",
        STEP_OPENING_TB: "Starting balances (opening trial balance)",
        STEP_GL_UPLOAD: "Transaction history (general ledger)",
    }
    missing: List[str] = []
    for key, label in label_for.items():
        item = by_key.get(key)
        if item is None or item.status == STATUS_NOT_STARTED:
            missing.append(label)
    return missing


def upload_stage_ready_to_advance(
    checklist_items: Iterable[ChecklistItem],
) -> bool:
    """Public wrapper around _required_uploads_present for templates / tests."""
    by_key = {item.key: item for item in checklist_items}
    return _required_uploads_present(by_key)


def current_stage(stages: List[WorkflowStage]) -> Optional[WorkflowStage]:
    """Return the stage marked 'current', or None if all are complete."""
    for s in stages:
        if s.status == STAGE_STATUS_CURRENT:
            return s
    return None


def completed_count(stages: List[WorkflowStage]) -> int:
    return sum(1 for s in stages if s.status == STAGE_STATUS_COMPLETE)


def progress_percent(stages: List[WorkflowStage]) -> int:
    """How far along the customer is, 0–100. Used for the slim progress bar."""
    if not stages:
        return 0
    done = completed_count(stages)
    return min(100, int(round(done * 100 / len(stages))))
