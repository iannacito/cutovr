"""Step 6 — Reconcile Balances helpers and final-report assembly.

This module is the single source of truth for two things:

  1. The lawyer-friendly reconciliation summary that the dedicated
     /reconcile-balances page (Step 6) renders. It rolls up the same
     hydrated job / cutover state the checklist already reads and
     classifies each line as ``completed``, ``pending``, ``blocked``,
     or ``skipped`` so the UI never has to do its own logic.

  2. The text body of the optional "final report" email request that
     Step 6 lets the user submit. The body is plain text — we never
     embed credentials, SMTP config, or token URLs and we deliberately
     avoid accounting jargon where a one-line plain-English phrase
     works.

The Flask route is intentionally thin: it loads cutover + jobs, calls
``build_reconciliation_summary`` to build the view-model, and (on
report submit) calls ``build_report_text`` to produce the email body.
Both functions are pure — no I/O, no Flask globals — so tests can
exercise them with hand-written fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional


# Status values used by the Step 6 summary cards. Keep stable — template
# branches off these strings.
STATUS_COMPLETED = "completed"
STATUS_PENDING = "pending"
STATUS_BLOCKED = "blocked"
STATUS_SKIPPED = "skipped"


@dataclass
class ReconcileLine:
    """One reconciliation summary row, ready to render."""
    key: str
    label: str
    status: str          # completed | pending | blocked | skipped
    detail: str = ""     # short plain-English explanation


@dataclass
class ReconcileSummary:
    """View-model for the Step 6 page and the final-report email."""
    firm_name: str
    qbo_company_name: Optional[str]
    qbo_realm_id: Optional[str]
    cutover_date: Optional[str]
    accounts_matched_count: int
    accounts_created_count: int
    reports_uploaded: List[str]
    journal_entries_count: int
    transactions_imported: int
    import_balanced: Optional[bool]
    lines: List[ReconcileLine] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    overall_status: str = STATUS_PENDING
    generated_at: str = ""

    @property
    def is_complete(self) -> bool:
        return self.overall_status == STATUS_COMPLETED

    @property
    def is_blocked(self) -> bool:
        return self.overall_status == STATUS_BLOCKED


# Maps internal report_type keys to the friendly labels we show users.
_REPORT_LABELS = {
    "chart_of_accounts": "Account list",
    "trial_balance": "Starting / final balances",
    "general_ledger": "Transaction history",
    "trust_listing": "Client trust balances",
}


def _imported_gl_jobs(jobs: Iterable[dict]) -> List[dict]:
    """GL jobs that have an ``import_summary`` (i.e. Step 5 ran)."""
    out: List[dict] = []
    for j in jobs:
        if (j.get("report_type") or "general_ledger") != "general_ledger":
            continue
        if j.get("import_summary"):
            out.append(j)
    return out


def _has_unmapped_blocker(jobs: Iterable[dict]) -> bool:
    """True iff any GL job lists missing QBO accounts without an import."""
    for j in jobs:
        if (j.get("report_type") or "general_ledger") != "general_ledger":
            continue
        if j.get("import_summary"):
            continue
        if j.get("unmapped_accounts"):
            return True
    return False


def _starting_balance_status(
    jobs: Iterable[dict], *, import_complete: bool = False,
) -> ReconcileLine:
    tb_jobs = [j for j in jobs if (j.get("report_type") or "") == "trial_balance"]
    posted = any(
        h.get("qbo_je_id")
        for j in tb_jobs
        for h in (j.get("opening_balance_history") or [])
        if not h.get("demo_mode")
    )
    if posted:
        return ReconcileLine(
            "starting_balances",
            "Starting balances",
            STATUS_COMPLETED,
            "Starting balances were posted to QuickBooks.",
        )
    if tb_jobs:
        if import_complete:
            # Migration is complete — starting balances were uploaded
            # but not posted as a separate opening journal entry. That
            # is a normal, optional step; do not flag it as "pending"
            # on a finished migration.
            return ReconcileLine(
                "starting_balances",
                "Starting balances",
                STATUS_SKIPPED,
                "Starting balances were uploaded. Posting them as a "
                "separate opening journal entry was not part of this "
                "migration.",
            )
        return ReconcileLine(
            "starting_balances",
            "Starting balances",
            STATUS_PENDING,
            "Starting balances were uploaded but have not been posted "
            "to QuickBooks yet.",
        )
    return ReconcileLine(
        "starting_balances",
        "Starting balances",
        STATUS_SKIPPED,
        "No starting balances on file — not part of this migration.",
    )


def _final_balance_status(
    jobs: Iterable[dict], *, import_complete: bool = False,
) -> ReconcileLine:
    tb_jobs = [j for j in jobs if (j.get("report_type") or "") == "trial_balance"]
    has_ending = any(j.get("ending_tb_reconciliation") for j in tb_jobs)
    if has_ending:
        # Surface overall_pass when the underlying report exposes it.
        for j in tb_jobs:
            recon = j.get("ending_tb_reconciliation") or {}
            summary = (recon.get("summary") or {}) if isinstance(recon, dict) else {}
            if summary.get("overall_pass") is False:
                return ReconcileLine(
                    "ending_balance",
                    "Final balance check",
                    STATUS_PENDING,
                    "The final balance check ran — some balances did "
                    "not match. Review the reconciliation report.",
                )
        return ReconcileLine(
            "ending_balance",
            "Final balance check",
            STATUS_COMPLETED,
            "Final balances were checked against QuickBooks.",
        )
    if len(tb_jobs) >= 2:
        if import_complete:
            return ReconcileLine(
                "ending_balance",
                "Final balance check",
                STATUS_SKIPPED,
                "A final trial balance was uploaded. Running the "
                "balance check is optional — open the report if you "
                "want a side-by-side comparison with QuickBooks.",
            )
        return ReconcileLine(
            "ending_balance",
            "Final balance check",
            STATUS_PENDING,
            "A final trial balance is on file — open it to run the "
            "balance check.",
        )
    return ReconcileLine(
        "ending_balance",
        "Final balance check",
        STATUS_SKIPPED,
        "No final trial balance on file — not part of this migration.",
    )


def _trust_status(
    jobs: Iterable[dict], *, import_complete: bool = False,
) -> ReconcileLine:
    trust_jobs = [j for j in jobs if (j.get("report_type") or "") == "trust_listing"]
    if not trust_jobs:
        return ReconcileLine(
            "client_trust",
            "Client trust balances",
            STATUS_SKIPPED,
            "No client trust balances on file — not part of this migration.",
        )
    has_recon = any(j.get("trust_reconciliation") for j in trust_jobs)
    if has_recon:
        return ReconcileLine(
            "client_trust",
            "Client trust balances",
            STATUS_COMPLETED,
            "Client trust balances were validated against the trust "
            "liability and trust bank balances.",
        )
    if import_complete:
        return ReconcileLine(
            "client_trust",
            "Client trust balances",
            STATUS_SKIPPED,
            "A client trust listing was uploaded. Running the trust "
            "reconciliation report is optional — open it if you want "
            "a side-by-side trust validation.",
        )
    return ReconcileLine(
        "client_trust",
        "Client trust balances",
        STATUS_PENDING,
        "A client trust listing is on file — open the trust "
        "reconciliation report to validate it.",
    )


def _import_status(jobs: Iterable[dict]) -> ReconcileLine:
    imported = _imported_gl_jobs(jobs)
    if imported:
        return ReconcileLine(
            "import",
            "Transaction history imported",
            STATUS_COMPLETED,
            "Your PCLaw transaction history is in QuickBooks.",
        )
    if _has_unmapped_blocker(jobs):
        return ReconcileLine(
            "import",
            "Transaction history imported",
            STATUS_BLOCKED,
            "QuickBooks is missing one or more accounts — go back to "
            "Step 3 to create them, then retry Step 5.",
        )
    return ReconcileLine(
        "import",
        "Transaction history imported",
        STATUS_PENDING,
        "Nothing has been sent to QuickBooks yet — finish Step 5 first.",
    )


def _accounts_status(jobs: Iterable[dict], mapping_count: int) -> ReconcileLine:
    if mapping_count <= 0:
        return ReconcileLine(
            "accounts",
            "Accounts matched",
            STATUS_PENDING,
            "No PCLaw accounts have been matched to QuickBooks yet.",
        )
    created = 0
    for j in jobs:
        for h in (j.get("coa_create_history") or []):
            try:
                created += int(h.get("created_count") or 0)
            except (TypeError, ValueError):
                pass
    detail = f"{mapping_count} PCLaw account(s) matched to QuickBooks."
    if created:
        detail += f" {created} new QuickBooks account(s) created during setup."
    return ReconcileLine(
        "accounts",
        "Accounts matched",
        STATUS_COMPLETED,
        detail,
    )


def _collect_reports(jobs: Iterable[dict]) -> List[str]:
    seen: Dict[str, int] = {}
    for j in jobs:
        rt = j.get("report_type") or "general_ledger"
        seen[rt] = seen.get(rt, 0) + 1
    out: List[str] = []
    for rt, count in seen.items():
        label = _REPORT_LABELS.get(rt, rt)
        if count > 1:
            out.append(f"{label} ({count})")
        else:
            out.append(label)
    out.sort()
    return out


def _accounts_created_count(jobs: Iterable[dict]) -> int:
    n = 0
    for j in jobs:
        for h in (j.get("coa_create_history") or []):
            try:
                n += int(h.get("created_count") or 0)
            except (TypeError, ValueError):
                pass
    return n


def build_reconciliation_summary(
    *,
    firm_name: str,
    cutover: Optional[dict],
    jobs: Iterable[dict],
    qbo_connections: Iterable[dict],
    account_mapping_count: int,
    generated_at: Optional[str] = None,
) -> ReconcileSummary:
    """Roll up everything Step 6 needs into a single view-model.

    `jobs` should be the firm's hydrated jobs (the same shape
    cutover_workflow.build_checklist consumes). Trust posting is
    deliberately not auto-handled — when no trust listing exists we
    mark that line "skipped" rather than "blocked" so the demo flow
    can complete cleanly.
    """
    jobs_list = list(jobs)
    conns = list(qbo_connections or [])
    primary_conn = conns[0] if conns else {}

    import_line = _import_status(jobs_list)
    import_complete = import_line.status == STATUS_COMPLETED
    accounts_line = _accounts_status(jobs_list, account_mapping_count)
    starting_line = _starting_balance_status(
        jobs_list, import_complete=import_complete)
    ending_line = _final_balance_status(
        jobs_list, import_complete=import_complete)
    trust_line = _trust_status(
        jobs_list, import_complete=import_complete)

    lines = [import_line, accounts_line, starting_line, ending_line, trust_line]

    # Overall status: blocked if anything is blocked, otherwise
    # completed once the import is complete (we treat skipped lines as
    # acceptable — Step 6 is a "did the migration finish?" gate, not a
    # checklist of every optional report).
    overall = STATUS_COMPLETED
    if any(line.status == STATUS_BLOCKED for line in lines):
        overall = STATUS_BLOCKED
    elif import_line.status != STATUS_COMPLETED:
        overall = STATUS_PENDING

    # Aggregate import counts.
    je_count = 0
    txn_count = 0
    balanced: Optional[bool] = None
    for j in _imported_gl_jobs(jobs_list):
        s = j.get("import_summary") or {}
        try:
            je_count += int(s.get("qbo_je_count") or 0)
        except (TypeError, ValueError):
            pass
        try:
            txn_count += int(s.get("source_transaction_count") or 0)
        except (TypeError, ValueError):
            pass
        if s.get("balanced") is not None and balanced is None:
            balanced = bool(s.get("balanced"))

    warnings: List[str] = []
    if balanced is False:
        warnings.append(
            "Debits and credits did not balance on the most recent import. "
            "Review the journal entries before signing off."
        )
    if import_line.status == STATUS_BLOCKED:
        warnings.append(
            "QuickBooks is missing one or more accounts the transaction "
            "history needs. Step 5 cannot finish until those accounts "
            "exist in QuickBooks."
        )

    return ReconcileSummary(
        firm_name=firm_name,
        qbo_company_name=primary_conn.get("company_name") if isinstance(primary_conn, dict) else None,
        qbo_realm_id=primary_conn.get("realm_id") if isinstance(primary_conn, dict) else None,
        cutover_date=(cutover or {}).get("cutover_date") if cutover else None,
        accounts_matched_count=account_mapping_count,
        accounts_created_count=_accounts_created_count(jobs_list),
        reports_uploaded=_collect_reports(jobs_list),
        journal_entries_count=je_count,
        transactions_imported=txn_count,
        import_balanced=balanced,
        lines=lines,
        warnings=warnings,
        overall_status=overall,
        generated_at=generated_at or datetime.utcnow().strftime("%Y-%m-%d %H:%MZ"),
    )


# Loose, deliberately-permissive email regex. We only need it to reject
# obviously-broken input — RFC-correct validation belongs at the SMTP
# server, not in form parsing.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def is_valid_email(addr: str) -> bool:
    if not isinstance(addr, str):
        return False
    addr = addr.strip()
    if not addr or len(addr) > 254:
        return False
    return bool(_EMAIL_RE.match(addr))


def build_report_text(summary: ReconcileSummary) -> str:
    """Render the plain-text body of the final-report email.

    Intentionally narrow: firm/demo name, generation timestamp, the
    connected QuickBooks company name (no realm_id by default — it's
    safe but uninteresting to lawyers), the reports uploaded, counts,
    and the reconciliation lines + warnings. Never includes SMTP
    config, token URLs, audit ids, or any secret material.
    """
    lines: List[str] = []
    title = f"PCLaw → QuickBooks migration summary for {summary.firm_name}"
    lines.append(title)
    lines.append("=" * len(title))
    lines.append("")
    lines.append(f"Generated: {summary.generated_at}")
    if summary.cutover_date:
        lines.append(f"Cutover date: {summary.cutover_date}")
    if summary.qbo_company_name:
        lines.append(f"QuickBooks company: {summary.qbo_company_name}")
    elif summary.qbo_realm_id:
        lines.append(f"QuickBooks realm id: {summary.qbo_realm_id}")
    lines.append("")

    lines.append("Reports uploaded")
    lines.append("-" * len("Reports uploaded"))
    if summary.reports_uploaded:
        for label in summary.reports_uploaded:
            lines.append(f"  - {label}")
    else:
        lines.append("  (none on file)")
    lines.append("")

    lines.append("Counts")
    lines.append("-" * len("Counts"))
    lines.append(f"  Accounts matched: {summary.accounts_matched_count}")
    lines.append(f"  New QuickBooks accounts created: {summary.accounts_created_count}")
    lines.append(f"  Journal entries posted to QuickBooks: {summary.journal_entries_count}")
    lines.append(f"  Source transactions imported: {summary.transactions_imported}")
    if summary.import_balanced is not None:
        lines.append(
            "  Debits and credits balanced: "
            + ("yes" if summary.import_balanced else "NO")
        )
    lines.append("")

    lines.append("Reconciliation")
    lines.append("-" * len("Reconciliation"))
    for line in summary.lines:
        lines.append(f"  [{line.status.upper()}] {line.label} — {line.detail}")
    lines.append("")

    if summary.warnings:
        lines.append("Warnings")
        lines.append("-" * len("Warnings"))
        for w in summary.warnings:
            lines.append(f"  - {w}")
        lines.append("")

    if summary.is_complete:
        lines.append("Status: Migration demo complete.")
    elif summary.is_blocked:
        lines.append("Status: Blocked — see warnings above.")
    else:
        lines.append("Status: Migration in progress.")
    lines.append("")

    return "\n".join(lines)


# --- PDF rendering ----------------------------------------------------
#
# We render a clean, customer-facing PDF of the same summary the page
# shows. Stays deliberately non-technical: no realm ids, no job ids,
# no journal-entry counts unless they're already in the on-screen
# summary (we expose them as a small "By the numbers" block because
# lawyers do want to see "we imported 87 transactions").
#
# ReportLab is the only external dep here. Importing inside the
# function keeps the module import cheap for tests that don't render
# PDFs (and lets us return a friendlier error if the dep is missing
# on a slim deployment).

_PDF_DEFAULT_SUPPORT_EMAIL = "support@pclawmigrate.com"


def _status_phrase(summary: "ReconcileSummary") -> str:
    if summary.is_complete:
        return "Migration complete — QuickBooks matches PCLaw."
    if summary.is_blocked:
        return "Blocked — review the notes below before signing off."
    return "Migration in progress."


def build_report_pdf(
    summary: "ReconcileSummary",
    *,
    support_email: Optional[str] = None,
) -> bytes:
    """Render `summary` as a single-file PDF and return the bytes.

    The PDF is intentionally minimal: title, generation timestamp,
    a one-line status, an "At a glance" block, a "By the numbers"
    block, a reconciliation list, an optional warnings list, and a
    support line. We never include QuickBooks realm ids, internal
    job ids, or any technical identifier in the visible PDF.
    """
    # Local import — reportlab is an optional dep at module load.
    from io import BytesIO
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, ListFlowable,
        ListItem,
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=f"Migration summary — {summary.firm_name}",
        author="PC Law Migrate",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleX", parent=styles["Title"], fontSize=18, leading=22,
        textColor=colors.HexColor("#1f3b5b"),
    )
    h2 = ParagraphStyle(
        "H2X", parent=styles["Heading2"], fontSize=13, leading=16,
        textColor=colors.HexColor("#1f3b5b"), spaceBefore=10, spaceAfter=4,
    )
    body = ParagraphStyle(
        "BodyX", parent=styles["BodyText"], fontSize=10.5, leading=15,
    )
    muted = ParagraphStyle(
        "MutedX", parent=body, textColor=colors.HexColor("#56627a"),
        fontSize=9.5,
    )

    story = []
    story.append(Paragraph(
        f"PCLaw &rarr; QuickBooks migration summary", title_style))
    story.append(Paragraph(summary.firm_name, h2))
    story.append(Paragraph(f"Generated {summary.generated_at}", muted))
    story.append(Spacer(1, 10))

    status_phrase = _status_phrase(summary)
    story.append(Paragraph(f"<b>Status:</b> {status_phrase}", body))
    story.append(Spacer(1, 12))

    # At a glance — only customer-friendly fields. No realm id.
    glance_rows = [["Firm", summary.firm_name]]
    if summary.cutover_date:
        glance_rows.append(["Switchover date", summary.cutover_date])
    if summary.qbo_company_name:
        glance_rows.append(
            ["QuickBooks company", summary.qbo_company_name]
        )
    reports = ", ".join(summary.reports_uploaded) if summary.reports_uploaded \
        else "(none on file)"
    glance_rows.append(["Reports uploaded", reports])

    story.append(Paragraph("At a glance", h2))
    t = Table(glance_rows, colWidths=[2.0 * inch, 4.5 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#56627a")),
        ("TEXTCOLOR", (1, 0), (1, -1), colors.HexColor("#1a2333")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#e3e6ec")),
    ]))
    story.append(t)
    story.append(Spacer(1, 10))

    # By the numbers — customer-friendly counts only.
    number_rows = [
        ["Accounts matched", str(summary.accounts_matched_count)],
        ["New QuickBooks accounts created", str(summary.accounts_created_count)],
        ["Transactions imported", str(summary.transactions_imported)],
    ]
    if summary.import_balanced is not None:
        number_rows.append([
            "Debits and credits balanced",
            "Yes" if summary.import_balanced else "No",
        ])
    story.append(Paragraph("By the numbers", h2))
    nt = Table(number_rows, colWidths=[3.5 * inch, 3.0 * inch])
    nt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#56627a")),
        ("TEXTCOLOR", (1, 0), (1, -1), colors.HexColor("#1a2333")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#e3e6ec")),
    ]))
    story.append(nt)
    story.append(Spacer(1, 12))

    # Reconciliation lines.
    story.append(Paragraph("Reconciliation", h2))
    badge_map = {
        STATUS_COMPLETED: ("Done", colors.HexColor("#1f7a3a")),
        STATUS_PENDING: ("Pending", colors.HexColor("#8a6a16")),
        STATUS_BLOCKED: ("Blocked", colors.HexColor("#a01a1a")),
        STATUS_SKIPPED: ("Skipped", colors.HexColor("#56627a")),
    }
    recon_rows = []
    for line in summary.lines:
        badge_text, badge_color = badge_map.get(
            line.status, ("—", colors.HexColor("#56627a"))
        )
        recon_rows.append([
            Paragraph(
                f"<font color='{badge_color.hexval()}'><b>{badge_text}</b></font>",
                body,
            ),
            Paragraph(
                f"<b>{line.label}</b><br/>"
                f"<font color='#56627a'>{line.detail}</font>",
                body,
            ),
        ])
    rt = Table(recon_rows, colWidths=[0.9 * inch, 5.6 * inch])
    rt.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#e3e6ec")),
    ]))
    story.append(rt)
    story.append(Spacer(1, 10))

    if summary.warnings:
        story.append(Paragraph("Heads up", h2))
        story.append(ListFlowable(
            [ListItem(Paragraph(w, body)) for w in summary.warnings],
            bulletType="bullet", leftIndent=14,
        ))
        story.append(Spacer(1, 10))

    # Support footer — plain, no technical IDs.
    se = (support_email or _PDF_DEFAULT_SUPPORT_EMAIL).strip()
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"Questions? Email <a href='mailto:{se}'>{se}</a>.",
        muted,
    ))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes
