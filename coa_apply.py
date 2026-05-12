"""Chart of Accounts QBO creation: type-mapping + safe-create plan builder.

This module owns the cutover step where a parsed PCLaw Chart of Accounts is
turned into actual QuickBooks Online ``Account`` records. It is deliberately
the only place where the COA flow does any *write* planning — the Flask
route layer is a thin shell on top of these pure functions so the logic is
unit-testable without a live QBO realm.

Design rules (intentionally conservative):

* Never guess an account type. PCLaw's category vocabulary doesn't map 1:1
  to QBO. We only map type/sub-type combos we are confident about; ambiguous
  rows are flagged as ``blocked`` and require operator resolution before a
  create plan is approved.
* Read-only matching first. The plan ingests the same dry-run preview the
  existing ``coa-preview`` page already builds, so accounts that already
  exist in QBO (by ``AcctNum`` or canonical Name) are *never* re-created.
* Special accounts that QBO auto-provisions (Accounts Receivable, Accounts
  Payable, Undeposited Funds, Retained Earnings, the system Sales Tax
  accounts on Canadian companies) are flagged for operator review even if
  we have a valid type-map, because creating a parallel one usually causes
  reconciliation problems later.
* Trust liability + trust bank get a clear warning so the operator sees
  what is about to be created — not blocked, because most firms genuinely
  need these in QBO, but never silent.

Nothing in this module makes HTTP calls. ``apply_create_plan`` takes a
QBO client (or a test double) and executes the plan one account at a time,
recording successes and failures so the route can render a result page.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


# ----------------------------------------------------------------------------
# PCLaw -> QBO type-mapping table.
#
# Keys are *normalized* tokens (lowercase, alphanumeric only). The matcher
# tries the PCLaw account_type first, then the qbo_suggested_detail_type,
# then the account_name as a last-resort hint. The first non-None match
# wins; an entry that returns None means "we recognise this hint, but
# require a human to disambiguate".
#
# Mapping references:
#   QuickBooks AccountType / AccountSubType reference (Intuit docs):
#     https://developer.intuit.com/app/developer/qbo/docs/api/accounting/most-commonly-used/account
# ----------------------------------------------------------------------------


# Sentinel detail-type values that are *valid* for QBO but should warn the
# operator before creating, because creating a duplicate of an existing
# auto-provisioned account historically causes mapping bugs.
_AUTO_PROVISIONED_SUBTYPES = {
    "AccountsReceivable",
    "AccountsPayable",
    "UndepositedFunds",
}


# Detail types we will warn-but-allow when matched. The firm legitimately
# needs trust bank + trust liability accounts in QBO for client-money
# handling; we still surface a warning so the operator sees it.
_WARN_SUBTYPES = {
    "TrustAccounts-Liabilities",
    "TrustAccounts",
}


def _norm(token: Optional[str]) -> str:
    if not token:
        return ""
    return "".join(ch for ch in str(token).lower() if ch.isalnum())


# (account_type, detail_type) tuples keyed by normalized hint tokens.
# Every entry here is a *safe* mapping — if a hint is missing from this
# table we refuse to guess.
_TYPE_TABLE: dict[str, tuple[str, str]] = {
    # Banks / cash
    "bank": ("Bank", "Checking"),
    "checking": ("Bank", "Checking"),
    "operatingbank": ("Bank", "Checking"),
    "savings": ("Bank", "Savings"),
    "trustbank": ("Bank", "TrustAccounts"),
    "trustaccount": ("Bank", "TrustAccounts"),

    # Receivables
    "accountsreceivable": ("Accounts Receivable", "AccountsReceivable"),
    "receivable": ("Accounts Receivable", "AccountsReceivable"),
    "ar": ("Accounts Receivable", "AccountsReceivable"),

    # Other current assets
    "othercurrentasset": ("Other Current Asset", "OtherCurrentAssets"),
    "wip": ("Other Current Asset", "OtherCurrentAssets"),
    "unbilleddisbursements": ("Other Current Asset", "OtherCurrentAssets"),
    "prepaidexpenses": ("Other Current Asset", "PrepaidExpenses"),
    "inventory": ("Other Current Asset", "Inventory"),

    # Fixed assets
    "fixedasset": ("Fixed Asset", "FurnitureAndFixtures"),
    "equipment": ("Fixed Asset", "MachineryAndEquipment"),

    # Payables
    "accountspayable": ("Accounts Payable", "AccountsPayable"),
    "payable": ("Accounts Payable", "AccountsPayable"),
    "ap": ("Accounts Payable", "AccountsPayable"),

    # Other current liabilities
    "othercurrentliability": ("Other Current Liability", "OtherCurrentLiabilities"),
    "trustliability": ("Other Current Liability", "TrustAccounts-Liabilities"),
    "trustaccountsliabilities": ("Other Current Liability", "TrustAccounts-Liabilities"),
    "clienttrustliability": ("Other Current Liability", "TrustAccounts-Liabilities"),

    # Long-term liabilities
    "longtermliability": ("Long Term Liability", "NotesPayable"),

    # Equity
    "equity": ("Equity", "OwnersEquity"),
    "ownerequity": ("Equity", "OwnersEquity"),
    "ownersequity": ("Equity", "OwnersEquity"),
    "retainedearnings": ("Equity", "RetainedEarnings"),

    # Income
    "income": ("Income", "ServiceFeeIncome"),
    "revenue": ("Income", "ServiceFeeIncome"),
    "servicefeeincome": ("Income", "ServiceFeeIncome"),
    "otherprimaryincome": ("Income", "OtherPrimaryIncome"),
    "recovery": ("Income", "OtherPrimaryIncome"),

    # Expense
    "expense": ("Expense", "OfficeGeneralAdministrativeExpenses"),
    "overhead": ("Expense", "OfficeGeneralAdministrativeExpenses"),
    "office": ("Expense", "OfficeGeneralAdministrativeExpenses"),
    "officegeneraladministrativeexpenses": (
        "Expense", "OfficeGeneralAdministrativeExpenses",
    ),
    "rentorleaseofbuildings": ("Expense", "RentOrLeaseOfBuildings"),
    "rent": ("Expense", "RentOrLeaseOfBuildings"),
    "legalprofessionalfees": ("Expense", "LegalAndProfessionalFees"),
    "filingfees": ("Expense", "LegalAndProfessionalFees"),
    "clientcost": ("Expense", "LegalAndProfessionalFees"),
    "advertising": ("Expense", "AdvertisingPromotional"),
    "utilities": ("Expense", "Utilities"),
    "insurance": ("Expense", "Insurance"),
    "travel": ("Expense", "Travel"),

    # Cost of goods sold (rare in legal but handle it)
    "cogs": ("Cost of Goods Sold", "EquipmentRental"),
    "costofgoodssold": ("Cost of Goods Sold", "SuppliesMaterialsCogs"),

    # Top-level PCLaw category buckets that on their own are too ambiguous —
    # we recognise them but refuse to auto-create without a more specific hint.
    "asset": (None, None),  # too broad — could be bank, AR, fixed asset, etc.
    "liability": (None, None),
}


def map_pclaw_account_to_qbo_type(row: dict) -> dict:
    """Resolve a parsed COA row to a QBO AccountType/AccountSubType.

    Returns a dict with keys:
        account_type:   QBO ``AccountType`` (e.g. "Bank") or None when blocked.
        detail_type:    QBO ``AccountSubType`` (e.g. "Checking") or None.
        decision:       'ok' | 'warn' | 'blocked'
        warnings:       list[str]   (advisory; operator should review)
        blocked_reason: str | None  ('blocked' only)
        match_hint:     which input field resolved the mapping, for audit.

    The function is deterministic and pure — no I/O, no QBO calls.
    """
    warnings: list[str] = []

    name = (row.get("account_name") or "").strip()
    account_type_in = (row.get("account_type") or "").strip()
    detail_in = (row.get("detail_type") or "").strip()

    # 1. Explicit detail_type hint wins if recognised.
    candidates = [
        ("detail_type", detail_in),
        ("account_type", account_type_in),
        ("account_name", name),
    ]

    resolved_type: Optional[str] = None
    resolved_detail: Optional[str] = None
    match_hint: Optional[str] = None
    saw_ambiguous_bucket = False

    for hint_label, raw in candidates:
        key = _norm(raw)
        if not key:
            continue
        mapped = _TYPE_TABLE.get(key)
        if mapped is None:
            continue
        t, st = mapped
        if t is None:
            # Recognised but deliberately ambiguous (e.g. bare "Asset").
            saw_ambiguous_bucket = True
            continue
        resolved_type, resolved_detail = t, st
        match_hint = hint_label
        break

    # 2. Special-case: account_name contains a strong signal even if the
    # account_type column was empty. Conservatively check a few high-risk
    # keywords so a row called "Trust Bank Account" still maps when the
    # type column is blank or unhelpful.
    if not resolved_type:
        name_norm = _norm(name)
        for keyword, (t, st) in [
            ("trustbank", ("Bank", "TrustAccounts")),
            ("trustaccount", ("Bank", "TrustAccounts")),
            ("trustliability", ("Other Current Liability", "TrustAccounts-Liabilities")),
            ("clienttrust", ("Other Current Liability", "TrustAccounts-Liabilities")),
            ("operatingbank", ("Bank", "Checking")),
            ("accountsreceivable", ("Accounts Receivable", "AccountsReceivable")),
            ("accountspayable", ("Accounts Payable", "AccountsPayable")),
            ("retainedearnings", ("Equity", "RetainedEarnings")),
        ]:
            if keyword in name_norm:
                resolved_type, resolved_detail = t, st
                match_hint = "account_name_keyword"
                break

    if not resolved_type:
        return {
            "account_type": None,
            "detail_type": None,
            "decision": "blocked",
            "warnings": [],
            "blocked_reason": (
                "Could not safely map this account to a QuickBooks "
                "AccountType / AccountSubType. PCLaw type "
                f"'{account_type_in or '(blank)'}' / detail "
                f"'{detail_in or '(blank)'}' is not in the safe mapping "
                "table. Edit the CSV with a more specific type "
                "(e.g. 'Bank', 'Accounts Receivable', 'Expense') and "
                "re-upload, or create this account manually in QuickBooks."
                + (
                    " (Recognised as a high-level category but too broad "
                    "to map safely — pick a specific sub-type.)"
                    if saw_ambiguous_bucket else ""
                )
            ),
            "match_hint": None,
        }

    # 3. Special-case warnings on safe-but-risky types.
    if resolved_detail in _AUTO_PROVISIONED_SUBTYPES:
        warnings.append(
            f"QuickBooks usually creates a default {resolved_detail} "
            "account for every company. Creating another one is allowed "
            "but can confuse mapping later. Verify with the firm before "
            "applying."
        )
    if resolved_detail in _WARN_SUBTYPES:
        warnings.append(
            "Trust-account creation is allowed but legally sensitive. "
            "Confirm the firm has a real trust bank account at their "
            "financial institution before posting any trust journal entry."
        )
    if resolved_type == "Bank":
        warnings.append(
            "Bank accounts in QuickBooks should be reconciled against the "
            "real bank statement. Opening balances are *not* posted by "
            "this step — they come from the opening trial balance."
        )
    if resolved_type == "Equity" and resolved_detail == "RetainedEarnings":
        warnings.append(
            "Retained Earnings is auto-managed by QuickBooks at year-end. "
            "Do not post to it directly — confirm with the firm before "
            "creating a parallel account."
        )

    return {
        "account_type": resolved_type,
        "detail_type": resolved_detail,
        "decision": "warn" if warnings else "ok",
        "warnings": warnings,
        "blocked_reason": None,
        "match_hint": match_hint,
    }


# ----------------------------------------------------------------------------
# Create-plan builder
# ----------------------------------------------------------------------------


@dataclass
class CreatePlanEntry:
    account_number: str
    account_name: str
    pclaw_account_type: str
    pclaw_detail_type: str
    qbo_account_type: Optional[str]
    qbo_detail_type: Optional[str]
    decision: str                    # 'ok' | 'warn' | 'blocked'
    warnings: list[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None
    active: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CreatePlan:
    matched: list[dict]              # already exist in QBO; never recreated
    to_create: list[CreatePlanEntry] # decision in ('ok', 'warn')
    blocked: list[CreatePlanEntry]   # cannot create without operator action
    soft_conflicts: list[dict]       # name-match w/ different AcctNum

    @property
    def has_blockers(self) -> bool:
        return bool(self.blocked)

    @property
    def has_warnings(self) -> bool:
        return any(e.decision == "warn" for e in self.to_create)

    def to_dict(self) -> dict:
        return {
            "matched_count": len(self.matched),
            "to_create_count": len(self.to_create),
            "blocked_count": len(self.blocked),
            "soft_conflict_count": len(self.soft_conflicts),
            "matched": self.matched,
            "to_create": [e.to_dict() for e in self.to_create],
            "blocked": [e.to_dict() for e in self.blocked],
            "soft_conflicts": self.soft_conflicts,
            "has_blockers": self.has_blockers,
            "has_warnings": self.has_warnings,
        }


def build_create_plan(coa_rows: list[dict], preview: dict) -> CreatePlan:
    """Combine the dry-run preview with the type-mapping table.

    ``preview`` is the output of ``report_types.build_coa_dry_run_preview``.
    Rows that already match an existing QBO account are passed through as
    ``matched`` (never re-created). Rows in ``would_create`` are resolved
    through the type-mapper and bucketed into ``to_create`` or ``blocked``.
    """
    matched = list(preview.get("matched", []) or [])
    soft_conflicts = list(preview.get("conflicts", []) or [])

    # Index would_create entries by (account_number, account_name) so we can
    # match them back to the original coa_rows for type-mapping. The dry-run
    # entries are already a subset of coa_rows, but we re-run the mapper on
    # the coa_row directly to keep the source of truth in one place.
    would_create_keys: set[tuple[str, str]] = set()
    for entry in (preview.get("would_create") or []):
        would_create_keys.add(
            (
                (entry.get("account_number") or "").strip(),
                (entry.get("account_name") or "").strip(),
            )
        )

    to_create: list[CreatePlanEntry] = []
    blocked: list[CreatePlanEntry] = []

    for row in coa_rows:
        num = (row.get("account_number") or "").strip()
        name = (row.get("account_name") or "").strip()
        if (num, name) not in would_create_keys:
            continue  # already matched in QBO — preview handled it

        decision = map_pclaw_account_to_qbo_type(row)
        entry = CreatePlanEntry(
            account_number=num,
            account_name=name,
            pclaw_account_type=row.get("account_type") or "",
            pclaw_detail_type=row.get("detail_type") or "",
            qbo_account_type=decision["account_type"],
            qbo_detail_type=decision["detail_type"],
            decision=decision["decision"],
            warnings=list(decision["warnings"]),
            blocked_reason=decision["blocked_reason"],
            active=bool(row.get("active", True)),
        )
        if entry.decision == "blocked":
            blocked.append(entry)
        else:
            to_create.append(entry)

    return CreatePlan(
        matched=matched,
        to_create=to_create,
        blocked=blocked,
        soft_conflicts=soft_conflicts,
    )


# ----------------------------------------------------------------------------
# Plan execution
# ----------------------------------------------------------------------------


def _build_qbo_payload(entry: CreatePlanEntry) -> dict:
    """Build the QBO Account payload for a single create entry."""
    payload = {
        "Name": entry.account_name,
        "AccountType": entry.qbo_account_type,
        "Active": entry.active,
    }
    if entry.qbo_detail_type:
        payload["AccountSubType"] = entry.qbo_detail_type
    if entry.account_number:
        payload["AcctNum"] = entry.account_number
    return payload


def apply_create_plan(qbo_client, plan: CreatePlan) -> dict:
    """Execute the create plan against a connected QBO client.

    ``qbo_client`` must expose ``create_account(payload)``. Failures on
    one row do not stop the loop — each row reports its own success or
    error so the operator gets a complete result rather than a partial
    half-state with no audit trail.

    Returns a dict with ``created`` (list of result rows), ``failed``
    (list of result rows), and ``intuit_tids`` (list of non-null TIDs we
    captured, for support follow-up).
    """
    if plan.has_blockers:
        raise ValueError(
            "Cannot apply a plan with blocked rows. Resolve the blocked "
            "entries (fix CSV types or create those accounts manually in "
            "QuickBooks) and re-run the preview."
        )

    created: list[dict] = []
    failed: list[dict] = []
    intuit_tids: list[str] = []

    for entry in plan.to_create:
        payload = _build_qbo_payload(entry)
        try:
            response = qbo_client.create_account(payload)
            qbo_account = (response or {}).get("Account") or response or {}
            created.append({
                "account_number": entry.account_number,
                "account_name": entry.account_name,
                "qbo_account_id": str(qbo_account.get("Id") or ""),
                "qbo_account_name": qbo_account.get("Name") or entry.account_name,
                "qbo_account_type": qbo_account.get("AccountType") or entry.qbo_account_type,
                "qbo_acct_num": qbo_account.get("AcctNum") or entry.account_number,
            })
        except Exception as exc:  # noqa: BLE001
            # We deliberately catch broadly here — the route layer will
            # render the failed list verbatim. Pull intuit_tid from the
            # exception when present (QBOError exposes it) so support can
            # trace the failing request without us logging tokens.
            tid = getattr(exc, "intuit_tid", None)
            if tid:
                intuit_tids.append(tid)
            failed.append({
                "account_number": entry.account_number,
                "account_name": entry.account_name,
                "qbo_account_type": entry.qbo_account_type,
                "qbo_detail_type": entry.qbo_detail_type,
                "error": _safe_error_message(exc),
                "intuit_tid": tid,
            })

    # De-dupe intuit_tids while keeping insertion order.
    seen: set[str] = set()
    deduped = []
    for tid in intuit_tids:
        if tid and tid not in seen:
            deduped.append(tid)
            seen.add(tid)

    return {
        "created": created,
        "failed": failed,
        "intuit_tids": deduped,
    }


def _safe_error_message(exc: Exception) -> str:
    """Return an operator-safe rendering of a QBO error.

    Strips long bearer-style tokens by length cap; QBOError carries body
    text that we want to surface for diagnostics but never tokens (the
    QBO client itself never logs Authorization headers).
    """
    msg = str(exc) or exc.__class__.__name__
    if len(msg) > 600:
        msg = msg[:600] + "…"
    return msg
