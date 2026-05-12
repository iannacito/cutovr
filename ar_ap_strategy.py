"""AR/AP migration strategy capture + safety gating.

Migrating accounts receivable and accounts payable from PCLaw to QBO is
one of the most error-prone steps in a cutover. The right answer depends
on:

  * **Accounting basis.** Cash-basis firms typically *skip* AR/AP
    migration entirely — there's nothing to post until the customer
    actually pays. Accrual-basis firms need open AR / open AP carried
    over so revenue and expense recognition stay correct.
  * **Country.** Canadian AR/AP requires sales-tax handling (GST/HST/PST)
    that QBO Canada handles differently from QBO US. We currently do
    not generate sales-tax-aware invoices automatically, so the safe
    path on Canada/accrual is "summary opening JE" (an Invoice-by-Invoice
    posting with full tax accuracy is future work).
  * **Clio involvement.** Firms running billing in Clio usually want
    their AR to live in Clio, not QBO. The QBO side then carries only
    summary balances.

The strategy stored against the firm captures their decision so the rest
of the migration UI can show appropriate guidance and so the upload /
import paths can block unsupported postings clearly rather than silently
half-implementing them.

This module is data + validation; the Flask layer owns the form and the
DB write. No QBO HTTP calls happen here.
"""

from __future__ import annotations

from typing import Optional


# Strategy identifiers — referenced by the cutover form, the migration
# checklist, and per-firm settings. Rename only with a migration plan.
STRATEGY_SKIP = "skip"
STRATEGY_SUMMARY_JE = "summary_je"
STRATEGY_OPEN_ITEMS = "open_items"

AR_AP_STRATEGY_CHOICES = (
    (STRATEGY_SKIP, "Skip AR/AP migration entirely"),
    (STRATEGY_SUMMARY_JE, "Summary opening journal entry (one JE per AR/AP account)"),
    (STRATEGY_OPEN_ITEMS, "Open-item list (one transaction per invoice / bill)"),
)
_VALID_STRATEGIES = {key for key, _ in AR_AP_STRATEGY_CHOICES}


# Strategies the current build of the app actually *supports executing*.
# Other strategies are still recorded so the operator can plan around
# them, but the upload / import paths refuse to post until the strategy
# is one we know how to do safely.
SUPPORTED_STRATEGIES = {STRATEGY_SKIP}


def validate_ar_ap_strategy(strategy: Optional[str]) -> str:
    """Return the canonical strategy id, or '' if unset / invalid.

    Empty / None / unknown strings are normalized to '' so the rest of
    the codebase treats the absence of a strategy as "operator hasn't
    decided yet" rather than as a typo'd value.
    """
    if not strategy:
        return ""
    s = str(strategy).strip().lower()
    return s if s in _VALID_STRATEGIES else ""


def guidance_for_strategy(
    strategy: Optional[str],
    *,
    country: Optional[str] = None,
    accounting_basis: Optional[str] = None,
    clio_involved: bool = False,
) -> dict:
    """Return a guidance dict that the template / docs can render.

    Keys:
      * label: human label for the strategy.
      * summary: 1-2 sentence summary.
      * supported: bool — True iff this build can actually execute it.
      * blockers: list[str] — reasons posting is blocked for this firm.
      * recommendations: list[str] — country/basis/clio-specific notes.
      * unsafe_to_auto_post: bool — explicit flag the upload route reads.
    """
    s = validate_ar_ap_strategy(strategy)
    country_code = (country or "").strip().upper()
    basis = (accounting_basis or "").strip().lower()

    out: dict = {
        "strategy": s,
        "label": dict(AR_AP_STRATEGY_CHOICES).get(s, "Not decided"),
        "supported": s in SUPPORTED_STRATEGIES,
        "summary": "",
        "blockers": [],
        "recommendations": [],
        "unsafe_to_auto_post": s != STRATEGY_SKIP,
    }

    # Country + basis recommendations are shown regardless of strategy
    # so the operator can compare options.
    if country_code == "CA":
        out["recommendations"].append(
            "Canada: AR/AP invoices carry GST/HST/PST. We do not currently "
            "generate sales-tax-aware invoices automatically. Carrying AR "
            "as open items will under-report tax. Use the summary JE path "
            "(or skip AR/AP migration) until the tax-aware path ships."
        )
    elif country_code == "US":
        out["recommendations"].append(
            "United States: sales tax on legal services varies by state. "
            "If your firm has no sales-tax exposure on legal fees, AR/AP "
            "migration is simpler — confirm with the firm's accountant."
        )

    if basis == "cash":
        out["recommendations"].append(
            "Cash basis: AR/AP balances are not part of the books — there's "
            "nothing to migrate. The standard move is to skip AR/AP and "
            "let QBO start collecting payments from cutover forward."
        )
    elif basis == "accrual":
        out["recommendations"].append(
            "Accrual basis: open AR / open AP must be carried over so "
            "revenue and expense recognition stay correct. The summary "
            "opening JE strategy is safer than full open-item posting "
            "while the open-item path is still being built."
        )

    if clio_involved:
        out["recommendations"].append(
            "Clio in use: keep AR in Clio, not QBO. On the QBO side carry "
            "only a summary AR balance via a single JE; per-invoice "
            "tracking belongs in Clio."
        )

    # Strategy-specific text + blocker decisions.
    if s == STRATEGY_SKIP:
        out["summary"] = (
            "AR/AP migration is intentionally skipped. QuickBooks starts "
            "collecting AR / paying AP from cutover forward. Historical "
            "open items stay in PCLaw."
        )
        # Skip strategy is the only one we can fully *honor* today — and
        # honoring "skip" is trivial: just don't post AR/AP.
    elif s == STRATEGY_SUMMARY_JE:
        out["summary"] = (
            "Plan to post a single opening journal entry that establishes "
            "the total AR and AP balances as of cutover. Per-customer / "
            "per-vendor detail is NOT brought across."
        )
        out["blockers"].append(
            "Summary opening JE for AR/AP is not yet wired into the "
            "import flow. The opening trial balance JE (when it includes "
            "the AR / AP accounts) is the closest available substitute. "
            "Treat per-customer detail as a manual follow-up."
        )
    elif s == STRATEGY_OPEN_ITEMS:
        out["summary"] = (
            "Plan to import every open invoice and bill as its own "
            "QuickBooks transaction so per-customer / per-vendor aging "
            "carries over."
        )
        out["blockers"].append(
            "Open-item AR/AP migration is not implemented. The PCLaw "
            "client-matter AR and vendor AP files (test_data/06_*.csv, "
            "07_*.csv) are not yet wired up. Use 'Skip' or 'Summary JE' "
            "for the migration; per-invoice migration is future work."
        )
        if country_code == "CA":
            out["blockers"].append(
                "Canadian sales-tax-aware invoice creation is not "
                "supported. Posting open AR invoices without GST/HST "
                "tracking will under-report tax — blocked."
            )
    else:
        out["summary"] = (
            "No AR/AP strategy chosen yet. Pick one on the cutover setup "
            "page so the rest of the migration plan can adapt."
        )

    return out


def block_message_for_unsupported_import(strategy: Optional[str]) -> Optional[str]:
    """Return a short refusal string the upload / import route can flash
    when a firm tries to post an AR/AP report under an unsupported
    strategy. Returns None when the operation is OK to proceed.
    """
    s = validate_ar_ap_strategy(strategy)
    if s in SUPPORTED_STRATEGIES or s == "":
        return None
    label = dict(AR_AP_STRATEGY_CHOICES).get(s, s)
    return (
        f"AR/AP strategy '{label}' is recorded but the import path for "
        "this strategy is not yet implemented. Nothing was posted. Open "
        "the cutover settings to switch strategy, or wait for the "
        "open-item / summary-JE flow to ship."
    )
