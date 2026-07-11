"""Smoke test for mapping_liveness. Run: python test_mapping_liveness.py"""
import sys
sys.path.insert(0, ".")
from mapping_liveness import collect_referenced_account_ids, find_dead_mappings

MAPPINGS = [
    {"qbo_account_id": "99",  "qbo_account_name": "Bank charges",
     "pclaw_account_number": "5070", "pclaw_account_name": "Bank Charges"},
    {"qbo_account_id": "105", "qbo_account_name": "Travel",
     "pclaw_account_number": "5410", "pclaw_account_name": "Travel"},
    {"qbo_account_id": "42",  "qbo_account_name": "Utilities",
     "pclaw_account_number": "5420", "pclaw_account_name": "Utilities"},
    {"qbo_account_id": "",    "qbo_account_name": "ignored"},
]
LIVE = [
    {"Id": "99", "Name": "Bank charges", "Active": True},
    {"Id": "42", "Name": "Utilities",    "Active": False},   # deactivated
    # 105 absent entirely — the sandbox-reseed / deleted case
]

def main():
    ref = collect_referenced_account_ids(MAPPINGS, extra_ids=["77", ""])
    assert set(ref) == {"99", "105", "42", "77"}, ref

    dead = find_dead_mappings(ref, LIVE)
    by_id = {d["qbo_account_id"]: d for d in dead}
    assert set(by_id) == {"105", "42", "77"}, by_id
    assert by_id["105"]["status"] == "missing", by_id["105"]
    assert by_id["105"]["pclaw_account_name"] == "Travel", by_id["105"]
    assert by_id["42"]["status"] == "inactive", by_id["42"]
    assert "Reactivate" in by_id["42"]["action"], by_id["42"]
    assert by_id["77"]["status"] == "missing", by_id["77"]
    assert "99" not in by_id  # active + present → not dead

    print(f"mapping_liveness smoke OK — {len(dead)} dead of {len(ref)} referenced")

if __name__ == "__main__":
    main()
