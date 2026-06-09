"""Preview-only onboarding flow content (not the live production intake).

This module holds the customer-facing copy for an *example* onboarding
experience that maps the latest product notes into a clean, guided flow.
It is deliberately framework-agnostic and read-only: nothing here submits,
stores, or charges anything. The Flask route in app.py renders it as a
clearly-marked preview at /onboarding-preview so the team can review the
proposed flow in-app before it becomes the real customer path.

Brand/copy rules (same as intake.py):
  - "QuickBooks", never "QBO" in customer-facing copy.
  - "PCLaw" only to name the source software / the reports to export.
  - Plain English. Customers are lawyers, not accountants.
  - For the first 25 clients the tone is semi-managed: Cutovr reviews the
    files and handles setup. We do not promise fully self-serve automation.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Header / intro copy
# ---------------------------------------------------------------------------

PREVIEW_HEADER = "Your migration, prepared for you"

PREVIEW_SUBCOPY = (
    "Choose your package, tell us your cutover dates, and upload the reports "
    "you have. Cutovr reviews everything and lets you know if anything is "
    "missing."
)

# Shown for the first-25-clients semi-managed positioning. This is the line
# that keeps us honest: a person reviews the files, we are not promising a
# fully automated, self-serve migration yet.
SEMI_MANAGED_NOTE = (
    "While we get started, onboarding is hands-on: a Cutovr specialist "
    "personally reviews your files, confirms anything that's missing, and "
    "prepares your migration for you. You won't be left to figure out "
    "accounting on your own."
)


# ---------------------------------------------------------------------------
# Progress sections (the five guided cards)
# ---------------------------------------------------------------------------

PREVIEW_SECTIONS = (
    {
        "key": "package",
        "step": "1",
        "title": "Package and migration period",
        "summary": (
            "Confirm the package you chose and the period of history we'll "
            "bring across. History is priced per year."
        ),
    },
    {
        "key": "firm",
        "step": "2",
        "title": "Firm details",
        "summary": (
            "A few simple details about your firm and the best person for us "
            "to reach. Nothing technical."
        ),
    },
    {
        "key": "reports",
        "step": "3",
        "title": "Reports to upload",
        "summary": (
            "Export these reports from PCLaw and upload whatever you have. "
            "We'll tell you if anything is missing."
        ),
    },
    {
        "key": "addons",
        "step": "4",
        "title": "Optional add-ons and special cases",
        "summary": (
            "Trust ledger detail, accounts receivable/payable, and other "
            "items that only apply to some firms."
        ),
    },
    {
        "key": "next",
        "step": "5",
        "title": "What happens next",
        "summary": (
            "What Cutovr does after you submit, and how we work around your "
            "Clio migration date."
        ),
    },
)


# ---------------------------------------------------------------------------
# Required reports checklist
#
# Each entry maps one of the product-notes reports into plain English. `when`
# carries the timing guidance (e.g. "as at the cutover date"); `note` carries
# the "if you have it" / "monthly is better" caveats. `add_on` flags items we
# position as a future/add-on piece rather than part of the base migration.
# ---------------------------------------------------------------------------

REPORTS_CHECKLIST = (
    {
        "key": "coa",
        "title": "Chart of Accounts (Account List)",
        "when": "",
        "note": "Your full list of accounts with their numbers and names.",
        "required": True,
        "add_on": False,
    },
    {
        "key": "tb_beginning",
        "title": "Trial Balance — beginning",
        "when": "as at the last day before your first migration year",
        "note": (
            "We use this to set your opening balances in QuickBooks."
        ),
        "required": True,
        "add_on": False,
    },
    {
        "key": "tb_ending",
        "title": "Trial Balance — ending",
        "when": "as at your end date",
        "note": "We use this to reconcile your balances and check the move.",
        "required": True,
        "add_on": False,
    },
    {
        "key": "trust_listing",
        "title": "Trust Listing",
        "when": "as at your migration cutover date",
        "note": (
            "A high-level summary of money you hold in trust for clients. "
            "We use it to set your trust balances."
        ),
        "required": True,
        "add_on": False,
    },
    {
        "key": "general_ledger",
        "title": "General Ledgers — monthly",
        "when": "monthly, from your start date to your end date",
        "note": (
            "Monthly files are preferred — they're more reliable than a "
            "single yearly export. If you can only export by year, send what "
            "you have and we'll work with it."
        ),
        "required": True,
        "add_on": False,
    },
    {
        "key": "trust_ledger",
        "title": "Trust Ledger (itemized trust detail)",
        "when": "",
        "note": (
            "Line-by-line trust detail. This is an optional add-on and part "
            "of the future Clio piece — most firms don't need it to get "
            "started."
        ),
        "required": False,
        "add_on": True,
    },
    {
        "key": "accounts_receivable",
        "title": "Accounts Receivable",
        "when": "",
        "note": (
            "Money clients owe you. Only if your firm tracks it — many "
            "cash-basis firms don't, and that's fine."
        ),
        "required": False,
        "add_on": False,
    },
    {
        "key": "accounts_payable",
        "title": "Accounts Payable",
        "when": "",
        "note": (
            "Money you owe vendors. Only if your firm tracks it — many "
            "cash-basis firms don't, and that's fine."
        ),
        "required": False,
        "add_on": False,
    },
    {
        "key": "vendor_customer_lists",
        "title": "Vendor list and customer / client list",
        "when": "",
        "note": (
            "Who you pay and who pays you, if these come out as separate "
            "reports."
        ),
        "required": False,
        "add_on": False,
    },
)


# ---------------------------------------------------------------------------
# What-happens-next steps
# ---------------------------------------------------------------------------

WHAT_HAPPENS_NEXT = (
    "Cutovr reviews your files and confirms if anything is missing.",
    "We prepare your migration into QuickBooks and set your opening balances.",
    "We work around your Clio migration date so nothing clashes.",
    "We email you the next step. You don't need to do anything else right now.",
)


# ---------------------------------------------------------------------------
# Secure-access copy (Clio add-on)
#
# IMPORTANT: we never collect Clio passwords or 2FA codes in a form. If an
# add-on genuinely needs Clio access, the team coordinates it separately.
# ---------------------------------------------------------------------------

SECURE_ACCESS_NOTE = (
    "If secure Clio access is required for an add-on, our team will "
    "coordinate a secure access process separately. We never ask for your "
    "Clio password or two-factor codes through this form."
)


# ---------------------------------------------------------------------------
# Copyable "Reports we need" email/checklist
#
# Plain-English, customer-friendly. The {placeholders} are filled in by
# build_reports_email() using the firm's dates, with safe fallbacks so the
# template still reads cleanly when a date hasn't been chosen yet.
# ---------------------------------------------------------------------------

_DATE_PLACEHOLDER = "YYYY-MM-DD"


def _d(value: str | None) -> str:
    """A date string for the email, or the YYYY-MM-DD placeholder."""
    v = (value or "").strip()
    return v or _DATE_PLACEHOLDER


def build_reports_email(
    *,
    firm_name: str | None = None,
    tb_beginning_date: str | None = None,
    tb_ending_date: str | None = None,
    cutover_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_trust_ledger: bool = False,
) -> str:
    """Build the copyable 'Reports we need' checklist email body.

    Read-only helper — produces plain text the customer can copy. All dates
    fall back to a clear YYYY-MM-DD placeholder so the example always reads
    cleanly even with nothing filled in.
    """
    greeting_name = (firm_name or "").strip()
    intro_to = f" for {greeting_name}" if greeting_name else ""

    lines = [
        f"Here are the reports we need{intro_to} to prepare your QuickBooks migration.",
        "Export these from PCLaw and send whatever you have — we'll let you know if anything is missing.",
        "",
        "- Chart of Accounts",
        f"- Trial Balance beginning as at ({_d(tb_beginning_date)})",
        f"- Trial Balance ending as at ({_d(tb_ending_date)})",
        f"- Trust Listing as at migration cutover ({_d(cutover_date)})",
    ]
    if include_trust_ledger:
        lines.append("- Trust Ledger (add-on selected)")
    else:
        lines.append("- Trust Ledger (only if you've added the trust-ledger add-on)")
    lines += [
        f"- General Ledgers, monthly from start date ({_d(start_date)}) to end date ({_d(end_date)})",
        "- Accounts Payable, if available",
        "- Accounts Receivable, if available",
        "",
        "Monthly General Ledgers are best — they're more reliable than a single yearly export.",
        "Many cash-basis firms don't have Accounts Receivable or Payable, and that's completely fine.",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Secondary resource links
#
# Kept visually secondary in the template. External links are clearly marked.
# ---------------------------------------------------------------------------

RESOURCE_LINKS = (
    {
        "label": "Clio to QuickBooks integration overview",
        "url": "https://drive.google.com/file/d/1dBe_lWJDmJqWg2rcBFsaN3SHGJ6AvL3h/view?usp=drive_link",
        "external": True,
    },
    {
        "label": "PCLaw GL Export Guide",
        "url": "https://drive.google.com/file/d/13oUiLzS_WUA2oskKK7X7k5nbeA_5KTBO/view?usp=drive_link",
        "external": True,
    },
    {
        "label": "Connecting Clio settings to QuickBooks (external article)",
        "url": "https://www.artesaniaccounting.com/blog/clio-settings-connecting-to-quickbooks",
        "external": True,
    },
)
