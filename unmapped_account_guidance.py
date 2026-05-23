"""Customer-friendly guidance for the unmapped-account import block.

The GL import route refuses to post Journal Entries to QuickBooks when the
PCLaw ledger references accounts that have no match in the connected
QuickBooks Online company. The block itself is correct safety behavior —
we never silently create or remap accounts at import time — but the raw
"Cannot import: ..." flash assumes the operator already understands what
to do next.

This module turns the raw blocker into a context-aware next-step CTA. It
classifies the firm's state into one of three buckets:

  * ``upload_coa``   — no Account List has been uploaded yet. The right
                      next step is to upload the PCLaw chart of accounts
                      so the app can mirror it into QuickBooks first.
  * ``finish_coa``   — an Account List exists but the COA hasn't been
                      finalized: either nothing has been pushed to QBO
                      yet, or the missing accounts aren't on the COA the
                      firm uploaded. The right next step is the COA
                      preview / create flow.
  * ``map_accounts`` — the Account List is on file and the firm has
                      already created accounts in QBO from it, but the
                      specific accounts the GL references still aren't
                      matchable. The right next step is the account
                      mapping page where the operator picks a QBO target
                      for each PCLaw account.

Nothing in this module talks to QBO or mutates the database — it's a
pure projection over (unmapped_keys, coa_rows, coa_create_history,
mapping_mode) so it's trivial to unit-test and safe to call from inside
the import route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence


# Bucket labels — referenced by templates and tests.
ACTION_UPLOAD_COA = "upload_coa"
ACTION_FINISH_COA = "finish_coa"
ACTION_MAP_ACCOUNTS = "map_accounts"


@dataclass
class UnmappedAccount:
    """A single PCLaw account that isn't matchable in QBO yet."""

    key: str                  # the original mapping key ("number" or "name")
    number: str = ""          # PCLaw account number, e.g. "1300"
    name: str = ""            # PCLaw account name, e.g. "Prepaid Expenses"
    in_coa: bool = False      # True if the firm's uploaded COA lists it

    @property
    def display(self) -> str:
        """Human-readable "1300 Prepaid Expenses" rendering used in flashes."""
        if self.number and self.name:
            return f"{self.number} {self.name}"
        return self.number or self.name or self.key


@dataclass
class UnmappedAccountGuidance:
    """Structured CTA payload produced for the import-blocked banner.

    The Flask route persists this on the job dict (as ``to_dict()``) so the
    job-detail template can render the CTA without having to re-run the
    classifier.
    """

    action: str                              # one of the ACTION_* constants
    accounts: List[UnmappedAccount]          # missing accounts, sorted
    headline: str                            # short, plain-English banner title
    body: str                                # paragraph of context for the user
    primary_cta_label: str
    primary_cta_endpoint: str                # Flask endpoint name (or "")
    primary_cta_kwargs: dict = field(default_factory=dict)
    secondary_cta_label: str = ""
    secondary_cta_endpoint: str = ""
    secondary_cta_kwargs: dict = field(default_factory=dict)
    company_label: str = ""                  # e.g. "your connected QuickBooks…"

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "accounts": [
                {
                    "key": a.key,
                    "number": a.number,
                    "name": a.name,
                    "in_coa": a.in_coa,
                    "display": a.display,
                }
                for a in self.accounts
            ],
            "headline": self.headline,
            "body": self.body,
            "primary_cta_label": self.primary_cta_label,
            "primary_cta_endpoint": self.primary_cta_endpoint,
            "primary_cta_kwargs": dict(self.primary_cta_kwargs),
            "secondary_cta_label": self.secondary_cta_label,
            "secondary_cta_endpoint": self.secondary_cta_endpoint,
            "secondary_cta_kwargs": dict(self.secondary_cta_kwargs),
            "company_label": self.company_label,
        }


def _split_unmapped_key(key: str, mapping_mode: str) -> tuple[str, str]:
    """Best-effort split of ``find_unmapped_accounts`` keys into (number, name).

    Upstream emits ``f"{account_number} {account_name}".strip()``. When the
    mapping mode is "number" the first whitespace-separated token is the
    number; otherwise everything is the name. Either side may be blank.
    """
    if not key:
        return ("", "")
    stripped = key.strip()
    if mapping_mode == "number":
        head, _, rest = stripped.partition(" ")
        return (head.strip(), rest.strip())
    # Name-mode rows still emit "<number> <name>" when both are present.
    head, _, rest = stripped.partition(" ")
    if head.isdigit() or (head and head[0].isdigit() and not rest):
        return (head.strip(), rest.strip())
    return ("", stripped)


def _coa_index(coa_rows: Sequence[dict]) -> tuple[set, set]:
    """Index the firm's uploaded COA by account number and lowercased name."""
    numbers: set = set()
    names: set = set()
    for r in coa_rows or []:
        num = (r.get("account_number") or "").strip()
        if num:
            numbers.add(num)
        nm = (r.get("account_name") or "").strip().lower()
        if nm:
            names.add(nm)
    return numbers, names


def _coa_finalized(coa_create_history: Iterable[dict]) -> bool:
    """True iff the firm has actually created at least one account in QBO
    from the uploaded COA.

    We treat "finalized" loosely on purpose — if anything was pushed from
    the preview at all, the COA has been touched and the right next step
    is the mapping page rather than the COA wizard. Operators that
    pushed *some* accounts can still finish the rest from the mapping
    page or by returning to the COA preview themselves.
    """
    for h in coa_create_history or ():
        try:
            if int(h.get("created_count") or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
        created = h.get("created") or []
        if isinstance(created, list) and created:
            return True
    return False


def _format_company_label(company_name: Optional[str], environment: str) -> str:
    """Plain-English label for the connected QBO company.

    We only fall back to "sandbox" when the deploy is explicitly running
    against Intuit's sandbox environment — otherwise we use a neutral
    "connected QuickBooks company" label that's accurate in both
    production and sandbox builds without implying the customer did
    anything wrong.
    """
    company_name = (company_name or "").strip()
    env = (environment or "").strip().lower()
    if company_name:
        if env == "sandbox":
            return f"your connected QuickBooks sandbox company ({company_name})"
        return f"your connected QuickBooks company ({company_name})"
    if env == "sandbox":
        return "your connected QuickBooks sandbox company"
    return "your connected QuickBooks company"


def classify_unmapped_accounts(
    *,
    unmapped_keys: Iterable[str],
    mapping_mode: str,
    coa_rows: Sequence[dict],
    coa_create_history: Iterable[dict],
    job_id: str,
    company_name: Optional[str] = None,
    environment: str = "sandbox",
) -> UnmappedAccountGuidance:
    """Pick the right next step for the operator and build the CTA payload.

    Args:
      unmapped_keys: the strings returned by
        ``pclaw_pipeline.find_unmapped_accounts``. They look like
        ``"1300 Prepaid Expenses"``.
      mapping_mode: ``"number"`` or ``"name"``; determines how to split
        the keys back into number + name.
      coa_rows: parsed rows from the firm's most recent Chart of Accounts
        upload (may be empty when no COA exists).
      coa_create_history: list of per-run create snapshots persisted on
        the COA job (see ``_collect_coa_context``).
      job_id: the GL job that hit the unmapped block — used to route the
        operator back to the right place if "map manually" is the next
        step.
      company_name: connected QBO company display name (optional).
      environment: ``"sandbox"`` or ``"production"`` from ``QBO_ENVIRONMENT``.

    The returned guidance always has ``accounts`` populated (the missing
    accounts, sorted by number) and never has a confusing "sandbox"
    label leak into a production banner.
    """
    keys_sorted = sorted({k.strip() for k in unmapped_keys if k and k.strip()})
    accounts: List[UnmappedAccount] = []
    coa_numbers, coa_names = _coa_index(coa_rows)
    for key in keys_sorted:
        number, name = _split_unmapped_key(key, mapping_mode)
        in_coa = False
        if number and number in coa_numbers:
            in_coa = True
        elif name and name.lower() in coa_names:
            in_coa = True
        accounts.append(UnmappedAccount(
            key=key, number=number, name=name, in_coa=in_coa,
        ))

    has_coa = bool(coa_rows)
    finalized = _coa_finalized(coa_create_history)
    company_label = _format_company_label(company_name, environment)

    # Pluralise the "One QuickBooks account is missing" line based on the
    # actual count so the lawyer-friendly headline reads naturally in
    # both the single- and multiple-missing-account cases.
    missing_count = len(accounts)
    if missing_count == 1:
        plain_headline = (
            "One QuickBooks account is missing. Create it from your "
            "PCLaw account list before sending."
        )
    else:
        plain_headline = (
            f"{missing_count} QuickBooks accounts are missing. Create "
            "them from your PCLaw account list before sending."
        )

    # Bucket 1 — no COA uploaded.
    if not has_coa:
        return UnmappedAccountGuidance(
            action=ACTION_UPLOAD_COA,
            accounts=accounts,
            headline=plain_headline,
            body=(
                "Your transaction history references PCLaw accounts that "
                "aren't in " + company_label + ". Upload your PCLaw "
                "Account List (Chart of Accounts) so we can mirror it "
                "into QuickBooks before this import."
            ),
            primary_cta_label="Upload Account List",
            primary_cta_endpoint="dashboard",
            primary_cta_kwargs={"_anchor": "intake"},
            secondary_cta_label="Match accounts manually instead",
            secondary_cta_endpoint="account_mapping",
            secondary_cta_kwargs={"job_id": job_id},
            company_label=company_label,
        )

    # Bucket 2 — COA exists but the COA hasn't been finalized in QBO yet,
    # OR the specific missing accounts are listed in the firm's COA but
    # still aren't in QBO (which also means the COA hasn't been pushed).
    missing_listed_in_coa = any(a.in_coa for a in accounts)
    if not finalized or missing_listed_in_coa:
        return UnmappedAccountGuidance(
            action=ACTION_FINISH_COA,
            accounts=accounts,
            headline=plain_headline,
            body=(
                "Your PCLaw Account List is uploaded but the missing "
                "account isn't in " + company_label + " yet. Use "
                "\"Create missing QuickBooks accounts\" on the Match "
                "accounts page — we'll add it with the right type and "
                "number, no manual QuickBooks setup needed."
            ),
            primary_cta_label="Create missing QuickBooks accounts",
            primary_cta_endpoint="account_mapping",
            primary_cta_kwargs={"job_id": job_id},
            secondary_cta_label="Review Account List",
            secondary_cta_endpoint="migration_checklist",
            secondary_cta_kwargs={},
            company_label=company_label,
        )

    # Bucket 3 — COA finalized but specific GL accounts still unmapped.
    return UnmappedAccountGuidance(
        action=ACTION_MAP_ACCOUNTS,
        accounts=accounts,
        headline=plain_headline,
        body=(
            "Your Account List is in " + company_label + ", but the "
            "transaction history references accounts that aren't there "
            "by number yet. Use \"Create missing QuickBooks accounts\" "
            "on the Match accounts page — we'll add them with the right "
            "type and number, or you can pick an existing QuickBooks "
            "account to match each one."
        ),
        primary_cta_label="Create missing QuickBooks accounts",
        primary_cta_endpoint="account_mapping",
        primary_cta_kwargs={"job_id": job_id},
        secondary_cta_label="Review Account List",
        secondary_cta_endpoint="migration_checklist",
        secondary_cta_kwargs={},
        company_label=company_label,
    )
