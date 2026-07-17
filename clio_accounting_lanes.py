"""Clio Accounting migration — per-lane data-flow plans.

Two migration lanes will feed Clio Accounting once its API opens:

  * PC Law  -> Clio Accounting  (``service_lanes.PCLAW_TO_CLIO_ACCOUNTING``)
  * QBO     -> Clio Accounting  (``service_lanes.QBO_TO_CLIO_ACCOUNTING``)

This module describes, per lane, the ordered set of *migration steps* Cutovr
will perform: which source artifact feeds which Clio capability, via which
payload builder. It is a PLAN, not an executor — it makes no API call and reads
no live source data. It lets the operator readiness view show exactly what each
lane will do and which steps are blocked on Clio's side today.

Design intent: keep the source-extraction details out of here. PCLaw already has
extractors/reports and QBO already has a read connection; this plan only names
the *canonical* inputs it expects and the Clio capability + builder each step
targets. That keeps the plan stable even as extractors evolve, and guarantees a
Clio lane can never reach into the QuickBooks *posting* flow — these steps only
ever call the Clio adapter (disabled by default).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import service_lanes as sl
import clio_accounting_capabilities as caps


@dataclass(frozen=True)
class MigrationStep:
    """One planned step in a lane's Clio Accounting cutover.

    ``capability`` is a key into the capability registry; ``builder`` names the
    payload builder in ``clio_accounting_payloads`` that produces its payload.
    ``source_artifact`` is the Cutovr-canonical input the step consumes.
    """

    key: str
    label: str
    source_artifact: str
    capability: str
    builder: str

    @property
    def blocked_reason(self) -> Optional[str]:
        """Why this step can't run live yet, or None if the capability is ready.

        A step is considered *blocked on Clio* when its capability isn't at least
        read/write-supported (or flag-disabled, which is a Cutovr gate, not a
        Clio gap). This drives the readiness view's per-step status.
        """
        cap = caps.capability(self.capability)
        if cap is None:
            return f"Unknown capability {self.capability!r}."
        ready = (
            caps.STATUS_READ_ONLY,
            caps.STATUS_WRITE_SUPPORTED,
            caps.STATUS_FEATURE_FLAG_DISABLED,
        )
        if cap.status not in ready:
            return f"Clio {self.capability} is {cap.status} (not ready)."
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        cap = caps.capability(self.capability)
        d["capability_status"] = cap.status if cap else None
        d["blocked_on_clio"] = self.blocked_reason is not None
        d["blocked_reason"] = self.blocked_reason
        return d


def _step(key, label, source_artifact, capability, builder) -> MigrationStep:
    return MigrationStep(key, label, source_artifact, capability, builder)


# ---------------------------------------------------------------------------
# PC Law -> Clio Accounting
#
# Source foundation: PCLaw reports/checklists already produced by Cutovr's PCLaw
# pipeline (chart of accounts, trial balances, GL, trust listing, A/R, A/P,
# vendor/client lists, historical backup). These map onto Clio ledger accounts,
# opening journal entries, trust/operating summaries, and reference lists.
# ---------------------------------------------------------------------------

_PCLAW_STEPS: tuple[MigrationStep, ...] = (
    _step("ledger_accounts", "Create ledger accounts (chart of accounts)",
          "PCLaw chart of accounts", "ledger_accounts.create", "ledger_account"),
    _step("opening_journal", "Post opening-balance journal entry",
          "PCLaw beginning/ending trial balance", "journal_entries.create", "journal_entry"),
    _step("trust_summary", "Prepare trust/operating summary journal entries",
          "PCLaw trust listing + operating balances", "journal_entries.create", "journal_entry"),
    _step("client_refs", "Resolve client references",
          "PCLaw client list", "clients.read", "build_reference"),
    _step("vendor_refs", "Resolve vendor references",
          "PCLaw vendor list", "vendors.read", "build_reference"),
    _step("historical_archive", "Generate historical archive reports",
          "PCLaw historical backup/GL", "reports.create", "report_request"),
)


# ---------------------------------------------------------------------------
# QBO -> Clio Accounting
#
# Source foundation: outputs of the existing QBO read/extraction interfaces
# (reused read-only — never the QBO *posting* path). These map onto Clio ledger
# accounts, journal entries, vendor bills, bill payments, clients, matters,
# vendors, expenses, and reports.
# ---------------------------------------------------------------------------

_QBO_STEPS: tuple[MigrationStep, ...] = (
    _step("ledger_accounts", "Create ledger accounts (chart of accounts)",
          "QBO chart of accounts", "ledger_accounts.create", "ledger_account"),
    _step("opening_journal", "Post opening-balance journal entry",
          "QBO trial balance at cutover", "journal_entries.create", "journal_entry"),
    _step("vendor_refs", "Resolve vendor references",
          "QBO vendor list", "vendors.read", "build_reference"),
    _step("client_refs", "Resolve client/customer references",
          "QBO customer list", "clients.read", "build_reference"),
    _step("matter_refs", "Resolve matter references",
          "QBO matter/class mapping", "matters.read", "build_reference"),
    _step("vendor_bills", "Create vendor bills",
          "QBO open bills", "vendor_bills.write", "vendor_bill"),
    _step("vendor_bill_payments", "Create vendor bill payments",
          "QBO bill payments", "vendor_bill_payments.write", "vendor_bill_payment"),
    _step("expenses", "Create expenses",
          "QBO expenses", "expenses.write", "expense"),
    _step("reports", "Generate cutover/archive reports",
          "QBO GL + reports", "reports.create", "report_request"),
)

_PLANS = {
    sl.PCLAW_TO_CLIO_ACCOUNTING: _PCLAW_STEPS,
    sl.QBO_TO_CLIO_ACCOUNTING: _QBO_STEPS,
}


def steps(lane: Optional[str]) -> list[MigrationStep]:
    """Ordered migration steps for a Clio Accounting lane (empty if unknown)."""
    known = sl.normalize(lane)
    return list(_PLANS.get(known, ()))


def plan(lane: Optional[str]) -> dict:
    """Serializable data-flow plan for a lane, for operator views / tests.

    Includes per-step Clio status + a count of steps currently blocked on Clio,
    and asserts (defensively) that a Clio lane never routes through QBO posting.
    """
    known = sl.normalize(lane)
    lane_steps = steps(known)
    blocked = [s for s in lane_steps if s.blocked_reason]
    return {
        "lane": known,
        "lane_label": sl.label(known),
        "is_clio_accounting": sl.is_clio_accounting(known),
        # Invariant: Clio lanes must never post to QuickBooks.
        "uses_qbo_posting": sl.uses_qbo_posting(known) if known else None,
        "steps": [s.to_dict() for s in lane_steps],
        "step_count": len(lane_steps),
        "blocked_count": len(blocked),
        "ready_count": len(lane_steps) - len(blocked),
    }


def all_plans() -> list[dict]:
    """Plans for both Clio Accounting lanes, in display order."""
    return [plan(lane) for lane in sl.CLIO_ACCOUNTING_LANES]
