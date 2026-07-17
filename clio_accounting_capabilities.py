"""Clio Accounting API v1 — internal capability registry.

This is the single, easily-editable source of truth for *what Cutovr believes
the Clio Accounting API v1 can do*, endpoint family by endpoint family, so the
rest of the app (adapter boundary, payload builders, operator readiness view)
can branch on a stable capability name instead of scattering assumptions.

WHY THIS EXISTS
---------------
Clio's Accounting API v1 has moved from prototype into build: read-only
endpoints shipped to staging, App Sec / Compliance reviews are clear, and the
platform team is landing write endpoints and foundational hardening
(idempotency, shared filtering, pagination max 200, OAS/CI + runtime contract
validation, correlation IDs). Phase-1 production go-live is targeted but not
open to Cutovr yet, and the official developer-portal OpenAPI schema is not
published.

So every status below is an *internal, assumed* status sourced from the
roadmap, NOT from official docs. It is deliberately NOT public-facing. When the
developer portal publishes the OpenAPI schema, update the statuses/notes here in
one place and everything downstream follows.

  IMPORTANT: nothing in this module makes a live API call. It only describes
  intent/readiness. Live calls live behind the disabled-by-default adapter in
  ``clio_accounting`` (see ``ClioAccountingAdapter``).

STATUS VOCABULARY
-----------------
Ordered from least to most ready:

  unavailable          — no such endpoint expected / not on the roadmap.
  production_pending    — on the roadmap but not yet in staging for us.
  staging_expected      — expected/known to exist in Clio's staging today.
  read_only             — read verified available (to us or in staging).
  write_supported       — write verified available in Clio's API surface.
  feature_flag_disabled — Cutovr could call it, but our own feature flag keeps
                          it OFF by default (safety gate; the normal state for
                          any write until we deliberately enable live mode).

Note the last value is a *Cutovr-side* gate, orthogonal to Clio readiness: even
a ``write_supported`` Clio endpoint is only ever exercised when
``CLIO_ACCOUNTING_API_ENABLED`` is set AND the operation is allow-listed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# Status vocabulary (stable strings — persisted in views/logs, never rename)
# ---------------------------------------------------------------------------

STATUS_UNAVAILABLE = "unavailable"
STATUS_PRODUCTION_PENDING = "production_pending"
STATUS_STAGING_EXPECTED = "staging_expected"
STATUS_READ_ONLY = "read_only"
STATUS_WRITE_SUPPORTED = "write_supported"
STATUS_FEATURE_FLAG_DISABLED = "feature_flag_disabled"

# Rough readiness ordering, used only for display/sorting. A higher rank means
# "closer to Cutovr being able to act on it". feature_flag_disabled ranks high
# on the Clio axis (the endpoint is there) but is gated on our side.
_STATUS_RANK = {
    STATUS_UNAVAILABLE: 0,
    STATUS_PRODUCTION_PENDING: 1,
    STATUS_STAGING_EXPECTED: 2,
    STATUS_READ_ONLY: 3,
    STATUS_WRITE_SUPPORTED: 4,
    STATUS_FEATURE_FLAG_DISABLED: 5,
}

ALL_STATUSES = (
    STATUS_UNAVAILABLE,
    STATUS_PRODUCTION_PENDING,
    STATUS_STAGING_EXPECTED,
    STATUS_READ_ONLY,
    STATUS_WRITE_SUPPORTED,
    STATUS_FEATURE_FLAG_DISABLED,
)


def status_rank(status: str) -> int:
    """Sort key for a status string (unknown statuses sort first)."""
    return _STATUS_RANK.get(status, -1)


# ---------------------------------------------------------------------------
# Capability model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Capability:
    """One Clio Accounting API v1 operation family + its assumed readiness.

    ``key``      stable slug, ``{family}.{operation}`` (e.g. ``ledger_accounts.create``).
    ``family``   endpoint family (ledger_accounts, journal_entries, ...).
    ``operation``verb (read, create, update, deactivate, reactivate, destroy, ...).
    ``status``   one of the STATUS_* constants — Cutovr's *assumed* status.
    ``note``     internal operator/developer context (never customer-facing).
    ``write``    True if this operation mutates data in Clio.
    """

    key: str
    family: str
    operation: str
    status: str
    note: str
    write: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status_rank"] = status_rank(self.status)
        return d


@dataclass(frozen=True)
class CapabilityFamily:
    """A group of capabilities for one endpoint family + display metadata."""

    family: str
    label: str
    capabilities: tuple[Capability, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "family": self.family,
            "label": self.label,
            "capabilities": [c.to_dict() for c in self.capabilities],
        }


def _cap(family, operation, status, note, write=False) -> Capability:
    return Capability(
        key=f"{family}.{operation}",
        family=family,
        operation=operation,
        status=status,
        note=note,
        write=write,
    )


# ---------------------------------------------------------------------------
# THE REGISTRY
#
# Assumed statuses sourced from the internal Clio Accounting API v1 roadmap
# (build phase; staging read-only shipped; write landing family by family;
# Phase-1 prod go-live pending; no public OpenAPI yet). These are ASSUMPTIONS.
#
# TODO(clio-docs): replace every status/note below with values verified against
# the official developer-portal OpenAPI schema once Clio publishes it. Keep the
# keys stable so downstream code/tests don't churn.
# ---------------------------------------------------------------------------

_FAMILIES: tuple[CapabilityFamily, ...] = (
    CapabilityFamily(
        family="ledger_accounts",
        label="Ledger Accounts",
        capabilities=(
            _cap("ledger_accounts", "read", STATUS_READ_ONLY,
                 "Read shipped to staging."),
            _cap("ledger_accounts", "create", STATUS_FEATURE_FLAG_DISABLED,
                 "Write complete in Clio; gated OFF by Cutovr feature flag.", write=True),
            _cap("ledger_accounts", "update", STATUS_FEATURE_FLAG_DISABLED,
                 "Write complete in Clio; gated OFF by Cutovr feature flag.", write=True),
            _cap("ledger_accounts", "deactivate", STATUS_FEATURE_FLAG_DISABLED,
                 "Deactivate complete in Clio; gated OFF by Cutovr feature flag.", write=True),
            _cap("ledger_accounts", "reactivate", STATUS_FEATURE_FLAG_DISABLED,
                 "Reactivate complete in Clio; gated OFF by Cutovr feature flag.", write=True),
        ),
    ),
    CapabilityFamily(
        family="journal_entries",
        label="Journal Entries",
        capabilities=(
            _cap("journal_entries", "read", STATUS_READ_ONLY,
                 "Read shipped to staging."),
            _cap("journal_entries", "create", STATUS_FEATURE_FLAG_DISABLED,
                 "Write complete in Clio; gated OFF by Cutovr feature flag.", write=True),
            _cap("journal_entries", "update", STATUS_FEATURE_FLAG_DISABLED,
                 "Write complete in Clio; gated OFF by Cutovr feature flag.", write=True),
            _cap("journal_entries", "destroy", STATUS_FEATURE_FLAG_DISABLED,
                 "Destroy complete in Clio; gated OFF by Cutovr feature flag.", write=True),
        ),
    ),
    CapabilityFamily(
        family="reports",
        label="Reports",
        capabilities=(
            _cap("reports", "create", STATUS_FEATURE_FLAG_DISABLED,
                 "Report create endpoint complete in Clio; gated OFF by Cutovr.", write=True),
            _cap("reports", "read", STATUS_READ_ONLY,
                 "Report read endpoint complete in Clio."),
        ),
    ),
    CapabilityFamily(
        family="vendor_bills",
        label="Vendor Bills",
        capabilities=(
            _cap("vendor_bills", "read", STATUS_READ_ONLY,
                 "VendorBill read complete."),
            _cap("vendor_bills", "write", STATUS_FEATURE_FLAG_DISABLED,
                 "VendorBill write complete in Clio; gated OFF by Cutovr.", write=True),
        ),
    ),
    CapabilityFamily(
        family="vendor_bill_payments",
        label="Vendor Bill Payments",
        capabilities=(
            _cap("vendor_bill_payments", "read", STATUS_READ_ONLY,
                 "VendorBillPayment read complete."),
            _cap("vendor_bill_payments", "write", STATUS_PRODUCTION_PENDING,
                 "VendorBillPayment write in progress on Clio's side.", write=True),
        ),
    ),
    CapabilityFamily(
        family="clients",
        label="Clients",
        capabilities=(
            _cap("clients", "read", STATUS_READ_ONLY,
                 "Client read complete; read-only for now."),
        ),
    ),
    CapabilityFamily(
        family="matters",
        label="Matters",
        capabilities=(
            _cap("matters", "read", STATUS_READ_ONLY,
                 "Matter read complete; read-only for now."),
        ),
    ),
    CapabilityFamily(
        family="vendors",
        label="Vendors",
        capabilities=(
            _cap("vendors", "read", STATUS_READ_ONLY,
                 "Vendor read complete."),
            _cap("vendors", "write", STATUS_PRODUCTION_PENDING,
                 "Vendor write coming on Clio's side.", write=True),
        ),
    ),
    CapabilityFamily(
        family="expenses",
        label="Expenses",
        capabilities=(
            _cap("expenses", "read", STATUS_PRODUCTION_PENDING,
                 "Expense read coming on Clio's side."),
            _cap("expenses", "write", STATUS_PRODUCTION_PENDING,
                 "Expense write coming on Clio's side.", write=True),
        ),
    ),
)

_FAMILY_BY_NAME = {f.family: f for f in _FAMILIES}
_CAP_BY_KEY = {c.key: c for fam in _FAMILIES for c in fam.capabilities}


# ---------------------------------------------------------------------------
# Foundational platform enhancements (not per-endpoint, but part of readiness)
#
# These describe cross-cutting API hardening from the roadmap. Tracked so the
# operator readiness view can show that idempotency/pagination/contract
# validation exist, which is what makes durable client work safe to build now.
# ---------------------------------------------------------------------------

PLATFORM_NOTES: tuple[dict, ...] = (
    {"key": "idempotency", "label": "Idempotency keys",
     "status": "available", "note": "Hardened idempotency on writes (roadmap)."},
    {"key": "filtering", "label": "Shared filtering", "status": "available",
     "note": "Shared filtering across list endpoints."},
    {"key": "pagination", "label": "Pagination (max 200)", "status": "available",
     "note": "Page size capped at 200 — reflected in payload builders."},
    {"key": "contract_validation_ci", "label": "CI/OAS contract validation",
     "status": "available", "note": "OpenAPI contract validated in CI."},
    {"key": "contract_validation_runtime", "label": "Runtime contract validation",
     "status": "available", "note": "Runtime request/response contract checks."},
    {"key": "observability", "label": "Correlation IDs / observability",
     "status": "available", "note": "Correlation IDs on requests for tracing."},
)

# Clio's list endpoints cap page size at 200 (roadmap). Payload builders and any
# future paginated reader must not request more than this.
MAX_PAGE_SIZE = 200

# Phase-1 production go-live target (internal planning only; not a commitment).
# TODO(clio-docs): confirm against official Clio production availability notice.
PHASE_1_GO_LIVE_TARGET = "2026-07-31"


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------

def families() -> list[CapabilityFamily]:
    """All capability families, in roadmap display order."""
    return list(_FAMILIES)


def all_capabilities() -> list[Capability]:
    """Flat list of every capability across all families."""
    return [c for fam in _FAMILIES for c in fam.capabilities]


def capability(key: str) -> Optional[Capability]:
    """Look up one capability by its ``family.operation`` key, or None."""
    return _CAP_BY_KEY.get(key)


def get_status(key: str) -> Optional[str]:
    """Assumed status string for a capability key, or None if unknown."""
    cap = _CAP_BY_KEY.get(key)
    return cap.status if cap else None


def is_write_supported_by_clio(key: str) -> bool:
    """True if Clio's API surface is believed to support this write.

    Note this is the *Clio* axis only. It says nothing about whether Cutovr
    will actually call it — that is gated separately by the adapter's live-mode
    flag and allow-list. ``feature_flag_disabled`` means Clio supports it but
    Cutovr keeps it off; ``write_supported`` means supported and not yet gated.
    """
    cap = _CAP_BY_KEY.get(key)
    if not cap or not cap.write:
        return False
    return cap.status in (STATUS_WRITE_SUPPORTED, STATUS_FEATURE_FLAG_DISABLED)


def registry_snapshot() -> dict:
    """Serializable snapshot for operator views / tests / logs.

    Secret-free and explicitly marked internal. Includes the platform notes and
    a per-status summary count so the operator view can render at a glance.
    """
    caps = all_capabilities()
    summary: dict[str, int] = {s: 0 for s in ALL_STATUSES}
    for c in caps:
        summary[c.status] = summary.get(c.status, 0) + 1
    return {
        "internal_only": True,
        "source": "assumed_from_roadmap",  # NOT official docs — see module docstring.
        "docs_published": False,
        "phase_1_go_live_target": PHASE_1_GO_LIVE_TARGET,
        "max_page_size": MAX_PAGE_SIZE,
        "families": [f.to_dict() for f in _FAMILIES],
        "platform_notes": [dict(n) for n in PLATFORM_NOTES],
        "status_summary": summary,
        "capability_count": len(caps),
    }
