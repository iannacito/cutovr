"""Migration service lanes — the durable source of truth for *what* a firm is
migrating and *where to*.

Cutovr started as a single done-for-you lane: PCLaw to QuickBooks Online. This
module introduces first-class, stable identifiers for that lane plus two new
"Clio Accounting Cutover Readiness" lanes so intake, admin, and the migration
workflow can branch on a firm's chosen service without scattering string
literals across the app.

Product reality (read before extending):

  Clio Accounting does NOT currently expose a public/open API for full
  accounting migration — no journal-entry posting, chart-of-accounts creation,
  opening balances, or direct general-ledger import. So the two Clio lanes are
  deliberately scoped as *readiness* services: Cutovr extracts and validates the
  source data, prepares a clean cutover package (including Clio-compatible bank
  CSVs where applicable), organizes the historical archive, and hands the firm's
  accountant/team a guided review. When Clio opens its Accounting API, the
  ``uses_qbo_posting`` / readiness scaffolding here is where direct automation
  will slot in — see ``clio_accounting.ClioAccountingIntegrationStatus``.

Only the PCLaw-to-QuickBooks lane runs the QuickBooks posting workflow. The two
Clio lanes must never be routed through QBO posting / "Send to QuickBooks".
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Lane identifiers (stable slugs — persisted, so never rename in place)
# ---------------------------------------------------------------------------

PCLAW_TO_QBO = "pclaw_to_qbo"
PCLAW_TO_CLIO_ACCOUNTING = "pclaw_to_clio_accounting"
QBO_TO_CLIO_ACCOUNTING = "qbo_to_clio_accounting"

# The lane assumed for any record created before service lanes existed, and for
# any record that never captured a lane. Preserves the original behavior:
# existing intakes/leads keep flowing through PCLaw -> QuickBooks.
DEFAULT_LANE = PCLAW_TO_QBO


# ---------------------------------------------------------------------------
# Lane metadata
#
# ``discovery_label`` is the calm, no-pricing phrase used on discovery / intake
# UI. ``uses_qbo_posting`` gates the existing QuickBooks "Send to QuickBooks"
# workflow — True only for the original lane.
# ---------------------------------------------------------------------------

_LANES: tuple[dict, ...] = (
    {
        "slug": PCLAW_TO_QBO,
        "label": "PC Law to QuickBooks Online",
        "short_label": "PCLaw → QuickBooks",
        "discovery_label": "PC Law to QuickBooks Online migration",
        "source": "PCLaw",
        "target": "QuickBooks Online",
        "is_clio_accounting": False,
        "uses_qbo_posting": True,
        "blurb": (
            "Our done-for-you migration: we scope, migrate, and reconcile your "
            "PCLaw books into QuickBooks Online."
        ),
    },
    {
        "slug": PCLAW_TO_CLIO_ACCOUNTING,
        "label": "PC Law to Clio Accounting Readiness",
        "short_label": "PCLaw → Clio Accounting",
        "discovery_label": "PC Law to Clio Accounting Readiness",
        "source": "PCLaw",
        "target": "Clio Accounting",
        "is_clio_accounting": True,
        "uses_qbo_posting": False,
        "blurb": (
            "We extract and validate your PCLaw data and prepare a clean Clio "
            "Accounting cutover package for a guided setup."
        ),
    },
    {
        "slug": QBO_TO_CLIO_ACCOUNTING,
        "label": "QuickBooks Online to Clio Accounting Readiness",
        "short_label": "QuickBooks → Clio Accounting",
        "discovery_label": "QuickBooks Online to Clio Accounting Readiness",
        "source": "QuickBooks Online",
        "target": "Clio Accounting",
        "is_clio_accounting": True,
        "uses_qbo_posting": False,
        "blurb": (
            "We collect and validate your QuickBooks Online data and prepare a "
            "clean Clio Accounting cutover package for a guided setup."
        ),
    },
)

_LANE_BY_SLUG = {lane["slug"]: lane for lane in _LANES}

# Lanes offered as selectable options on discovery / intake, in display order.
SELECTABLE_LANES = tuple(lane["slug"] for lane in _LANES)

# The subset positioned as "Clio Accounting Cutover Readiness" services.
CLIO_ACCOUNTING_LANES = tuple(
    lane["slug"] for lane in _LANES if lane["is_clio_accounting"]
)


def normalize(slug: Optional[str]) -> Optional[str]:
    """Return a known lane slug (trimmed/lowercased) or None."""
    if not slug:
        return None
    s = slug.strip().lower()
    return s if s in _LANE_BY_SLUG else None


def effective_lane(slug: Optional[str]) -> str:
    """Resolve to a concrete lane, falling back to the default.

    Use this in workflow code that must always have a lane (e.g. deciding
    whether QuickBooks posting applies). For *display* of a possibly-missing
    lane, prefer ``label`` with its "Not specified" fallback.
    """
    return normalize(slug) or DEFAULT_LANE


def meta(slug: Optional[str]) -> Optional[dict]:
    """Full metadata dict for a lane slug, or None if unknown."""
    known = normalize(slug)
    return _LANE_BY_SLUG.get(known) if known else None


def label(slug: Optional[str], *, default: str = "Not specified") -> str:
    """Human label for a lane slug. Unknown/missing -> ``default``."""
    m = meta(slug)
    return m["label"] if m else default


def short_label(slug: Optional[str], *, default: str = "Not specified") -> str:
    m = meta(slug)
    return m["short_label"] if m else default


def is_clio_accounting(slug: Optional[str]) -> bool:
    """True when the lane targets Clio Accounting (readiness lanes)."""
    m = meta(slug)
    return bool(m and m["is_clio_accounting"])


def uses_qbo_posting(slug: Optional[str]) -> bool:
    """True only for lanes that run the QuickBooks posting workflow.

    Missing/unknown lanes fall back to the default lane, which DOES post to
    QuickBooks — so pre-existing records keep their original behavior.
    """
    return _LANE_BY_SLUG[effective_lane(slug)]["uses_qbo_posting"]


def selectable_options() -> list[dict]:
    """Discovery/intake option list: [{slug, label, discovery_label, blurb}]."""
    return [
        {
            "slug": lane["slug"],
            "label": lane["label"],
            "discovery_label": lane["discovery_label"],
            "blurb": lane["blurb"],
            "is_clio_accounting": lane["is_clio_accounting"],
        }
        for lane in _LANES
    ]


# ---------------------------------------------------------------------------
# Service-lane detection from free text
#
# Calendly question labels/answers and event names are operator-defined free
# text. When a lead answers "which migration?" we map their words onto a stable
# slug so admin views and workflow branching stay clean. Order matters: the
# more specific Clio lanes are checked before the default.
# ---------------------------------------------------------------------------

def detect_service_lane(*texts: Optional[str]) -> Optional[str]:
    """Best-effort map free text onto a lane slug, or None if unclear.

    Checks each provided string (question answer, event name, etc.). Returns a
    stable slug when the text clearly names a source + Clio Accounting, or the
    original PCLaw -> QuickBooks lane. Never raises.
    """
    blob = " ".join(t for t in texts if t).strip().lower()
    if not blob:
        return None

    mentions_clio = "clio" in blob
    mentions_pclaw = "pclaw" in blob or "pc law" in blob
    mentions_qbo = (
        "quickbooks" in blob or "qbo" in blob or "quick books" in blob
    )

    if mentions_clio:
        if mentions_pclaw:
            return PCLAW_TO_CLIO_ACCOUNTING
        if mentions_qbo:
            return QBO_TO_CLIO_ACCOUNTING
        # "Clio Accounting readiness" with no clear source — leave for a human.
        return None
    if mentions_pclaw and mentions_qbo:
        return PCLAW_TO_QBO
    return None


# ---------------------------------------------------------------------------
# Readiness document checklists (Task D)
#
# Framed as "needed to PREPARE your Clio Accounting cutover package" — never
# "needed to import directly into Clio". Each item: {key, title, help, optional}.
# ``optional`` items use "if used / if available" language so firms aren't
# alarmed by documents that may not apply to them.
# ---------------------------------------------------------------------------

_PCLAW_CLIO_DOCS: tuple[dict, ...] = (
    {"key": "chart_of_accounts", "title": "Chart of Accounts",
     "help": "Your full account list with numbers and names.", "optional": False},
    {"key": "beginning_trial_balance", "title": "Beginning Trial Balance",
     "help": "Balances at the start of the period you want to carry over.", "optional": False},
    {"key": "ending_trial_balance", "title": "Ending / cutover Trial Balance",
     "help": "Balances as of your cutover date — the numbers Clio will open with.", "optional": False},
    {"key": "general_ledger_monthly", "title": "General Ledger by month",
     "help": "Transaction detail, ideally exported month by month.", "optional": False},
    {"key": "trust_listing", "title": "Trust Listing as of cutover",
     "help": "Client trust balances at the cutover date.", "optional": False},
    {"key": "trust_ledger_detail", "title": "Trust Ledger / detailed trust activity",
     "help": "Detailed trust transactions, if available.", "optional": True},
    {"key": "accounts_receivable", "title": "Accounts Receivable",
     "help": "Outstanding client balances, if you use A/R.", "optional": True},
    {"key": "accounts_payable", "title": "Accounts Payable",
     "help": "Amounts owed to vendors, if you use A/P.", "optional": True},
    {"key": "bank_statements", "title": "Bank statements or bank CSV exports",
     "help": "For your operating and trust accounts, so we can prepare Clio-compatible bank CSVs.", "optional": False},
    {"key": "vendor_list", "title": "Vendor list",
     "help": "Who you pay, if available as a report.", "optional": True},
    {"key": "customer_list", "title": "Client / customer list",
     "help": "Who pays you, if available as a report.", "optional": True},
    {"key": "historical_backup", "title": "Historical backup / export package",
     "help": "A full PCLaw backup or export for your archive, if available.", "optional": True},
)

_QBO_CLIO_DOCS: tuple[dict, ...] = (
    {"key": "chart_of_accounts", "title": "Chart of Accounts",
     "help": "Your full account list from QuickBooks Online.", "optional": False},
    {"key": "trial_balance", "title": "Trial Balance at cutover",
     "help": "Balances as of your cutover date — the numbers Clio will open with.", "optional": False},
    {"key": "general_ledger", "title": "General Ledger / transaction detail",
     "help": "Transaction detail for the scoped period.", "optional": False},
    {"key": "customer_list", "title": "Customer / client list",
     "help": "Your customers from QuickBooks Online.", "optional": False},
    {"key": "vendor_list", "title": "Vendor list",
     "help": "Your vendors from QuickBooks Online.", "optional": False},
    {"key": "ar_aging", "title": "A/R Aging",
     "help": "Outstanding receivables by age, if you use A/R.", "optional": True},
    {"key": "ap_aging", "title": "A/P Aging",
     "help": "Outstanding payables by age, if you use A/P.", "optional": True},
    {"key": "bank_cc_statements", "title": "Bank and credit card statements or QBO CSV exports",
     "help": "So we can prepare Clio-compatible bank CSVs.", "optional": False},
    {"key": "trust_reports", "title": "Trust account reports",
     "help": "If you track trust/IOLTA accounting in QuickBooks.", "optional": True},
    {"key": "reconciliation_reports", "title": "Reconciliation reports",
     "help": "For bank, trust, and credit card accounts.", "optional": False},
    {"key": "historical_archive", "title": "Historical report archive",
     "help": "Prior-period reports for your archive, if available.", "optional": True},
)

_READINESS_DOCS = {
    PCLAW_TO_CLIO_ACCOUNTING: _PCLAW_CLIO_DOCS,
    QBO_TO_CLIO_ACCOUNTING: _QBO_CLIO_DOCS,
}

READINESS_DOCS_TAGLINE = (
    "These are the documents we use to prepare your Clio Accounting cutover "
    "package. Send whatever you have — the more complete your files, the "
    "smoother your cutover."
)


def readiness_documents(slug: Optional[str]) -> list[dict]:
    """Document checklist for a Clio Accounting readiness lane.

    Returns an empty list for the QuickBooks lane (which has its own PCLaw
    report guidance) or an unknown lane.
    """
    known = normalize(slug)
    return list(_READINESS_DOCS.get(known, ()))


# ---------------------------------------------------------------------------
# Readiness workflow stages (Task E)
#
# A calm, non-invasive path for the Clio Accounting lanes. This is intentionally
# separate from the QuickBooks posting stepper — no "Send to QuickBooks" step.
# ---------------------------------------------------------------------------

READINESS_STAGES: tuple[dict, ...] = (
    {"key": "data_collected", "label": "Data collected",
     "blurb": "We’ve received your source exports and files."},
    {"key": "data_reviewed", "label": "Data reviewed",
     "blurb": "Our team has reviewed and validated the data."},
    {"key": "exceptions_identified", "label": "Exceptions identified",
     "blurb": "Anything that needs a decision is flagged for review."},
    {"key": "bank_csvs_prepared", "label": "Bank CSVs prepared",
     "blurb": "Clio-compatible bank CSVs are prepared, where applicable."},
    {"key": "opening_balance_package", "label": "Opening balance package prepared",
     "blurb": "Your validated opening balances are packaged for setup."},
    {"key": "historical_archive", "label": "Historical archive prepared",
     "blurb": "Prior-period records are organized for safekeeping."},
    {"key": "ready_for_review", "label": "Ready for discovery / accountant review",
     "blurb": "Your cutover package is ready for a guided review."},
)


def readiness_stages() -> list[dict]:
    return [dict(s) for s in READINESS_STAGES]
