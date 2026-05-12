"""
Multi-report support for PCLaw exports.

The migration product began with General Ledger only and posted JournalEntry
records to QuickBooks Online. Firms running a real cutover need at minimum
four reports from PCLaw:

  - General Ledger (importable: JournalEntry to QBO)
  - Chart of Accounts (preview/dry-run match against QBO Accounts)
  - Trial Balance     (parse + balance validation; reconciliation only)
  - Trust Listing     (parse + total + reconciliation; never auto-posted)

This module owns:

  - the report-type identifiers and human labels (REPORT_TYPES)
  - per-report column variants accepted from common PCLaw exports
  - normalized parsers for each report
  - per-report preflight summary builders (counts, totals, warnings)
  - a header-based auto-detector for upload

Nothing in this module calls QBO write endpoints. Trust Listing and Trial
Balance are deliberately read-only on QBO; they are validation /
reconciliation artifacts. Chart of Accounts ships with a non-destructive
dry-run preview that compares against QBO Account list and identifies
matches / would-be-creates. Actual QBO Account creation is gated behind
an explicit confirmation route (see app.py:apply_coa_to_qbo).

All cell formatting is sanitized through csv_safety on the way out for
report downloads; the parser layer keeps raw values for in-memory use.
"""

from __future__ import annotations

import csv
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional


REPORT_GENERAL_LEDGER = "general_ledger"
REPORT_CHART_OF_ACCOUNTS = "chart_of_accounts"
REPORT_TRIAL_BALANCE = "trial_balance"
REPORT_TRUST_LISTING = "trust_listing"

REPORT_TYPES = (
    REPORT_GENERAL_LEDGER,
    REPORT_CHART_OF_ACCOUNTS,
    REPORT_TRIAL_BALANCE,
    REPORT_TRUST_LISTING,
)

REPORT_LABELS = {
    REPORT_GENERAL_LEDGER: "General Ledger",
    REPORT_CHART_OF_ACCOUNTS: "Chart of Accounts",
    REPORT_TRIAL_BALANCE: "Trial Balance",
    REPORT_TRUST_LISTING: "Trust Listing",
}

# Whether each report type currently writes to QuickBooks Online.
#   importable: posts records to QBO after confirmation.
#   preview:    dry-run only; produces a side-by-side comparison.
#   readonly:   parsed for validation/reconciliation; never written.
REPORT_QBO_BEHAVIOR = {
    REPORT_GENERAL_LEDGER: "importable",
    REPORT_CHART_OF_ACCOUNTS: "preview",
    REPORT_TRIAL_BALANCE: "readonly",
    REPORT_TRUST_LISTING: "readonly",
}


def report_label(rt: Optional[str]) -> str:
    return REPORT_LABELS.get(rt or "", REPORT_LABELS[REPORT_GENERAL_LEDGER])


def is_valid_report_type(rt: Optional[str]) -> bool:
    return rt in REPORT_TYPES


# --- helpers ---------------------------------------------------------------


def _money(value) -> Decimal:
    """Parse a money-like cell. Empty/blank/None -> 0.00.

    Robust to common PCLaw export variants:
      - $ and , thousand separators
      - accounting parentheses for negatives: (1,234.56)
      - trailing "CR" / "DR" indicators on printed reports
      - leading +/- signs and unicode minus (-, –, —, U+2212)
      - non-breaking spaces and assorted whitespace
      - hyphen-only / "N/A" placeholder cells (treated as 0)
    """
    if value is None:
        return Decimal("0.00")
    s = str(value).strip()
    if not s:
        return Decimal("0.00")
    # Normalize unicode minus / dash variants and non-breaking space.
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    s = s.replace("\xa0", " ").strip()
    if s in {"-", "--", "—", "n/a", "N/A", "na", "NA", "--"}:
        return Decimal("0.00")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    # Trailing CR / DR indicators ("1,234.56 CR" -> credit/negative).
    upper = s.upper()
    if upper.endswith(" CR"):
        negative = True
        s = s[:-3].rstrip()
    elif upper.endswith("CR") and len(s) > 2 and not s[-3].isalpha():
        negative = True
        s = s[:-2].rstrip()
    elif upper.endswith(" DR"):
        s = s[:-3].rstrip()
    elif upper.endswith("DR") and len(s) > 2 and not s[-3].isalpha():
        s = s[:-2].rstrip()
    s = s.replace(",", "").replace("$", "").strip()
    if not s:
        return Decimal("0.00")
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal("0.00")
    if negative:
        d = -d
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Real-world PCLaw printouts include footer rows and subtotals that should
# be skipped at parse time so they don't pollute counts / balance checks.
_FOOTER_MARKERS = (
    "total", "subtotal", "grand total", "report total", "end of report",
    "end of file", "page ", "report generated", "*** end ***",
    "===", "_____",
)


def _looks_like_footer_or_subtotal(row: dict) -> bool:
    """Heuristic: True when this CSV row is a totals / footer / pagination
    artifact rather than a real data row. PCLaw GL / TB CSV exports often
    include these at the bottom of the file."""
    if not row:
        return False
    values = [str(v or "").strip().lower() for v in row.values()]
    blob = " ".join(values).strip()
    if not blob:
        return True
    first_nonempty = next((v for v in values if v), "")
    # A row whose first non-empty cell starts with "total" / "subtotal" /
    # "grand total" is almost always a footer line.
    if first_nonempty.startswith(("total ", "totals", "subtotal", "grand total")):
        return True
    if first_nonempty in {"total", "totals", "subtotal", "grand total",
                          "report total", "end of report", "end of file"}:
        return True
    # Pagination markers ("Page 1 of 4")
    if "page " in blob and " of " in blob and len(blob) < 60:
        return True
    return False


def _norm_header(h: str) -> str:
    """Normalize a header for matching across PCLaw export variants.

    Lowercases, replaces dashes / slashes / spaces with underscores, trims
    surrounding whitespace and quotes. Conservative: it keeps alpha-numerics
    intact so we don't accidentally collide unrelated columns.
    """
    h = (h or "").strip().lower().strip("\"'")
    out = []
    prev_us = False
    for ch in h:
        if ch.isalnum():
            out.append(ch)
            prev_us = False
        elif ch in (" ", "_", "-", "/", "."):
            if not prev_us:
                out.append("_")
                prev_us = True
        # everything else dropped (parentheses, etc.)
    s = "".join(out).strip("_")
    return s


def _index_headers(fieldnames: Optional[Iterable[str]]) -> dict[str, str]:
    """Map normalized header -> original header for a row dict."""
    index = {}
    for raw in (fieldnames or []):
        norm = _norm_header(raw)
        # First occurrence wins; PCLaw exports rarely duplicate columns.
        index.setdefault(norm, raw)
    return index


def _split_combined_account(value: str) -> tuple[str, str]:
    """Split a single "1000 - Operating Bank" cell into (number, name).

    PCLaw printouts sometimes combine the account number and name into a
    single column (especially when exported from the screen rather than a
    real report). Common separators: " - ", " – ", ":", " | ", and tabs.
    If only a number or only a name is present, the other side returns
    empty so the caller can fall back to dedicated columns.
    """
    if not value:
        return "", ""
    s = str(value).strip()
    for sep in (" - ", " – ", "—", ":", " | ", "\t"):
        if sep in s:
            left, _, right = s.partition(sep)
            left = left.strip()
            right = right.strip()
            if left and right:
                # Heuristic: digits-only or short alphanumeric left side -> number.
                if left.replace("-", "").replace(".", "").isdigit() or (
                    len(left) <= 8 and any(ch.isdigit() for ch in left)
                ):
                    return left, right
                # Otherwise treat the *right* side as the number if it looks like one.
                if right.replace("-", "").replace(".", "").isdigit():
                    return right, left
            # No confident split — fall through.
            break
    # Single token. Treat as a number when it's purely numeric/dash, otherwise name.
    if s.replace("-", "").replace(".", "").isdigit():
        return s, ""
    return "", s


def _pick(row: dict, header_index: dict[str, str], *aliases: str) -> str:
    """Return the first non-empty cell among the alias header names.

    Aliases are matched after normalization, so callers can pass either
    'account_number' or 'AccountNumber' and the lookup behaves the same.
    """
    for alias in aliases:
        norm = _norm_header(alias)
        raw_header = header_index.get(norm)
        if raw_header is None:
            continue
        value = row.get(raw_header)
        if value is None:
            continue
        s = str(value).strip()
        if s:
            return s
    return ""


# --- report-type detection -------------------------------------------------


# Each detector returns a small score; the highest score wins. We score by
# the *count* of required-ish columns that match so we don't accidentally
# flip a GL into a TB just because both have account_number.
def _score_general_ledger(idx: dict[str, str]) -> int:
    score = 0
    for h in ("transaction_id", "date", "account_number", "account_name", "debit", "credit"):
        if _norm_header(h) in idx:
            score += 1
    # GL is the only report with transaction_id; if it's there, this is GL.
    if _norm_header("transaction_id") in idx:
        score += 4
    return score


def _score_chart_of_accounts(idx: dict[str, str]) -> int:
    score = 0
    if _norm_header("account_number") in idx:
        score += 1
    if _norm_header("account_name") in idx:
        score += 1
    if any(_norm_header(h) in idx for h in ("account_type", "type", "category", "pclaw_category")):
        score += 2
    if any(_norm_header(h) in idx for h in ("parent_account", "parent_account_name", "parent_account_number", "header_account")):
        score += 1
    if any(_norm_header(h) in idx for h in ("debit", "credit", "debit_balance", "credit_balance")):
        # Looks more like TB than COA — penalize.
        score -= 2
    if any(_norm_header(h) in idx for h in ("client_id", "matter_id", "trust_balance")):
        score -= 3
    return score


def _score_trial_balance(idx: dict[str, str]) -> int:
    score = 0
    if _norm_header("account_number") in idx:
        score += 1
    if _norm_header("account_name") in idx:
        score += 1
    has_debit = any(_norm_header(h) in idx for h in ("debit_balance", "debit"))
    has_credit = any(_norm_header(h) in idx for h in ("credit_balance", "credit"))
    if has_debit and has_credit:
        score += 3
    if _norm_header("transaction_id") in idx:
        # TB doesn't have transactions.
        score -= 5
    if any(_norm_header(h) in idx for h in ("client_id", "matter_id", "trust_balance")):
        score -= 3
    return score


def _score_trust_listing(idx: dict[str, str]) -> int:
    score = 0
    if any(_norm_header(h) in idx for h in ("trust_balance", "balance")):
        score += 1
    if any(_norm_header(h) in idx for h in ("client_id", "client_name", "client", "matter_id", "matter_name", "matter")):
        score += 2
    if any(_norm_header(h) in idx for h in ("trust_bank_account", "trust_account", "bank_account")):
        score += 2
    if _norm_header("transaction_id") in idx:
        score -= 4
    return score


def detect_report_type(fieldnames: Optional[Iterable[str]]) -> Optional[str]:
    """Best-effort report-type guess from CSV headers.

    Returns one of REPORT_TYPES, or None if nothing scored high enough to
    be a confident guess. The upload UI lets the user override.
    """
    idx = _index_headers(fieldnames)
    if not idx:
        return None
    scores = {
        REPORT_GENERAL_LEDGER: _score_general_ledger(idx),
        REPORT_CHART_OF_ACCOUNTS: _score_chart_of_accounts(idx),
        REPORT_TRIAL_BALANCE: _score_trial_balance(idx),
        REPORT_TRUST_LISTING: _score_trust_listing(idx),
    }
    best, best_score = max(scores.items(), key=lambda kv: kv[1])
    # Require a meaningful lead so a half-formed CSV doesn't auto-pick.
    if best_score < 3:
        return None
    return best


# --- parsers ---------------------------------------------------------------


COA_REQUIRED = ("account_number", "account_name")
TB_REQUIRED = ("account_number", "account_name")
TRUST_REQUIRED = ("trust_balance",)


def _open_csv(path) -> tuple[list[dict], list[str]]:
    """Open a PCLaw CSV export, returning (data_rows, fieldnames).

    Resilient to a handful of real-world PCLaw quirks:
      * Pre-header preamble rows ("Report Date: ...", blank lines, "PCLaw
        General Ledger Report", etc.) above the actual column header.
        We scan the first ~20 lines for a row that looks like a header
        and use that as the start.
      * BOM-prefixed files (utf-8-sig handles that already).
      * Footer / subtotal / pagination rows at the bottom — skipped via
        ``_looks_like_footer_or_subtotal``.
      * Fully-blank rows scattered throughout — skipped.
    """
    p = Path(path)
    with p.open("r", newline="", encoding="utf-8-sig") as f:
        # Buffer up to ~20 lines while we search for the real header row.
        raw_lines: list[str] = []
        for _ in range(20):
            line = f.readline()
            if not line:
                break
            raw_lines.append(line)
        header_index = _find_header_line_index(raw_lines)
        rest = f.read()

    csv_text = "".join(raw_lines[header_index:]) + rest
    reader = csv.DictReader(csv_text.splitlines())
    fieldnames = list(reader.fieldnames or [])
    rows: list[dict] = []
    for row in reader:
        if row is None:
            continue
        if _looks_like_footer_or_subtotal(row):
            continue
        rows.append(row)
    return rows, fieldnames


def _find_header_line_index(lines: list[str]) -> int:
    """Return the index of the line that most plausibly contains the real
    CSV header. Looks for the first non-empty line that contains at least
    two known PCLaw header tokens. Falls back to the first non-empty line.
    """
    HEADER_TOKENS = (
        "account", "debit", "credit", "balance", "transaction", "date",
        "amount", "client", "matter", "trust", "type", "name", "number",
    )
    fallback = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        hits = sum(1 for t in HEADER_TOKENS if t in lower)
        # If line has commas AND >= 2 header tokens AND no $ sign, it's
        # very likely the header. (Data rows usually carry $ amounts.)
        if "," in stripped and hits >= 2 and "$" not in stripped:
            return i
        if fallback == 0 and stripped:
            fallback = i
    return fallback


def parse_chart_of_accounts(path) -> tuple[list[dict], list[str], list[str]]:
    """Return (normalized_rows, raw_fieldnames, missing_required).

    Real-world tolerances:
      * Combined "1000 - Operating Bank" cell under an "Account" column is
        split into account_number + account_name when dedicated columns
        are absent.
      * Parent hierarchy is read from any of the parent-account aliases
        listed below; absent fields produce an empty parent string.
      * Inactive flag accepts Yes/No, A/I, 1/0, true/false case-insensitively.
    """
    rows, fieldnames = _open_csv(path)
    idx = _index_headers(fieldnames)
    missing = [c for c in COA_REQUIRED if _norm_header(c) not in idx]

    # If neither account_number nor account_name columns exist but a
    # combined "account" column is present, we synthesize them at parse
    # time so the rest of the pipeline doesn't have to special-case it.
    has_num_col = _norm_header("account_number") in idx
    has_name_col = _norm_header("account_name") in idx
    combined_key = None
    for alias in ("account", "gl_account", "ledger_account"):
        if _norm_header(alias) in idx:
            combined_key = _norm_header(alias)
            break

    normalized = []
    for row in rows:
        account_number = _pick(row, idx, "account_number", "acct_num", "number")
        account_name = _pick(row, idx, "account_name", "name", "description_name")
        if (not account_number or not account_name) and combined_key is not None:
            combined = _pick(row, idx, "account", "gl_account", "ledger_account")
            num_from_combined, name_from_combined = _split_combined_account(combined)
            account_number = account_number or num_from_combined
            account_name = account_name or name_from_combined
        account_type = _pick(
            row, idx, "account_type", "type", "qbo_suggested_type", "pclaw_category", "category"
        )
        detail_type = _pick(row, idx, "qbo_suggested_detail_type", "detail_type", "sub_type")
        description = _pick(row, idx, "description", "notes", "memo")
        parent_number = _pick(
            row, idx,
            "parent_account_number", "parent_acct_num", "parent_number",
            "parent_id",
        )
        parent_name = _pick(
            row, idx,
            "parent_account_name", "parent_account", "parent",
            "parent_name", "header_account",
        )
        active_raw = _pick(row, idx, "active", "status", "is_active", "enabled")
        # Normalize active flag. Most exports use Yes/No, true/false, 1/0, A/I.
        active_lower = active_raw.lower()
        if active_lower in ("", "yes", "y", "true", "1", "active", "a"):
            active = True
        elif active_lower in ("no", "n", "false", "0", "inactive", "i"):
            active = False
        else:
            active = True
        opening_balance = _money(_pick(row, idx, "opening_balance", "balance"))
        normalized.append(
            {
                "account_number": account_number,
                "account_name": account_name,
                "account_type": account_type,
                "detail_type": detail_type,
                "description": description,
                "parent_account_number": parent_number,
                "parent_account_name": parent_name,
                "active": active,
                "opening_balance": f"{opening_balance:.2f}",
            }
        )
    # If both dedicated columns were missing but the combined column
    # rescued us, drop the missing-column warning for whatever we
    # successfully derived.
    if combined_key is not None:
        derived_num = any(r.get("account_number") for r in normalized)
        derived_name = any(r.get("account_name") for r in normalized)
        if derived_num and "account_number" in missing:
            missing.remove("account_number")
        if derived_name and "account_name" in missing:
            missing.remove("account_name")
    return normalized, fieldnames, missing


def parse_trial_balance(path) -> tuple[list[dict], list[str], list[str]]:
    """Parse a PCLaw Trial Balance export with real-world tolerances.

    Accepts:
      * Combined "1000 - Operating Bank" cell when dedicated columns
        are absent.
      * Signed net_balance / amount column where a negative value means
        credit (so 1234 -> debit, -1234 -> credit).
      * Negative debit cells (rare): treated as a credit of the same
        magnitude. This is a real defect in some printouts so we
        normalize and surface it as a parse-time assumption note.
      * Trailing CR / DR indicators on individual cells (handled by
        ``_money``).
    """
    rows, fieldnames = _open_csv(path)
    idx = _index_headers(fieldnames)
    missing = [c for c in TB_REQUIRED if _norm_header(c) not in idx]
    # The report should have *some* form of debit/credit OR a net balance.
    has_debit = any(_norm_header(h) in idx for h in ("debit_balance", "debit", "debit_amount"))
    has_credit = any(_norm_header(h) in idx for h in ("credit_balance", "credit", "credit_amount"))
    has_net = any(
        _norm_header(h) in idx
        for h in ("net_balance", "balance", "amount", "ending_balance", "closing_balance")
    )
    if not (has_debit or has_credit or has_net):
        missing.append("debit_balance/credit_balance or net_balance")

    has_num_col = _norm_header("account_number") in idx
    has_name_col = _norm_header("account_name") in idx
    combined_key = None
    for alias in ("account", "gl_account", "ledger_account"):
        if _norm_header(alias) in idx:
            combined_key = _norm_header(alias)
            break

    normalized = []
    for row in rows:
        account_number = _pick(row, idx, "account_number", "acct_num", "number")
        account_name = _pick(row, idx, "account_name", "name")
        if (not account_number or not account_name) and combined_key is not None:
            combined = _pick(row, idx, "account", "gl_account", "ledger_account")
            num_from_combined, name_from_combined = _split_combined_account(combined)
            account_number = account_number or num_from_combined
            account_name = account_name or name_from_combined
        debit = _money(_pick(row, idx, "debit_balance", "debit", "debit_amount"))
        credit = _money(_pick(row, idx, "credit_balance", "credit", "credit_amount"))
        # Negative-debit normalization. Defensive — keeps the totals row
        # honest when an export reports "Debit: -1234.00, Credit: 0.00".
        if debit < 0:
            credit += -debit
            debit = Decimal("0.00")
        if credit < 0:
            debit += -credit
            credit = Decimal("0.00")
        if debit == 0 and credit == 0 and has_net:
            net = _money(_pick(
                row, idx,
                "net_balance", "balance", "amount",
                "ending_balance", "closing_balance",
            ))
            if net > 0:
                debit = net
            elif net < 0:
                credit = -net
        as_of = _pick(row, idx, "as_of_date", "period", "period_end", "date")
        normalized.append(
            {
                "account_number": account_number,
                "account_name": account_name,
                "debit_balance": f"{debit:.2f}",
                "credit_balance": f"{credit:.2f}",
                "as_of_date": as_of,
            }
        )
    if combined_key is not None:
        derived_num = any(r.get("account_number") for r in normalized)
        derived_name = any(r.get("account_name") for r in normalized)
        if derived_num and "account_number" in missing:
            missing.remove("account_number")
        if derived_name and "account_name" in missing:
            missing.remove("account_name")
    return normalized, fieldnames, missing


def parse_trust_listing(path) -> tuple[list[dict], list[str], list[str]]:
    rows, fieldnames = _open_csv(path)
    idx = _index_headers(fieldnames)
    missing = [c for c in TRUST_REQUIRED if _norm_header(c) not in idx]
    has_client = any(
        _norm_header(h) in idx for h in ("client_id", "client_name", "client", "matter_id", "matter_name", "matter")
    )
    if not has_client:
        missing.append("client_id or matter_id (any client/matter identifier)")

    normalized = []
    for row in rows:
        client_id = _pick(row, idx, "client_id", "client_no", "client_number")
        client_name = _pick(row, idx, "client_name", "client")
        matter_id = _pick(row, idx, "matter_id", "matter_no", "matter_number")
        matter_name = _pick(row, idx, "matter_name", "matter")
        trust_bank = _pick(row, idx, "trust_bank_account", "trust_account", "bank_account", "trust_bank")
        balance = _money(_pick(row, idx, "trust_balance", "balance", "amount"))
        as_of = _pick(row, idx, "as_of_date", "as_of", "date", "period_end")
        normalized.append(
            {
                "client_id": client_id,
                "client_name": client_name,
                "matter_id": matter_id,
                "matter_name": matter_name,
                "trust_bank_account": trust_bank,
                "trust_balance": f"{balance:.2f}",
                "as_of_date": as_of,
            }
        )
    return normalized, fieldnames, missing


# --- preflights ------------------------------------------------------------


def build_coa_preflight(rows: list[dict], fieldnames: list[str], missing: list[str]) -> dict:
    """Counts + warnings for a parsed Chart of Accounts file."""
    rows = rows or []
    type_counts: dict[str, int] = {}
    rows_missing_name = 0
    rows_missing_type = 0
    seen_numbers: dict[str, int] = {}
    duplicates: list[str] = []
    inactive = 0
    parent_links = 0
    for r in rows:
        t = (r.get("account_type") or "").strip() or "(unspecified)"
        type_counts[t] = type_counts.get(t, 0) + 1
        if not (r.get("account_name") or "").strip():
            rows_missing_name += 1
        if not (r.get("account_type") or "").strip():
            rows_missing_type += 1
        if not r.get("active"):
            inactive += 1
        num = (r.get("account_number") or "").strip()
        if num:
            seen_numbers[num] = seen_numbers.get(num, 0) + 1
        if (r.get("parent_account_number") or r.get("parent_account_name") or "").strip():
            parent_links += 1
    for num, count in seen_numbers.items():
        if count > 1:
            duplicates.append(num)

    assumptions: list[str] = []
    if parent_links:
        assumptions.append(
            f"{parent_links} row(s) reference a parent account; the COA "
            "hierarchy preview will show how those would map in QuickBooks."
        )

    summary = {
        "report_type": REPORT_CHART_OF_ACCOUNTS,
        "report_label": REPORT_LABELS[REPORT_CHART_OF_ACCOUNTS],
        "account_count": len(rows),
        "type_counts": sorted(type_counts.items()),
        "rows_missing_name": rows_missing_name,
        "rows_missing_type": rows_missing_type,
        "inactive_account_count": inactive,
        "duplicate_account_numbers": sorted(duplicates),
        "parent_linked_count": parent_links,
        "assumptions": assumptions,
        "missing_required_columns": list(missing),
    }
    summary["ready"] = (
        not missing
        and rows_missing_name == 0
        and len(rows) > 0
        and not duplicates
    )
    return summary


def build_trial_balance_preflight(rows: list[dict], fieldnames: list[str], missing: list[str]) -> dict:
    rows = rows or []
    total_debit = Decimal("0.00")
    total_credit = Decimal("0.00")
    rows_missing_account = 0
    accounts = set()
    for r in rows:
        total_debit += _money(r.get("debit_balance"))
        total_credit += _money(r.get("credit_balance"))
        num = (r.get("account_number") or "").strip()
        name = (r.get("account_name") or "").strip()
        if not (num or name):
            rows_missing_account += 1
        else:
            accounts.add(num or name)
    delta = total_debit - total_credit
    balanced = (total_debit == total_credit) and len(rows) > 0
    summary = {
        "report_type": REPORT_TRIAL_BALANCE,
        "report_label": REPORT_LABELS[REPORT_TRIAL_BALANCE],
        "account_count": len(rows),
        "unique_account_count": len(accounts),
        "total_debit": f"{total_debit:.2f}",
        "total_credit": f"{total_credit:.2f}",
        "out_of_balance_amount": f"{delta:.2f}",
        "balanced": balanced,
        "rows_missing_account": rows_missing_account,
        "missing_required_columns": list(missing),
    }
    summary["ready"] = (
        not missing
        and balanced
        and rows_missing_account == 0
        and len(rows) > 0
    )
    return summary


def build_trust_listing_preflight(rows: list[dict], fieldnames: list[str], missing: list[str]) -> dict:
    rows = rows or []
    total_balance = Decimal("0.00")
    rows_missing_identifier = 0
    clients = set()
    matters = set()
    bank_accounts: dict[str, int] = {}
    negative_count = 0
    for r in rows:
        bal = _money(r.get("trust_balance"))
        total_balance += bal
        if bal < 0:
            negative_count += 1
        client = (r.get("client_id") or "").strip() or (r.get("client_name") or "").strip()
        matter = (r.get("matter_id") or "").strip() or (r.get("matter_name") or "").strip()
        if not (client or matter):
            rows_missing_identifier += 1
        if client:
            clients.add(client)
        if matter:
            matters.add(matter)
        bank = (r.get("trust_bank_account") or "").strip()
        if bank:
            bank_accounts[bank] = bank_accounts.get(bank, 0) + 1
    summary = {
        "report_type": REPORT_TRUST_LISTING,
        "report_label": REPORT_LABELS[REPORT_TRUST_LISTING],
        "row_count": len(rows),
        "client_count": len(clients),
        "matter_count": len(matters),
        "total_trust_balance": f"{total_balance:.2f}",
        "negative_balance_count": negative_count,
        "rows_missing_identifier": rows_missing_identifier,
        "trust_bank_accounts": sorted(bank_accounts.items()),
        "missing_required_columns": list(missing),
    }
    summary["ready"] = (
        not missing
        and rows_missing_identifier == 0
        and len(rows) > 0
        and negative_count == 0
    )
    return summary


# --- COA dry-run preview against QBO ---------------------------------------


def build_coa_dry_run_preview(coa_rows: list[dict], qbo_accounts_response: dict) -> dict:
    """Match parsed COA rows against a QBO accounts query response.

    Returns a JSON-friendly preview suitable for the job-detail page. Does
    not call any QBO write endpoint.

    Match precedence (best-effort, deterministic):
      1. AcctNum exact match on account_number.
      2. Name exact match on account_name (case-insensitive).
    """
    coa_rows = coa_rows or []
    qbo_accounts = (qbo_accounts_response or {}).get("QueryResponse", {}).get("Account", []) or []

    by_acctnum = {}
    by_name_lower = {}
    for a in qbo_accounts:
        if a.get("AcctNum"):
            by_acctnum[str(a["AcctNum"]).strip()] = a
        if a.get("Name"):
            by_name_lower[str(a["Name"]).strip().lower()] = a

    matched = []
    would_create = []
    conflicts = []  # name matches with different AcctNum, etc.
    for r in coa_rows:
        num = (r.get("account_number") or "").strip()
        name = (r.get("account_name") or "").strip()
        matched_account = None
        match_basis = None
        if num and num in by_acctnum:
            matched_account = by_acctnum[num]
            match_basis = "AcctNum"
        elif name and name.lower() in by_name_lower:
            matched_account = by_name_lower[name.lower()]
            match_basis = "Name"

        entry = {
            "account_number": num,
            "account_name": name,
            "account_type": r.get("account_type"),
            "detail_type": r.get("detail_type"),
            "active": bool(r.get("active", True)),
        }
        if matched_account:
            entry["qbo_account_id"] = matched_account.get("Id")
            entry["qbo_account_name"] = matched_account.get("Name")
            entry["qbo_acct_num"] = matched_account.get("AcctNum")
            entry["qbo_account_type"] = matched_account.get("AccountType")
            entry["match_basis"] = match_basis
            matched.append(entry)
            # Detect a soft conflict: matched by name but AcctNum differs.
            if (
                match_basis == "Name"
                and num
                and matched_account.get("AcctNum")
                and str(matched_account["AcctNum"]).strip() != num
            ):
                conflicts.append({
                    **entry,
                    "reason": (
                        f"PCLaw account number {num} differs from QBO AcctNum "
                        f"{matched_account.get('AcctNum')} for the same name."
                    ),
                })
        else:
            would_create.append(entry)

    return {
        "report_type": REPORT_CHART_OF_ACCOUNTS,
        "report_label": REPORT_LABELS[REPORT_CHART_OF_ACCOUNTS],
        "coa_row_count": len(coa_rows),
        "qbo_account_count": len(qbo_accounts),
        "matched_count": len(matched),
        "would_create_count": len(would_create),
        "conflict_count": len(conflicts),
        "matched": matched,
        "would_create": would_create,
        "conflicts": conflicts,
    }
