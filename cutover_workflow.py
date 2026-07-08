"""Cutover setup + migration-checklist helpers.

This module is the single source of truth for:

  * what fields make up a firm's cutover settings (the migration context
    they fill in before uploading data), and
  * what steps make up the end-to-end migration checklist and how each
    step's status is derived from existing job / report-type / QBO
    history rows.

Keeping the derivation here (rather than inside Flask routes) makes the
checklist easy to unit-test without spinning up the full app and lets
both the dedicated /migration-checklist page AND the dashboard nudge
share the same logic.

Nothing in this module performs QBO writes. Risky steps (opening-balance
journal entries, trust reconciliation, AR/AP posting) are tracked here
as "planned next" so the UI can communicate scope honestly.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable, List, Optional


# Canonical values exposed to the form. Free-text "Other" is accepted as
# well so we don't paint a firm into a corner.
COUNTRY_CHOICES = [
    ("CA", "Canada"),
    ("US", "United States"),
    ("OTHER", "Other"),
]

ACCOUNTING_BASIS_CHOICES = [
    ("accrual", "Accrual"),
    ("cash", "Cash"),
    ("unknown", "Not sure / undecided"),
]


# Checklist step ids — referenced by templates and tests. Don't rename
# these without bumping the templates that key off them.
STEP_CUTOVER_SETUP = "cutover_setup"
STEP_COA_UPLOAD = "coa_upload"
STEP_OPENING_TB = "opening_tb"
STEP_GL_UPLOAD = "gl_upload"
STEP_ENDING_TB = "ending_tb"
STEP_TRUST_LISTING = "trust_listing"
STEP_QBO_CONNECT = "qbo_connect"
STEP_ACCOUNT_MAPPING = "account_mapping"
STEP_DRY_RUN = "dry_run"
STEP_PROD_IMPORT = "prod_import"
STEP_RECONCILIATION = "reconciliation"

STATUS_NOT_STARTED = "not_started"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETE = "complete"


@dataclass
class ChecklistItem:
    key: str
    label: str
    status: str          # not_started | in_progress | complete
    summary: str = ""    # one-line human-friendly hint
    planned: bool = False  # True if the step is intentionally not yet built

    def to_dict(self) -> dict:
        return asdict(self)


# Short, plain-English accounting guidance surfaced on the cutover and
# checklist pages. Sourced from the mentor review email summarized in
# CUTOVER_WORKFLOW.md.
GUIDANCE_TEXT = {
    "cutover_date":
        "The cutover date is the day you switch from running the books in "
        "PCLaw to running them in QuickBooks Online. Transactions on or "
        "after this date should live in QBO; everything before it stays "
        "as history in PCLaw.",
    "opening_balance_date":
        "The opening balance date is the as-of date for the trial balance "
        "you'll use to seed QuickBooks. It's usually the day before your "
        "cutover (e.g. cutover 2026-04-01 → opening balance as of "
        "2026-03-31).",
    "period":
        "Period start/end bound the general ledger you plan to migrate. "
        "Most firms migrate a single fiscal year of GL detail; older "
        "history is preserved in PCLaw and summarized via the opening "
        "balance.",
    "country_basis":
        "Country and accounting basis change how AR/AP and trust are "
        "treated. Canadian firms usually need GST/HST handling; cash-basis "
        "firms typically skip AR/AP migration. We don't post AR/AP or "
        "trust balances automatically — those need a deliberate strategy "
        "you'll confirm before production.",
    "opening_balance_first":
        "Recommended order: upload the opening trial balance BEFORE the "
        "general ledger. The opening TB seeds QuickBooks at the cutover "
        "date; the GL then layers the period's transactions on top.",
    "ending_tb_after_gl":
        "After importing the GL, upload the ending trial balance and use "
        "it to spot-check QuickBooks against PCLaw. Differences usually "
        "mean an account mapping is wrong or an entry didn't post.",
    "trust_listing":
        "Trust listings are validated and reconciled, NOT auto-posted. "
        "Trust balances are client money — they should be re-established "
        "in QuickBooks (or Clio) through a deliberate journal entry per "
        "client matter, after you've confirmed the listing matches the "
        "trust bank.",
    "ar_ap":
        "AR/AP open balances are handled separately from the GL. Strategy "
        "depends on accounting basis and whether you bill from Clio: "
        "options include summary opening JEs by customer/vendor, full "
        "open-item lists imported as transactions, or skipping AR/AP "
        "entirely on cash basis.",
}


def cutover_setup_is_minimally_complete(cutover: Optional[dict]) -> bool:
    """A cutover row counts as 'complete' once the firm has named the
    cutover date plus at least country and accounting_basis.

    These three are the minimum needed for the rest of the UI to make
    sensible defaults (AR/AP treatment, tax handling, opening-balance
    date) and they're what the dashboard checks before clearing the
    "complete cutover setup" nudge.
    """
    if not cutover:
        return False
    return bool(
        cutover.get("cutover_date")
        and cutover.get("country")
        and cutover.get("accounting_basis")
    )


def cutover_setup_in_progress(cutover: Optional[dict]) -> bool:
    """True if the firm has saved *some* fields but not enough yet."""
    if not cutover:
        return False
    return any(
        cutover.get(k)
        for k in (
            "cutover_date", "opening_balance_date", "period_start",
            "period_end", "country", "accounting_basis", "migration_scope",
            "notes", "qbo_company_name",
        )
    )


def _has_any_report(jobs: Iterable[dict], report_type: str) -> bool:
    return any((j.get("report_type") or "general_ledger") == report_type
               for j in jobs)


def _has_imported_gl(jobs: Iterable[dict]) -> bool:
    """True if at least one GL job has successfully imported to QuickBooks.

    Checks import_summary first (the authoritative signal — survives status
    overwrites like "Duplicate blocked" from accidental re-submissions).
    Falls back to the status string for legacy jobs without import_summary.
    """
    for j in jobs:
        if (j.get("report_type") or "general_ledger") != "general_ledger":
            continue
        if j.get("import_summary"):
            return True
        status = (j.get("status") or "").lower()
        if "imported" in status and "not" not in status:
            return True
    return False


def _has_account_mappings(mapping_count: int) -> bool:
    return mapping_count > 0


def build_checklist(
    cutover: Optional[dict],
    firm_jobs: Iterable[dict],
    *,
    has_qbo_connection: bool,
    account_mapping_count: int = 0,
    coa_history_jobs: Optional[Iterable[dict]] = None,
) -> List[ChecklistItem]:
    """Build the ordered checklist from observed state.

    `firm_jobs` should be the list of job rows returned by
    `AppDB.list_jobs_for_firm`. We only read `report_type` and `status`
    so it's cheap to call on every dashboard render.

    `coa_history_jobs`, when given, is scanned for Chart-of-Accounts
    creation history *in addition to* `firm_jobs`. Callers pass the
    firm's full job set (including superseded uploads) here so that the
    "Chart of Accounts created in QuickBooks" milestone survives a later
    re-upload of the chart of accounts — creating accounts in QuickBooks
    is irreversible, so superseding the upload must not un-tick the step.
    Defaults to `firm_jobs` when omitted.

    Steps the app intentionally doesn't yet build (opening-balance JE
    creation, ending-TB reconciliation as a posted activity, trust
    posting, AR/AP) are returned with `planned=True` and a status that
    reflects whether the firm at least uploaded the supporting file.
    """
    jobs = list(firm_jobs)

    items: List[ChecklistItem] = []

    # 1. Cutover setup
    if cutover_setup_is_minimally_complete(cutover):
        items.append(ChecklistItem(
            STEP_CUTOVER_SETUP, "Cutover setup completed",
            STATUS_COMPLETE,
            summary=f"Cutover date {cutover.get('cutover_date')} "
                    f"· {cutover.get('country')} · "
                    f"{cutover.get('accounting_basis')}",
        ))
    elif cutover_setup_in_progress(cutover):
        items.append(ChecklistItem(
            STEP_CUTOVER_SETUP, "Cutover setup completed",
            STATUS_IN_PROGRESS,
            summary="Some fields saved — add cutover date, country, and "
                    "accounting basis to finish.",
        ))
    else:
        items.append(ChecklistItem(
            STEP_CUTOVER_SETUP, "Cutover setup completed",
            STATUS_NOT_STARTED,
            summary="Define cutover date, country, and accounting basis "
                    "before importing data.",
        ))

    # 2. Chart of Accounts uploaded / previewed / created
    coa_jobs = [j for j in jobs
                if (j.get("report_type") or "") == "chart_of_accounts"]
    # Account-creation history is read from the wider set (superseded
    # uploads included) so a re-upload can't hide an already-created COA.
    coa_history_source = list(coa_history_jobs) if coa_history_jobs is not None else jobs
    coa_history_jobs_filtered = [
        j for j in coa_history_source
        if (j.get("report_type") or "") == "chart_of_accounts"
    ]
    coa_created_history = [
        h for j in coa_history_jobs_filtered
        for h in (j.get("coa_create_history") or [])
    ]
    coa_created_total = sum(
        int(h.get("created_count") or 0) for h in coa_created_history
    )
    if coa_created_total > 0:
        coa_upload_count = len(coa_jobs) or len([
            j for j in coa_history_jobs_filtered
            if j.get("coa_create_history")
        ])
        items.append(ChecklistItem(
            STEP_COA_UPLOAD, "Chart of Accounts created in QuickBooks",
            STATUS_COMPLETE,
            summary=(
                f"{coa_created_total} QuickBooks account(s) created across "
                f"{coa_upload_count} upload(s)."
            ),
        ))
    elif coa_jobs:
        items.append(ChecklistItem(
            STEP_COA_UPLOAD, "Chart of Accounts uploaded / previewed",
            STATUS_IN_PROGRESS,
            summary=(
                f"{len(coa_jobs)} chart-of-accounts upload(s) on file. "
                "Open the preview and apply missing accounts to QuickBooks "
                "when ready."
            ),
        ))
    else:
        items.append(ChecklistItem(
            STEP_COA_UPLOAD, "Chart of Accounts uploaded / previewed",
            STATUS_NOT_STARTED,
            summary="Upload the PCLaw chart of accounts so you can preview "
                    "what would be created in QuickBooks.",
        ))

    # 3. Opening Trial Balance uploaded + posted
    tb_jobs = [j for j in jobs
               if (j.get("report_type") or "") == "trial_balance"]
    opening_je_posted = [
        h for j in tb_jobs for h in (j.get("opening_balance_history") or [])
        if not h.get("demo_mode") and h.get("qbo_je_id")
    ]
    if opening_je_posted:
        items.append(ChecklistItem(
            STEP_OPENING_TB, "Opening balance JE posted to QuickBooks",
            STATUS_COMPLETE,
            summary=(
                f"Opening balance JournalEntry posted "
                f"({len(opening_je_posted)} run(s))."
            ),
        ))
    elif tb_jobs:
        items.append(ChecklistItem(
            STEP_OPENING_TB, "Opening Trial Balance uploaded",
            STATUS_IN_PROGRESS,
            summary=f"{len(tb_jobs)} trial-balance upload(s) on file. "
                    "Open the opening-balance preview to post the seed JE.",
        ))
    else:
        items.append(ChecklistItem(
            STEP_OPENING_TB, "Opening Trial Balance uploaded",
            STATUS_NOT_STARTED,
            summary="Upload the PCLaw trial balance as of the day BEFORE "
                    "your cutover date.",
        ))

    # 4. General Ledger uploaded / imported
    gl_jobs = [j for j in jobs
               if (j.get("report_type") or "general_ledger") == "general_ledger"]
    if _has_imported_gl(jobs):
        imported_count = sum(
            1 for j in gl_jobs
            if j.get("import_summary")
            or "imported" in (j.get("status") or "").lower()
        )
        items.append(ChecklistItem(
            STEP_GL_UPLOAD, "General Ledger uploaded / imported",
            STATUS_COMPLETE,
            summary=f"{imported_count} GL job(s) posted to QuickBooks.",
        ))
    elif gl_jobs:
        items.append(ChecklistItem(
            STEP_GL_UPLOAD, "General Ledger uploaded / imported",
            STATUS_IN_PROGRESS,
            summary=f"{len(gl_jobs)} GL upload(s) on file — none imported "
                    "to QuickBooks yet.",
        ))
    else:
        items.append(ChecklistItem(
            STEP_GL_UPLOAD, "General Ledger uploaded / imported",
            STATUS_NOT_STARTED,
            summary="Upload the PCLaw general ledger for your migration "
                    "period.",
        ))

    # 5. Ending Trial Balance reconciliation
    ending_recon_built = any(
        j.get("ending_tb_reconciliation") for j in tb_jobs
    )
    if ending_recon_built:
        items.append(ChecklistItem(
            STEP_ENDING_TB, "Ending Trial Balance reconciled",
            STATUS_COMPLETE,
            summary="Ending TB reconciliation report has been built. "
                    "Download the CSV for your audit trail.",
        ))
    elif len(tb_jobs) >= 2:
        items.append(ChecklistItem(
            STEP_ENDING_TB, "Ending Trial Balance uploaded / checked",
            STATUS_IN_PROGRESS,
            summary=f"{len(tb_jobs)} trial-balance upload(s) on file. "
                    "Open the latest to run the reconciliation report.",
        ))
    elif tb_jobs:
        items.append(ChecklistItem(
            STEP_ENDING_TB, "Ending Trial Balance uploaded / checked",
            STATUS_IN_PROGRESS,
            summary="One trial balance on file. Upload the ending TB once "
                    "GL import is done so we can reconcile.",
        ))
    else:
        items.append(ChecklistItem(
            STEP_ENDING_TB, "Ending Trial Balance uploaded / checked",
            STATUS_NOT_STARTED,
            summary="After GL import, upload the ending trial balance to "
                    "reconcile QuickBooks against PCLaw.",
        ))

    # 6. Trust Listing uploaded / reconciled
    trust_jobs = [j for j in jobs
                  if (j.get("report_type") or "") == "trust_listing"]
    trust_reconciled = any(j.get("trust_reconciliation") for j in trust_jobs)
    if trust_reconciled:
        items.append(ChecklistItem(
            STEP_TRUST_LISTING, "Trust Listing reconciled",
            STATUS_COMPLETE,
            summary=(
                f"{len(trust_jobs)} trust-listing upload(s) reconciled. "
                "Trust posting remains manual (per-matter, operator-confirmed)."
            ),
            planned=True,  # auto-posting still intentionally NOT built
        ))
    elif trust_jobs:
        items.append(ChecklistItem(
            STEP_TRUST_LISTING, "Trust Listing uploaded / checked",
            STATUS_IN_PROGRESS,
            summary=f"{len(trust_jobs)} trust-listing upload(s) on file. "
                    "Open the trust reconciliation report to validate "
                    "against the trust liability / trust bank balances.",
            planned=True,
        ))
    else:
        items.append(ChecklistItem(
            STEP_TRUST_LISTING, "Trust Listing uploaded / checked",
            STATUS_NOT_STARTED,
            summary="Upload the PCLaw trust listing for validation. "
                    "Posting trust balances is a manual, deliberate step.",
            planned=True,
        ))

    # 7. QBO connected
    if has_qbo_connection:
        items.append(ChecklistItem(
            STEP_QBO_CONNECT, "QuickBooks Online connected",
            STATUS_COMPLETE,
            summary="At least one QuickBooks company is connected.",
        ))
    else:
        items.append(ChecklistItem(
            STEP_QBO_CONNECT, "QuickBooks Online connected",
            STATUS_NOT_STARTED,
            summary="Connect the QuickBooks Online company you're migrating "
                    "into.",
        ))

    # 8. Account mappings
    if _has_account_mappings(account_mapping_count):
        items.append(ChecklistItem(
            STEP_ACCOUNT_MAPPING, "Account mappings completed",
            STATUS_COMPLETE,
            summary=f"{account_mapping_count} PCLaw→QBO account mapping(s) "
                    "saved.",
        ))
    else:
        items.append(ChecklistItem(
            STEP_ACCOUNT_MAPPING, "Account mappings completed",
            STATUS_NOT_STARTED,
            summary="Map each PCLaw account to a QuickBooks account before "
                    "posting any journal entries.",
        ))

    # 9. Dry-run preview — only counts if there is a GL job with a
    # preflight. COA / TB / trust uploads also set a per-job preflight,
    # so without the report_type filter the dry-run step lights up the
    # moment the chart of accounts is uploaded, which lets Step 5
    # (Import) become current before the user has even matched accounts.
    has_dry_run = any(
        j.get("preflight")
        and (j.get("report_type") or "general_ledger") == "general_ledger"
        for j in jobs
    )
    if has_dry_run:
        items.append(ChecklistItem(
            STEP_DRY_RUN, "Dry-run preview completed",
            STATUS_COMPLETE,
            summary="At least one job has a preflight / dry-run preview.",
        ))
    else:
        items.append(ChecklistItem(
            STEP_DRY_RUN, "Dry-run preview completed",
            STATUS_NOT_STARTED,
            summary="Open a GL job and review the dry-run preview before "
                    "posting to QuickBooks.",
        ))

    # 10. Production import
    if _has_imported_gl(jobs):
        items.append(ChecklistItem(
            STEP_PROD_IMPORT, "Production import completed",
            STATUS_COMPLETE,
            summary="At least one GL has been posted to QuickBooks.",
        ))
    else:
        items.append(ChecklistItem(
            STEP_PROD_IMPORT, "Production import completed",
            STATUS_NOT_STARTED,
            summary="The final step: post the GL to QuickBooks after "
                    "everything above is green.",
        ))

    # 11. Reconciliation report
    has_reconciliation = any(
        (j.get("import_summary") or {}).get("reconciliation_built")
        or (j.get("verification") or {}).get("status") == "ok"
        for j in jobs
    )
    if has_reconciliation:
        items.append(ChecklistItem(
            STEP_RECONCILIATION, "Reconciliation report viewed",
            STATUS_COMPLETE,
            summary="A verification or reconciliation report is available "
                    "for at least one import.",
        ))
    else:
        items.append(ChecklistItem(
            STEP_RECONCILIATION, "Reconciliation report viewed",
            STATUS_NOT_STARTED,
            summary="After production import, download the reconciliation "
                    "/ verification report for your records.",
        ))

    return items


def next_recommended_step(items: List[ChecklistItem]) -> Optional[ChecklistItem]:
    """Return the first not-yet-complete item, or None if everything's done."""
    for item in items:
        if item.status != STATUS_COMPLETE:
            return item
    return None
