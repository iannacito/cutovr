"""Ending Trial Balance reconciliation.

After a firm has posted their opening trial balance and the period GL,
the *ending* trial balance is the proof point: it should match the
balances QuickBooks now reflects for the same date.

This module compares an uploaded ending TB against the firm's other
parsed reports (opening TB, parsed General Ledger activity) to produce
a per-account pass/fail report. It does NOT call the QBO Reports API
to fetch QuickBooks's own TB — that integration is complex (requires
the Reports endpoint, accrual vs cash basis flags, date alignment, and
a careful taxonomy mapping), so for this iteration we expose a clearly
documented limitation and rely on the parsed inputs the firm already
gave us.

Expected balance per account = (opening TB net) + (GL activity net for
the period). "Net" here means debit - credit, so a positive net is a
debit-side balance.

Tolerance for floating-point / rounding: 1 cent. Anything inside the
tolerance is reported as a pass.

Output structure is dict-only so the Flask route can hand it straight
to the template and render the CSV download without any custom shape
gymnastics.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional


_TOLERANCE = Decimal("0.01")


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


def _row_key(row: dict) -> tuple[str, str]:
    """Composite (account_number, account_name_lower) key used to align
    accounts across reports. Account number is the primary identifier; the
    name is captured as a fallback for files that lack numbers.
    """
    num = (row.get("account_number") or "").strip()
    name = (row.get("account_name") or "").strip().lower()
    return num, name


def _resolve_key(row: dict, all_rows: list[dict]) -> tuple[str, str]:
    """Best-effort cross-report alignment. If the ending TB row has a number,
    we use that; otherwise we fall back to the name. The all_rows arg is
    reserved for fuzzy lookups but unused today — exact match is fine.
    """
    num, name = _row_key(row)
    return (num, name) if num else ("", name)


def _net(row: dict) -> Decimal:
    """Return debit - credit for a TB row. Positive = debit balance."""
    return _money(row.get("debit_balance")) - _money(row.get("credit_balance"))


def _gl_net_by_account(gl_rows: Optional[Iterable[dict]]) -> dict[tuple[str, str], Decimal]:
    """Aggregate parsed GL rows by account into a net change per account.

    GL row shape (from pclaw_pipeline.load_general_ledger_csv) has the
    flat ``account_number`` / ``account_name`` / ``debit`` / ``credit``
    columns. Net change = debit - credit; this is what shifts the TB
    balance over the period.
    """
    out: dict[tuple[str, str], Decimal] = {}
    for r in (gl_rows or []):
        num = (r.get("account_number") or "").strip()
        name = (r.get("account_name") or "").strip().lower()
        if not (num or name):
            continue
        key = (num, name) if num else ("", name)
        out[key] = out.get(key, Decimal("0.00")) + (
            _money(r.get("debit")) - _money(r.get("credit"))
        )
    return out


def build_ending_tb_reconciliation(
    ending_tb_rows: list[dict],
    opening_tb_rows: Optional[list[dict]] = None,
    gl_rows: Optional[Iterable[dict]] = None,
) -> dict:
    """Build a reconciliation report comparing ending TB to expected balances.

    Expected balance = opening TB net + GL period net (positive = debit).
    Each ending TB account is bucketed:

      * ``match``  — within $0.01 of expected
      * ``diff``   — off by more than $0.01
      * ``unexpected`` — appears in ending TB but not in opening TB or GL
      * ``missing``    — appears in opening TB or GL but not in ending TB

    The output also notes the limitation: we did NOT consult QuickBooks
    directly. The "expected" column is built from the firm's own parsed
    reports. If the GL we received hasn't been imported into QBO yet,
    "match" only proves the PCLaw files are internally consistent — not
    that QBO matches.
    """
    ending_tb_rows = ending_tb_rows or []
    opening_tb_rows = opening_tb_rows or []
    gl_rows = list(gl_rows or [])

    opening_by_key: dict[tuple[str, str], dict] = {}
    for r in opening_tb_rows:
        opening_by_key[_resolve_key(r, opening_tb_rows)] = r
    gl_net = _gl_net_by_account(gl_rows)
    ending_by_key: dict[tuple[str, str], dict] = {}
    for r in ending_tb_rows:
        ending_by_key[_resolve_key(r, ending_tb_rows)] = r

    rows_out: list[dict] = []
    matched_count = 0
    diff_count = 0
    unexpected_count = 0
    missing_count = 0
    total_abs_diff = Decimal("0.00")

    all_keys: set[tuple[str, str]] = set(ending_by_key) | set(opening_by_key) | set(gl_net)
    for key in sorted(all_keys):
        ending_row = ending_by_key.get(key)
        opening_row = opening_by_key.get(key)
        gl_delta = gl_net.get(key, Decimal("0.00"))

        opening_net = _net(opening_row) if opening_row else Decimal("0.00")
        expected = (opening_net + gl_delta).quantize(Decimal("0.01"))
        actual = _net(ending_row).quantize(Decimal("0.01")) if ending_row else None

        if ending_row is None:
            # Account appears in opening or GL but not in ending TB.
            # Only flag as missing when there's a non-zero expected balance.
            if expected != 0:
                missing_count += 1
                rows_out.append({
                    "account_number": key[0],
                    "account_name": (opening_row or {"account_name": ""}).get("account_name") or "",
                    "status": "missing",
                    "expected": f"{expected:.2f}",
                    "actual": "",
                    "difference": f"{(-expected):.2f}",
                    "opening_net": f"{opening_net:.2f}",
                    "gl_net": f"{gl_delta:.2f}",
                })
                total_abs_diff += abs(expected)
            continue

        if opening_row is None and key not in gl_net:
            # Ending TB introduces an account we've never seen before.
            unexpected_count += 1
            diff = actual - expected
            rows_out.append({
                "account_number": key[0],
                "account_name": (ending_row.get("account_name") or "").strip(),
                "status": "unexpected",
                "expected": f"{expected:.2f}",
                "actual": f"{actual:.2f}",
                "difference": f"{diff:.2f}",
                "opening_net": f"{opening_net:.2f}",
                "gl_net": f"{gl_delta:.2f}",
            })
            total_abs_diff += abs(diff)
            continue

        diff = (actual - expected).quantize(Decimal("0.01"))
        if abs(diff) <= _TOLERANCE:
            matched_count += 1
            status = "match"
        else:
            diff_count += 1
            status = "diff"
            total_abs_diff += abs(diff)
        rows_out.append({
            "account_number": key[0],
            "account_name": (ending_row.get("account_name") or "").strip(),
            "status": status,
            "expected": f"{expected:.2f}",
            "actual": f"{actual:.2f}",
            "difference": f"{diff:.2f}",
            "opening_net": f"{opening_net:.2f}",
            "gl_net": f"{gl_delta:.2f}",
        })

    ending_total_debit = sum(_money(r.get("debit_balance")) for r in ending_tb_rows)
    ending_total_credit = sum(_money(r.get("credit_balance")) for r in ending_tb_rows)
    opening_total_debit = sum(_money(r.get("debit_balance")) for r in opening_tb_rows)
    opening_total_credit = sum(_money(r.get("credit_balance")) for r in opening_tb_rows)

    overall_pass = diff_count == 0 and missing_count == 0
    return {
        "summary": {
            "matched_count": matched_count,
            "diff_count": diff_count,
            "unexpected_count": unexpected_count,
            "missing_count": missing_count,
            "row_count": len(rows_out),
            "total_abs_difference": f"{total_abs_diff:.2f}",
            "ending_total_debit": f"{ending_total_debit:.2f}",
            "ending_total_credit": f"{ending_total_credit:.2f}",
            "opening_total_debit": f"{opening_total_debit:.2f}",
            "opening_total_credit": f"{opening_total_credit:.2f}",
            "opening_tb_available": bool(opening_tb_rows),
            "gl_available": bool(gl_rows),
            "overall_pass": overall_pass,
        },
        "rows": rows_out,
        "limitation": (
            "This reconciliation compares the uploaded ending Trial Balance "
            "to expected balances built from the firm's own opening TB + "
            "parsed GL activity. It does NOT call the QuickBooks Reports "
            "API to fetch a live QBO Trial Balance — that integration is "
            "tracked as future work. A pass here only proves the PCLaw "
            "files are internally consistent; spot-check the matched "
            "accounts in QuickBooks before signing off."
        ),
    }


def render_ending_tb_reconciliation_csv(report: dict) -> str:
    """Render the reconciliation report as a CSV string.

    Every cell is passed through ``csv_safety.sanitize_csv_cell`` to
    neutralize spreadsheet-formula injection from PCLaw-supplied text
    (account names, etc).
    """
    import io
    import csv as _csv
    from csv_safety import sanitize_csv_cell

    def _sanitize_row(row):
        return [sanitize_csv_cell(c) for c in row]

    buf = io.StringIO()
    raw_writer = _csv.writer(buf)
    class _SanWriter:
        def writerow(self, row):
            raw_writer.writerow(_sanitize_row(row))
    writer = _SanWriter()
    summary = report.get("summary") or {}
    writer.writerow(["Ending Trial Balance reconciliation"])
    writer.writerow([])
    writer.writerow(["Matched", summary.get("matched_count", 0)])
    writer.writerow(["Differences", summary.get("diff_count", 0)])
    writer.writerow(["Unexpected (only in ending TB)", summary.get("unexpected_count", 0)])
    writer.writerow(["Missing (expected, not in ending TB)", summary.get("missing_count", 0)])
    writer.writerow(["Total absolute difference", summary.get("total_abs_difference", "0.00")])
    writer.writerow(["Opening TB available?", "yes" if summary.get("opening_tb_available") else "no"])
    writer.writerow(["GL activity available?", "yes" if summary.get("gl_available") else "no"])
    writer.writerow(["Overall pass?", "yes" if summary.get("overall_pass") else "no"])
    writer.writerow([])
    writer.writerow([
        "account_number", "account_name", "status",
        "opening_net", "gl_net", "expected", "actual", "difference",
    ])
    for row in report.get("rows") or []:
        writer.writerow([
            row.get("account_number", ""),
            row.get("account_name", ""),
            row.get("status", ""),
            row.get("opening_net", ""),
            row.get("gl_net", ""),
            row.get("expected", ""),
            row.get("actual", ""),
            row.get("difference", ""),
        ])
    writer.writerow([])
    writer.writerow(["Limitation"])
    writer.writerow([report.get("limitation", "")])
    return buf.getvalue()
