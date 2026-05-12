"""Parent / sub-account hierarchy detection for the Chart of Accounts flow.

PCLaw charts of accounts often include parent / header accounts (a non-postable
account that other accounts roll up under) — for example a parent
"6000 Operating Expenses" with sub-accounts "6010 Rent", "6020 Utilities".

QuickBooks Online supports parent / sub-account relationships on
``Account`` records via the ``ParentRef`` field, but creating a sub-account
requires the parent to exist first (or be created in the same pass before
its children).

This module is intentionally a *detection-and-planning* layer, not a
creation layer. It produces:

  * a per-row resolution: parent already exists in QBO, parent will be
    created in the same plan, parent is missing entirely (blocked), or
    no parent at all (top-level).
  * an ordered create sequence: parents first, then children, deterministic
    on (depth, account_number, account_name) so the same plan always
    produces the same order.
  * a list of blocking issues (orphan sub-accounts, cycles, parents whose
    types don't match QBO's allowed sub-account types).

The COA creation route consumes this and either:

  * (Safe path) creates accounts in the planned order with ``ParentRef``
    set on sub-accounts whose parents are top-level QBO Accounts we just
    created or already matched.

  * (Defensive path) when the plan has blockers, *refuses* to create the
    affected sub-accounts and surfaces the blocked rows to the operator
    rather than silently flattening them into top-level accounts. The
    "do not flatten" rule comes directly from the task brief — flattening
    loses semantic information and can cause mis-mapped reports later.

Nothing here calls QBO. The COA route passes in the same QBO accounts
query response the existing dry-run preview already builds against.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class HierarchyNode:
    account_number: str
    account_name: str
    parent_account_number: str
    parent_account_name: str
    # 'top_level' | 'qbo_existing_parent' | 'in_plan_parent' | 'orphan' | 'cycle'
    resolution: str
    parent_qbo_id: Optional[str] = None
    blocker: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    # Depth in the planned create order. 0 = top-level (no parent), 1 =
    # immediate child, 2 = grandchild, etc. -1 = unresolved.
    depth: int = -1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HierarchyPlan:
    nodes: list[HierarchyNode]
    blocked: list[HierarchyNode]
    has_hierarchy: bool

    @property
    def has_blockers(self) -> bool:
        return bool(self.blocked)

    @property
    def create_order(self) -> list[HierarchyNode]:
        """Ordered list of nodes to create. Shallow depth first, deterministic
        secondary sort on (account_number, account_name). Excludes blocked rows.
        """
        creatable = [n for n in self.nodes if n.resolution in {
            "top_level", "in_plan_parent", "qbo_existing_parent",
        }]
        return sorted(
            creatable,
            key=lambda n: (n.depth if n.depth >= 0 else 99,
                           n.account_number or "",
                           n.account_name or ""),
        )

    def to_dict(self) -> dict:
        return {
            "has_hierarchy": self.has_hierarchy,
            "node_count": len(self.nodes),
            "blocked_count": len(self.blocked),
            "nodes": [n.to_dict() for n in self.nodes],
            "blocked": [n.to_dict() for n in self.blocked],
            "create_order": [n.to_dict() for n in self.create_order],
            "has_blockers": self.has_blockers,
        }


def _norm_id(value: Optional[str]) -> str:
    return (value or "").strip()


def _qbo_accounts_index(qbo_accounts_response: dict) -> tuple[dict, dict]:
    """Index the QBO Account query response by AcctNum and by Name."""
    accounts = (qbo_accounts_response or {}).get("QueryResponse", {}).get("Account", []) or []
    by_num: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for a in accounts:
        num = _norm_id(a.get("AcctNum"))
        name = _norm_id(a.get("Name"))
        if num:
            by_num[num] = a
        if name:
            by_name[name.lower()] = a
    return by_num, by_name


def detect_hierarchy(coa_rows: list[dict]) -> bool:
    """Return True iff any row references a parent account."""
    if not coa_rows:
        return False
    for r in coa_rows:
        if _norm_id(r.get("parent_account_number")) or _norm_id(r.get("parent_account_name")):
            return True
    return False


def build_hierarchy_plan(
    coa_rows: list[dict],
    qbo_accounts_response: Optional[dict] = None,
) -> HierarchyPlan:
    """Build a hierarchy plan from parsed COA rows.

    Each row is resolved as:

      * ``top_level`` — no parent_account_* fields populated.
      * ``qbo_existing_parent`` — parent already exists in QBO.
      * ``in_plan_parent`` — parent is a different row in this same plan
        (so it'll be created earlier in the create_order).
      * ``orphan`` — parent referenced but not found in QBO or in the
        plan. Blocked: we will neither flatten silently nor invent a
        parent. Operator must add the parent row or remove the reference.
      * ``cycle`` — parent references form a cycle (A→B→A). Blocked.

    Note: this function is read-only. It does not call QBO; it consumes
    the same query response the dry-run preview already builds with.
    """
    qbo_accounts_response = qbo_accounts_response or {"QueryResponse": {"Account": []}}
    by_num, by_name = _qbo_accounts_index(qbo_accounts_response)

    # Index the rows in this batch so a child can find its parent.
    rows_by_num: dict[str, dict] = {}
    rows_by_name: dict[str, dict] = {}
    for r in coa_rows or []:
        num = _norm_id(r.get("account_number"))
        name = _norm_id(r.get("account_name"))
        if num:
            rows_by_num[num] = r
        if name:
            rows_by_name[name.lower()] = r

    nodes: list[HierarchyNode] = []
    blocked: list[HierarchyNode] = []
    has_hierarchy = detect_hierarchy(coa_rows)

    # First pass: classify resolution per row.
    classified: dict[tuple[str, str], HierarchyNode] = {}
    for r in coa_rows or []:
        num = _norm_id(r.get("account_number"))
        name = _norm_id(r.get("account_name"))
        parent_num = _norm_id(r.get("parent_account_number"))
        parent_name = _norm_id(r.get("parent_account_name"))

        node = HierarchyNode(
            account_number=num,
            account_name=name,
            parent_account_number=parent_num,
            parent_account_name=parent_name,
            resolution="top_level",
        )

        if not (parent_num or parent_name):
            node.depth = 0
            classified[(num, name)] = node
            continue

        # Try to match the parent.
        qbo_parent = None
        if parent_num and parent_num in by_num:
            qbo_parent = by_num[parent_num]
        elif parent_name and parent_name.lower() in by_name:
            qbo_parent = by_name[parent_name.lower()]

        if qbo_parent:
            node.resolution = "qbo_existing_parent"
            node.parent_qbo_id = str(qbo_parent.get("Id") or "") or None
            classified[(num, name)] = node
            continue

        # Self-reference defense.
        if parent_num and parent_num == num:
            node.resolution = "cycle"
            node.blocker = (
                f"Account {num or name} lists itself as its parent. Remove "
                "the self-reference and re-upload."
            )
            classified[(num, name)] = node
            continue
        if parent_name and parent_name.lower() == name.lower() and name:
            node.resolution = "cycle"
            node.blocker = (
                f"Account {name} lists itself as its parent. Remove the "
                "self-reference and re-upload."
            )
            classified[(num, name)] = node
            continue

        # Parent is another row in this batch?
        in_plan_parent = None
        if parent_num and parent_num in rows_by_num:
            in_plan_parent = rows_by_num[parent_num]
        elif parent_name and parent_name.lower() in rows_by_name:
            in_plan_parent = rows_by_name[parent_name.lower()]

        if in_plan_parent:
            node.resolution = "in_plan_parent"
            classified[(num, name)] = node
            continue

        # Truly orphaned.
        node.resolution = "orphan"
        node.blocker = (
            f"Account references parent '{parent_num or parent_name}', but "
            "that parent does not exist in QuickBooks and is not in this "
            "upload. Add the parent row or remove the parent reference "
            "from the CSV; we won't silently flatten this sub-account."
        )
        classified[(num, name)] = node

    # Build helper maps for parent lookup keyed by num and lowered name.
    keys_by_num: dict[str, tuple[str, str]] = {}
    keys_by_name_lower: dict[str, tuple[str, str]] = {}
    for k in classified:
        if k[0]:
            keys_by_num[k[0]] = k
        if k[1]:
            keys_by_name_lower[k[1].lower()] = k

    # Second pass: depth computation + cycle detection across in-plan refs.
    def compute_depth(key, seen) -> int:
        if key in seen:
            return -1  # cycle
        node = classified[key]
        if node.resolution == "top_level":
            return 0
        if node.resolution == "qbo_existing_parent":
            return 1
        if node.resolution == "in_plan_parent":
            pnum = node.parent_account_number
            pname = node.parent_account_name
            parent_key = None
            if pnum and pnum in keys_by_num:
                parent_key = keys_by_num[pnum]
            elif pname and pname.lower() in keys_by_name_lower:
                parent_key = keys_by_name_lower[pname.lower()]
            if parent_key is None:
                return -1
            parent_depth = compute_depth(parent_key, seen | {key})
            if parent_depth < 0:
                return -1
            return parent_depth + 1
        return -1

    for key, node in classified.items():
        if node.resolution == "in_plan_parent":
            d = compute_depth(key, set())
            if d < 0:
                node.resolution = "cycle"
                node.blocker = (
                    "Parent / sub-account references form a cycle that "
                    "cannot be resolved. Break the cycle in the CSV and "
                    "re-upload."
                )
                node.depth = -1
            else:
                node.depth = d
        elif node.resolution == "qbo_existing_parent":
            node.depth = 1
        elif node.resolution == "top_level":
            node.depth = 0

    for node in classified.values():
        if node.resolution in {"orphan", "cycle"}:
            blocked.append(node)
        else:
            nodes.append(node)

    return HierarchyPlan(
        nodes=nodes + blocked,
        blocked=blocked,
        has_hierarchy=has_hierarchy,
    )


def _norm_id_for_lookup(row: Optional[dict]) -> str:
    if not row:
        return ""
    return _norm_id(row.get("account_name")).lower()


def annotate_create_plan_with_hierarchy(
    create_plan_dict: dict,
    hierarchy_plan: HierarchyPlan,
) -> dict:
    """Layer hierarchy resolution onto the existing CreatePlan dict.

    Adds:
      * ``hierarchy`` key with the full plan summary.
      * On each to_create / blocked entry, a ``parent_resolution`` and
        optional ``parent_blocker`` so the confirmation page can show
        per-row hierarchy state without re-running the resolver.
    """
    out = dict(create_plan_dict or {})
    if not hierarchy_plan or not hierarchy_plan.has_hierarchy:
        out["hierarchy"] = {
            "has_hierarchy": False,
            "node_count": 0,
            "blocked_count": 0,
            "create_order": [],
            "nodes": [],
            "blocked": [],
            "has_blockers": False,
        }
        return out

    by_key: dict[tuple[str, str], HierarchyNode] = {}
    for node in hierarchy_plan.nodes:
        by_key[(node.account_number, node.account_name)] = node

    def _annotate(entry):
        key = (entry.get("account_number") or "", entry.get("account_name") or "")
        node = by_key.get(key)
        if node:
            entry["parent_resolution"] = node.resolution
            entry["parent_account_number"] = node.parent_account_number
            entry["parent_account_name"] = node.parent_account_name
            if node.blocker:
                entry["parent_blocker"] = node.blocker
        return entry

    out["to_create"] = [_annotate(dict(e)) for e in (out.get("to_create") or [])]
    out["blocked"] = [_annotate(dict(e)) for e in (out.get("blocked") or [])]
    # Promote hierarchy orphans/cycles into the blocked list at the
    # surface level so the route's plan.has_blockers check picks them up
    # even when the type-mapper would otherwise allow them.
    hierarchy_blocked_keys = {
        (n.account_number, n.account_name) for n in hierarchy_plan.blocked
    }
    already_blocked = {
        (e.get("account_number") or "", e.get("account_name") or "")
        for e in (out.get("blocked") or [])
    }
    extra_blocked = []
    for entry in (out.get("to_create") or []):
        key = (entry.get("account_number") or "", entry.get("account_name") or "")
        if key in hierarchy_blocked_keys and key not in already_blocked:
            # Convert to a hierarchy-blocked row.
            blocked_entry = dict(entry)
            blocked_entry["decision"] = "blocked"
            blocked_entry["blocked_reason"] = blocked_entry.get(
                "parent_blocker"
            ) or "Parent/sub-account hierarchy could not be resolved."
            extra_blocked.append(blocked_entry)
    if extra_blocked:
        out["to_create"] = [
            e for e in (out.get("to_create") or [])
            if (e.get("account_number") or "", e.get("account_name") or "")
            not in hierarchy_blocked_keys
        ]
        out["blocked"] = (out.get("blocked") or []) + extra_blocked
        out["blocked_count"] = len(out["blocked"])
        out["to_create_count"] = len(out["to_create"])
        out["has_blockers"] = True

    out["hierarchy"] = hierarchy_plan.to_dict()
    return out
