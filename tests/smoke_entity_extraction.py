"""Smoke tests: A/R and A/P entity extraction from real PCLaw GL headers.

Background — Cesar's QA 2026-06-03 (item 9)
-------------------------------------------
A real PCLaw migration created only *one* customer ("PCLaw Test Customer")
even though the general ledger covered many clients. Root cause:
``derive_entity_hint`` looked only for the exact snake_case headers
``customer_name`` / ``client_name`` / ``client_id`` / ``matter_id``. PCLaw
exports the client/matter under headers like "Client", "Client Name",
"Matter" (and sometimes carries it in "Reference"), so every A/R line fell
through to the single default name — collapsing every client into one
QuickBooks customer.

The fix matches a broader set of header spellings case-insensitively
(ignoring spaces / separators) so distinct clients become distinct
customers, while still falling back to the safe default when a row truly
has no entity column.

Covered
-------
  E1  "Client Name" / "Client" / "Matter" headers are recognized for A/R.
  E2  Priority order is honored (an explicit customer column wins over a
      generic "reference").
  E3  A/P recognizes "Payee" / "Vendor" variants.
  E4  A row with no entity column still falls back to the default (import
      never breaks).
  E5  Distinct clients across rows yield distinct customer names (the
      actual bug: not everything collapses to one customer).

Run from project root::

    python3 tests/smoke_entity_extraction.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pclaw_pipeline import (  # noqa: E402
    DEFAULT_CUSTOMER_NAME,
    DEFAULT_VENDOR_NAME,
    derive_entity_hint,
)

AR = "Accounts Receivable"
AP = "Accounts Payable"


def e1_recognizes_pclaw_client_headers():
    for header in ("Client Name", "Client", "Matter", "client_name", "Client-Name"):
        row = {header: "Acme Industries", "debit": "100", "credit": "0"}
        kind, name, _id = derive_entity_hint(row, AR)
        assert kind == "Customer", (header, kind)
        assert name == "Acme Industries", (header, name)
    print("E1 OK: PCLaw client/matter header variants recognized for A/R")


def e2_priority_order_honored():
    # An explicit customer column must win over the generic "Reference".
    row = {"Reference": "INV-1001", "Client Name": "Beta LLP",
           "debit": "100", "credit": "0"}
    _kind, name, _id = derive_entity_hint(row, AR)
    assert name == "Beta LLP", name
    # When only Reference carries the entity, it is used rather than the
    # default (better than collapsing everyone into one customer).
    row2 = {"Reference": "Gamma Trust", "debit": "100", "credit": "0"}
    _k2, name2, _id2 = derive_entity_hint(row2, AR)
    assert name2 == "Gamma Trust", name2
    print("E2 OK: explicit customer column beats generic reference")


def e3_vendor_variants():
    for header in ("Payee", "Vendor", "vendor_name", "Supplier", "Paid To"):
        row = {header: "Office Supplies Co", "debit": "0", "credit": "50"}
        kind, name, _id = derive_entity_hint(row, AP)
        assert kind == "Vendor", (header, kind)
        assert name == "Office Supplies Co", (header, name)
    print("E3 OK: A/P recognizes payee/vendor/supplier variants")


def e4_safe_default_when_no_entity_column():
    row = {"account_number": "1200", "debit": "100", "credit": "0"}
    _kind, name, _id = derive_entity_hint(row, AR)
    assert name == DEFAULT_CUSTOMER_NAME, name
    row_ap = {"account_number": "2000", "debit": "0", "credit": "100"}
    _k, name_ap, _id_ap = derive_entity_hint(row_ap, AP)
    assert name_ap == DEFAULT_VENDOR_NAME, name_ap
    print("E4 OK: falls back to the safe default when no entity column")


def e5_distinct_clients_yield_distinct_customers():
    rows = [
        {"Client Name": "Acme Industries"},
        {"Client Name": "Beta LLP"},
        {"Client Name": "Gamma Trust"},
        {"Client Name": "Acme Industries"},  # repeat is fine
    ]
    names = {derive_entity_hint(r, AR)[1] for r in rows}
    assert names == {"Acme Industries", "Beta LLP", "Gamma Trust"}, names
    assert DEFAULT_CUSTOMER_NAME not in names, \
        "real clients must not collapse into the single default customer"
    print("E5 OK: distinct clients map to distinct customers (the bug is fixed)")


def main():
    failures = []
    for fn in (
        e1_recognizes_pclaw_client_headers,
        e2_priority_order_honored,
        e3_vendor_variants,
        e4_safe_default_when_no_entity_column,
        e5_distinct_clients_yield_distinct_customers,
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e}")
    if failures:
        raise SystemExit(f"{len(failures)} test(s) failed")
    print("\nALL ENTITY-EXTRACTION SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
