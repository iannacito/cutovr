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


# ===========================================================================
# Clio Accounting API v1 adapter boundary
#
# The single, clearly-named seam where future *live* Clio Accounting API calls
# will land. It exposes typed operation names for each endpoint family from the
# roadmap but, crucially, defaults to DISABLED / DRY-RUN: no network call is
# made unless ``CLIO_ACCOUNTING_API_ENABLED`` is set AND the operation is
# allow-listed. A write in the default (disabled) mode returns a structured
# *blocked* result — it never silently succeeds.
#
# Idempotency is a first-class part of the interface because Clio's roadmap
# includes hardened idempotency keys on writes: every operation accepts /
# generates an idempotency key and echoes it in the result metadata.
#
# Env (no secrets required to import or run tests):
#   CLIO_ACCOUNTING_API_ENABLED   truthy -> live mode permitted (still allow-listed)
#   CLIO_ACCOUNTING_API_BASE_URL  base URL for the API (unset in dry-run)
#   CLIO_ACCOUNTING_API_TOKEN     bearer/OAuth access token placeholder
#
# TODO(clio-docs): when the developer portal publishes the OpenAPI schema +
# auth model, implement ``_perform_live`` with a real HTTP client, confirm the
# OAuth scopes, and replace the idempotency-header name with Clio's official one.
# ===========================================================================

import uuid
from typing import Optional

import clio_accounting_capabilities as caps


# Result-status vocabulary for adapter operations (stable strings).
RESULT_DRY_RUN = "dry_run"      # validated + would-send payload; no call made.
RESULT_BLOCKED = "blocked"      # a write attempted while live mode is disabled.
RESULT_DISABLED = "disabled"    # operation not allow-listed / capability not ready.
RESULT_OK = "ok"               # a live call succeeded (future).
RESULT_ERROR = "error"          # a live call failed (future).

# Header Clio is expected to use for idempotency keys. Placeholder until the
# official docs confirm the exact name/format.
# TODO(clio-docs): confirm idempotency header name + key format.
IDEMPOTENCY_HEADER = "Idempotency-Key"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def new_idempotency_key(prefix: str = "cutovr") -> str:
    """Generate a stable, unique idempotency key for a write operation.

    Callers may pass their own key (e.g. derived from a job + entity id so a
    retry reuses it); this is the fallback when none is supplied.
    """
    return f"{prefix}-{uuid.uuid4()}"


@dataclass
class OperationResult:
    """The outcome of an adapter operation — always structured, never a bare
    success/failure boolean.

    ``status`` is one of the RESULT_* constants. For every write attempted while
    live mode is off, ``status`` is ``blocked`` and ``performed`` is False, so a
    caller can never mistake "we didn't post" for "we posted".
    """

    operation: str          # capability key, e.g. "journal_entries.create"
    status: str
    performed: bool          # True only when a live API call actually ran.
    dry_run: bool
    idempotency_key: Optional[str]
    message: str
    payload: Optional[dict] = None       # the canonical payload we would send.
    response: Optional[dict] = None      # live response (future); None in dry-run.

    def to_dict(self) -> dict:
        return asdict(self)


class ClioAccountingAdapter:
    """Interface to the (future) live Clio Accounting API v1.

    Construct once and call the typed operation methods. In the default
    disabled/dry-run configuration the adapter performs NO network I/O; it
    validates the operation against the capability registry and returns a
    structured result describing what *would* be sent. This lets Cutovr build
    and test the full call-site wiring now, safely, without a live endpoint.

    ``live_writes_allowed`` requires BOTH the global enable flag and a base URL
    to be configured. Even then, writes only proceed for capabilities Clio is
    believed to support; anything else returns a ``blocked``/``disabled`` result.
    """

    def __init__(
        self,
        *,
        enabled: Optional[bool] = None,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
    ):
        # Explicit args win (useful for tests); otherwise read env.
        self.enabled = _api_enabled_env() if enabled is None else bool(enabled)
        self.base_url = base_url if base_url is not None else _env("CLIO_ACCOUNTING_API_BASE_URL")
        # Token is a placeholder for a bearer/OAuth access token. Never logged.
        self._token = token if token is not None else _env("CLIO_ACCOUNTING_API_TOKEN")

    # -- configuration introspection (secret-free) --------------------------

    @property
    def live_writes_allowed(self) -> bool:
        """True only when live mode is on AND minimally configured.

        Requires the enable flag and a base URL. Token presence is checked at
        call time so the adapter can still dry-run without any secret set.
        """
        return bool(self.enabled and self.base_url)

    def config_summary(self) -> dict:
        """Secret-free snapshot for operator views. Never includes the token."""
        return {
            "enabled": self.enabled,
            "base_url_configured": bool(self.base_url),
            "token_configured": bool(self._token),
            "live_writes_allowed": self.live_writes_allowed,
            "mode": "live" if self.live_writes_allowed else "dry_run",
        }

    # -- typed operations ---------------------------------------------------
    # One method per roadmap write family. Reads are intentionally not modeled
    # as mutating operations; a future read client can be added the same way.

    def create_ledger_account(self, payload: dict, *, idempotency_key: Optional[str] = None) -> OperationResult:
        return self._execute("ledger_accounts.create", payload, idempotency_key)

    def update_ledger_account(self, payload: dict, *, idempotency_key: Optional[str] = None) -> OperationResult:
        return self._execute("ledger_accounts.update", payload, idempotency_key)

    def deactivate_ledger_account(self, payload: dict, *, idempotency_key: Optional[str] = None) -> OperationResult:
        return self._execute("ledger_accounts.deactivate", payload, idempotency_key)

    def reactivate_ledger_account(self, payload: dict, *, idempotency_key: Optional[str] = None) -> OperationResult:
        return self._execute("ledger_accounts.reactivate", payload, idempotency_key)

    def create_journal_entry(self, payload: dict, *, idempotency_key: Optional[str] = None) -> OperationResult:
        return self._execute("journal_entries.create", payload, idempotency_key)

    def update_journal_entry(self, payload: dict, *, idempotency_key: Optional[str] = None) -> OperationResult:
        return self._execute("journal_entries.update", payload, idempotency_key)

    def destroy_journal_entry(self, payload: dict, *, idempotency_key: Optional[str] = None) -> OperationResult:
        return self._execute("journal_entries.destroy", payload, idempotency_key)

    def create_report(self, payload: dict, *, idempotency_key: Optional[str] = None) -> OperationResult:
        return self._execute("reports.create", payload, idempotency_key)

    def create_vendor_bill(self, payload: dict, *, idempotency_key: Optional[str] = None) -> OperationResult:
        return self._execute("vendor_bills.write", payload, idempotency_key)

    def create_vendor_bill_payment(self, payload: dict, *, idempotency_key: Optional[str] = None) -> OperationResult:
        return self._execute("vendor_bill_payments.write", payload, idempotency_key)

    def create_vendor(self, payload: dict, *, idempotency_key: Optional[str] = None) -> OperationResult:
        return self._execute("vendors.write", payload, idempotency_key)

    def create_expense(self, payload: dict, *, idempotency_key: Optional[str] = None) -> OperationResult:
        return self._execute("expenses.write", payload, idempotency_key)

    # -- core dispatch ------------------------------------------------------

    def _execute(self, operation: str, payload: dict, idempotency_key: Optional[str]) -> OperationResult:
        """Validate + route a write operation. Never raises for control flow.

        Order of gates:
          1. Unknown/non-write capability -> disabled (programming error guard).
          2. Live mode off -> blocked (the default, safe path; no I/O).
          3. Clio doesn't support this write yet -> blocked.
          4. Live + supported + allow-listed -> perform (future).
        """
        key = idempotency_key or new_idempotency_key()
        cap = caps.capability(operation)

        if cap is None or not cap.write:
            return OperationResult(
                operation=operation, status=RESULT_DISABLED, performed=False,
                dry_run=not self.live_writes_allowed, idempotency_key=key,
                message=f"Unknown or non-write operation '{operation}'.",
                payload=payload,
            )

        if not self.live_writes_allowed:
            return OperationResult(
                operation=operation, status=RESULT_BLOCKED, performed=False,
                dry_run=True, idempotency_key=key,
                message=(
                    "Clio Accounting live mode is disabled — this write was "
                    "NOT sent. Payload validated for a future live run. Set "
                    "CLIO_ACCOUNTING_API_ENABLED and CLIO_ACCOUNTING_API_BASE_URL "
                    "to enable."
                ),
                payload=payload,
            )

        if not caps.is_write_supported_by_clio(operation):
            return OperationResult(
                operation=operation, status=RESULT_BLOCKED, performed=False,
                dry_run=False, idempotency_key=key,
                message=(
                    f"Clio Accounting does not yet support '{operation}' "
                    f"(assumed status: {cap.status}). Write NOT sent."
                ),
                payload=payload,
            )

        # Live + supported. The real HTTP client is intentionally not built yet.
        return self._perform_live(operation, payload, key)

    def _perform_live(self, operation: str, payload: dict, idempotency_key: str) -> OperationResult:
        """Placeholder for the real HTTP call. Not implemented until docs exist.

        Even in live mode we fail closed rather than pretend to post, so no code
        path can ever silently "succeed" against an endpoint we haven't built.

        TODO(clio-docs): implement with a real client:
          - POST {base_url}/{resource} with Authorization: Bearer <token>
          - send {IDEMPOTENCY_HEADER}: <idempotency_key>
          - validate response against the published OpenAPI schema
          - map to OperationResult(status=RESULT_OK, performed=True, response=...)
        """
        return OperationResult(
            operation=operation, status=RESULT_BLOCKED, performed=False,
            dry_run=False, idempotency_key=idempotency_key,
            message=(
                "Live Clio Accounting client not implemented yet — awaiting "
                "official developer-portal docs/OpenAPI. Write NOT sent."
            ),
            payload=payload,
        )


def get_adapter() -> ClioAccountingAdapter:
    """Construct an adapter from the current environment."""
    return ClioAccountingAdapter()
