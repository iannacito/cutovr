"""Clio Accounting integration — forward-looking placeholder.

Clio Accounting does not currently expose a public/open API for direct
accounting migration: no journal-entry posting, chart-of-accounts creation,
opening-balance import, or general-ledger load. Cutovr therefore serves the
Clio lanes as *cutover readiness* — we prepare a validated package for a guided
setup rather than posting into Clio directly (see ``service_lanes``).

This module is the single, clearly-named place where direct Clio Accounting API
automation will land once access exists. Today it reports, for operators, that
posting is not enabled. It is intentionally small: enough structure to give the
future API work an obvious home, with no unused machinery.

Flip behavior in the future by setting ``CLIO_ACCOUNTING_API_ENABLED=1`` (and
implementing the posting client) — the readiness workflow can then be upgraded
to direct automation without changing call sites that check ``is_enabled()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict


# Status vocabulary. ``not_enabled`` is the only value today; the constants are
# defined now so future code and tests have stable strings to branch on.
STATUS_NOT_ENABLED = "not_enabled"   # no public API access yet (current state)
STATUS_ENABLED = "enabled"           # direct posting available (future)


def _api_enabled_env() -> bool:
    return os.environ.get("CLIO_ACCOUNTING_API_ENABLED", "").lower() in (
        "1", "true", "yes", "on",
    )


@dataclass
class ClioAccountingIntegrationStatus:
    """A secret-free snapshot of whether direct Clio Accounting posting works.

    ``operator_message`` is developer/operator context (may say the API isn't
    enabled). ``customer_message`` is the calm, reassuring phrasing safe to show
    a prospect — it never says "unavailable" or anything alarming.
    """

    status: str
    enabled: bool
    operator_message: str
    customer_message: str

    def to_dict(self) -> dict:
        return asdict(self)


def integration_status() -> ClioAccountingIntegrationStatus:
    """Current Clio Accounting integration status.

    Defaults to *not enabled* because Clio has no public accounting API yet.
    Reads ``CLIO_ACCOUNTING_API_ENABLED`` so a future deploy can flip it on once
    the posting client is implemented.
    """
    if _api_enabled_env():
        return ClioAccountingIntegrationStatus(
            status=STATUS_ENABLED,
            enabled=True,
            operator_message="Clio Accounting API posting is enabled.",
            customer_message=(
                "Your Clio Accounting cutover can be applied with guided setup."
            ),
        )
    return ClioAccountingIntegrationStatus(
        status=STATUS_NOT_ENABLED,
        enabled=False,
        operator_message=(
            "Clio Accounting API posting is not enabled yet — Clio does not "
            "currently offer a public accounting API. Cutovr prepares a "
            "validated cutover package for guided setup instead of posting "
            "directly."
        ),
        customer_message=(
            "We prepare and validate your Clio Accounting cutover package so "
            "your team can complete a clean, guided setup in Clio."
        ),
    )


def is_enabled() -> bool:
    """True only when direct Clio Accounting API posting is available."""
    return integration_status().enabled
