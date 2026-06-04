"""
PCLaw General Ledger -> QuickBooks JournalEntry pipeline.

Input: a PCLaw GL CSV with at minimum these columns:
    transaction_id, date, account_number, account_name, debit, credit
Optional columns: description, reference, client_id, matter_id

Output: a list of QuickBooks JournalEntry payloads, one per transaction_id.
Each payload is validated to balance (debits == credits) before being built.
"""

import csv
from collections import defaultdict, OrderedDict
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO
from pathlib import Path

from csv_decode import open_csv_text


GL_REQUIRED_COLUMNS = ["transaction_id", "date", "account_number", "account_name", "debit", "credit"]


def money(value):
    if value is None or value == "":
        return Decimal("0.00")
    cleaned = str(value).replace(",", "").replace("$", "").strip() or "0"
    return Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize_txn_date(raw, transaction_id=None):
    """Coerce a PCLaw GL date into the ISO ``YYYY-MM-DD`` QuickBooks needs.

    QuickBooks rejects a JournalEntry whose ``TxnDate`` is not ISO. PCLaw's
    fresh export writes dates like ``Jan 4/21`` (see Cesar's QA
    2026-06-01); passing that string straight through made the *whole*
    import fail — often surfacing downstream as a confusing
    "fewer than 2 posting lines" error because the entry never built.

    We reuse :func:`gl_row_quality.parse_gl_date`, which already accepts
    the PCLaw native format, ISO, MM/DD/YYYY, ``D-MMM-YY``, and Excel
    serials. If the value still can't be read we raise a plain-English
    error that names the offending date and transaction so the
    validation report can point the user at the exact row.
    """
    from gl_row_quality import parse_gl_date  # local import to avoid cycle

    iso = parse_gl_date(raw)
    if iso:
        return iso
    where = f" in transaction {transaction_id}" if transaction_id else ""
    raise ValueError(
        f"We couldn't read the date '{raw}'{where}. PCLaw exports it as "
        "Jan 4/21 — that format is now accepted, along with YYYY-MM-DD "
        "and MM/DD/YYYY. Fix this row's date and re-upload."
    )


def load_general_ledger_csv(path):
    path = Path(path)
    text, _enc = open_csv_text(path)
    with StringIO(text) as f:
        reader = csv.DictReader(f)
        missing = [c for c in GL_REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(
                "PCLaw GL CSV is missing required columns: "
                + ", ".join(missing)
                + ". This pipeline expects the richer GL format with a transaction_id "
                "column (see test_data/02_general_ledger.csv)."
            )
        return list(reader)


def is_gl_format(fieldnames):
    """Cheap check used by the route to decide which parser to use."""
    if not fieldnames:
        return False
    return all(c in fieldnames for c in GL_REQUIRED_COLUMNS)


def group_rows_by_transaction(rows):
    groups = OrderedDict()
    for row in rows:
        groups.setdefault(row["transaction_id"], []).append(row)
    return groups


def validate_transaction_group(transaction_id, rows):
    total_debits = sum(money(row["debit"]) for row in rows)
    total_credits = sum(money(row["credit"]) for row in rows)

    if total_debits != total_credits:
        raise ValueError(
            f"Transaction {transaction_id} does not balance "
            f"(debits={total_debits}, credits={total_credits})."
        )

    for row in rows:
        if not row.get("date"):
            raise ValueError(f"Transaction {transaction_id} has a row with no date.")
        if not row.get("account_number") and not row.get("account_name"):
            raise ValueError(
                f"Transaction {transaction_id} has a row with no account_number or account_name."
            )


def build_account_mapping_from_numbers(qbo_accounts_response):
    accounts = qbo_accounts_response.get("QueryResponse", {}).get("Account", [])
    mapping = {}
    for account in accounts:
        acct_num = account.get("AcctNum")
        acct_id = account.get("Id")
        if acct_num and acct_id:
            mapping[str(acct_num)] = acct_id
    return mapping


def build_account_mapping_from_names(qbo_accounts_response):
    accounts = qbo_accounts_response.get("QueryResponse", {}).get("Account", [])
    mapping = {}
    for account in accounts:
        name = account.get("Name")
        acct_id = account.get("Id")
        if name and acct_id:
            mapping[name] = acct_id
    return mapping


def build_account_type_index(qbo_accounts_response):
    """Map QBO Account Id -> AccountType (e.g. 'Accounts Receivable')."""
    index = {}
    for account in qbo_accounts_response.get("QueryResponse", {}).get("Account", []):
        acct_id = account.get("Id")
        if acct_id:
            index[acct_id] = account.get("AccountType")
    return index


# Default entity names used when a PCLaw row has no explicit customer/vendor.
# Beginner-safe so the MVP does not require the user to pre-create entities.
DEFAULT_CUSTOMER_NAME = "PCLaw Test Customer"
DEFAULT_VENDOR_NAME = "PCLaw Test Vendor"

# Header spellings PCLaw actually exports for the client / matter that owns
# an A/R or A/P line, in priority order. Cesar QA item 9: a real PCLaw GL
# export labels the client column "Client" / "Client Name" / "Matter" (and
# carries it in "reference" on some report layouts), not the snake_case
# "customer_name" the first cut looked for. Because the lookup was both
# exact-match and snake_case-only, every A/R line fell through to the single
# DEFAULT_CUSTOMER_NAME — so the import created only one customer. We match
# case-insensitively and on the variants below so distinct clients/matters
# become distinct QuickBooks customers.
_CUSTOMER_KEY_CANDIDATES = (
    "customer_name", "customer", "client_name", "client",
    "client_matter", "matter_name", "matter", "client_id", "matter_id",
    "payor", "payer", "received_from", "name", "reference",
)
_VENDOR_KEY_CANDIDATES = (
    "vendor_name", "vendor", "payee", "supplier", "paid_to",
    "name", "reference",
)


def _normalize_key(key):
    """Lower-case a CSV header and collapse spaces / separators so
    "Client Name", "client-name", and "client_name" all compare equal.
    """
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def _first_entity_value(row, candidates):
    """Return the first non-empty value in ``row`` whose header matches one
    of ``candidates`` (compared after normalization), honoring the
    candidate priority order rather than the row's column order.
    """
    normalized = {_normalize_key(k): v for k, v in row.items()}
    for cand in candidates:
        val = normalized.get(_normalize_key(cand))
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def derive_entity_hint(row, account_type):
    """Return ('Customer'|'Vendor', display_name) or None for a GL row.

    QBO requires an Entity on JournalEntry lines that post to A/R or A/P.
    The client/matter (A/R) or vendor (A/P) is read from the PCLaw header
    variants in ``_CUSTOMER_KEY_CANDIDATES`` / ``_VENDOR_KEY_CANDIDATES``,
    matched case-insensitively, falling back to the beginner-safe default
    so the import never fails just because a row lacks an entity column.
    """
    if account_type == "Accounts Receivable":
        name = _first_entity_value(row, _CUSTOMER_KEY_CANDIDATES)
        return ("Customer", name or DEFAULT_CUSTOMER_NAME)
    if account_type == "Accounts Payable":
        name = _first_entity_value(row, _VENDOR_KEY_CANDIDATES)
        return ("Vendor", name or DEFAULT_VENDOR_NAME)
    return None


def find_unmapped_accounts(rows, account_mapping, mapping_mode):
    """Return the set of PCLaw account keys that have no QBO match.

    System-calculated accounts (Net Income / Net Income (Loss) / Current
    Year Earnings) are excluded — QuickBooks computes those from posted
    activity, so they should never appear on the "missing accounts"
    blocker. We import them here lazily to avoid a circular import with
    ``coa_apply``.
    """
    from coa_apply import is_system_calculated_account  # local import to avoid cycle
    unmapped = set()
    for row in rows:
        if is_system_calculated_account({"account_name": row.get("account_name")}):
            continue
        key = row["account_number"] if mapping_mode == "number" else row["account_name"]
        if key and key not in account_mapping:
            unmapped.add(f"{row.get('account_number', '')} {row.get('account_name', '')}".strip())
    return unmapped


def idempotency_doc_number(transaction_id):
    """Stable, short DocNumber for a PCLaw transaction reference.

    QuickBooks caps DocNumber at 21 characters and we want it to be the
    *same* value every time we (re)post the same PCLaw transaction, so a
    retry after a lost response can find the entry already in QBO instead
    of creating a duplicate. We prefix a short, readable tag and append a
    hash of the full reference so distinct references never collide even
    when their human-readable prefixes are truncated to the same string.
    """
    import hashlib

    ref = str(transaction_id or "").strip()
    digest = hashlib.sha1(ref.encode("utf-8")).hexdigest()[:8].upper()
    # Keep up to 11 readable chars from the reference, then "-" + 8-char
    # hash = at most 20 chars, comfortably under QBO's 21-char limit.
    readable = "".join(ch for ch in ref if ch.isalnum())[:11] or "PCLAW"
    return f"{readable}-{digest}"


def build_journal_entry_payload(
    transaction_id, rows, account_mapping, mapping_mode="number", account_type_index=None
):
    validate_transaction_group(transaction_id, rows)
    first_row = rows[0]
    lines = []
    account_type_index = account_type_index or {}

    for row in rows:
        debit = money(row["debit"])
        credit = money(row["credit"])
        if debit > 0 and credit > 0:
            raise ValueError(
                f"Transaction {transaction_id} has a row with both debit and credit set."
            )
        if debit == 0 and credit == 0:
            continue

        posting_type = "Debit" if debit > 0 else "Credit"
        amount = debit if debit > 0 else credit

        mapping_key = (
            row["account_number"] if mapping_mode == "number" else row["account_name"]
        )
        qbo_account_id = account_mapping.get(mapping_key)
        if not qbo_account_id:
            raise ValueError(
                f"No QBO account match for PCLaw account "
                f"{row.get('account_number')} / {row.get('account_name')} "
                f"(transaction {transaction_id})."
            )

        description = row.get("description") or f"PCLaw import {transaction_id}"
        line = {
            "Description": description[:4000],
            "Amount": float(amount),
            "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {
                "PostingType": posting_type,
                "AccountRef": {
                    "value": qbo_account_id,
                    "name": row.get("account_name") or "",
                },
            },
        }

        # Tag A/R or A/P lines with an entity hint. The import route resolves
        # these into real Customer / Vendor IDs and rewrites the line before
        # POSTing — see app.py:_resolve_entity_hints.
        account_type = account_type_index.get(qbo_account_id)
        hint = derive_entity_hint(row, account_type)
        if hint:
            line["_pclaw_entity_hint"] = {"type": hint[0], "name": hint[1]}

        lines.append(line)

    if len(lines) < 2:
        raise ValueError(
            f"Transaction {transaction_id} produced fewer than 2 lines; QBO requires at least 2."
        )

    return {
        "TxnDate": normalize_txn_date(first_row["date"], transaction_id),
        # Deterministic DocNumber makes a retry after a lost response
        # idempotent: the import route probes QBO for this DocNumber before
        # posting and reuses any existing entry instead of double-posting.
        "DocNumber": idempotency_doc_number(transaction_id),
        "PrivateNote": (
            f"Imported from PCLaw via pclaw-qbo | transaction_id={transaction_id}"
        ),
        "Line": lines,
    }


def plan_balanced_payloads(rows, account_mapping, mapping_mode="number", account_type_index=None):
    """Return ``(payloads, posted_ids)`` honouring source-journal grouping.

    See :mod:`gl_grouping` for the safety policy. When individual PCLaw
    references don't balance but a set of them sharing the same
    source-journal token (GB, GL, GJ, CER, …) does, the unbalanced
    references are merged into a single JE labelled ``GROUP-<token>``.
    Balanced individual references are still posted as their own JE so
    the firm can trace each PCLaw reference back to one QBO entry.
    """
    from gl_grouping import plan_posting_groups  # local import to avoid cycle
    from gl_row_quality import is_droppable_row  # local import to avoid cycle

    # Drop blank and zero-activity account-listing rows before grouping.
    # A 0.00/0.00 row with an account but no date is a PCLaw chart-listing
    # artifact that posts nothing; left in, it forms a phantom single-line
    # "transaction" that trips the "fewer than 2 posting lines" blocker.
    rows = [r for r in (rows or []) if not is_droppable_row(r)]

    grouped = group_rows_by_transaction(rows)
    plan = plan_posting_groups(grouped)

    if plan["still_blocked"]:
        # Last-line safety net: the validator should have refused to
        # let the user click Send to QuickBooks with anything in this
        # bucket. Re-checking here means we never accidentally post an
        # unbalanced batch even if the validation gate is bypassed.
        first = plan["still_blocked"][0]
        reasons = "; ".join(first.get("reasons") or ["unbalanced"])
        blocked_count = len(plan["still_blocked"])
        more = (
            f" {blocked_count - 1} other entr"
            + ("y is" if blocked_count == 2 else "ies are")
            + " also affected."
            if blocked_count > 1
            else ""
        )
        raise ValueError(
            f"One general-ledger entry couldn't be posted: {reasons} "
            f"(PCLaw reference {first['transaction_id']})."
            f"{more} A balanced entry needs at least one debit line and one "
            "credit line that add up to the same total. Download the "
            "validation report to see the exact rows, fix them in the CSV "
            "(or share a source-journal memo across the related rows), and "
            "re-upload."
        )

    payloads = []
    posted_ids = []
    for transaction_id, transaction_rows in plan["balanced_transactions"].items():
        payloads.append(
            build_journal_entry_payload(
                transaction_id=transaction_id,
                rows=transaction_rows,
                account_mapping=account_mapping,
                mapping_mode=mapping_mode,
                account_type_index=account_type_index,
            )
        )
        posted_ids.append(transaction_id)
    for group in plan["merged_groups"]:
        payloads.append(
            build_journal_entry_payload(
                transaction_id=group["group_id"],
                rows=group["rows"],
                account_mapping=account_mapping,
                mapping_mode=mapping_mode,
                account_type_index=account_type_index,
            )
        )
        posted_ids.append(group["group_id"])

    return payloads, posted_ids


def build_journal_entries_from_gl(rows, account_mapping, mapping_mode="number", account_type_index=None):
    """List-of-payloads view of :func:`plan_balanced_payloads`.

    Kept for backwards compatibility with callers that just want the
    payload list. New callers should prefer ``plan_balanced_payloads``
    so they can match the QBO response back to PCLaw / GROUP ids.
    """
    payloads, _ = plan_balanced_payloads(
        rows,
        account_mapping,
        mapping_mode=mapping_mode,
        account_type_index=account_type_index,
    )
    return payloads


def build_test_journal_entry(qbo_accounts_response, txn_date, amount=1.00, memo="PCLaw->QBO sandbox smoke test"):
    """
    Beginner-safe fallback: build a tiny balanced JournalEntry from any two
    active QBO accounts. Used when account mapping fails so the user can
    still confirm the integration writes to QBO.
    """
    accounts = qbo_accounts_response.get("QueryResponse", {}).get("Account", [])
    active = [a for a in accounts if a.get("Active", True) and a.get("Id")]
    if len(active) < 2:
        raise ValueError("QBO sandbox has fewer than 2 active accounts; cannot build a test entry.")

    debit_acct = active[0]
    credit_acct = active[1]
    return {
        "TxnDate": txn_date,
        "PrivateNote": memo,
        "Line": [
            {
                "Description": memo,
                "Amount": float(amount),
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": {
                    "PostingType": "Debit",
                    "AccountRef": {"value": debit_acct["Id"], "name": debit_acct.get("Name", "")},
                },
            },
            {
                "Description": memo,
                "Amount": float(amount),
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": {
                    "PostingType": "Credit",
                    "AccountRef": {"value": credit_acct["Id"], "name": credit_acct.get("Name", "")},
                },
            },
        ],
    }
