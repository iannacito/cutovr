"""Smoke tests: entity-name resolution for GL posting (Roadblock 1).

QuickBooks requires a Customer/Vendor on every A/R and A/P journal line.
The previous code passed whatever the GL row produced straight to QBO,
which created nameless entities when a row had no usable client/vendor
column. ``entity_resolution`` fixes this two ways:

  * It consults the firm's uploaded customer/vendor *listings* first, so
    names come from the firm's own records rather than a GL guess.
  * It refuses to return a blank name — raising ``EntityNameError`` so the
    import stops with a clear, fixable message instead of syncing a blank
    DisplayName.

Covered
-------
  R1  A listing match (by id, then by normalized name) wins over the
      GL-inferred hint.
  R2  A blank/whitespace hint with no listing match raises EntityNameError
      (no blank name ever returned).
  R3  With no listing, a non-blank GL hint is used as a safe fallback.
  R4  A customer matched by matter name / "customer - matter" combo
      resolves to the listing's customer name.
  R5  EntityNameError carries the offending rows so the operator sees
      exactly which lines need a name.

Run from project root::

    python3 tests/smoke_entity_resolution.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import entity_resolution as er  # noqa: E402


def r1_listing_match_wins():
    cust = er.build_customer_index([
        {"customer_id": "C-100", "customer_name": "Acme Industries LLP"},
    ])
    # By id: the GL hint name is ignored in favor of the listing's name.
    name = er.resolve_entity_name(
        kind="Customer", hint_name="acme (typo)", identifier="C-100", index=cust,
    )
    assert name == "Acme Industries LLP", name
    # By normalized name: spacing/case/punctuation differences still match.
    name2 = er.resolve_entity_name(
        kind="Customer", hint_name="ACME  industries  llp", identifier=None, index=cust,
    )
    assert name2 == "Acme Industries LLP", name2
    print("R1 OK: listing match (by id, then normalized name) wins over GL hint")


def r2_blank_name_refused():
    cust = er.build_customer_index([])  # empty listing
    for bad in ("", "   ", None, "\t\n"):
        try:
            er.resolve_entity_name(kind="Customer", hint_name=bad, index=cust)
        except er.EntityNameError as e:
            assert e.kind == "Customer"
            assert e.offenders, "offenders should not be empty"
        else:
            raise AssertionError(f"expected EntityNameError for hint {bad!r}")
    print("R2 OK: blank/whitespace names are refused (never synced to QuickBooks)")


def r3_gl_fallback_when_no_listing():
    # No index at all -> a non-blank GL hint is the safe fallback.
    name = er.resolve_entity_name(kind="Vendor", hint_name="Office Supplies Co", index=None)
    assert name == "Office Supplies Co", name
    # Empty index, same behavior.
    name2 = er.resolve_entity_name(
        kind="Vendor", hint_name="Courier Express", index=er.build_vendor_index([]),
    )
    assert name2 == "Courier Express", name2
    print("R3 OK: non-blank GL hint is used as fallback when no listing match")


def r4_matter_combo_resolves_to_customer():
    cust = er.build_customer_index([
        {"customer_id": "C-7", "customer_name": "Beta Holdings",
         "matter_id": "M-22", "matter_name": "Estate of Jones"},
    ])
    # GL A/R rows often reference the matter rather than the client.
    by_matter = er.resolve_entity_name(
        kind="Customer", hint_name="Estate of Jones", index=cust,
    )
    assert by_matter == "Beta Holdings", by_matter
    by_combo = er.resolve_entity_name(
        kind="Customer", hint_name="Beta Holdings - Estate of Jones", index=cust,
    )
    assert by_combo == "Beta Holdings", by_combo
    by_matter_id = er.resolve_entity_name(
        kind="Customer", hint_name="", identifier="M-22", index=cust,
    )
    assert by_matter_id == "Beta Holdings", by_matter_id
    print("R4 OK: matter name / combo / matter-id all resolve to the listing customer")


def r5_error_lists_offenders():
    try:
        er.resolve_entity_name(
            kind="Vendor", hint_name="  ", identifier="V-999", index=er.build_vendor_index([]),
        )
    except er.EntityNameError as e:
        assert "V-999" in "; ".join(e.offenders), e.offenders
    else:
        raise AssertionError("expected EntityNameError")
    print("R5 OK: EntityNameError names the offending row (id) for the operator")


def main():
    r1_listing_match_wins()
    r2_blank_name_refused()
    r3_gl_fallback_when_no_listing()
    r4_matter_combo_resolves_to_customer()
    r5_error_lists_offenders()
    print("\nAll entity-resolution smoke tests passed.")


if __name__ == "__main__":
    main()
