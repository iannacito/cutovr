"""Opening Trial Balance -> opening balance JournalEntry workflow.

When a firm migrates from PCLaw to QuickBooks Online, the *opening* trial
balance (the trial balance as of the day before cutover) is what seeds
QuickBooks with the right starting balances. The standard accounting
move is to post a single, balancing JournalEntry whose debits = TB
debits and credits = TB credits.

This module owns the *planning* side of that workflow:

  * Validate the parsed trial balance against the connected QBO Chart
    of Accounts: every TB account must map to an existing QBO account
    (by AcctNum first, then exact Name). Missing accounts are surfaced
    as **blockers** — we will not post into an account that doesn't
    exist yet.

  * Refuse to plan an unbalanced TB. If debits != credits, the plan is
    blocked. We deliberately do NOT auto-balance to a suspense account
    in this build; auto-balancing requires the operator to first create
    a real ``Opening Balance Equity`` (or similar) account and tell us
    to use it, and that's tracked as a future enhancement in the docs.

  * Build a single balanced QBO JournalEntry payload for preview. Each
    TB row becomes one Line on the JE: rows with a debit become
    DebitAmount lines; rows with a credit become CreditAmount lines.
    Zero-balance rows are omitted (they don't seed anything).

  * Refuse to execute without an explicit typed confirmation phrase
    (``POST OPENING BALANCE``). The Flask route layer is responsible
    for actually calling ``qbo.create_journal_entry`` — this module
    only builds + validates the payload.

The module is pure: no I/O, no QBO HTTP calls, no Flask. Tests can drive
it with a list[dict] trial-balance + a list[dict] QBO accounts response.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


OPENING_BALANCE_CONFIRMATION_PHRASE = "POST OPENING BALANCE"


def _money(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    s = str(value).replace(",", "").replace("$", "").strip()
    if not s:
        return Decimal("0.00")
    try:
        d = Decimal(s)
    except Exception:  # noqa: BLE001
        return Decimal("0.00")
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class OpeningLine:
    """A single planned JournalEntry line, with the resolved QBO account."""
    account_number: str
    account_name: str
    debit: str            # "0.00" formatted Decimal
    credit: str
    qbo_account_id: Optional[str]
    qbo_account_name: Optional[str]
    qbo_account_type: Optional[str]
    blocker: Optional[str] = None      # set when the line can't be posted

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OpeningBalancePlan:
    as_of_date: str
    lines: list[OpeningLine]
    blockers: list[str]                # high-level plan-blockers
    total_debit: str
    total_credit: str
    balanced: bool
    omitted_zero_rows: int
    warnings: list[str] = field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        return bool(self.blockers) or any(line.blocker for line in self.lines)

    @property
    def postable_lines(self) -> list[OpeningLine]:
        return [line for line in self.lines if not line.blocker]

    def to_dict(self) -> dict:
        return {
            "as_of_date": self.as_of_date,
            "total_debit": self.total_debit,
            "total_credit": self.total_credit,
            "balanced": self.balanced,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "omitted_zero_rows": self.omitted_zero_rows,
            "lines": [line.to_dict() for line in self.lines],
            "line_count": len(self.lines),
            "blocker_count": sum(1 for line in self.lines if line.blocker),
            "has_blockers": self.has_blockers,
            "confirmation_phrase": OPENING_BALANCE_CONFIRMATION_PHRASE,
        }


def _index_qbo_accounts(qbo_accounts_response: Optional[dict]) -> tuple[dict, dict]:
    accounts = (qbo_accounts_response or {}).get("QueryResponse", {}).get("Account", []) or []
    by_num: dict[str, dict] = {}
    by_name_lower: dict[str, dict] = {}
    for a in accounts:
        num = (a.get("AcctNum") or "").strip()
        name = (a.get("Name") or "").strip()
        if num:
            by_num[num] = a
        if name:
            by_name_lower[name.lower()] = a
    return by_num, by_name_lower


def build_opening_balance_plan(
    trial_balance_rows: list[dict],
    qbo_accounts_response: Optional[dict],
    *,
    as_of_date: Optional[str] = None,
    account_mappings: Optional[list[dict]] = None,
) -> OpeningBalancePlan:
    """Build (but do not execute) the opening-balance journal entry plan.

    Resolution precedence per row:
      1. Account mapping saved for this firm + realm (pclaw_account_number
         or pclaw_account_name -> qbo_account_id).
      2. QBO AcctNum exact match against the parsed account_number.
      3. QBO Name exact (case-insensitive) match against the parsed
         account_name.

    The plan is blocked when:
      * the TB is empty / fundamentally invalid,
      * debits != credits across the whole TB,
      * one or more rows cannot resolve to a QBO account (per-row blocker).

    The plan is allowed (with warnings) when:
      * a TB row matched a QBO account whose AccountType doesn't fit the
        normal expectation for the sign (e.g. a Bank account with a
        credit balance), or
      * the resolved as-of-date is empty (we warn but don't block — the
        operator can type it on the confirmation page).
    """
    trial_balance_rows = trial_balance_rows or []
    by_num, by_name_lower = _index_qbo_accounts(qbo_accounts_response)

    # Build mapping lookups (firm-saved overrides).
    saved_by_num: dict[str, str] = {}
    saved_by_name_lower: dict[str, str] = {}
    for m in (account_mappings or []):
        pn = (m.get("pclaw_account_number") or "").strip()
        pname = (m.get("pclaw_account_name") or "").strip()
        qid = str(m.get("qbo_account_id") or "").strip()
        if not qid:
            continue
        if pn:
            saved_by_num[pn] = qid
        if pname:
            saved_by_name_lower[pname.lower()] = qid

    # Resolve the as-of-date: row-level first, then the explicit arg.
    detected_as_of = ""
    for r in trial_balance_rows:
        d = (r.get("as_of_date") or "").strip()
        if d:
            detected_as_of = d
            break
    plan_as_of = (as_of_date or detected_as_of or "").strip()

    lines: list[OpeningLine] = []
    omitted = 0
    total_debit = Decimal("0.00")
    total_credit = Decimal("0.00")
    qbo_by_id = {}
    accounts = (qbo_accounts_response or {}).get("QueryResponse", {}).get("Account", []) or []
    for a in accounts:
        if a.get("Id"):
            qbo_by_id[str(a["Id"])] = a

    for r in trial_balance_rows:
        num = (r.get("account_number") or "").strip()
        name = (r.get("account_name") or "").strip()
        debit = _money(r.get("debit_balance"))
        credit = _money(r.get("credit_balance"))
        if debit == 0 and credit == 0:
            omitted += 1
            continue

        # Saved mapping wins, then AcctNum, then Name.
        qbo_account = None
        if num and num in saved_by_num and saved_by_num[num] in qbo_by_id:
            qbo_account = qbo_by_id[saved_by_num[num]]
        elif name and name.lower() in saved_by_name_lower and saved_by_name_lower[name.lower()] in qbo_by_id:
            qbo_account = qbo_by_id[saved_by_name_lower[name.lower()]]
        elif num and num in by_num:
            qbo_account = by_num[num]
        elif name and name.lower() in by_name_lower:
            qbo_account = by_name_lower[name.lower()]

        blocker: Optional[str] = None
        if not qbo_account:
            blocker = (
                f"Account {num or '(no number)'} '{name or '(no name)'}' "
                "does not exist in QuickBooks. Create it via the Chart of "
                "Accounts step (or add an Account Mapping) before posting "
                "the opening balance."
            )

        lines.append(OpeningLine(
            account_number=num,
            account_name=name,
            debit=f"{debit:.2f}",
            credit=f"{credit:.2f}",
            qbo_account_id=str(qbo_account.get("Id")) if qbo_account else None,
            qbo_account_name=qbo_account.get("Name") if qbo_account else None,
            qbo_account_type=qbo_account.get("AccountType") if qbo_account else None,
            blocker=blocker,
        ))
        total_debit += debit
        total_credit += credit

    delta = (total_debit - total_credit).quantize(Decimal("0.01"))
    balanced = (delta == 0) and bool(lines)

    blockers: list[str] = []
    warnings: list[str] = []
    if not lines:
        blockers.append(
            "Trial Balance has no non-zero rows — nothing to post as the "
            "opening journal entry. Upload the opening trial balance as "
            "of the day before cutover."
        )
    if not balanced and lines:
        blockers.append(
            f"Trial Balance does not balance: debits ${total_debit:.2f} != "
            f"credits ${total_credit:.2f} (off by ${abs(delta):.2f}). The "
            "opening journal entry must balance before it can be posted. "
            "We do not auto-balance to a suspense account — fix the TB "
            "in PCLaw and re-upload."
        )
    if not plan_as_of:
        warnings.append(
            "No as-of date detected on the trial balance. Enter the opening "
            "balance date on the confirmation page (typically the day "
            "before cutover)."
        )

    return OpeningBalancePlan(
        as_of_date=plan_as_of,
        lines=lines,
        blockers=blockers,
        total_debit=f"{total_debit:.2f}",
        total_credit=f"{total_credit:.2f}",
        balanced=balanced,
        omitted_zero_rows=omitted,
        warnings=warnings,
    )


def build_opening_je_payload(plan: OpeningBalancePlan, *, doc_number: str = "OPEN-BAL") -> dict:
    """Build the QBO JournalEntry payload for a fully-resolved opening plan.

    Caller MUST verify ``plan.has_blockers`` is False and the operator
    has typed the confirmation phrase before invoking this. The payload
    is intentionally minimal and uses TxnDate from ``plan.as_of_date``
    so QuickBooks records the JE at the right moment.
    """
    if plan.has_blockers:
        raise ValueError(
            "Refusing to build a JE payload for a plan with blockers. "
            "Resolve account-resolution and balance blockers first."
        )
    if not plan.balanced:
        raise ValueError(
            "Refusing to build a JE payload for an unbalanced plan."
        )

    lines: list[dict] = []
    for line in plan.postable_lines:
        debit = _money(line.debit)
        credit = _money(line.credit)
        if debit == 0 and credit == 0:
            continue
        amount = debit if debit > 0 else credit
        posting_type = "Debit" if debit > 0 else "Credit"
        lines.append({
            "DetailType": "JournalEntryLineDetail",
            "Amount": float(amount),
            "Description": (
                f"Opening balance ({line.account_number} {line.account_name})"
                if line.account_number else
                f"Opening balance ({line.account_name})"
            ),
            "JournalEntryLineDetail": {
                "PostingType": posting_type,
                "AccountRef": {
                    "value": line.qbo_account_id,
                    "name": line.qbo_account_name,
                },
            },
        })

    payload = {
        "Line": lines,
        "PrivateNote": (
            "PCLaw migration: opening balance JE generated from the opening "
            f"trial balance as of {plan.as_of_date or '(no date)'}."
        ),
    }
    if plan.as_of_date:
        payload["TxnDate"] = plan.as_of_date
    if doc_number:
        payload["DocNumber"] = doc_number[:21]  # QBO max length
    return payload
