"""Resolve QuickBooks Customer / Vendor display names for GL posting.

The general-ledger import posts Accounts Receivable / Accounts Payable
lines, and QuickBooks requires an ``Entity`` (a Customer or Vendor) on
each of those lines. The name for that entity comes from one of two
places:

  1. The uploaded vendor / customer **listings** (the firm's own
     authoritative list of who they bill and who they pay). This is the
     preferred source — names come straight from the firm's records.
  2. A name **inferred from the GL row** itself (a client / matter or
     payee column). This is the fallback when no listing match is found.

Two failure modes this module exists to prevent:

  * **Empty names in QuickBooks.** The previous code passed whatever the
    GL row produced straight to ``create_customer`` / ``create_vendor``.
    When a row had no usable client/vendor column the name could be an
    empty or whitespace-only string, and QuickBooks happily created an
    entity with a blank ``DisplayName``. Operators then saw nameless
    customers / vendors they could not act on. ``resolve_entity_name``
    refuses to return a blank name and ``EntityNameError`` makes the
    import stop with a clear, fixable message instead.

  * **Listings ignored.** The listings were parsed and stored on the job
    but never consulted at posting time, so a firm that uploaded a clean
    vendor list still got GL-inferred names. This module indexes the
    listings and matches on identifier first, then normalized name.

Nothing here performs I/O or talks to QuickBooks; it is a pure
projection over (parsed listings, entity hint) so it is trivially
testable without a live QBO connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


def _normalize_name(value) -> str:
    """Collapse a display name to a comparable key.

    Lower-cased, alphanumeric-only so "Smith & Co.", "smith and co",
    and "SMITH  &  CO" all compare differently-but-safely. We keep
    ``and`` distinct from ``&`` on purpose — over-normalizing risks
    merging two genuinely different entities, which is worse than
    missing a match (the fallback is still a real, non-blank name).
    """
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _clean(value) -> str:
    return str(value or "").strip()


@dataclass
class EntityIndex:
    """Lookup tables built from a parsed vendor or customer listing."""

    by_id: Dict[str, str] = field(default_factory=dict)
    by_name: Dict[str, str] = field(default_factory=dict)
    names: List[str] = field(default_factory=list)

    def lookup(self, *, identifier: Optional[str], name: Optional[str]) -> Optional[str]:
        """Return the listing's display name for an id or name, or None.

        Identifier match wins over name match because a client/matter id
        is unambiguous where a name can collide.
        """
        ident = _normalize_name(identifier)
        if ident and ident in self.by_id:
            return self.by_id[ident]
        key = _normalize_name(name)
        if key and key in self.by_name:
            return self.by_name[key]
        return None


def build_customer_index(parsed_customer_list) -> EntityIndex:
    """Index a parsed customer listing (see report_types.parse_customer_list).

    Customers can be matched by customer id, matter id, the customer
    name itself, or a "customer - matter" combined name, since GL A/R
    rows reference clients in any of those forms.
    """
    index = EntityIndex()
    for row in parsed_customer_list or []:
        name = _clean(row.get("customer_name"))
        if not name:
            continue
        index.names.append(name)
        name_key = _normalize_name(name)
        if name_key:
            index.by_name.setdefault(name_key, name)
        matter_name = _clean(row.get("matter_name"))
        if matter_name:
            combo = f"{name} - {matter_name}"
            index.by_name.setdefault(_normalize_name(combo), name)
            index.by_name.setdefault(_normalize_name(matter_name), name)
        for id_field in ("customer_id", "matter_id"):
            ident = _normalize_name(row.get(id_field))
            if ident:
                index.by_id.setdefault(ident, name)
    return index


def build_vendor_index(parsed_vendor_list) -> EntityIndex:
    """Index a parsed vendor listing (see report_types.parse_vendor_list)."""
    index = EntityIndex()
    for row in parsed_vendor_list or []:
        name = _clean(row.get("vendor_name"))
        if not name:
            continue
        index.names.append(name)
        name_key = _normalize_name(name)
        if name_key:
            index.by_name.setdefault(name_key, name)
        ident = _normalize_name(row.get("vendor_id"))
        if ident:
            index.by_id.setdefault(ident, name)
    return index


class EntityNameError(Exception):
    """A GL entity could not be resolved to a non-blank display name.

    Carries the ``kind`` (Customer / Vendor) and a list of human-readable
    descriptions of the offending rows so the import route can show the
    operator exactly what needs a name before posting can proceed.
    """

    def __init__(self, kind: str, offenders: List[str]):
        self.kind = kind
        self.offenders = offenders
        preview = "; ".join(offenders[:5])
        more = "" if len(offenders) <= 5 else f" (+{len(offenders) - 5} more)"
        super().__init__(
            f"{len(offenders)} {kind} line(s) have no usable name: {preview}{more}"
        )


# Names the GL fallback uses when a row carries no client/vendor at all.
# These are deliberately obvious placeholders, but they are still
# non-blank, so they never create a nameless QuickBooks entity.
_PLACEHOLDER_NAMES = {
    "pclaw test customer",
    "pclaw test vendor",
}


def is_placeholder_name(name) -> bool:
    return _normalize_name(name) in {
        _normalize_name(p) for p in _PLACEHOLDER_NAMES
    }


def resolve_entity_name(
    *,
    kind: str,
    hint_name: Optional[str],
    identifier: Optional[str] = None,
    index: Optional[EntityIndex] = None,
    allow_gl_fallback: bool = True,
) -> str:
    """Resolve the QuickBooks display name for one A/R or A/P entity.

    Resolution order:

      1. Listing match (by identifier, then by normalized name).
      2. The GL-inferred ``hint_name`` (the fallback), only when
         ``allow_gl_fallback`` is true and the name is non-blank.

    Raises ``EntityNameError`` if no non-blank name can be resolved, so
    the caller never sends a blank ``DisplayName`` to QuickBooks.
    """
    if index is not None:
        matched = index.lookup(identifier=identifier, name=hint_name)
        if matched and matched.strip():
            return matched.strip()

    cleaned = _clean(hint_name)
    if cleaned and allow_gl_fallback:
        return cleaned

    raise EntityNameError(kind, [_describe(hint_name, identifier)])


def _describe(name, identifier) -> str:
    name = _clean(name)
    identifier = _clean(identifier)
    if identifier and name:
        return f"{name} (id {identifier})"
    if identifier:
        return f"id {identifier}"
    if name:
        return name
    return "(blank)"
