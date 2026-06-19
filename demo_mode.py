"""
Demo mode: a dedicated, repeatable demo workspace for Cutovr.

The product gets demoed dozens to hundreds of times against one connected
QuickBooks Online sandbox/demo company. Without this module the demo
operator has to either purge QBO by hand between runs or rotate between
QBO companies — both of which are slow and error-prone. Demo mode solves
the *app-side* half of that problem:

  - A demo workspace page (``/demo``) exposes a "Start new demo" action.
  - Starting a new demo archives prior demo jobs for the firm so the
    dashboard / checklist render a fresh state. Nothing in QuickBooks is
    touched.
  - Sample reports are generated on demand with a unique demo run id
    embedded in transaction ids / memos / customer / vendor names. That
    lets repeated imports against the same QBO company avoid duplicate
    protection collisions without weakening duplicate protection for
    real production imports.

Gating model
------------

Demo mode is only ever exposed when *either* of the following is true:

  1. The environment variable ``DEMO_MODE`` (alias ``APP_DEMO_MODE``) is
     set to a truthy value at process start. This is the recommended
     pattern for a dedicated staging/demo deployment.
  2. The logged-in user is in the operator allowlist (``OPERATOR_EMAILS``)
     — operators always see the demo affordance regardless of env, so we
     can drive a demo from a production-config'd app *if* we are signed
     in as an internal operator.

In all other cases the route 404s, the nav link is hidden, and the demo
sample-data downloads still 404. Normal customer behavior is unchanged.

Safety constraints (do not relax these in this module):

  - Demo reset never touches QuickBooks. It only archives app-side jobs
    for the logged-in firm.
  - We never expose a "purge QBO" / "delete in QBO" action.
  - Duplicate protection (sha256 file dedup, JE DocNumber uniqueness in
    QBO) is unchanged for production imports — demo run ids are scoped to
    the demo dataset generator only.
"""

from __future__ import annotations

import csv
import io
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Environment gating
# ---------------------------------------------------------------------------

# Truthy values accepted by the env-var parser. Mirrors the existing
# convention elsewhere in the codebase (CSRF_DISABLE, QBO_REAL_IMPORT, ...).
_TRUTHY = ("1", "true", "yes", "on")


def is_demo_mode_enabled() -> bool:
    """Return True iff the current deploy is a dedicated demo/staging deploy.

    Read at call time so tests can monkey-patch the environment between
    individual cases. The dedicated env var ``DEMO_MODE`` is preferred;
    ``APP_DEMO_MODE`` is accepted for symmetry with ``APP_ENV``.
    """
    raw = os.environ.get("DEMO_MODE") or os.environ.get("APP_DEMO_MODE") or ""
    return raw.strip().lower() in _TRUTHY


def demo_visible_for_user(user: Optional[dict], is_operator: bool) -> bool:
    """Should this logged-in user see demo affordances?

    Demo mode is visible when:
      - The deploy itself is a demo deploy (DEMO_MODE=true), OR
      - The user is an internal operator (so we can demo from anywhere).

    A logged-out visitor never sees demo controls — even on a demo
    deploy — because demo reset is firm-scoped and only makes sense
    after auth.
    """
    if not user:
        return False
    if is_demo_mode_enabled():
        return True
    return bool(is_operator)


# ---------------------------------------------------------------------------
# Demo run identifier
#
# Embedded into transaction ids, memos, and entity names in the generated
# demo dataset. The id is short, URL-safe, and unique-enough across the
# hundreds-of-demos-per-year scale Dan operates at.
# ---------------------------------------------------------------------------


def new_demo_run_id() -> str:
    """Return a fresh demo run id like ``D-20260522T143012-3F9A``.

    The timestamp prefix makes the id sortable and easy for the demo
    operator to recognise in QBO ("oh, that's the 2:30pm run"). The
    random suffix removes any chance of two demos in the same minute
    colliding when wall-clock resolution is coarser than expected.
    """
    ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%dT%H%M%S")
    suffix = secrets.token_hex(2).upper()
    return f"D-{ts}-{suffix}"


# ---------------------------------------------------------------------------
# Demo sample dataset
#
# We deliberately keep the sample dataset small and internally balanced
# so it imports cleanly into QBO. Account numbers match the bundled
# ``test_data/01_chart_of_accounts.csv`` so the COA-mapping step has
# something realistic to show.
#
# The dataset has two important properties for repeatable demos:
#
#   1. Every transaction id, customer name, vendor name, and (where
#      practical) memo carries the demo run id. So if Dan demos at
#      2:30pm and again at 4:00pm against the same QBO company,
#      neither run's transaction ids collide with the other and QBO's
#      DocNumber dedup is satisfied.
#
#   2. The account list (chart of accounts) is *not* salted with the
#      run id — that would force QBO to create a duplicate Chart of
#      Accounts each demo. Instead we preserve the well-known account
#      numbers so the second demo's COA step sees the prior demo's
#      accounts already mapped.
# ---------------------------------------------------------------------------


# Stable account list. Account numbers match
# ``test_data/01_chart_of_accounts.csv`` so the COA flow recognises them.
DEMO_CHART_OF_ACCOUNTS_ROWS = [
    # (number, name, account_type, pclaw_category, qbo_type, qbo_detail, opening, active)
    ("1000", "Operating Bank", "Asset", "Bank", "Bank", "Checking", "25000.00", "Yes"),
    ("1010", "Trust Bank", "Asset", "Trust Bank", "Bank", "Trust Account", "8500.00", "Yes"),
    ("1100", "Accounts Receivable", "Asset", "Receivable", "Accounts Receivable", "Accounts Receivable", "4200.00", "Yes"),
    ("1200", "Unbilled Disbursements", "Asset", "WIP", "Other Current Asset", "Other Current Asset", "0.00", "Yes"),
    ("2000", "Accounts Payable", "Liability", "Payable", "Accounts Payable", "Accounts Payable", "1900.00", "Yes"),
    ("2100", "Client Trust Liability", "Liability", "Trust Liability", "Other Current Liability", "Trust Accounts - Liabilities", "8500.00", "Yes"),
    ("3000", "Owner Equity", "Equity", "Equity", "Equity", "Owner's Equity", "25000.00", "Yes"),
    ("4000", "Legal Fees Revenue", "Income", "Revenue", "Income", "Service/Fee Income", "0.00", "Yes"),
    ("4100", "Disbursement Recovery", "Income", "Recovery", "Income", "Other Primary Income", "0.00", "Yes"),
    ("5000", "Rent Expense", "Expense", "Overhead", "Expense", "Rent or Lease of Buildings", "0.00", "Yes"),
    ("5100", "Office Expense", "Expense", "Overhead", "Expense", "Office/General Administrative Expenses", "0.00", "Yes"),
    ("5200", "Filing Fees Expense", "Expense", "Client Cost", "Expense", "Legal & Professional Fees", "0.00", "Yes"),
]

DEMO_COA_HEADER = [
    "account_number", "account_name", "account_type",
    "pclaw_category", "qbo_suggested_type", "qbo_suggested_detail_type",
    "opening_balance", "active",
]


# Trial balance: every row balances against ``DEMO_CHART_OF_ACCOUNTS_ROWS``
# after the GL transactions are posted. Hard-coded rather than recomputed
# so the demo dataset is auditable at a glance.
#
# This file plays the role of *both* the "opening trial balance" upload
# (Step 2) and the "ending trial balance" upload (Step 6). Because the
# bundled GL is internally balanced (debits == credits) the opening and
# ending balances coincide — every entry it posts is a transfer between
# accounts rather than a net increase in equity. Treating it as
# opening+ending keeps the demo dataset minimal and auditable while
# still exercising both upload steps.
DEMO_TRIAL_BALANCE_ROWS = [
    # (number, name, debit_balance, credit_balance)
    ("1000", "Operating Bank", "24250.00", "0.00"),
    ("1010", "Trust Bank", "8500.00", "0.00"),
    ("1100", "Accounts Receivable", "4200.00", "0.00"),
    ("1200", "Unbilled Disbursements", "0.00", "0.00"),
    ("2000", "Accounts Payable", "0.00", "1900.00"),
    ("2100", "Client Trust Liability", "0.00", "8500.00"),
    ("3000", "Owner Equity", "0.00", "25000.00"),
    ("4000", "Legal Fees Revenue", "0.00", "4200.00"),
    ("4100", "Disbursement Recovery", "0.00", "0.00"),
    ("5000", "Rent Expense", "1900.00", "0.00"),
    ("5100", "Office Expense", "0.00", "0.00"),
    ("5200", "Filing Fees Expense", "750.00", "0.00"),
]

DEMO_TB_HEADER = ["account_number", "account_name", "debit_balance", "credit_balance"]


# Ending Trial Balance ("Final balance check"): same numeric rows as the
# opening TB above, since the demo GL is internally balanced. Kept as a
# separate sample so the demo workspace can offer the customer-facing
# Step 6 ("Final balance check") report under its own name.
DEMO_ENDING_TB_ROWS = DEMO_TRIAL_BALANCE_ROWS


# General-ledger template. Two-sided so debits == credits. Account numbers
# stay stable; only the transaction_id / customer_name / vendor_name /
# description carry the run-id salt so each repeated demo posts as
# distinct QBO journal entries.
_GL_TEMPLATE = [
    # (txn_idx, date, account_number, account_name, client_id, matter_id,
    #  reference, description, customer_template, vendor_template, debit, credit)
    (1, "2026-05-01", "1000", "Operating Bank", "", "", "DEP-001", "Opening operating cash", "", "", "25000.00", "0.00"),
    (1, "2026-05-01", "3000", "Owner Equity", "", "", "DEP-001", "Opening operating cash", "", "", "0.00", "25000.00"),
    (2, "2026-05-01", "1010", "Trust Bank", "C-100", "M-1001", "TRUST-001", "Opening trust balance for Smith purchase", "", "", "8500.00", "0.00"),
    (2, "2026-05-01", "2100", "Client Trust Liability", "C-100", "M-1001", "TRUST-001", "Opening trust liability for Smith purchase", "", "", "0.00", "8500.00"),
    (3, "2026-05-02", "1100", "Accounts Receivable", "C-200", "M-2001", "INV-1001", "Invoice to Johnson Family Law", "Johnson Family Law [{run}]", "", "4200.00", "0.00"),
    (3, "2026-05-02", "4000", "Legal Fees Revenue", "C-200", "M-2001", "INV-1001", "Invoice to Johnson Family Law", "", "", "0.00", "4200.00"),
    (4, "2026-05-02", "5200", "Filing Fees Expense", "C-300", "M-3001", "CHK-501", "Court filing fee paid for Chen litigation", "", "", "750.00", "0.00"),
    (4, "2026-05-02", "1000", "Operating Bank", "C-300", "M-3001", "CHK-501", "Court filing fee paid for Chen litigation", "", "", "0.00", "750.00"),
    (5, "2026-05-03", "5000", "Rent Expense", "", "", "EFT-RENT", "May office rent", "", "", "1900.00", "0.00"),
    (5, "2026-05-03", "2000", "Accounts Payable", "", "", "EFT-RENT", "May office rent accrued", "", "Acme Property Mgmt [{run}]", "0.00", "1900.00"),
]


DEMO_GL_HEADER = [
    "transaction_id", "date", "account_number", "account_name",
    "client_id", "matter_id", "reference", "description",
    "customer_name", "vendor_name", "debit", "credit",
]


def _txn_id(run_id: str, idx: int) -> str:
    """Return a transaction id like ``JE-D20260522T143012-3F9A-0001``.

    Strips the leading ``D-`` from the run id so the resulting string
    stays under typical QBO DocNumber length limits (21 chars) when the
    base index is small. We keep the run-id body but drop the dash to
    save characters.
    """
    body = run_id.removeprefix("D-").replace("-", "")
    return f"JE-{body}-{idx:04d}"


def build_demo_gl_rows(run_id: str) -> list:
    """Return demo GL rows salted with ``run_id``.

    Each row is a list aligned with ``DEMO_GL_HEADER``. The salting
    pattern:

      - ``transaction_id`` is rewritten to ``JE-<runbody>-<idx>``.
      - ``description`` has ``[<run_id>]`` appended.
      - ``customer_name`` / ``vendor_name`` substitute ``{run}`` if the
        template carries the marker.

    Account numbers, dates, and amounts are left alone — the goal is to
    test repeatable demos, not to fuzz the underlying ledger.
    """
    out = []
    for tpl in _GL_TEMPLATE:
        (idx, date, acct_no, acct_name, client_id, matter_id, ref, desc,
         cust_tpl, vend_tpl, debit, credit) = tpl
        out.append([
            _txn_id(run_id, idx),
            date,
            acct_no,
            acct_name,
            client_id,
            matter_id,
            ref,
            f"{desc} [{run_id}]",
            cust_tpl.format(run=run_id) if cust_tpl else "",
            vend_tpl.format(run=run_id) if vend_tpl else "",
            debit,
            credit,
        ])
    return out


# Trust listing: one row, salted with the run id so the trust-recon
# reconciliation step shows fresh "as of" data each run.
def build_demo_trust_listing_rows(run_id: str) -> list:
    return [
        [
            "C-100",
            f"Smith Holdings Inc. [{run_id}]",
            "M-1001",
            "Commercial Purchase",
            "1010",
            "8500.00",
            "2026-05-03",
        ],
    ]


DEMO_TRUST_HEADER = [
    "client_id", "client_name", "matter_id", "matter_name",
    "trust_bank_account", "trust_balance", "as_of_date",
]


# ---------------------------------------------------------------------------
# CSV rendering
# ---------------------------------------------------------------------------


def _render_csv(header: list, rows: Iterable) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


def render_chart_of_accounts_csv() -> str:
    """Return the demo COA CSV. Not salted (account numbers must stay
    stable so the second demo's COA step maps to the first demo's QBO
    accounts instead of creating duplicates)."""
    return _render_csv(DEMO_COA_HEADER, DEMO_CHART_OF_ACCOUNTS_ROWS)


def render_trial_balance_csv() -> str:
    return _render_csv(DEMO_TB_HEADER, DEMO_TRIAL_BALANCE_ROWS)


def render_ending_trial_balance_csv() -> str:
    """Return the demo Ending Trial Balance ("Final balance check") CSV."""
    return _render_csv(DEMO_TB_HEADER, DEMO_ENDING_TB_ROWS)


def render_general_ledger_csv(run_id: str) -> str:
    return _render_csv(DEMO_GL_HEADER, build_demo_gl_rows(run_id))


def render_trust_listing_csv(run_id: str) -> str:
    return _render_csv(DEMO_TRUST_HEADER, build_demo_trust_listing_rows(run_id))


# ---------------------------------------------------------------------------
# Balanced-by-construction check (used by tests)
# ---------------------------------------------------------------------------


def gl_is_balanced(run_id: str) -> bool:
    """Return True iff debit total == credit total in the salted GL.

    Demo dataset must always be internally balanced or the COA-first
    validation step will (correctly) refuse to import.
    """
    debit_total = 0
    credit_total = 0
    for row in build_demo_gl_rows(run_id):
        debit_total += int(round(float(row[10]) * 100))
        credit_total += int(round(float(row[11]) * 100))
    return debit_total == credit_total


# ---------------------------------------------------------------------------
# App-side demo reset
# ---------------------------------------------------------------------------


DEMO_ARCHIVED_STATUS_PREFIX = "Archived (demo reset"

# Marks a job whose report type was re-uploaded. When a lawyer uploads a
# replacement general ledger (or other report) for the same firm, the
# prior active job of that type is flipped to this status so it stops
# being treated as the "current" workflow context. Without this, Step 5
# could keep importing the *old* GL — it iterates firm GL jobs newest
# first but prefers any job that already has a QuickBooks connection, so
# a freshly uploaded (not-yet-connected) replacement loses to the stale
# connected upload. (Cesar QA item 4.) The job stays in the DB for the
# operator panel and audit history; it's just no longer "active".
SUPERSEDED_STATUS_PREFIX = "Superseded (replaced by newer upload"


def is_archived_demo_job(job: dict) -> bool:
    """True iff ``job`` was archived by a prior ``Start new demo`` reset.

    Jobs archived this way must be excluded from the dashboard, migration
    checklist, and customer workflow stepper so that ``Start new demo``
    actually clears the visible state. They stay in the DB (and so remain
    visible to the operator panel and audit history) so the reset is
    reversible / auditable, but they should no longer be treated as
    "current" workflow context.
    """
    status = (job.get("status") or "").strip()
    return status.startswith(DEMO_ARCHIVED_STATUS_PREFIX)


def is_superseded_job(job: dict) -> bool:
    """True iff ``job`` was replaced by a newer upload of the same type."""
    status = (job.get("status") or "").strip()
    return status.startswith(SUPERSEDED_STATUS_PREFIX)


def is_failed_job(job: dict) -> bool:
    """True iff ``job`` ended in a parse/validation failure.

    Failed uploads must never be picked as a Step 5 import target: a
    broken or wrong-shape file that fell back to the general-ledger pool
    could otherwise become the "latest GL" and be sent to QuickBooks.
    The upload path records these with a status beginning "Error:".
    """
    status = (job.get("status") or "").strip().lower()
    return status.startswith("error")


def filter_active_jobs(jobs) -> list:
    """Return only the jobs that should drive the dashboard/checklist.

    Drops jobs archived by a demo reset or superseded by a newer upload
    of the same report type; every other status (including real
    production "Imported" / "Failed" rows) is left alone.
    """
    return [
        j for j in jobs
        if not is_archived_demo_job(j) and not is_superseded_job(j)
    ]


def supersede_prior_jobs(db, firm_id: int, report_type: str,
                         keep_job_id: str) -> int:
    """Mark prior active jobs of ``report_type`` for a firm as superseded.

    Called after a new report of the same type is successfully ingested,
    so the fresh upload is unambiguously the active one. Returns the
    number of jobs flipped. Skips ``keep_job_id`` (the new upload), jobs
    already archived/superseded, and jobs of a different report type.
    Best-effort: a single bad row never blocks the new upload.
    """
    try:
        rows = db.list_jobs_for_firm(firm_id, limit=500) or []
    except Exception:  # noqa: BLE001
        return 0
    superseded = 0
    for job in rows:
        if job.get("id") == keep_job_id:
            continue
        rt = (job.get("report_type") or "general_ledger")
        if rt != report_type:
            continue
        status = (job.get("status") or "").strip()
        if status.startswith(DEMO_ARCHIVED_STATUS_PREFIX) or \
                status.startswith(SUPERSEDED_STATUS_PREFIX):
            continue
        try:
            db.update_job_status(
                job["id"], f"{SUPERSEDED_STATUS_PREFIX} {keep_job_id})"
            )
            superseded += 1
        except Exception:  # noqa: BLE001
            pass
    return superseded


def reset_demo_workspace(db, firm_id: int, run_id: str,
                         clear_cutover: bool = False) -> dict:
    """Archive all jobs for a firm so the demo starts from a clean slate.

    Returns a dict ``{"archived_jobs": int, "cleared_mappings": int,
    "cleared_cutover": int, "run_id": str}``.

    ``clear_cutover`` (default False) additionally removes the firm's
    cutover_settings row so the migration restarts from a blank Step 1.
    The repeatable demo deliberately leaves this False — re-typing the
    cutover date every demo run would be hostile. Production "Start a new
    migration" passes True so a genuinely fresh batch does not inherit the
    prior batch's cutover date / country / basis.

    Important:

      - This does **not** delete jobs. They stay in the DB so the
        operator panel and audit log keep a full history. We just flip
        the status string so the dashboard / checklist treat them as
        finished.
      - This does **not** touch QuickBooks Online in any way. It is a
        purely app-side reset. The caller's UI copy must make that
        clear: any QBO data created by prior demos is still there and
        must be cleaned up inside QuickBooks if desired.
      - Account mappings are cleared so the next demo walks the user
        through Step 3 (Match accounts) again. Without this, the
        workflow stepper sees firm-wide saved mappings from the
        previous demo and treats Match as already complete — which
        skips the user past Step 3 / Step 4 and lands them on Step 5
        Import even though they haven't actually re-matched accounts
        for the fresh run. Mappings are cheap to re-confirm via the
        existing alias auto-matcher and the create-missing flow.
      - QBO connections are deliberately preserved. The demo plays
        against one dedicated QuickBooks demo company across many
        runs — forcing a re-OAuth each demo would be hostile.
      - Scoped strictly to the supplied ``firm_id``. Callers must
        already have firm-scoped the user (login_required +
        firm-ownership check) before invoking this.
    """
    jobs = db.list_jobs_for_firm(firm_id, limit=500)
    archived = 0
    archived_status = f"{DEMO_ARCHIVED_STATUS_PREFIX} {run_id})"
    for job in jobs:
        status = (job.get("status") or "").strip()
        # Don't re-archive a job that's already been archived by a prior
        # reset. Otherwise the status string churns and the operator
        # panel timeline gets cluttered.
        if status.startswith(DEMO_ARCHIVED_STATUS_PREFIX):
            continue
        db.update_job_status(job["id"], archived_status)
        archived += 1

    # Clear account mappings across every connected QBO realm for this
    # firm. Best-effort — older deploys that did not ship the
    # delete-all helper still get a working reset, the workflow stepper
    # just won't re-walk Step 3 on those deploys.
    cleared_mappings = 0
    try:
        conns = db.list_qbo_connections_for_firm(firm_id)
    except Exception:
        conns = []
    for conn in conns:
        try:
            mappings = db.list_account_mappings(firm_id, conn["realm_id"])
        except Exception:
            mappings = []
        for m in mappings:
            try:
                db.delete_account_mapping(
                    firm_id=firm_id, realm_id=conn["realm_id"],
                    pclaw_account_number=m.get("pclaw_account_number"),
                    pclaw_account_name=m.get("pclaw_account_name"),
                )
                cleared_mappings += 1
            except Exception:
                # Don't let a single bad row block the whole reset.
                pass

    cleared_cutover = 0
    if clear_cutover:
        try:
            cleared_cutover = db.delete_cutover_settings(firm_id)
        except Exception:  # noqa: BLE001
            # Older deploys without the delete helper still get a working
            # reset; Step 1 just keeps its prior values.
            cleared_cutover = 0

    return {
        "archived_jobs": archived,
        "cleared_mappings": cleared_mappings,
        "cleared_cutover": cleared_cutover,
        "run_id": run_id,
    }
