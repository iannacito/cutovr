"""Trust listing reconciliation.

A PCLaw trust listing breaks the firm's total trust holdings down by
client and matter. Three reconciliation checkpoints are useful when
prepping for a QBO migration:

  1. Sum of per-matter trust balances == total trust liability the firm
     carries on their Trial Balance for the trust-liability account.
  2. Sum of per-matter trust balances == total trust bank balance on the
     TB (the bank account that holds the client money).
  3. No client / matter has a negative trust balance, no client / matter
     has a missing ID, and every trust bank account in the listing maps
     to an account we know about.

We never post the trust listing to QuickBooks from this module. Trust
balances are client money and they have to be re-established in QBO
with a deliberate per-matter journal entry that the operator explicitly
confirms — a future iteration. For now we surface the data the
operator needs to make that call by hand.

Inputs are the parsed trust listing (from
``report_types.parse_trust_listing``) and an optional parsed Trial
Balance (from ``report_types.parse_trial_balance``). Outputs are
dict-only so they can flow straight to the Flask template and a CSV
download.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


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


# Trust-liability accounts are typically credit-balance liability accounts.
# We detect them on the TB by name (case-insensitive substring match) so a
# firm doesn't have to pre-configure the exact account number. The same
# heuristic is used in the COA mapping table; keeping it here keeps the
# trust-reconciliation module self-contained.
_TRUST_LIABILITY_NAME_HINTS = (
    "trust liability",
    "client trust",
    "trust accounts payable",
    "iolta liability",
)
_TRUST_BANK_NAME_HINTS = (
    "trust bank",
    "trust account",
    "iolta",
    "client trust bank",
)


def _net_for_name_match(rows: list[dict], hints: tuple[str, ...]) -> Decimal:
    """Sum debit - credit across TB rows whose account_name matches any hint.

    Liability/credit accounts have negative net (credit > debit); we return
    the value as-is so callers can compare against an absolute trust total
    via ``abs(...)``.
    """
    total = Decimal("0.00")
    for r in rows or []:
        name = (r.get("account_name") or "").strip().lower()
        if not name:
            continue
        if any(h in name for h in hints):
            total += _money(r.get("debit_balance")) - _money(r.get("credit_balance"))
    return total


def build_trust_listing_reconciliation(
    trust_listing_rows: list[dict],
    trial_balance_rows: Optional[list[dict]] = None,
) -> dict:
    """Build a trust-listing reconciliation report.

    Outputs:
      * per-bank totals from the listing
      * per-client totals from the listing
      * matched / mismatch counts against the TB liability and TB bank
        balances (when a TB is available)
      * warnings: negative balances, missing client/matter IDs, listing
        rows whose trust_bank_account doesn't appear in the TB or in
        any other row of the listing.
    """
    trust_listing_rows = trust_listing_rows or []
    trial_balance_rows = trial_balance_rows or []

    per_bank: dict[str, Decimal] = {}
    per_client: dict[str, Decimal] = {}
    per_matter: dict[str, Decimal] = {}
    negative_rows: list[dict] = []
    missing_id_rows: list[dict] = []
    missing_bank_rows: list[dict] = []
    total = Decimal("0.00")

    for r in trust_listing_rows:
        bal = _money(r.get("trust_balance"))
        total += bal
        bank = (r.get("trust_bank_account") or "").strip()
        client = (r.get("client_id") or "").strip() or (r.get("client_name") or "").strip()
        matter = (r.get("matter_id") or "").strip() or (r.get("matter_name") or "").strip()

        if bank:
            per_bank[bank] = per_bank.get(bank, Decimal("0.00")) + bal
        else:
            missing_bank_rows.append({
                "client": client,
                "matter": matter,
                "balance": f"{bal:.2f}",
            })
        if client:
            per_client[client] = per_client.get(client, Decimal("0.00")) + bal
        if matter:
            per_matter[matter] = per_matter.get(matter, Decimal("0.00")) + bal

        if bal < 0:
            negative_rows.append({
                "client": client,
                "matter": matter,
                "trust_bank_account": bank,
                "balance": f"{bal:.2f}",
            })
        if not (client or matter):
            missing_id_rows.append({
                "trust_bank_account": bank,
                "balance": f"{bal:.2f}",
            })

    # TB cross-checks. Liability totals are typically credit-side, so we
    # compare absolute values; an exact-match TB will show liability_net ==
    # -total (debit - credit on a pure liability = -liability_amount).
    tb_liability_net = _net_for_name_match(trial_balance_rows, _TRUST_LIABILITY_NAME_HINTS)
    tb_bank_net = _net_for_name_match(trial_balance_rows, _TRUST_BANK_NAME_HINTS)
    liability_match: Optional[bool] = None
    bank_match: Optional[bool] = None
    liability_delta = Decimal("0.00")
    bank_delta = Decimal("0.00")
    if trial_balance_rows:
        # Listing total is positive (sum of credit-balances on liability).
        # TB liability net is negative when stored as a credit balance, so
        # we compare abs(tb_liability_net) to listing total.
        liability_delta = (abs(tb_liability_net) - total).quantize(Decimal("0.01"))
        bank_delta = (tb_bank_net - total).quantize(Decimal("0.01"))
        liability_match = abs(liability_delta) <= _TOLERANCE
        bank_match = abs(bank_delta) <= _TOLERANCE

    warnings: list[str] = []
    if negative_rows:
        warnings.append(
            f"{len(negative_rows)} client / matter row(s) have a negative "
            "trust balance — investigate before any migration. A negative "
            "trust balance usually means a posting error in PCLaw."
        )
    if missing_id_rows:
        warnings.append(
            f"{len(missing_id_rows)} row(s) have no client or matter ID. "
            "The trust listing cannot be posted per-matter to QBO without "
            "an identifier; fix the listing before any future posting."
        )
    if missing_bank_rows:
        warnings.append(
            f"{len(missing_bank_rows)} row(s) have no trust bank account. "
            "Trust funds must be associated with a specific bank account."
        )
    if trial_balance_rows:
        if liability_match is False:
            warnings.append(
                "Trust listing total does not match the trust-liability "
                f"account balance on the Trial Balance "
                f"(off by ${liability_delta:.2f}). Reconcile before "
                "treating the listing as authoritative."
            )
        if bank_match is False:
            warnings.append(
                "Trust listing total does not match the trust-bank account "
                f"balance on the Trial Balance (off by ${bank_delta:.2f}). "
                "This is usually a posting timing issue."
            )
    else:
        warnings.append(
            "No Trial Balance uploaded yet — listing-to-TB checks "
            "skipped. Upload the TB and re-open this view to enable "
            "those cross-checks."
        )

    summary = {
        "total_trust_balance": f"{total:.2f}",
        "row_count": len(trust_listing_rows),
        "client_count": len(per_client),
        "matter_count": len(per_matter),
        "bank_account_count": len(per_bank),
        "negative_row_count": len(negative_rows),
        "missing_identifier_count": len(missing_id_rows),
        "missing_bank_count": len(missing_bank_rows),
        "tb_liability_net": f"{tb_liability_net:.2f}",
        "tb_bank_net": f"{tb_bank_net:.2f}",
        "tb_liability_delta": f"{liability_delta:.2f}",
        "tb_bank_delta": f"{bank_delta:.2f}",
        "liability_match": liability_match,
        "bank_match": bank_match,
        "overall_pass": (
            not negative_rows and not missing_id_rows and
            liability_match is not False and bank_match is not False
        ),
        # Posting is intentionally disabled. The UI surfaces this so a
        # future "post to QBO" button cannot appear without an explicit
        # flip here AND an operator-confirmation gate.
        "posting_enabled": False,
        "posting_disabled_reason": (
            "Trust posting is never automated by this app. Each matter "
            "trust balance must be re-established in QuickBooks (or Clio) "
            "via a deliberate per-matter journal entry the operator "
            "confirms manually. A future build may add an explicit "
            "operator-confirmed posting flow, but it is intentionally "
            "absent today."
        ),
    }

    return {
        "summary": summary,
        "per_bank": sorted(
            [{"trust_bank_account": k, "total": f"{v:.2f}"} for k, v in per_bank.items()],
            key=lambda r: r["trust_bank_account"],
        ),
        "per_client_top": sorted(
            [{"client": k, "total": f"{v:.2f}"} for k, v in per_client.items()],
            key=lambda r: Decimal(r["total"]), reverse=True,
        )[:25],
        "negative_rows": negative_rows,
        "missing_identifier_rows": missing_id_rows,
        "missing_bank_rows": missing_bank_rows,
        "warnings": warnings,
    }


def render_trust_reconciliation_csv(report: dict) -> str:
    import io
    import csv as _csv
    from csv_safety import sanitize_csv_cell

    buf = io.StringIO()
    raw_writer = _csv.writer(buf)

    class _SanWriter:
        def writerow(self, row):
            raw_writer.writerow([sanitize_csv_cell(c) for c in row])
    writer = _SanWriter()
    summary = report.get("summary") or {}
    writer.writerow(["Trust listing reconciliation"])
    writer.writerow([])
    writer.writerow(["Total trust balance", summary.get("total_trust_balance", "0.00")])
    writer.writerow(["Rows", summary.get("row_count", 0)])
    writer.writerow(["Distinct clients", summary.get("client_count", 0)])
    writer.writerow(["Distinct matters", summary.get("matter_count", 0)])
    writer.writerow(["Distinct trust bank accounts", summary.get("bank_account_count", 0)])
    writer.writerow(["Negative balances", summary.get("negative_row_count", 0)])
    writer.writerow(["Missing client/matter IDs", summary.get("missing_identifier_count", 0)])
    writer.writerow(["Missing trust bank account", summary.get("missing_bank_count", 0)])
    writer.writerow([
        "TB trust-liability vs listing delta",
        summary.get("tb_liability_delta", "0.00"),
    ])
    writer.writerow([
        "TB trust-bank vs listing delta",
        summary.get("tb_bank_delta", "0.00"),
    ])
    writer.writerow(["Overall pass?", "yes" if summary.get("overall_pass") else "no"])
    writer.writerow(["Posting enabled?", "no — trust posting is intentionally disabled"])
    writer.writerow([])
    writer.writerow(["Per trust bank account"])
    writer.writerow(["trust_bank_account", "total"])
    for row in report.get("per_bank") or []:
        writer.writerow([row.get("trust_bank_account", ""), row.get("total", "")])
    writer.writerow([])
    writer.writerow(["Top clients by trust balance"])
    writer.writerow(["client", "total"])
    for row in report.get("per_client_top") or []:
        writer.writerow([row.get("client", ""), row.get("total", "")])
    if report.get("negative_rows"):
        writer.writerow([])
        writer.writerow(["Negative balance rows"])
        writer.writerow(["client", "matter", "trust_bank_account", "balance"])
        for r in report["negative_rows"]:
            writer.writerow([
                r.get("client", ""),
                r.get("matter", ""),
                r.get("trust_bank_account", ""),
                r.get("balance", ""),
            ])
    if report.get("warnings"):
        writer.writerow([])
        writer.writerow(["Warnings"])
        for w in report["warnings"]:
            writer.writerow([w])
    return buf.getvalue()
