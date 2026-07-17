"""Clio Accounting API v1 — canonical payload builders.

Cutovr prepares migration data in *its own* stable, canonical shape and maps
that shape to Clio Accounting request payloads here, in one place. The point is
NOT to guess Clio's final wire schema (the official OpenAPI is not published
yet) — it is to localize the eventual schema change to this module. Everything
upstream (extractors, mappers, the readiness workflow) speaks Cutovr-canonical;
only these builders know the Clio field names.

Each builder:
  * takes plain Cutovr-canonical values (dicts / primitives),
  * returns a JSON-serializable ``dict`` ready to hand to the adapter,
  * attaches ``_meta`` with an idempotency key + a schema-version marker so a
    future live run has idempotency wired end to end,
  * makes NO network call and needs NO secrets.

  TODO(clio-docs): every field name and required/optional decision below is an
  ASSUMPTION. Replace with the official developer-portal schema when published,
  and add real validation (required fields, enums, value ranges). Keeping the
  canonical inputs stable means those changes stay inside this file.
"""

from __future__ import annotations

from typing import Any, Optional

import clio_accounting as ca
import clio_accounting_capabilities as caps

# Bump when the assumed Clio payload shape changes, so stored/dry-run payloads
# are traceable to the schema they were built against.
PAYLOAD_SCHEMA_VERSION = "v1-assumed-2026-07"


def _meta(operation: str, idempotency_key: Optional[str], source_ref: Optional[str]) -> dict:
    """Common ``_meta`` block attached to every canonical payload.

    ``_meta`` is Cutovr-internal envelope data, not part of Clio's body. The
    adapter reads ``idempotency_key`` from here (or the explicit arg) and sends
    it as a header; ``_meta`` itself would be stripped before the wire call.
    """
    return {
        "operation": operation,
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "idempotency_key": idempotency_key or ca.new_idempotency_key(),
        "source_ref": source_ref,   # Cutovr job/entity id for traceability.
        "assumed_schema": True,     # NOT validated against official docs yet.
    }


def _clean(d: dict) -> dict:
    """Drop keys whose value is None so payloads stay tidy and diffable.

    Nested dicts are cleaned recursively; the ``_meta`` block is preserved as-is.
    """
    out: dict[str, Any] = {}
    for k, v in d.items():
        if k == "_meta":
            out[k] = v
            continue
        if v is None:
            continue
        out[k] = _clean(v) if isinstance(v, dict) else v
    return out


# ---------------------------------------------------------------------------
# Ledger accounts (chart of accounts)
# ---------------------------------------------------------------------------

def build_ledger_account(
    *,
    number: Optional[str],
    name: str,
    account_type: Optional[str] = None,
    subtype: Optional[str] = None,
    parent_number: Optional[str] = None,
    active: bool = True,
    description: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> dict:
    """Canonical ledger-account (chart-of-accounts) create/update payload."""
    return _clean({
        "account_number": number,
        "name": name,
        "account_type": account_type,   # TODO(clio-docs): confirm enum values.
        "subtype": subtype,
        "parent_account_number": parent_number,
        "active": active,
        "description": description,
        "_meta": _meta("ledger_accounts.create", idempotency_key, source_ref),
    })


def build_ledger_account_status_change(
    *,
    number: str,
    active: bool,
    idempotency_key: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> dict:
    """Payload for deactivate/reactivate of an existing ledger account."""
    op = "ledger_accounts.reactivate" if active else "ledger_accounts.deactivate"
    return _clean({
        "account_number": number,
        "active": active,
        "_meta": _meta(op, idempotency_key, source_ref),
    })


# ---------------------------------------------------------------------------
# Journal entries
# ---------------------------------------------------------------------------

def build_journal_entry(
    *,
    entry_date: str,
    lines: list[dict],
    memo: Optional[str] = None,
    reference: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> dict:
    """Canonical journal-entry payload.

    ``lines`` is a list of Cutovr-canonical dicts, each with at least
    ``account_number`` and one of ``debit`` / ``credit``. Balancing is validated
    (debits must equal credits) because an unbalanced entry can never post — this
    is a real invariant, not a schema guess.
    """
    norm_lines = [_journal_line(ln) for ln in lines]
    _assert_balanced(norm_lines)
    return _clean({
        "date": entry_date,
        "memo": memo,
        "reference": reference,
        "lines": norm_lines,
        "_meta": _meta("journal_entries.create", idempotency_key, source_ref),
    })


def _journal_line(line: dict) -> dict:
    debit = _money(line.get("debit"))
    credit = _money(line.get("credit"))
    return {
        "account_number": line.get("account_number"),
        "debit": debit,
        "credit": credit,
        "description": line.get("description"),
    }


def _money(v: Any) -> float:
    """Coerce a monetary value to a rounded float (2dp). None -> 0.0."""
    if v is None or v == "":
        return 0.0
    return round(float(v), 2)


def _assert_balanced(lines: list[dict]) -> None:
    debits = round(sum(l["debit"] for l in lines), 2)
    credits = round(sum(l["credit"] for l in lines), 2)
    if debits != credits:
        raise ValueError(
            f"Journal entry does not balance: debits={debits} credits={credits}"
        )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def build_report_request(
    *,
    report_type: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    as_of_date: Optional[str] = None,
    filters: Optional[dict] = None,
    idempotency_key: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> dict:
    """Canonical report-generation request payload."""
    return _clean({
        "report_type": report_type,   # TODO(clio-docs): confirm supported types.
        "start_date": start_date,
        "end_date": end_date,
        "as_of_date": as_of_date,
        "filters": filters or None,
        "_meta": _meta("reports.create", idempotency_key, source_ref),
    })


# ---------------------------------------------------------------------------
# Vendor bills & payments
# ---------------------------------------------------------------------------

def build_vendor_bill(
    *,
    vendor_ref: str,
    bill_date: str,
    due_date: Optional[str] = None,
    bill_number: Optional[str] = None,
    lines: Optional[list[dict]] = None,
    total: Optional[float] = None,
    memo: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> dict:
    """Canonical vendor-bill payload."""
    return _clean({
        "vendor_ref": vendor_ref,
        "bill_date": bill_date,
        "due_date": due_date,
        "bill_number": bill_number,
        "lines": [_clean(dict(l)) for l in (lines or [])] or None,
        "total": _money(total) if total is not None else None,
        "memo": memo,
        "_meta": _meta("vendor_bills.write", idempotency_key, source_ref),
    })


def build_vendor_bill_payment(
    *,
    vendor_ref: str,
    payment_date: str,
    amount: float,
    bill_refs: Optional[list[str]] = None,
    payment_method: Optional[str] = None,
    bank_account_number: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> dict:
    """Canonical vendor-bill-payment payload.

    Note: the Clio write endpoint for this family is roadmap-`production_pending`
    (write in progress), so the adapter will block a live send — this builder
    still produces the payload so dry-runs and tests are ready ahead of time.
    """
    return _clean({
        "vendor_ref": vendor_ref,
        "payment_date": payment_date,
        "amount": _money(amount),
        "bill_refs": bill_refs or None,
        "payment_method": payment_method,
        "bank_account_number": bank_account_number,
        "_meta": _meta("vendor_bill_payments.write", idempotency_key, source_ref),
    })


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------

def build_expense(
    *,
    expense_date: str,
    amount: float,
    account_number: Optional[str] = None,
    vendor_ref: Optional[str] = None,
    matter_ref: Optional[str] = None,
    memo: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> dict:
    """Canonical expense payload (Clio expense read/write is roadmap-`coming`)."""
    return _clean({
        "expense_date": expense_date,
        "amount": _money(amount),
        "account_number": account_number,
        "vendor_ref": vendor_ref,
        "matter_ref": matter_ref,
        "memo": memo,
        "_meta": _meta("expenses.write", idempotency_key, source_ref),
    })


# ---------------------------------------------------------------------------
# Reference resolution placeholders (vendor / client / matter)
#
# Clients & Matters are read-only and Vendor write is pending, so Cutovr cannot
# create these in Clio yet. During a migration we still need to *reference* them
# from bills/payments/expenses. This builds an unresolved placeholder that a
# future resolver step (once Clio read/write are wired) can fill with a real id.
# ---------------------------------------------------------------------------

def build_reference(
    *,
    entity_type: str,          # "vendor" | "client" | "matter"
    display_name: str,
    external_id: Optional[str] = None,
    email: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> dict:
    """Unresolved reference to a vendor/client/matter for later resolution."""
    if entity_type not in ("vendor", "client", "matter"):
        raise ValueError(f"Unknown reference entity_type: {entity_type!r}")
    # Not run through _clean: clio_id/resolved are intentionally kept even when
    # empty, because they are the fields a future resolver mutates in place.
    return {
        "entity_type": entity_type,
        "display_name": display_name,
        "external_id": external_id,   # id in the source system (PCLaw/QBO).
        "email": email,
        "clio_id": None,              # filled by a future resolver against Clio.
        "resolved": False,
        "_meta": {
            "schema_version": PAYLOAD_SCHEMA_VERSION,
            "source_ref": source_ref,
            "assumed_schema": True,
        },
    }


def is_reference_resolved(ref: dict) -> bool:
    """True once a reference has a Clio id attached."""
    return bool(ref.get("resolved")) and bool(ref.get("clio_id"))


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

# Maps each builder's target operation to the capability key it will hit, so a
# caller / test can confirm every builder aligns with a known capability.
BUILDER_OPERATIONS = {
    "ledger_account": "ledger_accounts.create",
    "ledger_account_status_change": "ledger_accounts.deactivate",
    "journal_entry": "journal_entries.create",
    "report_request": "reports.create",
    "vendor_bill": "vendor_bills.write",
    "vendor_bill_payment": "vendor_bill_payments.write",
    "expense": "expenses.write",
}


def builder_operations_are_known() -> bool:
    """True if every builder maps to a capability the registry knows about."""
    return all(caps.capability(op) is not None for op in BUILDER_OPERATIONS.values())
