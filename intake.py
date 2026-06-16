"""Post-purchase onboarding intake: report guidance, plan context, emails.

This module is deliberately UI- and framework-agnostic so it can be unit
tested without a Flask request. The Flask routes in app.py own the
request/response and persistence; everything that does not need a request
lives here.

Customer-facing copy follows the Cutovr brand rules:
  - "QuickBooks", never "QBO".
  - "PCLaw" only to describe the source software / the reports to export.
  - Plain English. Lawyers are not accountants.
"""

from __future__ import annotations

import json
import os
from typing import Optional


# ---------------------------------------------------------------------------
# Plan context
#
# Mirrors the Stripe plan slugs in stripe_checkout.PLAN_ENV_VARS so a
# ?plan=standard query param coming back from a Stripe success redirect (or
# session metadata) resolves to a clean, lawyer-friendly label. Unknown /
# missing slugs fall back to a neutral label rather than erroring.
# ---------------------------------------------------------------------------

PLAN_LABELS = {
    "essential": "Essential (current year)",
    "standard": "Standard (up to 3 years)",
    "complete": "Complete (3+ years)",
    "extra_year": "Extra historical year",
    "priority_turnaround": "Priority turnaround",
    "assisted_review": "Assisted review call",
}

# Slugs we present as a selectable plan when none arrives from the URL. Add-ons
# are intentionally excluded — the base plan is what the intake form cares about.
SELECTABLE_PLANS = ("essential", "standard", "complete")


# ---------------------------------------------------------------------------
# Plan context + what's included
#
# Plans are an INTERNAL scoping label only — they describe how much history a
# firm wants moved, so the team and operator panel can categorize an intake.
# We do NOT show a public price for any plan: pricing is given on the
# discovery call after we scope the migration. `price_display` is intentionally
# "Quote on discovery call" for every plan so nothing in customer-facing copy
# or emails surfaces a dollar amount.
# ---------------------------------------------------------------------------

QUOTE_ON_CALL = "Quote on discovery call"

PLAN_DETAILS = {
    "essential": {
        "name": "Essential",
        "tagline": "Current year",
        "price_display": QUOTE_ON_CALL,
        "is_quote": True,
        "includes": (
            "Guided upload of your PCLaw reports",
            "QuickBooks connection",
            "Account matching with a preview before anything posts",
            "Final migration report",
        ),
    },
    "standard": {
        "name": "Standard",
        "tagline": "Up to 3 years",
        "price_display": QUOTE_ON_CALL,
        "is_quote": True,
        "includes": (
            "Everything in Essential",
            "Up to 3 years of history",
            "Trust balance support",
            "Final migration report",
        ),
    },
    "complete": {
        "name": "Complete",
        "tagline": "3+ years of history",
        "price_display": QUOTE_ON_CALL,
        "is_quote": True,
        "includes": (
            "Everything in Standard",
            "3 or more years of history",
            "Larger file volume",
            "Final reconciliation report",
        ),
    },
}


def plan_label(slug: Optional[str]) -> str:
    """Human label for a plan slug, or a neutral fallback."""
    if not slug:
        return "Not specified"
    return PLAN_LABELS.get(slug.strip().lower(), slug.strip())


def normalize_plan(slug: Optional[str]) -> Optional[str]:
    """Return a known plan slug (lowercased) or None."""
    if not slug:
        return None
    s = slug.strip().lower()
    return s if s in PLAN_LABELS else None


def plan_detail(slug: Optional[str]) -> Optional[dict]:
    """Return the pricing/includes dict for a base plan slug, or None."""
    if not slug:
        return None
    return PLAN_DETAILS.get(slug.strip().lower())


def plan_price_display(slug: Optional[str]) -> str:
    """Customer-facing price/quote string for a plan, or empty if unknown."""
    detail = plan_detail(slug)
    return detail["price_display"] if detail else ""


# ---------------------------------------------------------------------------
# Payment status
#
# We are Stripe-ready but NOT collecting card details here. An intake is only
# ever "paid" when a real Stripe success/webhook path confirms it — never from
# the intake form itself. Everything submitted through /intake starts as
# "pending" so customer/internal copy and receipts stay honest.
# ---------------------------------------------------------------------------

PAYMENT_PENDING = "pending"
PAYMENT_PAID = "paid"
PAYMENT_UNPAID = "unpaid"

_PAYMENT_STATUS_LABELS = {
    PAYMENT_PAID: "Paid",
    PAYMENT_PENDING: "Pending — secure card payment to be connected",
    PAYMENT_UNPAID: "Unpaid",
}


def normalize_payment_status(value: Optional[str]) -> str:
    """Coerce any stored/incoming value to a known status, defaulting pending."""
    s = (value or "").strip().lower()
    if s in (PAYMENT_PAID, PAYMENT_PENDING, PAYMENT_UNPAID):
        return s
    return PAYMENT_PENDING


def payment_status_label(value: Optional[str]) -> str:
    """Human label for a payment status."""
    return _PAYMENT_STATUS_LABELS[normalize_payment_status(value)]


def is_paid(value: Optional[str]) -> bool:
    """True only when payment is genuinely confirmed paid."""
    return normalize_payment_status(value) == PAYMENT_PAID


# ---------------------------------------------------------------------------
# Recommended PCLaw reports
#
# Plain-English guidance for what to export from PCLaw. Order matters: the
# most important / most-commonly-available reports come first. `key` is a
# stable id used by tests and (optionally) by upload tagging.
# ---------------------------------------------------------------------------

RECOMMENDED_REPORTS = (
    {
        "key": "chart_of_accounts",
        "title": "Chart of Accounts / Account List",
        "help": "Your full list of accounts with their numbers and names.",
    },
    {
        "key": "general_ledger",
        "title": "General Ledger / Transaction History",
        "help": "Every transaction. If you can export it month by month, send the monthly files — that helps us the most.",
    },
    {
        "key": "trial_balance",
        "title": "Trial Balance / Starting Balances",
        "help": "Your opening balances at the date you want to move over.",
    },
    {
        "key": "ending_balances",
        "title": "Final Balance Check / Ending Balances",
        "help": "Your closing balances, if you have them. We use these to double-check the move.",
    },
    {
        "key": "trust_listing",
        "title": "Trust Listing / Client Trust Balances",
        "help": "Money you hold in trust for clients, listed by client.",
    },
    {
        "key": "vendor_customer_lists",
        "title": "Vendor list and Customer / Client list",
        "help": "Who you pay and who pays you, if these come out as separate reports.",
    },
    {
        "key": "account_numbers_reference",
        "title": "Anything that shows account numbers and descriptions",
        "help": "Any extra export that spells out what each account number means.",
    },
)

UPLOAD_GUIDANCE_TAGLINE = (
    "Upload whatever you have. The more complete your files are, the "
    "smoother your migration will be."
)


# ---------------------------------------------------------------------------
# Internal recipients
# ---------------------------------------------------------------------------

def internal_recipients(support_email: Optional[str]) -> list[str]:
    """Resolve the internal intake notification recipients.

    Preference order:
      1. INTERNAL_INTAKE_EMAILS (comma-separated), if set.
      2. The configured SUPPORT_EMAIL passed in by the caller, if it is a
         real (non-placeholder) address.

    Returns a de-duplicated, order-preserving list. Empty if nothing usable
    is configured (caller should then skip the internal email without
    treating it as an error).
    """
    out: list[str] = []
    seen: set[str] = set()

    raw = os.environ.get("INTERNAL_INTAKE_EMAILS") or ""
    for piece in raw.split(","):
        addr = piece.strip()
        low = addr.lower()
        if addr and low not in seen:
            seen.add(low)
            out.append(addr)

    if not out and support_email:
        s = support_email.strip()
        if s and not s.endswith("@your-domain.example"):
            out.append(s)

    return out


# ---------------------------------------------------------------------------
# Email bodies
# ---------------------------------------------------------------------------

def _upload_summary_lines(uploads: list[dict]) -> list[str]:
    if not uploads:
        return ["  (no files were attached — the firm can add files later)"]
    lines = []
    for u in uploads:
        name = u.get("filename") or "(unnamed file)"
        label = u.get("report_label") or ""
        if label:
            lines.append(f"  - {name}  [{label}]")
        else:
            lines.append(f"  - {name}")
    return lines


def _payment_block_customer(payment_status: Optional[str]) -> str:
    """Customer-facing pricing/next-step paragraph.

    We never show a dollar amount or imply a charge. Pricing is given on the
    discovery call after we scope the migration. The `payment_status` argument
    is retained for the internal record but does not change customer copy
    unless payment is genuinely confirmed paid (post-call, private link).
    """
    if is_paid(payment_status):
        return (
            "Payment\n"
            "  Your payment has been received — thank you. This email is your\n"
            "  confirmation of payment.\n"
        )
    return (
        "Pricing\n"
        "  Your details are saved. We'll provide a clear quote on the\n"
        "  discovery call. You have not been charged yet, and this is not a receipt.\n"
    )


def customer_email_bodies(
    *, first_name: str, app_name: str, support_email: Optional[str],
    plan: Optional[str] = None,
    clio_migration_date: Optional[str] = None,
    uploads: Optional[list[dict]] = None,
    payment_status: Optional[str] = None,
) -> tuple[str, str]:
    """Build (subject, body_text) for the customer's next-steps email."""
    name = (first_name or "").strip() or "there"
    subject = f"Welcome to {app_name} — here's what happens next"
    contact = ""
    if support_email and not support_email.endswith("@your-domain.example"):
        contact = (
            f"\nIf you have any questions, just reply to this email or reach "
            f"us at {support_email}.\n"
        )
    reports = "\n".join(f"  • {r['title']}" for r in RECOMMENDED_REPORTS)

    plan_line = ""
    if normalize_plan(plan):
        plan_line = f"Your plan\n  {plan_label(plan)} — {plan_price_display(plan)}\n\n"

    clio_line = ""
    if (clio_migration_date or "").strip():
        clio_line = (
            f"Your Clio migration date\n  {clio_migration_date.strip()}\n\n"
        )

    uploads = uploads or []
    upload_summary = "\n".join(_upload_summary_lines(uploads))
    upload_line = (
        f"Files you sent us ({len(uploads)})\n{upload_summary}\n\n"
    )

    payment_block = _payment_block_customer(payment_status) + "\n"

    body = f"""Hi {name},

Thanks for choosing {app_name}. We've received your onboarding details and
any files you uploaded.

{plan_line}{clio_line}{upload_line}{payment_block}What happens next
  1. Our team reviews your information and your PCLaw reports.
  2. On a short discovery call, we review your migration and provide a
     clear quote.
  3. We prepare your move into QuickBooks and start on your Clio
     migration date.
  4. We'll email you the next step.

You don't need to do anything else right now.

The reports that help us most
{reports}

{UPLOAD_GUIDANCE_TAGLINE}

You can always send us more files later — nothing is locked in, and
more complete files make for a smoother migration.
{contact}
— The {app_name} team
"""
    return subject, body


def internal_email_bodies(
    *,
    app_name: str,
    reference: str,
    firm_name: str,
    first_name: str,
    last_name: str,
    position: Optional[str],
    phone: Optional[str],
    email: str,
    plan: Optional[str],
    clio_migration_date: Optional[str],
    uploads: list[dict],
    admin_link: Optional[str] = None,
    payment_status: Optional[str] = None,
) -> tuple[str, str]:
    """Build (subject, body_text) for the internal team notification."""
    subject = f"[{app_name}] New intake: {firm_name} ({reference})"
    contact_name = f"{first_name} {last_name}".strip()
    upload_lines = _upload_summary_lines(uploads)
    plan_price = plan_price_display(plan)
    plan_line = plan_label(plan) + (f"  ({plan_price})" if plan_price else "")
    lines = [
        f"New post-purchase intake received.",
        "",
        f"Reference:        {reference}",
        f"Firm:             {firm_name}",
        f"Contact:          {contact_name}",
        f"Position:         {position or '(not given)'}",
        f"Phone:            {phone or '(not given)'}",
        f"Email:            {email}",
        f"Plan / service:   {plan_line}",
        f"Payment status:   {payment_status_label(payment_status)}",
        f"Clio migration:   {clio_migration_date or '(not given)'}",
        "",
        f"Uploaded reports ({len(uploads)}):",
        *upload_lines,
    ]
    if admin_link:
        lines += ["", f"Open in operator panel: {admin_link}"]
    return subject, "\n".join(lines) + "\n"
