"""Post-push GL reconciliation: PCLaw source vs what QBO now holds.

This packages the two-layer verification worked out in GLCheckr against the
demo company (validated line-for-line against Cesar's manual Jun-2022 TB
check) into a pure module the app can call after each GL push:

  1. High-level: ending balance per account per month, PCLaw vs QBO.
     Catches wrong-account postings that file-level totals never see
     (e.g. the 0025<->4156 bank transfers, the 5260/5460 insurance swap).

  2. Line-by-line drill-down for every account-month that introduced a
     variance: transactions matched PCLaw<->QBO by (entry number, amount),
     then (source-journal token, amount), then (amount). The unmatched
     remainder from each side IS the fix list, with PCLaw entry numbers
     the operator can search for in QBO.

Design mirrors ``tb_reconciliation.py``: pure (no Flask, no QBO HTTP, no
pandas), Decimal money, list-of-dict rows in, dict report out, 1-cent
tolerance.

Row shapes
----------
PCLaw GL rows (from pclaw_pipeline / the monthly export parser)::

    {"date": "2022-05-23" (ISO) or datetime, "account_number": "5320",
     "account_name": "Maintenance/Repair", "debit": "7000.00",
     "credit": "", "memo"/"source_journal": "GB",
     "transaction_id"/"entry_number": "270955",
     "vendor_name": "...", "description": "..."}

QBO rows (from the JournalEntry lines the app posted, or a parsed GL
report export **already converted to debit-positive signs**)::

    {"date": ..., "account_number": "5320" (or "" when QBO lacks AcctNum),
     "account_name": "Maintenance/Repair", "amount": "-7000.00" signed
     debit-positive OR separate "debit"/"credit",
     "doc_number": "270955" or "GROUPGB270955270956"}

Openings: {"account_number", "account_name", "balance"} signed, debit +.

Reserved-account awareness (reserved_accounts.py): "Net Income (Loss)" and
the "-PCLaw" holding accounts are matched to each other and flagged
``equity-rollforward`` instead of raw variance, because QBO auto-rolls FY
net income into native Retained Earnings and holds migrated balances in
the holding accounts.
"""

from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional

TOLERANCE = Decimal("0.01")

KNOWN_SOURCE_JOURNALS: tuple[str, ...] = (
    "GB", "GL", "GJ", "CER", "SJ", "AR", "AP", "TR", "BR", "PJ", "CR", "CD", "TB",
)

# Account-name fragments that identify equity accounts QBO computes or the
# migration holds in "-PCLaw" accounts. Compared as a family, not per-row.
_EQUITY_FAMILY_TOKENS = ("net income", "retained earnings")
_HOLDING_SUFFIXES = ("-pclaw", "-pc law")


def _money(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    s = str(value).replace(",", "").replace("$", "").strip()
    if not s or s in {"-", "--"}:
        return Decimal("0.00")
    try:
        d = Decimal(s)
    except Exception:  # noqa: BLE001
        return Decimal("0.00")
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _month_key(value) -> str:
    """'2022-05' from an ISO date string / datetime / PCLaw 'May 23/22'."""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m")
    s = str(value or "").strip()
    if len(s) >= 7 and s[4] == "-":
        return s[:7]
    # PCLaw "Mmm d/yy" dialect
    import datetime
    for fmt in ("%b %d/%y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    return s[:7]


def _acct_key(row: dict) -> str:
    num = str(row.get("account_number") or "").strip()
    if num.endswith(".0"):
        num = num[:-2]
    if num:
        return num
    return "~" + str(row.get("account_name") or "").strip().casefold()


def _row_net(row: dict) -> Decimal:
    if row.get("amount") not in (None, ""):
        return _money(row.get("amount"))
    return _money(row.get("debit")) - _money(row.get("credit"))


def source_journal_token(row: dict) -> str:
    """PCLaw source-journal token, or the token embedded in a cutovr
    GROUP<token>... doc number on the QBO side."""
    for key in ("source_journal", "memo", "journal"):
        tok = str(row.get(key) or "").strip().upper()
        if tok in KNOWN_SOURCE_JOURNALS:
            return tok
    doc = str(row.get("doc_number") or "").strip().upper()
    if doc.startswith("GROUP"):
        rest = doc[5:]
        for tok in sorted(KNOWN_SOURCE_JOURNALS, key=len, reverse=True):
            if rest.startswith(tok):
                return tok
    return ""


def _entry_id(row: dict) -> str:
    for key in ("transaction_id", "entry_number", "doc_number", "entry"):
        v = str(row.get(key) or "").strip()
        if v.endswith(".0"):
            v = v[:-2]
        if v:
            return v
    return ""


def _is_equity_family(name: str) -> bool:
    n = str(name or "").casefold()
    return any(tok in n for tok in _EQUITY_FAMILY_TOKENS)


def _strip_holding_suffix(name: str) -> str:
    n = str(name or "").strip()
    low = n.casefold()
    for suf in _HOLDING_SUFFIXES:
        if low.endswith(suf):
            return n[: len(n) - len(suf)].strip()
    return n


def resolve_qbo_accounts(qbo_rows: list[dict], pclaw_rows: list[dict],
                         pclaw_openings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Attach PCLaw account numbers to QBO rows that lack them, by
    normalized name (QBO accounts created outside the mapping have no
    AcctNum — root cause #1 in RECOMMENDATIONS.md). '-PCLaw' holding
    accounts resolve to their source account's name. Returns
    (rows, resolution_log)."""
    names: dict[str, str] = {}
    for r in list(pclaw_openings) + list(pclaw_rows):
        num = _acct_key(r)
        nm = str(r.get("account_name") or "").strip().casefold()
        if nm and not num.startswith("~") and nm not in names:
            names[nm] = num

    log: list[dict] = []
    out: list[dict] = []
    seen: dict[str, Optional[str]] = {}
    for r in qbo_rows:
        key = _acct_key(r)
        if not key.startswith("~"):
            out.append(r)
            continue
        raw_name = str(r.get("account_name") or "").strip()
        if raw_name not in seen:
            base = _strip_holding_suffix(raw_name)
            hit = names.get(raw_name.casefold()) or names.get(base.casefold())
            seen[raw_name] = hit
            log.append({
                "qbo_account": raw_name,
                "resolution": (f"name match -> {hit}" if hit
                               else "unresolved (no PCLaw counterpart)"),
            })
        hit = seen[raw_name]
        if hit:
            r = dict(r)
            r["account_number"] = hit
        out.append(r)
    return out, log


def _balances_by_month(rows: Iterable[dict], openings: Iterable[dict]
                       ) -> tuple["OrderedDict[str, dict]", list[str]]:
    """Per-account: opening + cumulative net per month.
    Returns (accounts, sorted_months). accounts[key] =
    {"name": ..., "opening": Decimal, "activity": {month: Decimal}}."""
    accounts: "OrderedDict[str, dict]" = OrderedDict()
    months: set[str] = set()

    def bucket(key: str, name: str) -> dict:
        b = accounts.get(key)
        if b is None:
            b = {"name": name, "opening": Decimal("0.00"), "activity": {}}
            accounts[key] = b
        if name and not b["name"]:
            b["name"] = name
        return b

    for r in openings or []:
        b = bucket(_acct_key(r), str(r.get("account_name") or "").strip())
        b["opening"] += _money(r.get("balance"))
    for r in rows or []:
        key = _acct_key(r)
        if key == "~":
            continue
        b = bucket(key, str(r.get("account_name") or "").strip())
        m = _month_key(r.get("date"))
        months.add(m)
        b["activity"][m] = b["activity"].get(m, Decimal("0.00")) + _row_net(r)
    return accounts, sorted(months)


def _match_transactions(p_rows: list[dict], q_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """3-pass greedy match. Returns (pclaw_unmatched, qbo_unmatched)."""
    q_free = list(range(len(q_rows)))

    def claim(pred) -> Optional[int]:
        for idx, qi in enumerate(q_free):
            if pred(q_rows[qi]):
                return q_free.pop(idx)
        return None

    p_unmatched: list[dict] = []
    remaining: list[dict] = []
    for pr in p_rows:  # pass 1: entry id + amount
        entry, amt = _entry_id(pr), _row_net(pr)
        if entry and claim(lambda q: _entry_id(q) == entry and _row_net(q) == amt) is not None:
            continue
        remaining.append(pr)
    second: list[dict] = []
    for pr in remaining:  # pass 2: source-journal token + amount
        tok, amt = source_journal_token(pr), _row_net(pr)
        if tok and claim(lambda q: source_journal_token(q) == tok and _row_net(q) == amt) is not None:
            continue
        second.append(pr)
    for pr in second:  # pass 3: amount only
        amt = _row_net(pr)
        if claim(lambda q: _row_net(q) == amt) is None:
            p_unmatched.append(pr)
    q_unmatched = [q_rows[qi] for qi in q_free]
    return p_unmatched, q_unmatched


def build_post_push_reconciliation(
    pclaw_rows: list[dict],
    qbo_rows: list[dict],
    pclaw_openings: Optional[list[dict]] = None,
    qbo_openings: Optional[list[dict]] = None,
    tolerance: Decimal = TOLERANCE,
) -> dict:
    """Two-layer reconciliation. Returns::

        {"summary": {...}, "detail": [...], "drilldown": [...],
         "resolution_log": [...], "overall_pass": bool}

    detail rows: one per account per month with pclaw_balance, qbo_balance,
    variance, and status: "match" | "diff" | "pclaw-only" | "qbo-only" |
    "equity-rollforward".
    drilldown rows: for each account-month that INTRODUCED a variance, the
    unmatched transactions from each side (side "pclaw-only"/"qbo-only")
    plus "opening-difference" rows, each carrying the source-journal token
    and PCLaw entry / QBO doc number.
    """
    pclaw_openings = pclaw_openings or []
    qbo_openings = qbo_openings or []
    qbo_rows, resolution_log = resolve_qbo_accounts(
        list(qbo_rows or []), list(pclaw_rows or []), pclaw_openings)

    p_accts, p_months = _balances_by_month(pclaw_rows, pclaw_openings)
    q_accts, q_months = _balances_by_month(qbo_rows, qbo_openings)
    months = sorted(set(p_months) | set(q_months))
    all_keys = sorted(set(p_accts) | set(q_accts))

    # index transactions per (account, month) for the drill-down
    def index_rows(rows):
        out: dict[tuple[str, str], list[dict]] = {}
        for r in rows or []:
            key = (_acct_key(r), _month_key(r.get("date")))
            out.setdefault(key, []).append(r)
        return out

    p_by_am = index_rows(pclaw_rows)
    q_by_am = index_rows(qbo_rows)

    detail: list[dict] = []
    drilldown: list[dict] = []
    diff_accounts: set[str] = set()

    for key in all_keys:
        p = p_accts.get(key)
        q = q_accts.get(key)
        name = (p or q or {}).get("name", "")
        equity = _is_equity_family(name)
        p_bal = (p or {}).get("opening", Decimal("0.00"))
        q_bal = (q or {}).get("opening", Decimal("0.00"))
        prev_var = Decimal("0.00")
        opening_var = p_bal - q_bal
        for m in months:
            p_bal += (p or {"activity": {}})["activity"].get(m, Decimal("0.00"))
            q_bal += (q or {"activity": {}})["activity"].get(m, Decimal("0.00"))
            variance = (p_bal - q_bal).quantize(Decimal("0.01"))
            if equity:
                status = "equity-rollforward"
            elif p is None:
                status = "qbo-only"
            elif q is None:
                status = "pclaw-only"
            elif abs(variance) <= tolerance:
                status = "match"
            else:
                status = "diff"
            detail.append({
                "account_number": key.lstrip("~"),
                "account_name": name,
                "month": m,
                "pclaw_balance": f"{p_bal:.2f}",
                "qbo_balance": f"{q_bal:.2f}",
                "variance": f"{variance:.2f}",
                "status": status,
            })
            if status in ("diff", "pclaw-only", "qbo-only"):
                diff_accounts.add(key)
            # drill into the month that introduced / changed the variance
            introduced = abs(variance - prev_var) > tolerance
            if status == "diff" and introduced:
                if m == months[0] and abs(opening_var) > tolerance:
                    drilldown.append({
                        "account_number": key.lstrip("~"), "account_name": name,
                        "month": m, "side": "opening-difference",
                        "date": "", "amount": f"{opening_var:.2f}",
                        "journal": "", "entry": "",
                        "detail": "PCLaw vs QBO opening balances differ",
                    })
                p_un, q_un = _match_transactions(
                    p_by_am.get((key, m), []), q_by_am.get((key, m), []))
                for side, rows in (("pclaw-only", p_un), ("qbo-only", q_un)):
                    for u in rows:
                        drilldown.append({
                            "account_number": key.lstrip("~"),
                            "account_name": name,
                            "month": m,
                            "side": side,
                            "date": str(u.get("date") or ""),
                            "amount": f"{_row_net(u):.2f}",
                            "journal": source_journal_token(u),
                            "entry": _entry_id(u),
                            "detail": " | ".join(x for x in (
                                str(u.get("vendor_name") or u.get("name") or "").strip(),
                                str(u.get("description") or "").strip()) if x),
                        })
            prev_var = variance

    n_diff = sum(1 for d in detail if d["status"] == "diff")
    overall_pass = n_diff == 0 and not any(
        d["status"] in ("pclaw-only", "qbo-only") and
        abs(Decimal(d["variance"])) > tolerance for d in detail)
    last_month = months[-1] if months else ""
    total_final_var = sum(
        abs(Decimal(d["variance"])) for d in detail
        if d["month"] == last_month and d["status"] not in ("equity-rollforward",))

    return {
        "summary": {
            "accounts_compared": len(all_keys),
            "accounts_with_differences": len(diff_accounts),
            "account_months_flagged": n_diff,
            "drilldown_rows": len(drilldown),
            "total_final_variance": f"{total_final_var:.2f}",
            "months": months,
            "overall_pass": overall_pass,
        },
        "detail": detail,
        "drilldown": drilldown,
        "resolution_log": resolution_log,
        "overall_pass": overall_pass,
        "limitation": (
            "QBO balances are built from the rows supplied (posted JEs or a "
            "parsed GL export converted to debit-positive signs). Equity "
            "accounts (Retained Earnings / Net Income families, -PCLaw "
            "holding accounts) are flagged equity-rollforward, not raw "
            "variances, because QBO rolls fiscal-year net income into "
            "Retained Earnings implicitly."
        ),
    }
