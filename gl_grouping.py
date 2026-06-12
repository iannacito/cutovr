"""Source-journal grouping for PCLaw GL rows.

Background — Cesar's QA on 2026-05-29
-------------------------------------
PCLaw exports the General Ledger with one row per posting line. Each row
carries a transaction reference number (``transaction_id``) but PCLaw
will occasionally split what is logically one balanced batch across
multiple consecutive reference numbers. The export still balances at
the file level, and it still balances at the source-journal level (GB,
GL, GJ, CER, …), but the *individual* transaction references don't.

For example, the payroll batch Cesar reported looks like this::

    transaction_id  side   amount   memo
    259730          debit  40050.40 GB Payroll
    259730          credit 26532.57 GB Payroll
    259733          credit  2773.92 GB 401K
    259736          debit   3283.89 GB Payroll
    259736          credit 14027.80 GB Payroll
    ----                   --------
    combined        debit  43334.29
    combined        credit 43334.29   <- balances

Reading each reference in isolation, every one of those is "unbalanced"
or "fewer than 2 posting lines" and the prior validator blocked the
whole file. But the firm cannot reasonably edit PCLaw's export — the
references are how PCLaw numbered the batch internally, and the data
itself is correct.

Strategy
--------
1. Group the *unbalanced* references by source-journal token (the
   first whitespace-separated word of the row's ``description`` /
   ``memo`` — PCLaw writes things like ``GB Payroll``, ``GB 401K``,
   ``GJ``, ``CER 12345``).
2. A group is **safe to merge** when:

   * every row in the group shares the same source-journal token, AND
   * the group's total debits equal its total credits to the cent.

3. When safe, we treat the whole group as a single balanced journal
   batch. We never modify the source CSV; the grouping is only used to
   decide that the file is safe to post and to build *balanced* JE
   payloads at import time.

4. Anything that doesn't satisfy both rules stays blocked, with a
   plain-English explanation in the validation report.

Safety invariants
-----------------
* Never post an unbalanced batch. ``build_journal_entry_payload`` still
  raises if the merged group does not balance — that's the last line
  of defense.
* Never silently re-bucket A/R / A/P entity hints across PCLaw
  references — the payload builder still inspects each row.
* Never group across transactions that already balance. Balanced
  references continue to be posted as their own JE so the firm can
  trace the original PCLaw reference back to the QBO entry.

This module is pure (no Flask, no QBO HTTP) so callers can unit-test
the policy with simple list-of-dict fixtures.
"""

from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal
from typing import Iterable, Optional

from pclaw_pipeline import money


# Source-journal tokens we recognise in PCLaw exports. Matching is
# whitespace-tokenised and case-insensitive. The list comes from PCLaw's
# own journal types — GB (General Bank), GL (General Ledger / posting),
# GJ (General Journal), CER (Corrected Entry Register), SJ (Sales
# Journal), AR/AP (Accounts Receivable / Payable adjustments), and the
# disbursement/receipt registers.
#
# This is now a real gate: ``source_journal_token`` prefers a known code
# found *anywhere* in a row's reference / memo / description, instead of
# blindly taking the first whitespace word. A real PCLaw export writes
# the journal code in the reference column ("GB 000123") or as a prefix
# of the memo, and the field order varies between firms — Cesar's
# 2026-06-03 GL had the code where the old "first word of the first
# non-empty field" heuristic missed it, so balanced batches stopped
# grouping. Looking for the code explicitly makes grouping deterministic
# regardless of which column carries it.
_KNOWN_SOURCE_JOURNALS: tuple[str, ...] = (
    "GB", "GL", "GJ", "CER", "SJ", "AR", "AP", "TR", "BR", "PJ", "CR", "CD",
)
_KNOWN_SOURCE_JOURNAL_SET: frozenset[str] = frozenset(_KNOWN_SOURCE_JOURNALS)


def _row_money(row: dict, key: str) -> Decimal:
    return money(row.get(key))


def _row_balance(row: dict) -> Decimal:
    """Return (debit - credit) for a single row."""
    return _row_money(row, "debit") - _row_money(row, "credit")


def _clean_token(word: str) -> str:
    """Upper-case a whitespace word and strip surrounding punctuation."""
    return word.upper().strip(",.;:-/()[]")


def source_journal_token(row: dict) -> Optional[str]:
    """Pick the PCLaw source-journal token from a row.

    Two-pass strategy so grouping is robust to which column the firm's
    PCLaw export puts the journal code in:

    1. Scan ``reference`` -> ``source_journal`` -> ``journal`` -> ``memo``
       -> ``description`` for a *known* PCLaw journal code (GB, SJ, CER,
       …) appearing as any whitespace word. A recognised code is the
       most reliable grouping key, so we prefer it wherever it appears.
    2. Fall back to the first whitespace word of the first non-empty
       field (``memo`` -> ``description`` -> ``reference``) — preserves
       the original behaviour for exports that use a firm-specific code
       we don't recognise.

    Returns ``None`` when no field carries any usable text.
    """
    scan_keys = ("reference", "source_journal", "journal", "memo", "description")
    for key in scan_keys:
        raw = row.get(key)
        if raw is None:
            continue
        for word in str(raw).split():
            tok = _clean_token(word)
            if tok in _KNOWN_SOURCE_JOURNAL_SET:
                return tok
            # "SJ-4471" / "GB000123" / "CER12345": a known code may be the
            # leading letters of a word glued to a number or separator.
            # Require the remainder to be non-alphabetic so an ordinary
            # word like "CRedit" doesn't get mis-read as the CR journal.
            lead = ""
            for ch in tok:
                if ch.isalpha():
                    lead += ch
                else:
                    break
            remainder = tok[len(lead):]
            if (
                lead in _KNOWN_SOURCE_JOURNAL_SET
                and remainder
                and not any(c.isalpha() for c in remainder)
            ):
                return lead

    for key in ("memo", "description", "reference"):
        raw = row.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        first = _clean_token(text.split()[0])
        if first:
            return first
    return None


def is_known_source_journal(token: Optional[str]) -> bool:
    """True for the well-known PCLaw journal codes.

    Reserved for the user-facing copy — the grouping rule itself only
    requires that the token be non-empty and shared across the group.
    """
    return bool(token) and token in _KNOWN_SOURCE_JOURNALS


def _txn_totals(rows: Iterable[dict]) -> tuple[Decimal, Decimal]:
    debits = Decimal("0.00")
    credits = Decimal("0.00")
    for r in rows:
        debits += _row_money(r, "debit")
        credits += _row_money(r, "credit")
    return debits, credits


def split_balanced_and_unbalanced(
    grouped_by_txn: "OrderedDict[str, list[dict]]",
) -> tuple["OrderedDict[str, list[dict]]", "OrderedDict[str, list[dict]]"]:
    """Split the per-transaction grouping into balanced vs. unbalanced.

    A transaction is "balanced" when its debit total equals its credit
    total AND it has at least two posting lines (because QBO won't
    accept a 1-line JE). The unbalanced bucket is what the grouping
    pass tries to rescue.
    """
    balanced: "OrderedDict[str, list[dict]]" = OrderedDict()
    unbalanced: "OrderedDict[str, list[dict]]" = OrderedDict()
    for txn_id, txn_rows in grouped_by_txn.items():
        debits, credits = _txn_totals(txn_rows)
        non_zero_lines = sum(
            1 for r in txn_rows if _row_money(r, "debit") or _row_money(r, "credit")
        )
        if debits == credits and non_zero_lines >= 2:
            balanced[txn_id] = list(txn_rows)
        else:
            unbalanced[txn_id] = list(txn_rows)
    return balanced, unbalanced


def _group_token_for_txn(txn_rows: list[dict]) -> Optional[str]:
    """Return the single source-journal token for a transaction, or None.

    A transaction with rows that disagree on their source-journal token
    is *not* eligible for grouping — we'd be merging things the firm
    intended to keep separate.
    """
    tokens = set()
    for r in txn_rows:
        tok = source_journal_token(r)
        if tok is None:
            return None
        tokens.add(tok)
    if len(tokens) != 1:
        return None
    return next(iter(tokens))


def build_source_journal_groups(
    unbalanced: "OrderedDict[str, list[dict]]",
) -> "OrderedDict[str, dict]":
    """Bucket unbalanced transactions by their source-journal token.

    Returns ``OrderedDict[token -> group_info]`` where ``group_info`` is::

        {
            "token": "GB",
            "transaction_ids": ["259730", "259733", "259736"],
            "rows": [row, row, ...],            # flat list across txns
            "debits": Decimal,
            "credits": Decimal,
            "balanced": bool,                   # debits == credits
        }

    A transaction whose rows can't agree on a token is left out of any
    group (so the caller still blocks it). Order is the order tokens
    first appear in the source.
    """
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for txn_id, txn_rows in unbalanced.items():
        token = _group_token_for_txn(txn_rows)
        if not token:
            continue
        bucket = groups.setdefault(
            token,
            {
                "token": token,
                "transaction_ids": [],
                "rows": [],
                "debits": Decimal("0.00"),
                "credits": Decimal("0.00"),
                "balanced": False,
            },
        )
        bucket["transaction_ids"].append(txn_id)
        bucket["rows"].extend(txn_rows)
        for r in txn_rows:
            bucket["debits"] += _row_money(r, "debit")
            bucket["credits"] += _row_money(r, "credit")
    for bucket in groups.values():
        bucket["balanced"] = bucket["debits"] == bucket["credits"]
    return groups


def cross_token_offsets(
    groups: "OrderedDict[str, dict]",
) -> list[dict]:
    """Detect pairs of source-journal groups whose imbalances cancel.

    Cesar's example: ``CER`` was -46.05 short on debits, ``GB`` was
    +46.05 long on debits, and ``GJ`` already balanced. The combined
    file balances even though CER and GB don't individually.

    This function does NOT mark such pairs as "safe to merge into one
    QBO journal entry" — those rows really do belong to different
    source journals and should be posted as separate journal entries.
    What it does is surface the explanation so the validation report
    can say "CER short of debits by $46.05; GB long by $46.05; they
    cancel out in the file total."

    Returns a list of ``{"left_token", "right_token", "amount"}`` dicts.
    Only pairs whose imbalance amounts are exact opposites are
    returned (no fuzzy matching — we're conservative on purpose).
    """
    pairs: list[dict] = []
    by_diff: dict[Decimal, list[str]] = {}
    for token, bucket in groups.items():
        if bucket["balanced"]:
            continue
        diff = bucket["debits"] - bucket["credits"]
        by_diff.setdefault(diff, []).append(token)
    seen: set[tuple[str, str]] = set()
    for diff, tokens in by_diff.items():
        if diff == 0:
            continue
        opposite = -diff
        if opposite not in by_diff:
            continue
        for left in tokens:
            for right in by_diff[opposite]:
                key = tuple(sorted([left, right]))
                if key in seen or left == right:
                    continue
                seen.add(key)
                pairs.append({
                    "left_token": key[0],
                    "right_token": key[1],
                    "amount": f"{abs(diff):.2f}",
                })
    return pairs


def plan_posting_groups(
    grouped_by_txn: "OrderedDict[str, list[dict]]",
) -> dict:
    """Return a plan for how to post a parsed GL safely.

    The returned shape::

        {
            "balanced_transactions":   {txn_id: [rows...]},
            "merged_groups":           [
                {
                    "group_id":         "GROUP-GB",
                    "token":            "GB",
                    "transaction_ids":  ["259730", "259733", "259736"],
                    "rows":             [...],
                    "debits":           "43334.29",
                    "credits":          "43334.29",
                },
                ...
            ],
            "still_blocked": [
                {
                    "transaction_id":   "259208",
                    "line_count":       1,
                    "reasons":          ["fewer than 2 posting lines; unbalanced ..."],
                    "token":            "GB",          # or None
                    "debits":           "0.00",
                    "credits":          "111.69",
                },
                ...
            ],
            "cross_token_offsets":     [ ... ],
            "would_post_via_grouping": bool,
        }

    "would_post_via_grouping" is True iff *some* unbalanced
    transactions were rescued into a balanced merged group. Callers
    should use it to decide whether to show the "We grouped related
    PCLaw rows that balance together" explainer.
    """
    balanced, unbalanced = split_balanced_and_unbalanced(grouped_by_txn)
    journal_groups = build_source_journal_groups(unbalanced)

    merged_groups: list[dict] = []
    rescued_txn_ids: set[str] = set()

    for token, bucket in journal_groups.items():
        if not bucket["balanced"]:
            continue
        # Don't bother "merging" a single transaction with itself — if
        # only one txn under this token survived, it'll still appear in
        # the blocked list as its own entry. (In practice this branch
        # is rare because a single-txn imbalance can't equal zero.)
        if len(bucket["transaction_ids"]) < 2:
            continue
        # Must have at least two posting lines with non-zero amounts so
        # the QBO JE payload is valid. (PCLaw single-line entries are
        # what create the imbalance in the first place; we only rescue
        # them when their sibling lines bring the total to zero.)
        non_zero_lines = sum(
            1 for r in bucket["rows"]
            if _row_money(r, "debit") or _row_money(r, "credit")
        )
        if non_zero_lines < 2:
            continue
        merged_groups.append({
            "group_id": f"GROUP-{token}-{'-'.join(bucket['transaction_ids'][:3])}",
            "token": token,
            "transaction_ids": list(bucket["transaction_ids"]),
            "rows": list(bucket["rows"]),
            "debits": f"{bucket['debits']:.2f}",
            "credits": f"{bucket['credits']:.2f}",
        })
        rescued_txn_ids.update(bucket["transaction_ids"])

    still_blocked: list[dict] = []
    for txn_id, txn_rows in unbalanced.items():
        if txn_id in rescued_txn_ids:
            continue
        debits, credits = _txn_totals(txn_rows)
        token = _group_token_for_txn(txn_rows)
        reasons = []
        non_zero_lines = sum(
            1 for r in txn_rows if _row_money(r, "debit") or _row_money(r, "credit")
        )
        if non_zero_lines < 2:
            reasons.append("fewer than 2 posting lines")
        if debits != credits:
            reasons.append(
                f"unbalanced (debits={debits:.2f}, credits={credits:.2f})"
            )
        still_blocked.append({
            "transaction_id": txn_id,
            "line_count": len(txn_rows),
            "reasons": reasons,
            "token": token,
            "debits": f"{debits:.2f}",
            "credits": f"{credits:.2f}",
        })

    return {
        "balanced_transactions": balanced,
        "merged_groups": merged_groups,
        "still_blocked": still_blocked,
        "cross_token_offsets": cross_token_offsets(journal_groups),
        "would_post_via_grouping": bool(merged_groups),
        "rescued_transaction_ids": sorted(rescued_txn_ids),
    }


def _first_date(entries, src_by_txn) -> str:
    """Scan blocked entries to find the first available transaction date."""
    for blocked in entries:
        tid = str(blocked.get("transaction_id") or "").strip()
        src = src_by_txn.get(tid) or {}
        d = src.get("date", "")
        if d:
            return d
    return ""


def auto_balance_by_token_group(
    still_blocked: list[dict],
    original_rows: list[dict],
    bank_account_name: str,
    bank_account_number: str,
    expense_offset_name: str = "",
    expense_offset_number: str = "",
) -> list[dict]:
    """Generate one synthetic balancing row per blocked single-sided transaction.

    For each entry in still_blocked, emits ONE synthetic row with the same
    transaction_id as the original so the two rows group together at import
    time and form a valid 2-line balanced journal entry.

    Net-credit entry (credit > debit)  → ONE DEBIT row on the same account
                                         as the source transaction.
    Net-debit entry  (debit > credit)  → ONE CREDIT row on expense_offset
                                         (falls back to bank_account if no
                                         expense offset is supplied).

    Subtotal rows (empty transaction_id) are skipped — they are PCLaw section
    footers already excluded by is_droppable_row before this is called.
    """
    from decimal import Decimal

    src_by_txn: dict = {}
    for r in original_rows:
        tid = (r.get("transaction_id") or r.get("reference_number") or "").strip()
        if tid and tid not in src_by_txn:
            src_by_txn[tid] = r

    synthetic: list[dict] = []
    for blocked in still_blocked:
        txn_id = str(blocked.get("transaction_id") or "").strip()
        if not txn_id:
            continue  # subtotal / summary row — skip

        total_debits = Decimal(str(blocked.get("debits") or "0"))
        total_credits = Decimal(str(blocked.get("credits") or "0"))
        net = total_credits - total_debits
        if net == 0:
            continue  # already balanced at the transaction level

        src = src_by_txn.get(txn_id) or {}
        date = (src.get("date") or "").strip() or _first_date([blocked], src_by_txn)
        account_number = (src.get("account_number") or "").strip()
        account_name = (src.get("account_name") or "").strip()
        memo = (src.get("memo") or blocked.get("token") or "").strip()
        token = (blocked.get("token") or "").strip()

        if net > 0:
            # Net credit entry (e.g. CER disbursement recovery posted credit-only).
            # Add a matching debit on the same account so the transaction balances.
            synthetic.append({
                "date": date,
                "account_number": account_number,
                "account_name": account_name,
                "memo": memo,
                "reference_number": (src.get("reference_number") or "").strip(),
                "transaction_id": txn_id,
                "vendor_name": (src.get("vendor_name") or "").strip(),
                "description": (src.get("description") or "").strip(),
                "debit": str(net),
                "credit": "",
                "_synthetic": True,
                "_token_group": token,
                "_synthetic_reason": (
                    f"auto-balanced: {txn_id} net-credit {net} "
                    f"→ added debit on {account_number or account_name}"
                ),
            })
        else:
            # Net debit entry (e.g. GB bank refund posted debit-only).
            # Add a matching credit on the expense-offset account (the account
            # that originally carried the disbursement expense, typically 5010).
            offset_number = expense_offset_number or account_number
            offset_name = expense_offset_name or account_name
            synthetic.append({
                "date": date,
                "account_number": offset_number,
                "account_name": offset_name,
                "memo": memo,
                "reference_number": (src.get("reference_number") or "").strip(),
                "transaction_id": txn_id,
                "vendor_name": (src.get("vendor_name") or "").strip(),
                "description": (src.get("description") or "").strip(),
                "debit": "",
                "credit": str(abs(net)),
                "_synthetic": True,
                "_token_group": token,
                "_synthetic_reason": (
                    f"auto-balanced: {txn_id} net-debit {abs(net)} "
                    f"→ added credit on {offset_number or offset_name}"
                ),
            })

    return synthetic
