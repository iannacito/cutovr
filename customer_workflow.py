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
    # Direct-jump URL for the stepper rail. Populated for completed and
    # current stages so a lawyer can click back to a finished step (or
    # the one they're on). Upcoming stages stay unclickable on purpose —
    # jumping *ahead* is what produced Cesar's "Step 3 jumped to Step 4"
    # confusion, so the rail only ever moves you to work you've reached.
    nav_url: str = ""
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
            "nav_url": self.nav_url,
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
    gl_job_id: Optional[str] = None,
) -> tuple:
    """Return (cta_label, cta_url) for the *current* stage.

    url_for is optional so this module is unit-testable without Flask.
    When unavailable we return relative paths that match the existing
    routes — these are not used in tests, only as a defensive fallback.

    `ready_to_advance` is consulted only for the Upload stage. When the
    user has all three core reports on file but the Upload stage hasn't
    rolled up to `complete` yet, we steer them to Step 3 instead of
    nagging them to upload more.

    `gl_job_id` when provided routes Steps 5 and 6 to job-scoped URLs
    instead of global dispatch routes.
    """
    def u(endpoint: str, fallback: str) -> str:
        if url_for is None:
            return fallback
        try:
            return url_for(endpoint)
        except Exception:
            return fallback

    if stage_key == STAGE_SETUP:
        # User testing: lawyers parsed "Proceed to Step 2: Upload reports"
        # as instructional rather than as the next action. "Step 2: Upload
        # Your Reports" reads as a clear, plain-English next button. Point
        # at the upload area on the dashboard so this button actually moves
        # the user forward rather than self-linking back to Step 1.
        return ("Step 2: Upload Your Reports",
                u("dashboard", "/dashboard") + "#intake")
    if stage_key == STAGE_UPLOAD:
        if ready_to_advance:
            return ("Proceed to Step 3: Match accounts",
                    u("match_accounts_entry", "/match-accounts"))
        if has_jobs:
            return ("Upload another report", u("dashboard", "/dashboard") + "#intake")
        return ("Upload your reports", u("dashboard", "/dashboard") + "#intake")
    if stage_key == STAGE_MATCH:
        # Dashboard / checklist context: Match is the current stage but the
        # user is *not* on the Match page yet, so the CTA invites them onto
        # it. The label is a plain "Match accounts" rather than the old
        # "Proceed to Step 3: Match accounts" — on the Match page itself
        # that read as self-referential (Cesar 2026-06-03 QA), and
        # build_customer_stages now suppresses this CTA entirely when the
        # user is already on the page (on_match_page=True), letting the
        # page's own forward CTA to Step 4 stand alone.
        return ("Match accounts",
                u("match_accounts_entry", "/match-accounts"))
    if stage_key == STAGE_REVIEW:
        return ("Proceed to Step 4: Review import",
                u("import_job_entry", "/import-job"))
    if stage_key == STAGE_IMPORT:
        # The customer is *on* Step 5 — point the stepper CTA at the
        # actual send action rather than telling them to "Proceed to
        # Step 5" while they are already there. The send-to-qbo page
        # renders its own confirmation form; the stepper CTA simply
        # focuses that page (anchor to the send card).
        if gl_job_id:
            try:
                _url = url_for("send_to_qbo", job_id=gl_job_id) + "#send-to-qbo-card"
            except Exception:
                _url = f"/jobs/{gl_job_id}/send-to-qbo#send-to-qbo-card"
        else:
            _url = u("send_to_qbo_entry", "/send-to-qbo") + "#send-to-qbo-card"
        return ("Send to QuickBooks", _url)
    if stage_key == STAGE_RECONCILE:
        if gl_job_id:
            try:
                _url = url_for("reconcile_balances_job", job_id=gl_job_id)
            except Exception:
                _url = f"/jobs/{gl_job_id}/reconcile-balances"
        else:
            _url = u("reconcile_balances", "/reconcile-balances")
        return ("Proceed to Step 6: Reconcile balances", _url)
    return ("", "")


def _stage_back_link(
    current_key: str,
    url_for: Optional[Callable[..., str]],
    gl_job_id: Optional[str] = None,
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

    `gl_job_id` when provided routes Step 6's back link to the job-scoped
    Step 5 URL instead of the global dispatch route.
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
    # Every URL must be a real, working route — never a bare '#'.
    if current_key == STAGE_RECONCILE and gl_job_id:
        try:
            back_url = url_for("send_to_qbo", job_id=gl_job_id)
        except Exception:
            back_url = f"/jobs/{gl_job_id}/send-to-qbo"
        return ("Back to Step 5: Send to QuickBooks", back_url)

    table = {
        STAGE_UPLOAD:    ("Back to Step 1: Setup",
                          u("cutover_setup", "/cutover")),
        STAGE_MATCH:     ("Back to Step 2: Upload reports",
                          u("dashboard", "/dashboard") + "#intake"),
        STAGE_REVIEW:    ("Back to Step 3: Match accounts",
                          u("match_accounts_entry", "/match-accounts")),
        STAGE_IMPORT:    ("Back to Step 4: Review import",
                          u("import_job_entry", "/import-job")),
        STAGE_RECONCILE: ("Back to Step 5: Send to QuickBooks",
                          u("send_to_qbo_entry", "/send-to-qbo")),
    }
    return table.get(current_key, ("", ""))


def _stage_nav_url(
    stage_key: str,
    url_for: Optional[Callable[..., str]],
    gl_job_id: Optional[str] = None,
) -> str:
    """Canonical entry URL for a stage's rail bubble.

    Maps each customer-facing stage to the route that owns it so the
    stepper rail can deep-link. Mirrors the back-link table but is keyed
    by the stage *itself* (not the step before it). Returns "" when no
    safe destination exists (e.g. url_for unavailable in unit tests).

    `gl_job_id` when provided routes Steps 5 and 6 rail links to the
    job-scoped URLs instead of global dispatch routes.
    """
    def u(endpoint: str, fallback: str) -> str:
        if url_for is None:
            return ""
        try:
            return url_for(endpoint)
        except Exception:
            return fallback

    if stage_key == STAGE_IMPORT and gl_job_id:
        return (url_for("send_to_qbo", job_id=gl_job_id)
                if url_for else f"/jobs/{gl_job_id}/send-to-qbo")
    if stage_key == STAGE_RECONCILE and gl_job_id:
        return (url_for("reconcile_balances_job", job_id=gl_job_id)
                if url_for else f"/jobs/{gl_job_id}/reconcile-balances")

    table = {
        STAGE_SETUP:     lambda: u("cutover_setup", "/cutover"),
        STAGE_UPLOAD:    lambda: (u("uploaded_reports", "/uploaded-reports")
                                  or u("dashboard", "/dashboard")),
        STAGE_MATCH:     lambda: u("match_accounts_entry", "/match-accounts"),
        STAGE_REVIEW:    lambda: u("import_job_entry", "/import-job"),
        STAGE_IMPORT:    lambda: u("send_to_qbo_entry", "/send-to-qbo"),
        STAGE_RECONCILE: lambda: u("reconcile_balances", "/reconcile-balances"),
    }
    factory = table.get(stage_key)
    return factory() if factory else ""


def build_customer_stages(
    checklist_items: Iterable[ChecklistItem],
    *,
    url_for: Optional[Callable[..., str]] = None,
    has_jobs: bool = False,
    match_blocked: bool = False,
    match_blocked_job_id: Optional[str] = None,
    force_current_stage: Optional[str] = None,
    review_blocker: Optional[str] = None,
    review_job_id: Optional[str] = None,
    on_match_page: bool = False,
    gl_job_id: Optional[str] = None,
) -> List[WorkflowStage]:
    """Project the detailed checklist into the 6-stage customer stepper.

    Args:
      checklist_items: output of cutover_workflow.build_checklist().
      url_for: Flask's url_for or any callable that resolves endpoint
        names. Optional — pass None in unit tests.
      has_jobs: whether the firm has uploaded at least one job (used to
        tune CTA wording, e.g. "Upload your first report" vs
        "Upload another report").
      match_blocked: when True, the Match stage is forced to "current"
        even if the underlying checklist items have rolled up to
        complete. Use this when a downstream check (e.g. the GL import
        route) detects that the QuickBooks company is still missing
        accounts the transaction history needs. Step 4 and Step 5
        cannot be "complete" while Match is blocked.
      match_blocked_job_id: optional GL job id that triggered the
        match-blocked state. When supplied, the Match stage's CTA
        deep-links to that job's account-mapping page so the user can
        click straight into "Create missing QuickBooks accounts".
      force_current_stage: when set, anchor the stepper's "current"
        marker to this stage key regardless of what the underlying
        checklist rollup says. Use this when the user is *viewing*
        a specific step's page (e.g. Step 1 / cutover setup) so the
        nav row reflects "you are on Step N" rather than "Step N is
        next" — without this, revisiting Step 1 after saving leaks a
        misleading "Back to Step 1" CTA pointing at the page itself.

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

    # Sequential gating: a stage cannot be complete if any earlier stage
    # is not complete. The raw per-stage rollup ignores ordering, which
    # lets state from prior workflow runs (e.g. a previously connected
    # QBO company, mappings saved against PCLaw account names that the
    # current upload happens to reuse, a preflight set on any uploaded
    # file) leapfrog the user past Match / Review and land them on
    # Step 5 Import even though they haven't actually walked through the
    # in-between steps for the current run. Once we encounter a
    # non-complete stage we force every later stage back to upcoming so
    # the stepper progresses strictly in order.
    blocked = False
    for stage in stages:
        if blocked:
            stage.status = STAGE_STATUS_UPCOMING
            continue
        if stage.status != STAGE_STATUS_COMPLETE:
            blocked = True

    # match_blocked override: an external check (the GL import path)
    # detected that the connected QuickBooks company is missing one or
    # more accounts the uploaded transaction history references. The
    # raw checklist can't see this — account_mapping_count > 0 is
    # enough to flip STEP_ACCOUNT_MAPPING to complete even when a few
    # rows are still unmapped — so the gating below would let Step 4
    # and Step 5 look "ready" while the import would actually fail.
    # Force the Match stage back to non-complete so the stepper points
    # the user at the create-missing-accounts CTA in Step 3.
    if match_blocked:
        for stage in stages:
            if stage.key == STAGE_MATCH:
                stage.status = STAGE_STATUS_CURRENT  # normalized below
            elif stage.key in (STAGE_REVIEW, STAGE_IMPORT, STAGE_RECONCILE):
                stage.status = STAGE_STATUS_UPCOMING

    # Normalize: exactly one "current" — the first non-complete stage.
    current_index = None
    for i, stage in enumerate(stages):
        if stage.status != STAGE_STATUS_COMPLETE:
            current_index = i
            break

    # If the caller is pinning the stepper to a specific stage (because
    # the user is on that step's page), override the computed current
    # index. Stages before the forced one are marked complete; the
    # forced stage becomes current; later stages become upcoming. This
    # guarantees that the back / next CTAs always reflect the page the
    # user is actually looking at, never a downstream "Back to this
    # very page" loop.
    if force_current_stage is not None:
        for i, stage in enumerate(stages):
            if stage.key == force_current_stage:
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
                gl_job_id=gl_job_id,
            )
            stage.back_label, stage.back_url = _stage_back_link(
                stage.key, url_for,
                gl_job_id=gl_job_id,
            )
        else:
            stage.status = STAGE_STATUS_UPCOMING
        # Rail deep-link: completed steps (revisit) and the current step
        # (re-focus) are clickable; upcoming steps deliberately are not,
        # so the rail can never skip a lawyer ahead past unfinished work.
        if stage.status in (STAGE_STATUS_COMPLETE, STAGE_STATUS_CURRENT):
            stage.nav_url = _stage_nav_url(stage.key, url_for, gl_job_id=gl_job_id)

    # When the Match stage is blocked by missing-account detection,
    # override its CTA to send the user straight into the
    # create-missing-accounts flow with copy that names the problem.
    if match_blocked:
        for stage in stages:
            if stage.key != STAGE_MATCH:
                continue
            if match_blocked_job_id:
                if url_for is not None:
                    try:
                        stage.cta_url = url_for(
                            "account_mapping",
                            job_id=match_blocked_job_id,
                        )
                    except Exception:
                        stage.cta_url = (
                            f"/jobs/{match_blocked_job_id}/account-mapping"
                        )
                else:
                    stage.cta_url = (
                        f"/jobs/{match_blocked_job_id}/account-mapping"
                    )
            stage.cta_label = "Create missing QuickBooks accounts"
            break

    # On the Match page itself, suppress the stepper-level Match CTA — but
    # never when matching is blocked, where the stepper's
    # "Create missing QuickBooks accounts" CTA is the page's actionable
    # next step. Otherwise the page already owns its own forward action
    # (the top + footer "Proceed to Step 4" / "Save matches" buttons), so a
    # stepper CTA would either self-reference Step 3 or duplicate the Step 4
    # button. Cesar 2026-06-03 QA flagged both the self-referential
    # "Proceed to Step 3" label and the stretched, doubled CTAs.
    if on_match_page and not match_blocked:
        for stage in stages:
            if stage.key == STAGE_MATCH and stage.status == STAGE_STATUS_CURRENT:
                stage.cta_label = ""
                stage.cta_url = ""
                break

    # Review-stage CTA override. When the user is on the Step 4 review
    # page, the stepper-level CTA must reflect the *actual* blocker the
    # page is showing — not the generic "Proceed to Step 4: Review
    # import" copy, and never a stale "Create missing QuickBooks
    # accounts" button when accounts are already matched.
    if review_blocker:
        for stage in stages:
            if stage.key != STAGE_REVIEW or stage.status != STAGE_STATUS_CURRENT:
                continue
            def _u(endpoint, fallback, **kw):
                if url_for is None:
                    return fallback
                try:
                    return url_for(endpoint, **kw)
                except Exception:
                    return fallback
            if review_blocker == "ready":
                stage.cta_label = "Step 5: Send to QuickBooks"
                stage.cta_url = _u(
                    "send_to_qbo_entry", "/send-to-qbo"
                )
            elif review_blocker == "unmatched":
                if review_job_id:
                    stage.cta_url = _u(
                        "account_mapping",
                        f"/jobs/{review_job_id}/account-mapping",
                        job_id=review_job_id,
                    )
                else:
                    stage.cta_url = _u(
                        "match_accounts_entry", "/match-accounts"
                    )
                stage.cta_label = "Match accounts"
            elif review_blocker in ("blocked_txns", "unbalanced"):
                if review_job_id:
                    stage.cta_url = _u(
                        "validation_report_csv",
                        f"/jobs/{review_job_id}/validation-report.csv",
                        job_id=review_job_id,
                    )
                else:
                    stage.cta_url = ""
                stage.cta_label = "Download validation report"
            else:
                # Preview unavailable / generic error — suppress the
                # stepper CTA. The in-page panel surfaces the right
                # recovery action (re-upload, reconnect QuickBooks, etc.)
                # so a duplicate CTA in the stepper would only confuse.
                stage.cta_label = ""
                stage.cta_url = ""
            break

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
