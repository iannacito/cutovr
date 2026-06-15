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
    "Choose how much history to move, tell us about your firm, and pay "
    "securely. Only then do we tell you exactly which reports we need — and "
    "we handle the migration around your Clio migration date."
)


# ---------------------------------------------------------------------------
# Plan cards (Step 1)
#
# Plans are history-based, chosen by how much history is migrated. They are
# an internal scoping label only — we do NOT show a public dollar amount.
# Pricing is given on the discovery call after we scope the migration:
#   Essentials  Current year
#   Standard    Up to three years  (most common)
#   Complete    Three or more years
#
# `slug` maps to stripe_checkout.BASE_PLANS for any future private payment
# link. No public CTA exposes a price — the customer books a discovery call.
# ---------------------------------------------------------------------------

QUOTE_ON_CALL = "Quote on discovery call"

PLAN_CARDS = (
    {
        "key": "essential",
        "slug": "essential",
        "name": "Essentials",
        "price": QUOTE_ON_CALL,
        "period": "Current year",
        "summary": "Best for firms that only need this year moved over.",
        "covers": (
            "This year's history into QuickBooks",
            "Chart of accounts and opening balances",
            "Trust balances set up",
            "Final migration report",
        ),
        "featured": False,
        "quote": False,
    },
    {
        "key": "standard",
        "slug": "standard",
        "name": "Standard",
        "price": QUOTE_ON_CALL,
        "period": "Up to three years",
        "summary": "The default for most firms making the switch.",
        "covers": (
            "Everything in Essentials",
            "Up to three years of history",
            "Trust balance support",
            "Final migration report",
        ),
        "featured": True,
        "quote": False,
    },
    {
        "key": "complete",
        "slug": None,
        "name": "Complete",
        "price": QUOTE_ON_CALL,
        "period": "Three or more years",
        "summary": "For firms with a deeper historical record to bring across.",
        "covers": (
            "Everything in Standard",
            "Three or more years of history",
            "Larger file volumes handled for you",
            "Final reconciliation report",
        ),
        "featured": False,
        "quote": True,
    },
)

# Short note that keeps pricing honest: it's driven by how much history we
# move, not by how many people work at the firm.
PRICING_BASIS_NOTE = (
    "Your price depends on how much history we bring across — not the size "
    "of your firm."
)

# Shown for the first-25-clients semi-managed positioning. This is the line
# that keeps us honest: a person reviews the files, we are not promising a
# fully automated, self-serve migration yet.
SEMI_MANAGED_NOTE = (
    "A Cutovr specialist reviews your files and prepares the migration "
    "with you."
)


# ---------------------------------------------------------------------------
# Guided sequence
#
# The customer journey is package-first and payment-gated:
#   Step 1  Choose how much history to move        (plan cards)
#   Step 2  Tell us about your firm                (details + secure payment)
#   ── after payment ──────────────────────────────────────────────────────
#   Step 3  Upload the reports we need             (plan-specific checklist
#                                                    + upload area)
#   Step 4  What happens next                      (confirmation + Clio date)
#
# `after_payment` marks the steps that only become available once payment is
# complete. In this read-only preview they render inside a clearly-labelled
# "After payment" band so reviewers see the sequence without the reports being
# presented as a pre-payment requirement.
# ---------------------------------------------------------------------------

# Headings called out explicitly in the product copy guidance.
STEP1_HEADING = "Choose how much history to move"
STEP2_HEADING = "Tell us about your firm"
STEP3_HEADING = "Upload the reports we need"

PREVIEW_SECTIONS = (
    {
        "key": "package",
        "step": "1",
        "title": STEP1_HEADING,
        "summary": (
            "Pricing is based on how much history we move — not your "
            "firm's size."
        ),
        "after_payment": False,
    },
    {
        "key": "firm",
        "step": "2",
        "title": STEP2_HEADING,
        "summary": (
            "Your details, your migration date, and the login you'll use. "
            "Then secure checkout through Stripe."
        ),
        "after_payment": False,
    },
    {
        "key": "reports",
        "step": "3",
        "title": STEP3_HEADING,
        "summary": (
            "Once payment is complete, we tell you exactly which reports to "
            "export from PCLaw for your plan, and you upload them here."
        ),
        "after_payment": True,
    },
    {
        "key": "next",
        "step": "4",
        "title": "What happens next",
        "summary": (
            "After your files are in, we confirm by email and prepare your "
            "migration around your Clio migration date."
        ),
        "after_payment": True,
    },
)


# ---------------------------------------------------------------------------
# Step 2 — firm / account details (preview-safe form fields)
#
# These describe the fields the live flow will collect. In the preview the
# form is non-submitting and the password field is rendered as a plain,
# non-credential placeholder (see template) so nothing is collected or stored.
# Order is chosen to read top-to-bottom like a clean signup: who you are,
# how to reach you, your firm, your Clio date, then the Cutovr login.
# ---------------------------------------------------------------------------

FIRM_FIELDS = (
    {"key": "first_name", "label": "First name", "type": "text",
     "group": "you", "autocomplete": "given-name"},
    {"key": "last_name", "label": "Last name", "type": "text",
     "group": "you", "autocomplete": "family-name"},
    {"key": "email", "label": "Work email", "type": "email",
     "group": "you", "autocomplete": "email"},
    {"key": "phone", "label": "Phone number", "type": "tel",
     "group": "you", "autocomplete": "tel"},
    {"key": "firm_name", "label": "Law firm name", "type": "text",
     "group": "firm", "autocomplete": "organization"},
    {"key": "employees", "label": "Number of employees at the firm",
     "type": "number", "group": "firm", "autocomplete": "off"},
    {"key": "position", "label": "Your position at the firm", "type": "text",
     "group": "firm", "autocomplete": "organization-title"},
    {"key": "clio_migration_date", "label": "Clio migration date",
     "type": "date", "group": "firm", "autocomplete": "off",
     "help": "We schedule your QuickBooks migration around this date."},
    {"key": "username", "label": "Create a username", "type": "text",
     "group": "login", "autocomplete": "username"},
    # Rendered as a plain text placeholder in the preview — never type=password
    # and never submitted, so no credential is collected or stored here.
    {"key": "password", "label": "Create a password", "type": "preview-password",
     "group": "login", "autocomplete": "off",
     "help": "Sets the password for your Cutovr login (not your Clio login)."},
)

FIRM_FIELD_GROUPS = (
    {"key": "you", "title": "About you"},
    {"key": "firm", "title": "About your firm"},
    {"key": "login", "title": "Your Cutovr login"},
)

# Every field in FIRM_FIELDS is required to move on from Step 2. Keeping the
# list derived from FIRM_FIELDS means the form, the gate, and the tests share
# one source of truth — add a field above and it's automatically required.
REQUIRED_FIRM_FIELD_KEYS = tuple(f["key"] for f in FIRM_FIELDS)


def plan_by_key(key: str | None):
    """Return the PLAN_CARDS entry for `key`, or None if it isn't a real plan.

    Used by the Step 1 gate to confirm a selected package is one of the three
    offered plans before letting the customer move on to Step 2.
    """
    for plan in PLAN_CARDS:
        if plan["key"] == (key or ""):
            return plan
    return None


def missing_firm_fields(details: dict | None) -> list[str]:
    """Return the labels of any required Step 2 fields that are blank.

    `details` is the submitted/draft mapping of field key -> value. A field
    counts as provided when it has a non-empty, stripped value. The returned
    labels are the customer-facing ones from FIRM_FIELDS so messages read
    cleanly (e.g. "Law firm name").
    """
    details = details or {}
    missing = []
    for f in FIRM_FIELDS:
        if not str(details.get(f["key"], "")).strip():
            missing.append(f["label"])
    return missing


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
        "key": "vendor_list",
        "title": "Vendor List",
        "when": "",
        "note": (
            "Who you pay. We use it to complete your General Ledger "
            "postings on a cash basis and cut down on manual cleanup."
        ),
        "required": True,
        "add_on": False,
    },
    {
        "key": "customer_list",
        "title": "Customer / Client List",
        "when": "",
        "note": (
            "Who pays you. We use it to complete your General Ledger "
            "postings on a cash basis and cut down on manual cleanup."
        ),
        "required": True,
        "add_on": False,
    },
)


# ---------------------------------------------------------------------------
# Payment panel copy (Step 2 — Stripe Checkout, no card fields here)
# ---------------------------------------------------------------------------

PAYMENT_HEADING = "Pay securely"

# The exact reassurance line from the product copy guidance.
PAYMENT_REASSURANCE = (
    "Checkout is handled by Stripe — we never see or store your card "
    "details."
)


# ---------------------------------------------------------------------------
# What-happens-next steps (shown after payment + upload)
# ---------------------------------------------------------------------------

WHAT_HAPPENS_NEXT = (
    "Cutovr confirms by email that we've received your information and files.",
    "Our team reviews your data and confirms if anything is missing.",
    "We prepare your migration into QuickBooks and set your opening balances.",
    "Your migration takes place on the same date as your Clio migration date.",
    "If we need anything, we'll reach out using the contact details you gave us.",
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
# Confirmation copy (shown after all files are uploaded/submitted)
#
# CONFIRMATION_SUMMARY is the on-page banner; build_confirmation_email()
# produces the body of the email the customer would receive. Both reuse the
# Clio migration date so the scheduling promise is concrete. The {date} falls
# back to the clear placeholder when nothing has been chosen yet.
# ---------------------------------------------------------------------------

def confirmation_summary(clio_migration_date: str | None = None) -> str:
    """One-line on-page confirmation banner that names the Clio date."""
    return (
        "We've received your files. Our team is reviewing them now. Your "
        "migration is scheduled around your Clio migration date: "
        f"{_d(clio_migration_date)}."
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
        "- Vendor List (who you pay)",
        "- Customer / Client List (who pays you)",
        "- Accounts Payable, if available",
        "- Accounts Receivable, if available",
        "",
        "Monthly General Ledgers are best — they're more reliable than a single yearly export.",
        "The Vendor and Customer/Client lists let us complete your General Ledger postings on a "
        "cash basis and cut down on manual cleanup.",
        "Many cash-basis firms don't have Accounts Receivable or Payable, and that's completely fine.",
    ]
    return "\n".join(lines) + "\n"


def build_confirmation_email(
    *,
    firm_name: str | None = None,
    clio_migration_date: str | None = None,
    contact_email: str | None = None,
) -> str:
    """Build the post-upload confirmation email body.

    Read-only helper — produces the plain text a customer would receive once
    all their files are submitted. It states that we received the files, that
    the team is reviewing them, that the migration happens on their Clio
    migration date, and how to reach us. Dates fall back to the YYYY-MM-DD
    placeholder so the example reads cleanly with nothing filled in.
    """
    greeting_name = (firm_name or "").strip()
    greeting = f"Hi {greeting_name}," if greeting_name else "Hi,"
    reach_us = (contact_email or "").strip() or "your Cutovr contact email"

    lines = [
        greeting,
        "",
        "Thank you — we've received the information and files you submitted.",
        "Our team is now reviewing your data.",
        "",
        "Your migration will take place on the same date as your Clio "
        f"migration date: {_d(clio_migration_date)}.",
        "",
        f"If you need anything in the meantime, just reply to this email or "
        f"reach out at {reach_us}.",
        "If we need anything from you, we'll reach out using the contact "
        "details you provided.",
        "",
        "— The Cutovr team",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Secondary resource links
#
# App-hosted, customer-facing guide pages — NOT shared internal Google Drive
# links. Each `endpoint` is a Flask route name (see app.py) so the template
# can build the URL with url_for(). Kept visually quiet in the template.
# ---------------------------------------------------------------------------

RESOURCE_LINKS = (
    {
        "label": "Exporting your General Ledger from PCLaw",
        "endpoint": "guide_pclaw_general_ledger_export",
    },
    {
        "label": "Reports we need, and why",
        "endpoint": "guide_reports_needed",
    },
    {
        "label": "Clio and QuickBooks: what to know",
        "endpoint": "guide_clio_quickbooks_overview",
    },
)


# ---------------------------------------------------------------------------
# App-hosted guide pages
#
# Customer-facing, plain-English instruction pages that replace the internal
# Google Drive links. Content is based on the product notes, not fetched from
# any private link. Each guide is concise and step-by-step.
#
# Shape: {title, intro, sections: [{heading, body, items?}]} — rendered by
# templates/guide.html.
# ---------------------------------------------------------------------------

GUIDE_PCLAW_GL_EXPORT = {
    "slug": "pclaw-general-ledger-export",
    "title": "Exporting your General Ledger from PCLaw",
    "intro": (
        "Your General Ledger is the record of every transaction. We use it to "
        "rebuild your history inside QuickBooks. Here's how to export it from "
        "PCLaw in the form that works best for us."
    ),
    "sections": (
        {
            "heading": "Export monthly, not yearly",
            "body": (
                "Please export one General Ledger file per month for the "
                "period we're migrating. Monthly files are more reliable than "
                "a single large yearly export — they're easier to open, less "
                "likely to be cut short, and let us spot a gap quickly."
            ),
            "points": (
                "One file per month, named by month if you can.",
                "Cover the full migration period, start month to end month.",
                "If you can only export by year, send what you have — we'll "
                "work with it.",
            ),
        },
        {
            "heading": "How to export",
            "body": "In PCLaw, the General Ledger report can be saved as a file:",
            "points": (
                "Open the General Ledger report in PCLaw.",
                "Set the date range to a single month.",
                "Export or save the report as a CSV or Excel file.",
                "Repeat for each month in your migration period.",
            ),
        },
        {
            "heading": "Don't worry about getting it perfect",
            "body": (
                "Upload whatever you're able to export. A Cutovr specialist "
                "reviews every file and will tell you if a month is missing "
                "or doesn't look right. You won't be left to figure out the "
                "accounting on your own."
            ),
            "points": (),
        },
    ),
}

GUIDE_REPORTS_NEEDED = {
    "slug": "reports-needed",
    "title": "Reports we need, and why",
    "intro": (
        "Here are the reports we ask for, in plain English. Export them from "
        "PCLaw and upload whatever you have — we'll let you know if anything "
        "is missing. Many firms don't have every report, and that's fine."
    ),
    "sections": (
        {
            "heading": "Chart of Accounts",
            "body": (
                "Your full list of accounts with their numbers and names. We "
                "use it to set up the matching list of accounts in QuickBooks."
            ),
            "points": (),
        },
        {
            "heading": "Trial Balance — beginning",
            "body": (
                "A snapshot of your balances as at the day before your first "
                "migration year. We use it to set your opening balances in "
                "QuickBooks so your history starts from the right place."
            ),
            "points": (),
        },
        {
            "heading": "Trial Balance — ending",
            "body": (
                "A snapshot of your balances as at your end date. We use it to "
                "reconcile your balances and confirm the move landed correctly."
            ),
            "points": (),
        },
        {
            "heading": "Trust Listing",
            "body": (
                "A high-level summary of the money you hold in trust for "
                "clients, as at your cutover date. We use it to set your trust "
                "balances."
            ),
            "points": (),
        },
        {
            "heading": "Trust Ledger (add-on)",
            "body": (
                "Line-by-line trust detail. This is an optional add-on and "
                "part of the Clio piece — most firms don't need it to get "
                "started."
            ),
            "points": (),
        },
        {
            "heading": "General Ledgers — monthly",
            "body": (
                "Your transaction history, exported one month at a time for "
                "the migration period. Monthly files are more reliable than a "
                "single yearly export. See the PCLaw export guide for steps."
            ),
            "points": (),
        },
        {
            "heading": "Accounts Payable — if available",
            "body": (
                "Money you owe vendors. Only if your firm tracks it — many "
                "cash-basis firms don't, and that's completely fine."
            ),
            "points": (),
        },
        {
            "heading": "Accounts Receivable — if available",
            "body": (
                "Money clients owe you. Only if your firm tracks it — many "
                "cash-basis firms don't, and that's completely fine."
            ),
            "points": (),
        },
    ),
}

GUIDE_CLIO_QUICKBOOKS_OVERVIEW = {
    "slug": "clio-quickbooks-overview",
    "title": "Clio and QuickBooks: what to know",
    "intro": (
        "Many firms use Clio alongside QuickBooks. The connection between them "
        "has limits, especially around trust. Here's what that means for your "
        "migration — in plain English."
    ),
    "sections": (
        {
            "heading": "The Clio–QuickBooks connection has limits",
            "body": (
                "Clio and QuickBooks can share some information, but the "
                "integration doesn't cover everything. A few things — trust "
                "accounting in particular — don't carry across cleanly on "
                "their own."
            ),
            "points": (),
        },
        {
            "heading": "Trust is handled with care",
            "body": (
                "Retainer and trust balances aren't supported in QuickBooks "
                "the same way they are in a legal trust system. Where the "
                "connection falls short, Cutovr uses a careful manual process "
                "to make sure your trust balances are correct. You don't have "
                "to set this up yourself."
            ),
            "points": (),
        },
        {
            "heading": "If we need secure Clio access",
            "body": (
                "If an add-on genuinely needs access to your Clio data, our "
                "team will coordinate a secure access process with you "
                "separately. We will never ask for your Clio password or "
                "two-factor codes through this site."
            ),
            "points": (),
        },
    ),
}

GUIDES = {
    GUIDE_PCLAW_GL_EXPORT["slug"]: GUIDE_PCLAW_GL_EXPORT,
    GUIDE_REPORTS_NEEDED["slug"]: GUIDE_REPORTS_NEEDED,
    GUIDE_CLIO_QUICKBOOKS_OVERVIEW["slug"]: GUIDE_CLIO_QUICKBOOKS_OVERVIEW,
}
