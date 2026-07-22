"""GL row quality: date parsing, blank detection, beginning-balance routing.

Cesar's QA on 2026-05-29 found that the corrected-file flow let the user
through to Send to QuickBooks even though the underlying CSV still had
single-sided beginning-balance rows. Two underlying issues caused that:

1. Date detection only checked for non-empty strings. Excel exports
   commonly carry the serial integer (``45323``) or quoted date strings
   (``"01/15/2026"``); rows with those were counted as "ok" even when
   the date could not be parsed downstream.

2. "Blank row" was conflated with "no-date row". A truly empty row
   (no debit, no credit, no account, no date) should be silently
   dropped — those are footer / pagination artifacts. A row with a
   real amount + account but no date is a *blocked* row that needs
   a fix or belongs on the opening trial balance.

This module is pure (no Flask, no QBO HTTP) so it can be exercised
in tests with simple list[dict] fixtures. The preflight + validation
report + Step 4 review UI consume the classification it returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from pclaw_pipeline import money


# Excel's 1900 epoch base. Excel treats 1900 as a leap year (it isn't),
# so day 60 (1900-02-29) is fictional. Practically every PCLaw / law-firm
# export we have seen post-dates 2000, so the 2-day offset isn't visible.
_EXCEL_EPOCH = date(1899, 12, 30)


# Date format strings we attempt, in order. PCLaw's own GL export uses
# MM/DD/YYYY in Canada and the US; Excel re-exports often coerce to
# YYYY-MM-DD or D-MMM-YY. The list is intentionally tight — adding more
# permissive formats hides genuine bad input.
#
# A fresh-off-PCLaw export writes the date as "Jan 4/21" (month
# abbreviation, day, slash, two-digit year) — see Cesar's QA 2026-06-01.
# Firms were having to hand-edit every row to a format the app accepted
# before the GL would import. The "%b %d/%y" / "%b %-d/%y" family below
# accepts that native format directly so no manual edit is needed.
_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",        # Excel locale rewrites
    "%d-%m-%Y",
    "%d-%b-%Y",        # "15-Jan-2026"
    "%d-%b-%y",
    "%b %d, %Y",       # "Jan 15, 2026"
    "%B %d, %Y",       # "January 15, 2026"
    "%b %d/%Y",        # "Jan 4/2021"
    "%b %d/%y",        # "Jan 4/21" — PCLaw native export
    "%B %d/%Y",        # "January 4/2021"
    "%B %d/%y",        # "January 4/21"
    "%b-%d/%y",        # "Jan-4/21"
    "%b/%d/%y",        # "Jan/4/21"
    "%Y%m%d",
)

# Tokens that PCLaw / Excel place in the "date" column to mark a beginning
# balance line. Matched case-insensitively against a stripped value.
_BEGINNING_BALANCE_TOKENS: tuple[str, ...] = (
    "beginning balance",
    "opening balance",
    "balance forward",
    "bal fwd",
    "b/f",
    "b.f.",
    "carried forward",
    "c/f",
    "starting balance",
)


def parse_gl_date(raw) -> Optional[str]:
    """Parse a GL date cell into ISO ``YYYY-MM-DD`` or return None.

    Accepts:
      * Already-ISO strings.
      * The common PCLaw / Excel string formats listed in ``_DATE_FORMATS``.
      * Excel serial integers / serial floats (``45323``, ``45323.0``).
      * ``datetime`` / ``date`` instances (csv.DictReader returns strings,
        but openpyxl-driven test fixtures and downstream callers can hand
        us a real date).

    Returns ``None`` for empty values, beginning-balance tokens, or
    anything else we cannot confidently coerce to a date. Callers
    distinguish "empty" from "invalid" via :func:`is_beginning_balance_token`
    and :func:`is_blank_value`.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()

    text = str(raw).strip().strip('"').strip("'")
    if not text:
        return None
    # Beginning balance markers are explicitly *not* dates.
    if text.lower() in _BEGINNING_BALANCE_TOKENS:
        return None

    # Excel serials: pure integer or float (typical range 25000..60000
    # covers 1968-2064). We bound the range so a stray amount like
    # "1000" or "0.00" doesn't get mis-read as 1902-09-26.
    try:
        if text.replace(",", "").replace(" ", "").lstrip("-").replace(".", "", 1).isdigit():
            n = float(text.replace(",", ""))
            if 20000 <= n <= 80000:
                serial = int(round(n))
                try:
                    return (_EXCEL_EPOCH + timedelta(days=serial)).isoformat()
                except (OverflowError, ValueError):
                    return None
    except ValueError:
        pass

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def is_beginning_balance_token(raw) -> bool:
    """True when the value names a beginning-balance row.

    Used to route those rows to the opening trial balance flow instead
    of dropping them as "blank" (where the user loses sight of them)
    or flagging them as "blocked, fix the CSV" (which they cannot fix —
    PCLaw genuinely exports those rows without a date).
    """
    if raw is None:
        return False
    return str(raw).strip().lower() in _BEGINNING_BALANCE_TOKENS


def is_blank_value(raw) -> bool:
    """True when this cell contributes no signal (None / whitespace / empty)."""
    if raw is None:
        return True
    return not str(raw).strip()


def is_blank_row(row: dict) -> bool:
    """A row is blank when *every* significant field is empty.

    Trailing / leading blank rows in Excel-edited PCLaw exports are a
    real source of "I removed the bad rows and it still fails" reports —
    the user deleted the cell contents but the row itself remained.
    Treating them as blank lets us silently skip them.

    Significant fields: ``date``, ``account_number``, ``account_name``,
    ``debit``, ``credit``. ``description``/``transaction_id`` alone do
    not count — a footer like "Total" with no amounts is still blank
    for our purposes.
    """
    for field_name in ("date", "account_number", "account_name", "debit", "credit"):
        if not is_blank_value(row.get(field_name)):
            return False
    return True


def is_zero_activity_row(row: dict) -> bool:
    """True for an account-listing row with no date and no money.

    PCLaw GL exports list every account, including those with no postings
    in the period, as a 0.00/0.00 row with no date. These post nothing and
    must not be grouped into phantom "fewer than 2 posting lines"
    transactions or flagged as "fix the date". They are not *blank*
    (they carry an account), so callers need this dedicated check.
    """
    if is_blank_row(row):
        return False
    has_date = not is_blank_value(row.get("date"))
    if has_date:
        return False
    if is_beginning_balance_token(row.get("date")):
        return False
    debit = money(row.get("debit"))
    credit = money(row.get("credit"))
    if debit != 0 or credit != 0:
        return False
    has_account = bool(
        (row.get("account_number") or "").strip()
        or (row.get("account_name") or "").strip()
    )
    return has_account


def is_subtotal_row(row: dict) -> bool:
    """True for PCLaw section-subtotal rows (non-zero amount, no transaction_id, no context).

    PCLaw GL exports append a "Total of Recoveries" (or similar) summary row
    at the end of a CER section.  These carry a date, account, and a summed
    debit/credit but have no transaction_id or reference number.  Left in,
    they form a phantom one-line "transaction" that blocks the import.

    However, a row without transaction_id is still a REAL entry (not droppable)
    when it has a date or description/memo — e.g. "Total of Recoveries" with
    date + description + balancing debit (fixes Feb imbalance). Only a bare
    amount with NO date and NO description/memo is a true dangling footer.

    Distinct from zero-activity account-listing rows (no amount) and blank rows
    (no account).
    """
    if is_blank_row(row) or is_zero_activity_row(row):
        return False
    if (row.get("transaction_id") or "").strip():
        return False  # real transaction row — has a reference
    has_amount = money(row.get("debit")) != 0 or money(row.get("credit")) != 0
    if not has_amount:
        return False
    # A no-transaction_id row with a date or description/memo is a real entry.
    # Only a bare amount (no id, no date, no context) is a footer to drop.
    has_date = bool(parse_gl_date(row.get("date")))
    has_desc = bool((row.get("description") or "").strip()
                    or (row.get("memo") or "").strip())
    if has_date or has_desc:
        return False  # real entry — keep it
    return True  # amount only, no id/date/description → footer → drop


def is_droppable_row(row: dict) -> bool:
    """True when a row contributes nothing to the import.

    Covers:
    * blank rows (no fields at all — trailing Excel rows)
    * zero-activity account-listing rows (account with 0.00/0.00, no date)
    * PCLaw section-subtotal rows (non-zero amount, no transaction_id)
    * beginning-balance / opening-balance rows — PCLaw GL exports always
      include one "Opening Balance" row per account. These are one-sided
      (debit OR credit only) and belong in the Starting Balances step, not
      in the general ledger. Including them in the balance totals makes
      every PCLaw export appear unbalanced. Silently strip them here so
      the actual transaction rows are what the balance gate sees.
    """
    return (
        is_blank_row(row)
        or is_zero_activity_row(row)
        or is_subtotal_row(row)
        or is_beginning_balance_token(row.get("date"))
    )


@dataclass
class RowClassification:
    """How a single GL row should be treated for import + reporting."""

    index: int                       # 1-based row number in the source CSV
    transaction_id: str
    parsed_date: Optional[str]       # ISO date if parseable, else None
    raw_date: str
    account_label: str               # "1234 Operating Cash" or "(blank)"
    debit: str
    credit: str
    kind: str                        # "ok" | "blank" | "no_date" |
                                     # "beginning_balance" | "single_sided" |
                                     # "unparseable_date" | "no_account"
    reason: str = ""
    plain_fix: str = ""

    def to_dict(self) -> dict:
        return {
            "row_number": self.index,
            "transaction_id": self.transaction_id,
            "parsed_date": self.parsed_date,
            "raw_date": self.raw_date,
            "account": self.account_label,
            "debit": self.debit,
            "credit": self.credit,
            "kind": self.kind,
            "reason": self.reason,
            "plain_fix": self.plain_fix,
        }


def _account_label(row: dict) -> str:
    num = (row.get("account_number") or "").strip()
    name = (row.get("account_name") or "").strip()
    if num and name:
        return f"{num} {name}"
    return num or name or "(blank)"


def _classify_row(idx: int, row: dict) -> RowClassification:
    raw_date = str(row.get("date") or "").strip()
    txn = str(row.get("transaction_id") or "").strip()
    debit_value = row.get("debit")
    credit_value = row.get("credit")
    debit = money(debit_value)
    credit = money(credit_value)
    has_amount = (debit != 0) or (credit != 0)
    has_account = bool((row.get("account_number") or "").strip()
                       or (row.get("account_name") or "").strip())

    label = _account_label(row)
    debit_str = f"{debit:.2f}"
    credit_str = f"{credit:.2f}"

    if is_blank_row(row):
        return RowClassification(
            index=idx, transaction_id=txn, parsed_date=None, raw_date=raw_date,
            account_label=label, debit=debit_str, credit=credit_str,
            kind="blank",
            reason="Row is empty — ignored.",
            plain_fix="",
        )

    # Beginning-balance tokens — common in PCLaw exports as a "Balance
    # Forward" row with one side only. They belong on Starting Balances.
    if is_beginning_balance_token(raw_date) or (
        not raw_date and not has_amount and has_account
    ):
        # The bare account-name row pattern (no date, no amount) also
        # comes back from PCLaw as a beginning-balance marker.
        if is_beginning_balance_token(raw_date) or (debit != 0 or credit != 0):
            return RowClassification(
                index=idx, transaction_id=txn, parsed_date=None, raw_date=raw_date,
                account_label=label, debit=debit_str, credit=credit_str,
                kind="beginning_balance",
                reason=(
                    "Beginning-balance / balance-forward row. Beginning "
                    "balances belong on the Starting Balances upload, "
                    "not in the general ledger."
                ),
                plain_fix=(
                    "Remove this row from the general-ledger CSV. Upload "
                    "the opening trial balance from Step 2 if you "
                    "haven't already — that is what seeds QuickBooks "
                    "with starting balances."
                ),
            )

    if not has_account:
        return RowClassification(
            index=idx, transaction_id=txn, parsed_date=None, raw_date=raw_date,
            account_label=label, debit=debit_str, credit=credit_str,
            kind="no_account",
            reason="Row has no account number or account name.",
            plain_fix=(
                "Add the PCLaw account number or name to this row in "
                "the CSV, or delete the row if it was a header or total."
            ),
        )

    # Zero-activity account listing line: an account name/number with no
    # date and no debit/credit. PCLaw GL exports routinely include one such
    # row per account in the chart (the "account header" that precedes its
    # postings, or accounts with no movement in the period). These carry no
    # money, so they post nothing — flagging them as "fix the date" sent
    # lawyers chasing 50+ phantom errors (Cesar QA 2026-06-03, where every
    # 0.00/0.00 account row showed "Row has an amount but no transaction
    # date"). Treat them as non-blocking: there is nothing to import or fix.
    if not raw_date and not has_amount:
        return RowClassification(
            index=idx, transaction_id=txn, parsed_date=None, raw_date=raw_date,
            account_label=label, debit=debit_str, credit=credit_str,
            kind="zero_activity",
            reason="Account listing row with no date and no amount — nothing to import.",
            plain_fix="",
        )

    parsed = parse_gl_date(raw_date)
    if not raw_date:
        # Real, signal-carrying row (it has a non-zero amount) but no date.
        # Most often this is a beginning-balance line the firm forgot to date.
        return RowClassification(
            index=idx, transaction_id=txn, parsed_date=None, raw_date=raw_date,
            account_label=label, debit=debit_str, credit=credit_str,
            kind="no_date",
            reason="Row has an amount but no transaction date.",
            plain_fix=(
                "If this row is a beginning balance, remove it from the "
                "general-ledger CSV and add it to the opening trial "
                "balance instead. Otherwise, fill in the date "
                "(YYYY-MM-DD) and re-upload."
            ),
        )

    if parsed is None:
        return RowClassification(
            index=idx, transaction_id=txn, parsed_date=None, raw_date=raw_date,
            account_label=label, debit=debit_str, credit=credit_str,
            kind="unparseable_date",
            reason=(
                f"We could not read '{raw_date}' as a date."
            ),
            plain_fix=(
                "Edit the date to YYYY-MM-DD (for example 2026-01-15) "
                "or MM/DD/YYYY and re-upload. The PCLaw export format "
                "(for example Jan 4/21) and Excel serial numbers between "
                "20000 and 80000 are also accepted."
            ),
        )

    if debit != 0 and credit != 0:
        return RowClassification(
            index=idx, transaction_id=txn, parsed_date=parsed, raw_date=raw_date,
            account_label=label, debit=debit_str, credit=credit_str,
            kind="single_sided",
            reason="Row has both a debit and a credit set.",
            plain_fix=(
                "Split this row into two — one debit row and one credit "
                "row that share the same transaction_id."
            ),
        )

    return RowClassification(
        index=idx, transaction_id=txn, parsed_date=parsed, raw_date=raw_date,
        account_label=label, debit=debit_str, credit=credit_str,
        kind="ok",
    )


@dataclass
class RowQualityReport:
    """Aggregate of :class:`RowClassification` across a parsed GL file.

    Consumed by the preflight builder, the validation-report renderer,
    and the Step 4 review template. ``problem_rows`` is the trimmed
    list of rows the user needs to act on; ``beginning_balance_rows``
    is split off so we can render the dedicated Starting Balances
    guidance instead of just "row is blocked".
    """
    total_rows: int = 0
    blank_rows: int = 0
    ok_rows: int = 0
    problem_rows: list[RowClassification] = field(default_factory=list)
    beginning_balance_rows: list[RowClassification] = field(default_factory=list)

    @property
    def has_problems(self) -> bool:
        return bool(self.problem_rows)

    @property
    def has_beginning_balances(self) -> bool:
        return bool(self.beginning_balance_rows)

    def counts_by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.problem_rows + self.beginning_balance_rows:
            out[r.kind] = out.get(r.kind, 0) + 1
        return out


def classify_gl_rows(rows: Iterable[dict]) -> RowQualityReport:
    """Classify every row in a parsed PCLaw GL CSV.

    Always returns a report (never raises) so the caller can fold the
    counts into the preflight even when the file is completely empty.
    """
    report = RowQualityReport()
    if not rows:
        return report
    for idx, row in enumerate(rows, start=1):
        report.total_rows += 1
        cls = _classify_row(idx, row)
        if cls.kind in ("blank", "zero_activity"):
            # Zero-activity account listing rows carry no money and post
            # nothing; fold them into the dropped-rows count rather than
            # the user-facing "needs a fix" list.
            report.blank_rows += 1
            continue
        if cls.kind == "ok":
            report.ok_rows += 1
            continue
        if cls.kind == "beginning_balance":
            report.beginning_balance_rows.append(cls)
            continue
        report.problem_rows.append(cls)
    return report
