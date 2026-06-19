"""Smoke tests: persisted entity map + duplicate-name recovery (Roadblock 1).

QuickBooks requires a Customer (A/R) or Vendor (A/P) on each journal line,
referenced by **Id**. Re-posting the same general ledger must not re-create
or re-resolve the same names, and a QuickBooks duplicate-name rejection must
recover by reusing the existing entity rather than failing the whole post.

``app._resolve_entity_hints`` / ``_find_or_create_entity`` own that logic.
We drive them with a fake QBO client (no network) and the real app DB so
the per-firm entity map round-trips through SQLite exactly as in production.

Covered
-------
  E1  A resolved A/R line references the QuickBooks Id (EntityRef.value),
      not the raw name string, and tags it as a Customer.
  E2  Re-posting reuses the persisted Id: no second find/create call.
  E3  A QuickBooks duplicate-name error on create recovers by re-querying
      and reusing the existing entity.
  E4  Setup cards reflect real state (COA/Vendors&Clients/Opening Balances).

Run from project root::

    python3 tests/smoke_entity_map_posting.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-entity-map")

import app as appmod  # noqa: E402
import migration_hub as mh  # noqa: E402
from qbo_client import QBOError  # noqa: E402

db = appmod.db


def _new_firm(email):
    firm_id, _user_id = db.create_firm_and_admin("Entity Map Firm", email, "passw0rd!1234")
    return firm_id


class FakeQBO:
    """Records calls so the tests can assert find/create were skipped."""

    def __init__(self, *, existing=None, duplicate_on_create=False):
        # existing: {("Customer"|"Vendor", display_name): id}
        self.existing = dict(existing or {})
        self.duplicate_on_create = duplicate_on_create
        self.find_calls = 0
        self.create_calls = 0
        self._next_id = 1000

    def _find(self, kind, name):
        self.find_calls += 1
        eid = self.existing.get((kind, name))
        return {"Id": eid, "DisplayName": name} if eid else None

    def find_customer_by_name(self, name):
        return self._find("Customer", name)

    def find_vendor_by_name(self, name):
        return self._find("Vendor", name)

    def _create(self, kind, name):
        self.create_calls += 1
        if self.duplicate_on_create:
            # First simulate that the entity actually exists under this name
            # so the recovery re-query finds it, then raise the QBO error.
            self.existing[(kind, name)] = self.existing.get((kind, name)) or str(self._next_id)
            self._next_id += 1
            raise QBOError("6240: Duplicate Name Exists Error", status_code=400)
        self._next_id += 1
        eid = str(self._next_id)
        self.existing[(kind, name)] = eid
        return {"Id": eid, "DisplayName": name}

    def create_customer(self, name):
        return self._create("Customer", name)

    def create_vendor(self, name):
        return self._create("Vendor", name)


def _ar_payload(name="Acme Corp"):
    """One balanced JE with an A/R line tagged with a customer hint."""
    return [{
        "TxnDate": "2026-01-01",
        "Line": [
            {
                "Amount": 100.0,
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": {"PostingType": "Debit",
                                           "AccountRef": {"value": "1"}},
                "_pclaw_entity_hint": {"type": "Customer", "name": name,
                                       "identifier": None},
            },
            {
                "Amount": 100.0,
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": {"PostingType": "Credit",
                                           "AccountRef": {"value": "2"}},
            },
        ],
    }]


def e1_entity_ref_is_id():
    firm_id = _new_firm("e1@example.test")
    qbo = FakeQBO()
    created = appmod._resolve_entity_hints(
        qbo, _ar_payload("Acme Corp"), firm_id=firm_id, realm_id="realm-e1")
    # Re-run on a fresh payload to inspect the rewritten line.
    p = _ar_payload("Acme Corp")
    appmod._resolve_entity_hints(qbo, p, firm_id=firm_id, realm_id="realm-e1")
    entity = p[0]["Line"][0]["JournalEntryLineDetail"]["Entity"]
    assert entity["Type"] == "Customer", entity
    assert entity["EntityRef"]["value"], "EntityRef must carry the QBO Id"
    assert entity["EntityRef"]["value"].isdigit(), entity["EntityRef"]["value"]
    assert "_pclaw_entity_hint" not in p[0]["Line"][0], "hint must be popped"
    assert created and created[0][0] == "Customer"
    print("E1 OK: A/R line references the QuickBooks Id, tagged as Customer")


def e2_rerun_reuses_persisted_id():
    firm_id = _new_firm("e2@example.test")
    qbo = FakeQBO()
    appmod._resolve_entity_hints(
        qbo, _ar_payload("Beta LLP"), firm_id=firm_id, realm_id="realm-e2")
    assert qbo.create_calls == 1, qbo.create_calls
    # A brand-new client object (cold caches) must hit the persisted map,
    # not QBO, on the second post of the same name.
    qbo2 = FakeQBO()
    appmod._resolve_entity_hints(
        qbo2, _ar_payload("Beta LLP"), firm_id=firm_id, realm_id="realm-e2")
    assert qbo2.find_calls == 0, qbo2.find_calls
    assert qbo2.create_calls == 0, qbo2.create_calls
    print("E2 OK: re-posting reuses the persisted entity Id (no QBO call)")


def e3_duplicate_name_recovers():
    firm_id = _new_firm("e3@example.test")
    qbo = FakeQBO(duplicate_on_create=True)
    p = _ar_payload("Gamma & Sons")
    appmod._resolve_entity_hints(qbo, p, firm_id=firm_id, realm_id="realm-e3")
    entity = p[0]["Line"][0]["JournalEntryLineDetail"]["Entity"]
    assert entity["EntityRef"]["value"], "should recover an Id after duplicate error"
    # The recovered Id is persisted, so a rerun makes no QBO calls.
    qbo2 = FakeQBO()
    appmod._resolve_entity_hints(
        qbo2, _ar_payload("Gamma & Sons"), firm_id=firm_id, realm_id="realm-e3")
    assert qbo2.create_calls == 0 and qbo2.find_calls == 0
    print("E3 OK: a QuickBooks duplicate-name error recovers by reusing the entity")


def e4_setup_cards_reflect_state():
    # Nothing done yet.
    cards = mh.build_setup_cards(
        has_qbo_connection=False, account_mapping_count=0,
        coa_created_count=0, customer_list_count=0, vendor_list_count=0,
        opening_balance_state="none")
    by_key = {c.key: c for c in cards}
    assert not by_key["chart_of_accounts"].done
    assert by_key["chart_of_accounts"].needs_attention
    assert not by_key["vendors_clients"].done
    assert by_key["opening_balances"].status_label == "Not started"

    # Everything in place; opening balances posted.
    cards = mh.build_setup_cards(
        has_qbo_connection=True, account_mapping_count=12,
        coa_created_count=0, customer_list_count=4, vendor_list_count=3,
        opening_balance_state="posted")
    by_key = {c.key: c for c in cards}
    assert by_key["chart_of_accounts"].done
    assert by_key["vendors_clients"].done
    assert by_key["opening_balances"].done

    # A failed opening-balance post surfaces as needs-attention.
    cards = mh.build_setup_cards(
        has_qbo_connection=True, account_mapping_count=1,
        coa_created_count=5, customer_list_count=0, vendor_list_count=0,
        opening_balance_state="failed")
    by_key = {c.key: c for c in cards}
    assert by_key["opening_balances"].needs_attention
    assert by_key["opening_balances"].status_label == "Needs attention"
    print("E4 OK: setup cards reflect COA / Vendors&Clients / Opening Balances state")


def main():
    e1_entity_ref_is_id()
    e2_rerun_reuses_persisted_id()
    e3_duplicate_name_recovers()
    e4_setup_cards_reflect_state()
    print("\nAll entity-map posting smoke tests passed.")


if __name__ == "__main__":
    main()
