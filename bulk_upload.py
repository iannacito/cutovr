"""Bulk upload helpers.

This module supports a customer-facing flow where a firm uploads ALL of
their PCLaw exports in a single submission and the app figures out which
file is which. The existing single-file ``/upload`` flow stays untouched.

Responsibilities:

  * ``classify_csv`` — given a CSV file path and its original filename,
    return a ``ClassificationResult`` containing the detected report
    type (or ``None``), a confidence label, a short human-readable
    reason, and the parsed header list.
  * ``resolve_collisions`` — given a list of per-file classifications,
    flag duplicates of the same required report type as
    ``needs_review`` so we never silently overwrite earlier categorized
    uploads.
  * ``missing_required`` — given the categorized set, return the list
    of required PCLaw reports still missing for the migration workflow.

The classifier combines three independent signals:

  1. Header-based scoring (the existing ``detect_report_type`` from
     ``report_types``).
  2. Filename hints (e.g. ``opening_tb_2026.csv``, ``trust_listing.csv``,
     ``gl_jan_jun.csv``).
  3. Content patterns from the first ~20 data rows (e.g. presence of
     a ``transaction_id``-like column with non-empty values implies
     General Ledger; rows with ``client_id``/``matter_id`` plus a
     balance imply Trust Listing).

We deliberately treat the classifier as a *recommendation engine*, not
an authority. Anything below ``CONFIDENCE_MEDIUM`` is surfaced to the
customer as ``needs_review`` so a human can confirm before the workflow
moves forward. The upload route never overwrites or imports based on a
low-confidence guess.

Nothing in this module touches the database, QBO, or filesystem outside
the CSV path the caller hands in.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

from report_types import (
    REPORT_CHART_OF_ACCOUNTS,
    REPORT_GENERAL_LEDGER,
    REPORT_LABELS,
    REPORT_TRIAL_BALANCE,
    REPORT_TRUST_LISTING,
    REPORT_VENDOR_LIST,
    REPORT_CUSTOMER_LIST,
    REPORT_TYPES,
    detect_report_type,
    is_valid_report_type,
)


CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_NONE = "none"


STATUS_CATEGORIZED = "categorized"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_DUPLICATE = "duplicate"
STATUS_UNREADABLE = "unreadable"
STATUS_REJECTED = "rejected"


# Required reports for the migration workflow. Order matters: the
# customer-facing checklist surfaces what's still missing in this
# sequence.
REQUIRED_REPORTS = (
    REPORT_CHART_OF_ACCOUNTS,
    REPORT_TRIAL_BALANCE,       # opening trial balance
    REPORT_GENERAL_LEDGER,
    REPORT_TRUST_LISTING,
    REPORT_VENDOR_LIST,
    REPORT_CUSTOMER_LIST,
)


# Filename keyword -> report_type. Matched case-insensitive against the
# basename minus extension. Order matters: the most specific keyword
# wins on the first hit.
_FILENAME_HINTS: list[tuple[str, str]] = [
    # Chart of Accounts
    ("chart_of_accounts", REPORT_CHART_OF_ACCOUNTS),
    ("chart-of-accounts", REPORT_CHART_OF_ACCOUNTS),
    ("chartofaccounts", REPORT_CHART_OF_ACCOUNTS),
    ("coa", REPORT_CHART_OF_ACCOUNTS),
    # Trust Listing (must come before "trust" alone so we don't snag a
    # trust GL by accident — but "trust listing" is itself a subset
    # match for "trust", so we evaluate longer keywords first).
    ("trust_listing", REPORT_TRUST_LISTING),
    ("trust-listing", REPORT_TRUST_LISTING),
    ("trustlisting", REPORT_TRUST_LISTING),
    ("client_trust", REPORT_TRUST_LISTING),
    ("trust_balance", REPORT_TRUST_LISTING),
    ("trust_bank", REPORT_TRUST_LISTING),
    # Trial Balance (both opening and ending).
    ("trial_balance", REPORT_TRIAL_BALANCE),
    ("trial-balance", REPORT_TRIAL_BALANCE),
    ("trialbalance", REPORT_TRIAL_BALANCE),
    ("opening_tb", REPORT_TRIAL_BALANCE),
    ("opening-tb", REPORT_TRIAL_BALANCE),
    ("ending_tb", REPORT_TRIAL_BALANCE),
    ("ending-tb", REPORT_TRIAL_BALANCE),
    ("openingtb", REPORT_TRIAL_BALANCE),
    ("endingtb", REPORT_TRIAL_BALANCE),
    # General Ledger / transaction history.
    ("general_ledger", REPORT_GENERAL_LEDGER),
    ("general-ledger", REPORT_GENERAL_LEDGER),
    ("generalledger", REPORT_GENERAL_LEDGER),
    ("transaction_history", REPORT_GENERAL_LEDGER),
    ("transaction-history", REPORT_GENERAL_LEDGER),
    ("transactions", REPORT_GENERAL_LEDGER),
    ("journal", REPORT_GENERAL_LEDGER),
    ("_gl_", REPORT_GENERAL_LEDGER),
    ("_gl.", REPORT_GENERAL_LEDGER),
    # Vendor list (who you pay).
    ("vendor_list", REPORT_VENDOR_LIST),
    ("vendor-list", REPORT_VENDOR_LIST),
    ("vendorlist", REPORT_VENDOR_LIST),
    ("vendors", REPORT_VENDOR_LIST),
    ("payee_list", REPORT_VENDOR_LIST),
    # Customer / client list (who pays you).
    ("customer_list", REPORT_CUSTOMER_LIST),
    ("customer-list", REPORT_CUSTOMER_LIST),
    ("customerlist", REPORT_CUSTOMER_LIST),
    ("customers", REPORT_CUSTOMER_LIST),
    ("client_list", REPORT_CUSTOMER_LIST),
    ("client-list", REPORT_CUSTOMER_LIST),
    ("clientlist", REPORT_CUSTOMER_LIST),
    # Less-specific catch-alls run last.
    ("trust", REPORT_TRUST_LISTING),
    ("ledger", REPORT_GENERAL_LEDGER),
    ("balance", REPORT_TRIAL_BALANCE),
]


@dataclass
class ClassificationResult:
    """Per-file classification record returned by ``classify_csv``."""
    filename: str
    report_type: Optional[str]
    report_label: str = ""
    confidence: str = CONFIDENCE_NONE
    status: str = STATUS_NEEDS_REVIEW
    reason: str = ""
    headers: List[str] = field(default_factory=list)
    detector_signals: List[str] = field(default_factory=list)
    # Free-text warning surfaced to the customer in the review screen.
    # Empty when the file looks fine.
    warning: str = ""

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "report_type": self.report_type,
            "report_label": self.report_label,
            "confidence": self.confidence,
            "status": self.status,
            "reason": self.reason,
            "headers": list(self.headers),
            "detector_signals": list(self.detector_signals),
            "warning": self.warning,
        }


def _norm_filename_token(name: str) -> str:
    """Lowercase, drop the extension, normalize separators to single ``_``.

    "Opening TB - Q1 2026.csv" -> "opening_tb_q1_2026"
    """
    base = Path(name).stem.lower()
    out_chars: list[str] = []
    prev_us = False
    for ch in base:
        if ch.isalnum():
            out_chars.append(ch)
            prev_us = False
        else:
            if not prev_us:
                out_chars.append("_")
                prev_us = True
    return "".join(out_chars).strip("_")


def detect_report_type_from_filename(filename: str) -> Optional[str]:
    """Pure helper: return a report_type from a filename, or None."""
    token = _norm_filename_token(filename)
    if not token:
        return None
    decorated = f"_{token}_"
    for needle, rt in _FILENAME_HINTS:
        if needle in decorated or needle in token:
            return rt
    return None


def _read_headers_and_sample(path: Path, sample_rows: int = 20) -> tuple[list[str], list[dict]]:
    """Return (headers, sample rows) using the same encoding strategy as
    the existing parsers. Resilient to a few rows of preamble above the
    real header (common in PCLaw printouts)."""
    # First pass: try a forgiving CSV read.
    try:
        with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            rows = []
            for i, row in enumerate(reader):
                rows.append(row)
                if i >= 40:
                    break
        if not rows:
            return [], []
        # Pick the first row that looks like a header (has at least 2
        # non-empty cells and at least one alpha character).
        header_idx = 0
        for i, row in enumerate(rows[:20]):
            non_empty = [c for c in row if (c or "").strip()]
            if len(non_empty) >= 2 and any(any(ch.isalpha() for ch in c) for c in non_empty):
                header_idx = i
                break
        headers = [str(h or "").strip() for h in rows[header_idx]]
        sample = []
        for r in rows[header_idx + 1: header_idx + 1 + sample_rows]:
            if not any((c or "").strip() for c in r):
                continue
            d = {}
            for i, h in enumerate(headers):
                if h:
                    d[h] = r[i] if i < len(r) else ""
            sample.append(d)
        return headers, sample
    except Exception:
        return [], []


def _has_nonempty(rows: Iterable[dict], header: str) -> int:
    """Count how many sample rows have a non-empty value for ``header``."""
    count = 0
    for r in rows:
        v = (r.get(header) or "").strip() if isinstance(r, dict) else ""
        if v:
            count += 1
    return count


def _index_by_normalized_header(headers: Iterable[str]) -> dict:
    """Map normalized header (lowercase, alnum-only) -> original header."""
    idx: dict = {}
    for h in headers:
        norm = "".join(ch.lower() for ch in (h or "") if ch.isalnum())
        if norm and norm not in idx:
            idx[norm] = h
    return idx


def _content_score(headers: List[str], sample: List[dict]) -> dict:
    """Apply lightweight content patterns. Returns {report_type: score}."""
    norm = _index_by_normalized_header(headers)

    def col(*candidates: str) -> Optional[str]:
        for c in candidates:
            key = "".join(ch for ch in c.lower() if ch.isalnum())
            if key in norm:
                return norm[key]
        return None

    scores = {rt: 0 for rt in REPORT_TYPES}

    txid_col = col("transactionid", "trans_id", "txn_id")
    if txid_col:
        if _has_nonempty(sample, txid_col) > 0:
            scores[REPORT_GENERAL_LEDGER] += 5

    debit_col = col("debit", "debitbalance")
    credit_col = col("credit", "creditbalance")
    if debit_col and credit_col:
        # Both columns present: either GL (per-transaction) or TB
        # (per-account). Disambiguate via transaction_id and via
        # whether multiple rows share the same account_number.
        if not txid_col:
            scores[REPORT_TRIAL_BALANCE] += 2

    trust_balance_col = col("trustbalance")
    if trust_balance_col:
        scores[REPORT_TRUST_LISTING] += 3
    matter_id_col = col("matterid", "matter")
    client_id_col = col("clientid", "client")
    if matter_id_col and client_id_col:
        scores[REPORT_TRUST_LISTING] += 2

    type_col = col("accounttype", "type", "category", "pclawcategory")
    if type_col and not (debit_col and credit_col):
        scores[REPORT_CHART_OF_ACCOUNTS] += 2

    # Vendor / customer listings: name-bearing rows with no debit/credit and
    # no transaction_id. These are flat entity lists, not ledgers.
    if not (debit_col and credit_col) and not txid_col:
        if col("vendorname", "vendor", "payee"):
            scores[REPORT_VENDOR_LIST] += 3
        if col("customername", "customer", "clientname") and not trust_balance_col:
            scores[REPORT_CUSTOMER_LIST] += 3

    return scores


def classify_csv(path: Path, filename: str) -> ClassificationResult:
    """Classify a single CSV. Never raises — returns ``status=unreadable``
    when the file can't be parsed."""
    safe_name = filename or path.name
    headers, sample = _read_headers_and_sample(path)
    if not headers:
        return ClassificationResult(
            filename=safe_name,
            report_type=None,
            status=STATUS_UNREADABLE,
            confidence=CONFIDENCE_NONE,
            reason=(
                "Could not read CSV headers. Make sure the file is a "
                "PCLaw CSV export and try again."
            ),
        )

    header_detected = detect_report_type(headers)
    filename_detected = detect_report_type_from_filename(safe_name)
    content_scores = _content_score(headers, sample)

    signals: list[str] = []
    if header_detected:
        signals.append(f"headers→{REPORT_LABELS.get(header_detected, header_detected)}")
    if filename_detected:
        signals.append(f"filename→{REPORT_LABELS.get(filename_detected, filename_detected)}")
    best_content = max(content_scores.items(), key=lambda kv: kv[1])
    if best_content[1] > 0:
        signals.append(
            f"content→{REPORT_LABELS.get(best_content[0], best_content[0])} ({best_content[1]})"
        )

    # Score combine. Header detection is the strongest signal because
    # it inspects the structure of the file rather than its name.
    final_scores = {rt: 0 for rt in REPORT_TYPES}
    if header_detected:
        final_scores[header_detected] += 4
    if filename_detected:
        final_scores[filename_detected] += 2
    for rt, s in content_scores.items():
        if s > 0:
            final_scores[rt] += min(s, 4)

    best_rt, best_score = max(final_scores.items(), key=lambda kv: kv[1])
    if best_score <= 0:
        return ClassificationResult(
            filename=safe_name,
            report_type=None,
            status=STATUS_NEEDS_REVIEW,
            confidence=CONFIDENCE_NONE,
            reason=(
                "We couldn't identify this report. Set the report type "
                "manually below to continue."
            ),
            headers=headers,
            detector_signals=signals,
        )

    # Confidence buckets. High when at least two independent signals
    # agree (header + filename, or header + content). Medium when one
    # signal is decisive. Low when only a weak signal is present.
    agree_count = 0
    if header_detected == best_rt:
        agree_count += 1
    if filename_detected == best_rt:
        agree_count += 1
    if best_content[0] == best_rt and best_content[1] > 0:
        agree_count += 1

    if agree_count >= 2:
        confidence = CONFIDENCE_HIGH
        status = STATUS_CATEGORIZED
    elif header_detected == best_rt:
        confidence = CONFIDENCE_MEDIUM
        status = STATUS_CATEGORIZED
    elif filename_detected == best_rt and best_content[0] == best_rt:
        confidence = CONFIDENCE_MEDIUM
        status = STATUS_CATEGORIZED
    else:
        confidence = CONFIDENCE_LOW
        status = STATUS_NEEDS_REVIEW

    reason_parts = []
    if header_detected == best_rt:
        reason_parts.append("CSV headers matched")
    if filename_detected == best_rt:
        reason_parts.append("filename contained a known keyword")
    if best_content[0] == best_rt and best_content[1] > 0:
        reason_parts.append("data columns/values matched")
    reason = (
        "; ".join(reason_parts)
        if reason_parts
        else "best-guess match based on weak signals — please confirm"
    )

    return ClassificationResult(
        filename=safe_name,
        report_type=best_rt,
        report_label=REPORT_LABELS.get(best_rt, ""),
        confidence=confidence,
        status=status,
        reason=reason,
        headers=headers,
        detector_signals=signals,
    )


def resolve_collisions(
    results: List[ClassificationResult],
) -> List[ClassificationResult]:
    """Mark duplicate categorizations as ``needs_review`` so we never
    silently overwrite a previously categorized upload.

    Trial Balance is the one report a firm legitimately uploads twice
    (opening and ending), so duplicates of ``REPORT_TRIAL_BALANCE`` are
    annotated with a warning but allowed through.

    Returns the *same* list (mutated in place) for caller convenience.
    """
    seen: dict[str, int] = {}
    indices_by_type: dict[str, list[int]] = {}
    for i, r in enumerate(results):
        if not r.report_type or r.status != STATUS_CATEGORIZED:
            continue
        indices_by_type.setdefault(r.report_type, []).append(i)
        seen[r.report_type] = seen.get(r.report_type, 0) + 1

    for rt, indices in indices_by_type.items():
        if len(indices) <= 1:
            continue
        if rt == REPORT_TRIAL_BALANCE:
            # Two TB files is the expected pattern (opening + ending).
            # Annotate but keep both categorized.
            for i in indices:
                results[i].warning = (
                    "Multiple trial-balance files uploaded — typically one is "
                    "the opening TB and the other is the ending TB. Confirm "
                    "which is which on the next screen."
                )
        elif rt == REPORT_GENERAL_LEDGER:
            # A firm migrating a full year uploads one general ledger per
            # month, so several GL files in a batch is expected — they all
            # stand. Annotate so the operator can confirm the set is the
            # months they meant, but never block them as duplicates.
            for i in indices:
                results[i].warning = (
                    f"{len(indices)} general-ledger files uploaded — monthly "
                    "ledgers are expected and will all be kept. Confirm these "
                    "are the months you meant on the next screen."
                )
        else:
            # Any other duplicate is suspicious; require human review.
            for i in indices:
                results[i].status = STATUS_DUPLICATE
                results[i].confidence = CONFIDENCE_LOW
                results[i].warning = (
                    f"Two files were detected as {REPORT_LABELS.get(rt, rt)}. "
                    "Only one should be uploaded — please review and pick "
                    "the correct one before continuing."
                )
    return results


def missing_required(results: List[ClassificationResult]) -> List[str]:
    """Return the list of required report_types that aren't represented
    by any successfully categorized result.

    Files in ``needs_review`` / ``duplicate`` / ``unreadable`` status
    do NOT count as covering their guessed type."""
    have = {
        r.report_type
        for r in results
        if r.report_type and r.status == STATUS_CATEGORIZED
    }
    return [rt for rt in REQUIRED_REPORTS if rt not in have]


def summarize_bulk(
    results: List[ClassificationResult],
) -> dict:
    """Compact summary suitable for templates / JSON responses."""
    by_type: dict[str, int] = {}
    for r in results:
        if r.report_type and r.status == STATUS_CATEGORIZED:
            by_type[r.report_type] = by_type.get(r.report_type, 0) + 1
    return {
        "file_count": len(results),
        "categorized": sum(1 for r in results if r.status == STATUS_CATEGORIZED),
        "needs_review": sum(
            1 for r in results
            if r.status in (STATUS_NEEDS_REVIEW, STATUS_DUPLICATE, STATUS_UNREADABLE)
        ),
        "by_type": by_type,
        "missing_required": missing_required(results),
    }


def is_acceptable_override(report_type: Optional[str]) -> bool:
    """Validation helper for the manual-correction form. We only accept
    one of the known report-type ids; blank means "don't change"."""
    if report_type in (None, ""):
        return True
    return is_valid_report_type(report_type)
