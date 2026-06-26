"""Reserved PCLaw account naming for the QuickBooks migration.

A few accounts on a PCLaw trial balance map to accounts QuickBooks owns or
calculates itself — Net Income, Retained Earnings, Accounts Receivable,
Accounts Payable. Posting an opening balance straight into those would
either fail (QBO computes Net Income on its own) or pollute QuickBooks'
built-in totals (a lump A/R opening balance with no customer behind it).

The migration keeps those balances in clearly-labelled *holding* accounts
suffixed with "-PC Law" (``Net Income-PC Law``, ``RE-PC Law``,
``AR-PC Law``, ``AP-PC Law``) so QuickBooks' native accounts stay clean
and the later A/R and A/P work can move the balances onto real invoices
and bills.

This module is the single source of truth for those reserved names and the
QuickBooks account type each holding account should be created as. It is
pure: no I/O, no QBO calls, no Flask. ``coa_apply`` uses it so the holding
accounts are createable (and never mistaken for QBO's auto-calculated
Net Income), and ``opening_balance`` uses it to route reserved trial-balance
rows to the holding account instead of the native QuickBooks account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


RESERVED_SUFFIX = "-PC Law"


def _norm(token: Optional[str]) -> str:
    """Lowercase + strip to alphanumerics, mirroring coa_apply._norm."""
    if not token:
        return ""
    return "".join(ch for ch in str(token).lower() if ch.isalnum())


@dataclass(frozen=True)
class ReservedAccount:
    """A QuickBooks-owned account we hold PCLaw balances away from.

    ``label`` is the lawyer-facing name of the native QuickBooks account
    (e.g. "Net Income"). ``pc_law_name`` is the holding account we post to
    instead. ``qbo_account_type`` / ``qbo_detail_type`` are what the holding
    account is created as in QuickBooks.
    """

    key: str
    label: str
    pc_law_name: str
    qbo_account_type: str
    qbo_detail_type: str
    # Normalised name tokens that identify the *native* PCLaw row.
    _match_tokens: tuple[str, ...]


# Net Income is detected by token *containment* (so "Net Income (Loss)",
# "Net Income / Loss", "Current Year Earnings" all match), matching the
# system-calculated detector in coa_apply. Retained Earnings, A/R, and A/P
# are matched by exact normalised name so we never grab an unrelated row
# like "Income Tax Payable" or "Trust Receivable".
_NET_INCOME_TOKENS = (
    "netincome",
    "netincomeloss",
    "netloss",
    "currentyearearnings",
    "currentearnings",
)

RESERVED_ACCOUNTS: tuple[ReservedAccount, ...] = (
    ReservedAccount(
        key="net_income",
        label="Net Income",
        pc_law_name=f"Net Income{RESERVED_SUFFIX}",
        qbo_account_type="Equity",
        qbo_detail_type="OwnersEquity",
        _match_tokens=_NET_INCOME_TOKENS,
    ),
    ReservedAccount(
        key="retained_earnings",
        label="Retained Earnings",
        pc_law_name=f"RE{RESERVED_SUFFIX}",
        qbo_account_type="Equity",
        qbo_detail_type="OwnersEquity",
        _match_tokens=("retainedearnings", "re"),
    ),
    ReservedAccount(
        key="accounts_receivable",
        label="Accounts Receivable",
        pc_law_name=f"AR{RESERVED_SUFFIX}",
        qbo_account_type="Other Current Asset",
        qbo_detail_type="OtherCurrentAssets",
        _match_tokens=("accountsreceivable", "accountreceivable", "ar"),
    ),
    ReservedAccount(
        key="accounts_payable",
        label="Accounts Payable",
        pc_law_name=f"AP{RESERVED_SUFFIX}",
        qbo_account_type="Other Current Liability",
        qbo_detail_type="OtherCurrentLiabilities",
        _match_tokens=("accountspayable", "accountpayable", "ap"),
    ),
)


# Normalised forms of the holding-account names (e.g. "netincomepclaw").
_PC_LAW_NAME_NORMS = {_norm(r.pc_law_name): r for r in RESERVED_ACCOUNTS}


def is_reserved_pc_law_name(account_name: Optional[str]) -> bool:
    """True iff ``account_name`` is already one of our holding accounts.

    Lets callers tell "Net Income" (route it) apart from "Net Income-PC Law"
    (the holding account itself — leave it alone, and let it be created).
    """
    return _norm(account_name) in _PC_LAW_NAME_NORMS


def reserved_for_pc_law_name(account_name: Optional[str]) -> Optional[ReservedAccount]:
    """Return the ReservedAccount whose holding name matches, or None."""
    return _PC_LAW_NAME_NORMS.get(_norm(account_name))


def match_reserved(account_name: Optional[str]) -> Optional[ReservedAccount]:
    """Return the ReservedAccount a *native* PCLaw row maps to, or None.

    Returns None for the holding accounts themselves (so we never route
    "AR-PC Law" → "AR-PC Law" in a loop) and for ordinary accounts.
    """
    norm = _norm(account_name)
    if not norm:
        return None
    if norm in _PC_LAW_NAME_NORMS:
        return None
    # Net Income family: containment so decorations still match.
    for r in RESERVED_ACCOUNTS:
        if r.key != "net_income":
            continue
        if any(tok in norm for tok in r._match_tokens):
            return r
    # Retained Earnings / A/R / A/P: exact normalised name only.
    for r in RESERVED_ACCOUNTS:
        if r.key == "net_income":
            continue
        if norm in r._match_tokens:
            return r
    return None
