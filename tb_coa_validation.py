"""Cross-validate a Trial Balance against the firm's Chart of Accounts.

The helper at this email's request: when both a Chart of Accounts and a
Trial Balance have been uploaded, the Trial Balance ("starting balances")
step should only proceed once every TB account has a resolved COA / QBO
mapping AND the inferred TB behaviour does not conflict with the COA /
QBO account type.

This module is pure: no I/O, no Flask, no QBO HTTP calls. Callers pass:

  * ``tb_rows``         — parsed Trial Balance rows (see
                          ``report_types.parse_trial_balance``).
  * ``coa_rows``        — parsed Chart of Accounts rows for the firm
                          (latest COA job's ``parsed_coa``). May be
                          empty when the firm has not uploaded a COA
                          yet — that case is reported as a top-level
                          ``no_coa`` blocker, not silently ignored.
  * ``qbo_accounts``    — raw response from ``QBOClient.get_accounts``
                          (the same shape used elsewhere). Optional —
                          when the firm has not connected QBO yet the
                          validator still reports COA-only issues.
  * ``account_mappings``— firm-saved PCLaw→QBO account mapping rows.

The validator returns a JSON-friendly dict so it can be rendered
straight into the opening-balance / TB-readiness templates and copied
into the audit log without round-tripping through another schema.

Status vocabulary, lifted from the helper's email and used uniformly in
the templates so customers / operators see the same wording everywhere:

  * ``ready``               — TB account resolves to a real COA row and,
                              when present, the QBO account type does
                              not conflict.
  * ``missing_from_coa``    — TB row has no matching COA row by number
                              or name. The COA needs to be finalized
                              first.
  * ``needs_account_type``  — COA row exists but its account_type is
                              blank / un-recognised. Operator must
                              manually correct on the COA step.
  * ``needs_qbo_match``     — COA finalized but the account has not yet
                              been created in / mapped to QBO.
  * ``type_mismatch``       — the COA / QBO account type disagrees with
                              the TB row's apparent posting direction
                              (e.g. an Accounts Receivable account with
                              a credit-only balance, or a Liability
                              mapped where the TB clearly expects AP).
  * ``created_in_qbo``      — TB row resolves to an account that the
                              app's COA-create history shows we just
                              created in QBO during this migration.

The top-level result also exposes ``ready`` (bool): True only when
every TB row is ``ready`` or ``created_in_qbo`` AND there is at least
one COA row to validate against. The opening-balance route uses this
to refuse posting until COA is finalized.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Optional


STATUS_READY = "ready"
STATUS_MISSING_FROM_COA = "missing_from_coa"
STATUS_NEEDS_ACCOUNT_TYPE = "needs_account_type"
STATUS_NEEDS_QBO_MATCH = "needs_qbo_match"
STATUS_TYPE_MISMATCH = "type_mismatch"
STATUS_CREATED_IN_QBO = "created_in_qbo"
# AR/AP/Retained Earnings/Net Income → will post via -PCLaw accounts.
# Treated as ready from the COA validation gate perspective.
STATUS_PCLAW_RESERVED = "pclaw_reserved"


STATUS_LABELS = {
    STATUS_READY: "Ready",
    STATUS_MISSING_FROM_COA: "Missing from COA",
    STATUS_NEEDS_ACCOUNT_TYPE: "Needs account type",
    STATUS_NEEDS_QBO_MATCH: "Needs QBO match",
    STATUS_TYPE_MISMATCH: "Type mismatch",
    STATUS_CREATED_IN_QBO: "Created in QBO",
    STATUS_PCLAW_RESERVED: "Will use -PCLaw account",
}

# Statuses that allow the Trial Balance step to proceed. Anything else
# blocks posting; the operator must resolve on the COA step.
READY_STATUSES = frozenset({
    STATUS_READY,
    STATUS_CREATED_IN_QBO,
    STATUS_PCLAW_RESERVED,
})


# Mirrors opening_balance.py reserved constants (kept local — no circular import).
_TB_RESERVED_QBO_TYPES: frozenset[str] = frozenset({
    "Accounts Receivable",
    "Accounts Payable",
})
_TB_RESERVED_NAME_FRAGMENTS: tuple[str, ...] = (
    "retained earnings",
    "net income",
)


def _tb_is_reserved(name: str, qbo_type: Optional[str]) -> bool:
    """True for any account that gets -PCLaw routing in the opening balance plan."""
    if qbo_type and qbo_type in _TB_RESERVED_QBO_TYPES:
        return True
    name_l = (name or "").lower()
    return any(f in name_l for f in _TB_RESERVED_NAME_FRAGMENTS)


# Special accounts that the email called out as the highest-risk
# slip-throughs: AR, AP, Trust Bank, Client Trust Liability, Bank,
# Equity, Income, Expense. The validator checks these against TB
# row direction so a Liability mapped to an Accounts Payable doesn't
# pass silently when the names disagree.
_SPECIAL_KEYWORDS = {
    "accountsreceivable": "Accounts Receivable",
    "receivable": "Accounts Receivable",
    "accountspayable": "Accounts Payable",
    "payable": "Accounts Payable",
    "trustbank": "Bank",
    "trustaccount": "Bank",
    "trustliability": "Other Current Liability",
    "clienttrustliability": "Other Current Liability",
    "clienttrust": "Other Current Liability",
    "operatingbank": "Bank",
    "retainedearnings": "Equity",
}


def _norm(text: Optional[str]) -> str:
    if not text:
        return ""
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def _money(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    s = str(value).replace(",", "").replace("$", "").strip()
    if not s:
        return Decimal("0.00")
    try:
        return Decimal(s)
    except Exception:  # noqa: BLE001
        return Decimal("0.00")


@dataclass
class TBRowValidation:
    account_number: str
    account_name: str
    status: str
    status_label: str
    debit: str
    credit: str
    # Resolved sources
    coa_account_type: Optional[str] = None
    coa_detail_type: Optional[str] = None
    qbo_account_id: Optional[str] = None
    qbo_account_type: Optional[str] = None
    qbo_account_name: Optional[str] = None
    # Operator-facing reason for non-ready rows.
    reason: Optional[str] = None
    # Used by templates to badge "Created in QBO" rows.
    created_in_qbo: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TBCOAValidation:
    rows: list[TBRowValidation]
    has_coa: bool
    has_qbo: bool
    counts: dict[str, int]
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        if not self.has_coa:
            return False
        if not self.rows:
            return False
        return all(r.status in READY_STATUSES for r in self.rows)

    def to_dict(self) -> dict:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "has_coa": self.has_coa,
            "has_qbo": self.has_qbo,
            "counts": dict(self.counts),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "ready": self.ready,
            "status_labels": dict(STATUS_LABELS),
        }


def _expected_type_for(name: str, account_type: str) -> Optional[str]:
    """Return the expected QBO AccountType when the row's name strongly
    implies a special account (AR/AP/Trust/Bank/Equity/etc).

    Returns ``None`` when the name is generic — the validator then falls
    back to direction-vs-type heuristics instead of name-based ones.
    """
    norm_name = _norm(name)
    norm_type = _norm(account_type)
    for keyword, expected in _SPECIAL_KEYWORDS.items():
        if keyword in norm_name or keyword in norm_type:
            return expected
    return None


def _direction_conflict(account_type: Optional[str], debit: Decimal,
                        credit: Decimal) -> Optional[str]:
    """Heuristic check on the TB direction vs. the resolved QBO account type.

    Conservative: flags only the worst-fit pairs so we don't warn on
    normal credit-balance liabilities or debit-balance assets. The
    template prints this as a warning, not a blocker, because a real
    contra-account can legitimately invert the usual sign.
    """
    if not account_type:
        return None
    t = account_type.strip()
    has_debit = debit > 0
    has_credit = credit > 0
    if t in ("Bank", "Accounts Receivable", "Other Current Asset",
             "Fixed Asset", "Expense", "Other Asset",
             "Cost of Goods Sold") and has_credit and not has_debit:
        return (
            f"{t} accounts normally carry a debit balance, but this row "
            "has only a credit balance. Confirm this is intentional "
            "(contra-account) or fix the account type on the COA step."
        )
    if t in ("Accounts Payable", "Other Current Liability",
             "Long Term Liability", "Equity", "Income",
             "Other Income") and has_debit and not has_credit:
        return (
            f"{t} accounts normally carry a credit balance, but this row "
            "has only a debit balance. Confirm this is intentional or "
            "fix the account type on the COA step."
        )
    return None


def _index_coa(coa_rows: list[dict]) -> tuple[dict, dict]:
    by_num: dict[str, dict] = {}
    by_name_lower: dict[str, dict] = {}
    for r in coa_rows or []:
        num = (r.get("account_number") or "").strip()
        name = (r.get("account_name") or "").strip()
        if num:
            by_num[num] = r
        if name:
            by_name_lower[name.lower()] = r
    return by_num, by_name_lower


def _index_qbo(qbo_accounts_response: Optional[dict]) -> tuple[dict, dict, dict]:
    accounts = (qbo_accounts_response or {}).get(
        "QueryResponse", {}).get("Account", []) or []
    by_num: dict[str, dict] = {}
    by_name_lower: dict[str, dict] = {}
    by_id: dict[str, dict] = {}
    for a in accounts:
        num = (a.get("AcctNum") or "").strip()
        name = (a.get("Name") or "").strip()
        aid = str(a.get("Id") or "").strip()
        if num:
            by_num[num] = a
        if name:
            by_name_lower[name.lower()] = a
        if aid:
            by_id[aid] = a
    return by_num, by_name_lower, by_id


def _resolved_via_mapping(
    num: str, name: str,
    account_mappings: list[dict],
    qbo_by_id: dict,
) -> Optional[dict]:
    if not account_mappings:
        return None
    nl = name.lower()
    for m in account_mappings:
        pn = (m.get("pclaw_account_number") or "").strip()
        pname = (m.get("pclaw_account_name") or "").strip().lower()
        qid = str(m.get("qbo_account_id") or "").strip()
        if not qid or qid not in qbo_by_id:
            continue
        if (pn and pn == num) or (pname and pname == nl):
            return qbo_by_id[qid]
    return None


def _qbo_id_in_create_history(
    qbo_id: str,
    coa_create_history: Optional[list[dict]],
) -> bool:
    """True if this QBO account was created by us during the COA step.

    The COA-create history records each ``created`` entry with
    ``qbo_account_id``. Surfacing this on a TB row is what gives the
    customer the "Created in QBO" badge the email asked for, so they
    can see that the account is finalized rather than just matched.
    """
    if not qbo_id or not coa_create_history:
        return False
    for run in coa_create_history:
        for entry in run.get("created") or []:
            if str(entry.get("qbo_account_id") or "") == str(qbo_id):
                return True
    return False


def validate_tb_against_coa(
    tb_rows: list[dict],
    coa_rows: Optional[list[dict]],
    qbo_accounts_response: Optional[dict] = None,
    *,
    account_mappings: Optional[list[dict]] = None,
    coa_create_history: Optional[list[dict]] = None,
    coa_type_overrides: Optional[dict[str, dict]] = None,
) -> TBCOAValidation:
    """Cross-validate every TB row against the firm's COA.

    Returns a ``TBCOAValidation`` whose ``ready`` is True only when the
    Trial Balance step can safely proceed: every row resolved, every
    type recognised, every special account (AR/AP/Trust/etc.) consistent
    with the COA / QBO type.
    """
    tb_rows = tb_rows or []
    coa_rows = coa_rows or []
    account_mappings = account_mappings or []
    coa_create_history = coa_create_history or []
    coa_type_overrides = coa_type_overrides or {}

    has_coa = bool(coa_rows) or bool(account_mappings)
    has_qbo = bool(
        (qbo_accounts_response or {}).get("QueryResponse", {}).get("Account")
    )

    by_num, by_name_lower = _index_coa(coa_rows)
    qbo_by_num, qbo_by_name_lower, qbo_by_id = _index_qbo(qbo_accounts_response)

    rows: list[TBRowValidation] = []
    counts: dict[str, int] = {s: 0 for s in STATUS_LABELS}
    blockers: list[str] = []
    warnings: list[str] = []

    if not has_coa:
        # Top-level blocker: caller should refuse to post until COA is up.
        blockers.append(
            "No Chart of Accounts uploaded for this firm. Upload and "
            "finalize the Chart of Accounts before posting the opening "
            "trial balance — the COA defines the account types the TB "
            "will post into."
        )

    for r in tb_rows:
        num = (r.get("account_number") or "").strip()
        name = (r.get("account_name") or "").strip()
        debit = _money(r.get("debit_balance"))
        credit = _money(r.get("credit_balance"))

        # 1. Resolve to a COA row (by number first, then by name).
        coa_row = None
        if num and num in by_num:
            coa_row = by_num[num]
        elif name and name.lower() in by_name_lower:
            coa_row = by_name_lower[name.lower()]

        # Apply manual operator override on top of the parsed COA row.
        override = coa_type_overrides.get(num) if num else None
        if not override and name:
            # Fallback for COAs without account numbers — key on name.
            override = coa_type_overrides.get(name.lower())
        if coa_row and override:
            coa_row = {
                **coa_row,
                "account_type": override.get("account_type") or coa_row.get("account_type"),
                "detail_type": override.get("detail_type") or coa_row.get("detail_type"),
            }

        # 2. Resolve to a QBO account: mapping, then number, then name.
        qbo_account = _resolved_via_mapping(
            num, name, account_mappings, qbo_by_id
        )
        if not qbo_account and num and num in qbo_by_num:
            qbo_account = qbo_by_num[num]
        if not qbo_account and name and name.lower() in qbo_by_name_lower:
            qbo_account = qbo_by_name_lower[name.lower()]

        # 3. Decide status. Ordering matters — missing-from-COA wins
        # over needs-account-type so the operator sees the right next
        # action.
        status = STATUS_READY
        reason: Optional[str] = None

        if coa_row is None:
            status = STATUS_MISSING_FROM_COA
            reason = (
                f"Trial Balance account {num or '(no number)'} "
                f"'{name or '(no name)'}' is not in the Chart of Accounts. "
                "Add it on the COA step (or correct the number/name) so "
                "the opening balance can post to a known account."
            )
        elif not (coa_row.get("account_type") or "").strip():
            status = STATUS_NEEDS_ACCOUNT_TYPE
            reason = (
                "Chart of Accounts row exists but its PCLaw / QuickBooks "
                "account type is blank. Manually set the account type on "
                "the COA review step before posting the trial balance."
            )
        elif qbo_account is None and has_qbo:
            status = STATUS_NEEDS_QBO_MATCH
            reason = (
                "Chart of Accounts row exists but the account has not been "
                "created in or mapped to QuickBooks yet. Finalize the "
                "Chart of Accounts (or add an Account Mapping) before "
                "posting the opening balance."
            )

        # Reserved-account check (AR/AP/Retained Earnings/Net Income).
        # These get -PCLaw routing in the opening balance, so they're considered
        # "ready" even without a direct QBO mapping.
        qbo_type_resolved = (
            (qbo_account.get("AccountType") or "").strip() if qbo_account else ""
        )
        if status not in (STATUS_MISSING_FROM_COA, STATUS_NEEDS_ACCOUNT_TYPE) and \
                _tb_is_reserved(name, qbo_type_resolved):
            status = STATUS_PCLAW_RESERVED
            pclaw_target = f"{(name or '').strip()}-PCLaw"
            if "net income" in (name or "").lower():
                reason = (
                    f"Net Income will be posted to '{pclaw_target}' and "
                    "immediately closed into 'Retained Earnings-PCLaw' via "
                    "the opening balance journal entry (not directly to "
                    "QuickBooks' auto-calculated Net Income account)."
                )
            else:
                reason = (
                    f"This account will be posted to '{pclaw_target}' in the "
                    "opening balance journal entry (not directly to the "
                    "QuickBooks account with this name, since QuickBooks "
                    "manages that account automatically)."
                )

        # 4. AR/AP/Trust/Bank/Equity/etc. special-account guard.
        # If the *name* says "Accounts Payable" but the resolved QBO type
        # is a generic Liability, that is the exact mismatch the email
        # called out — flag it loudly instead of letting it through.
        if status in READY_STATUSES:
            expected_type = _expected_type_for(
                name, coa_row.get("account_type") if coa_row else ""
            )
            if expected_type and qbo_account:
                qbo_type = (qbo_account.get("AccountType") or "").strip()
                if qbo_type and qbo_type != expected_type:
                    status = STATUS_TYPE_MISMATCH
                    reason = (
                        f"This row looks like a {expected_type} (based on "
                        f"its name), but the matched QuickBooks account "
                        f"'{qbo_account.get('Name')}' has type "
                        f"'{qbo_type}'. QuickBooks Accounts Receivable / "
                        "Accounts Payable cannot be mapped to a generic "
                        "Liability or Asset account — confirm or correct "
                        "on the COA step before posting."
                    )
            if status in READY_STATUSES and expected_type and coa_row:
                coa_type = (coa_row.get("account_type") or "").strip()
                # Token-level match so "Accounts Payable" vs
                # "AccountsPayable" still counts as equivalent.
                if coa_type and _norm(coa_type) and _norm(expected_type) \
                        and _norm(coa_type) != _norm(expected_type):
                    status = STATUS_TYPE_MISMATCH
                    reason = (
                        f"COA row type '{coa_type}' disagrees with the "
                        f"account name (looks like a {expected_type}). "
                        "Fix the account type on the COA step or rename "
                        "the account so they agree."
                    )

        # 5. Direction-vs-type warning (non-blocking; surfaced under
        # "warnings" not "blockers").
        if status in READY_STATUSES:
            qbo_type = (qbo_account or {}).get("AccountType") if qbo_account else None
            coa_type = (coa_row or {}).get("account_type") if coa_row else None
            warn = _direction_conflict(qbo_type or coa_type, debit, credit)
            if warn:
                warnings.append(
                    f"{num or '(no number)'} {name}: {warn}"
                )

        # 6. Created-in-QBO promotion (informational badge).
        created_in_qbo = False
        if qbo_account and _qbo_id_in_create_history(
            str(qbo_account.get("Id") or ""), coa_create_history,
        ):
            created_in_qbo = True
            if status == STATUS_READY:
                status = STATUS_CREATED_IN_QBO

        counts[status] = counts.get(status, 0) + 1
        rows.append(TBRowValidation(
            account_number=num,
            account_name=name,
            status=status,
            status_label=STATUS_LABELS[status],
            debit=f"{debit:.2f}",
            credit=f"{credit:.2f}",
            coa_account_type=(coa_row or {}).get("account_type"),
            coa_detail_type=(coa_row or {}).get("detail_type"),
            qbo_account_id=str(qbo_account["Id"]) if qbo_account else None,
            qbo_account_type=(qbo_account or {}).get("AccountType"),
            qbo_account_name=(qbo_account or {}).get("Name"),
            reason=reason,
            created_in_qbo=created_in_qbo,
        ))

    # Top-level blocker rollups so the template can flash one summary
    # line instead of N per-row errors.
    if counts.get(STATUS_MISSING_FROM_COA, 0):
        blockers.append(
            f"{counts[STATUS_MISSING_FROM_COA]} Trial Balance account(s) "
            "are not in the Chart of Accounts. Finalize the COA first."
        )
    if counts.get(STATUS_NEEDS_ACCOUNT_TYPE, 0):
        blockers.append(
            f"{counts[STATUS_NEEDS_ACCOUNT_TYPE]} Chart of Accounts row(s) "
            "still need an account type. Set the type on the COA step."
        )
    if counts.get(STATUS_TYPE_MISMATCH, 0):
        blockers.append(
            f"{counts[STATUS_TYPE_MISMATCH]} account(s) have a type "
            "mismatch between the COA / QuickBooks and the account name "
            "(likely AR/AP misclassification). Resolve on the COA step "
            "before posting."
        )
    if counts.get(STATUS_NEEDS_QBO_MATCH, 0) and has_qbo:
        blockers.append(
            f"{counts[STATUS_NEEDS_QBO_MATCH]} Chart of Accounts row(s) "
            "are not yet created in QuickBooks. Run the COA create step."
        )

    return TBCOAValidation(
        rows=rows,
        has_coa=has_coa,
        has_qbo=has_qbo,
        counts=counts,
        blockers=blockers,
        warnings=warnings,
    )
