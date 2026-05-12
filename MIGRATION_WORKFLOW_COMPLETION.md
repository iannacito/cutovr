# Migration workflow completion

This document covers the six migration-workflow tasks that landed in
the **Migration workflow completion** PR (feature branch
`feature/migration-workflow-completion`). Together they fill in the
gaps that were marked "Planned next" in
[`CUTOVER_WORKFLOW.md`](CUTOVER_WORKFLOW.md) and
[`MULTI_REPORT_SUPPORT.md`](MULTI_REPORT_SUPPORT.md) after the COA
creation PR (#19) shipped.

The guiding principle is **safety first**:

* every QBO write requires a typed confirmation phrase,
* no auto-balancing, auto-flattening, or auto-posting happens silently,
* unsupported strategies are *blocked with clear messages*, not
  half-implemented.

---

## 1. Opening Trial Balance → opening balance JournalEntry

**Module:** [`opening_balance.py`](opening_balance.py)
**Route:** `GET/POST /jobs/<job_id>/opening-balance`
**Template:** `templates/opening-balance.html`
**Confirmation phrase:** `POST OPENING BALANCE`

Builds a single balancing `JournalEntry` from the parsed Trial Balance.
Each TB row becomes one JE line; debit rows post Debit, credit rows
post Credit, zero-balance rows are omitted.

Plan blockers (no post under any circumstances):

* TB does not balance — we deliberately **do not** auto-balance to a
  suspense account.
* One or more TB rows do not resolve to a QBO account (by saved
  Account Mapping, then `AcctNum`, then `Name`). Fix in the Chart of
  Accounts step or add a mapping before retrying.
* QBO is not connected.
* The operator did not type the confirmation phrase exactly.

The plan respects the firm's saved per-account mappings, so a TB
account named differently from its QBO counterpart can be resolved
without renaming files.

`opening_balance_history` is appended to the job dict on each
successful post, and the migration checklist promotes the **Opening
balance** step to *Complete* the moment the first real post lands.

## 2. Ending Trial Balance reconciliation

**Module:** [`tb_reconciliation.py`](tb_reconciliation.py)
**Routes:** `GET /jobs/<job_id>/ending-tb-reconciliation`,
            `GET /jobs/<job_id>/ending-tb-reconciliation.csv`
**Template:** `templates/ending-tb-reconciliation.html`

Compares the uploaded ending TB to the **expected** balance per account,
where:

```
expected = opening TB net (debit - credit)
         + GL period net  (debit - credit)
```

Each account is bucketed:

* `match` — within $0.01 of expected
* `diff` — exceeds the tolerance
* `unexpected` — appears in ending TB only
* `missing` — has a non-zero expected balance but isn't in ending TB

The downloadable CSV captures the same data plus a documented
**limitation**: this reconciliation never calls the QuickBooks Reports
API to fetch QBO's own TB. Direct QBO-balance lookup is intentionally
out-of-scope for this PR (requires accrual/cash flags, exact period
alignment, and a careful taxonomy mapping). The reconciliation
therefore proves PCLaw files are internally consistent — operators
should still spot-check matched accounts in QBO before signing off.

The checklist promotes the **Ending TB** step to *Complete* once any
ending-TB reconciliation report has been built for the firm.

## 3. Trust Listing reconciliation

**Module:** [`trust_reconciliation.py`](trust_reconciliation.py)
**Routes:** `GET /jobs/<job_id>/trust-reconciliation`,
            `GET /jobs/<job_id>/trust-reconciliation.csv`
**Template:** `templates/trust-reconciliation.html`

Parses per-bank, per-client, and per-matter totals from the listing,
and (when a TB has been uploaded) compares the listing total to:

* the trust-liability balance on the TB
  (account names containing "trust liability", "client trust", etc.),
* the trust-bank balance on the TB
  (account names containing "trust bank", "iolta", "client trust bank").

The report surfaces:

* mismatches against either TB anchor (with explicit deltas),
* negative trust balances (any negative is a data-quality red flag),
* rows missing a client/matter ID or trust bank account,
* the top 25 clients by trust balance.

**Trust posting is intentionally NOT automated.** The report's summary
exposes a hard `posting_enabled: false` flag with a documented reason.
The existing `import-to-qbo` safety gate (`import_blocked_report_type`
audit) continues to refuse any trust-listing post. A future
operator-confirmed per-matter posting flow is allowed but is *not* in
this PR.

The checklist promotes the **Trust Listing** step to *Complete* the
first time the reconciliation view is opened (which builds the report
on the server side and records `trust_reconciliation` on the job).

## 4. AR/AP migration strategy controls

**Module:** [`ar_ap_strategy.py`](ar_ap_strategy.py)
**Surfaced in:** the existing cutover setup form, the migration
checklist, and the upload/import safety gate.
**DB:** new `ar_ap_strategy` column on `cutover_settings`
(additive, nullable, backward-compatible).

The cutover setup form now captures a strategy:

| Strategy id      | Label                                              | Status        |
| ---------------- | -------------------------------------------------- | ------------- |
| `skip`           | Skip AR/AP migration entirely                       | Supported     |
| `summary_je`     | Summary opening JE (one JE per AR/AP account)       | Guidance only |
| `open_items`     | Open-item list (one transaction per invoice/bill)   | Guidance only |

The cutover page renders country/basis/Clio-specific recommendations
and explicit blocker reasons under each strategy:

* Canadian firms with `open_items` see a clear blocker because we don't
  generate sales-tax-aware invoices yet.
* Cash-basis firms see "skip is the standard move."
* Firms with Clio see "AR belongs in Clio; keep QBO summary-only."

`ar_ap_strategy.block_message_for_unsupported_import` returns a refusal
string the upload/import paths can flash when an unsupported strategy
is selected. Today's path is: choose a strategy now, plan around it
with explicit guidance, and the next PR can light up actual
per-invoice/per-summary posting under an additional confirmation gate.

## 5. Parent/sub-account hierarchy support

**Module:** [`coa_hierarchy.py`](coa_hierarchy.py)
**Wired into:** the existing COA preview, confirmation, and apply
routes.

The COA parser now reads parent-account fields
(`parent_account_number` / `parent_account_name` / `parent_account` /
`header_account`) and the hierarchy resolver buckets every row as:

* `top_level` — no parent, create at top of chart.
* `qbo_existing_parent` — parent already exists in QBO.
* `in_plan_parent` — parent is another row in the same upload.
* `orphan` — parent referenced but not found anywhere (**blocked**).
* `cycle` — A→B→A (**blocked**).

The resolver produces a deterministic `create_order` (depth then
account_number then name) so parents land before children when a
future apply pass starts wiring up `ParentRef`. Hierarchy blockers are
**folded into the existing CreatePlan.blocked list**, so the
`coa-confirm` page's "Cannot proceed: blocked rows" gate covers them
too — there is no silent flattening of orphan sub-accounts.

**Limitation.** This PR adds detection, preview, ordering, and blocking.
Actually wiring `ParentRef` into the QBO `Account` create payload (so
QBO files the new account under its parent) is left to a follow-up PR
because it requires a second QBO query to translate the in-plan parent
to its just-created QBO Id mid-loop. The current behavior is to:

* refuse to flatten (orphans and cycles are blocked),
* show the operator the planned hierarchy so they can audit it,
* create rows as top-level when the parent exists in QBO and they were
  approved (no silent flattening of orphans).

## 6. Broader real-world PCLaw parser support

**Module touched:** [`report_types.py`](report_types.py)

Hardenings landed across the existing parsers (COA, TB, Trust Listing,
and indirectly GL because the helpers are shared):

* **Money parsing** now handles `$`, `,`, accounting parentheses
  `(1,234.56)`, trailing `CR`/`DR` indicators, unicode minus characters
  (`−`, `–`, `—`), non-breaking spaces, and placeholder cells like
  `-`/`--`/`N/A`. Negative debits and negative credits are normalized
  into the correct column so the totals stay honest.
* **Header detection** scans the first ~20 lines for the row most
  plausibly containing the real header (two or more known header
  tokens, no `$` signs) so PCLaw-style "Report run on …" preambles
  don't break the parser.
* **Footer / subtotal skipping** drops rows whose first non-empty cell
  starts with `total`, `subtotal`, `grand total`, `report total`,
  `end of report`, or matches a `Page X of Y` pattern.
* **Combined account fields** like a single `account` column
  containing `1000 - Operating Bank` are split into
  `account_number=1000` / `account_name=Operating Bank` when dedicated
  columns are absent.
* **Net-balance fallback** on TB widened to accept `amount`,
  `ending_balance`, `closing_balance` in addition to the original
  `net_balance` / `balance`.
* **COA preflight** now reports `parent_linked_count` and surfaces an
  assumptions list (e.g. "X rows reference a parent account — the
  hierarchy preview shows how they'd map").

The legacy GL format detector and the existing GL pipeline are
untouched — uploads that already worked still work.

---

## What is intentionally NOT in this PR

* **Trust posting to QBO.** Trust reconciliation is read-only.
  Per-matter posting under a separate operator-confirmed phrase is
  future work.
* **Live QBO TB lookup for ending-TB reconciliation.** We compare to
  expected balances from the parsed reports rather than calling the
  QBO Reports API.
* **`ParentRef` on COA create.** Hierarchy is detected, ordered, and
  blocked safely; wiring the actual parent on the create payload is
  the next iteration.
* **Full AR/AP posting flows.** `summary_je` and `open_items` are
  recorded but the import path still refuses to post under them.

Each of these is called out in the relevant code module so a future
contributor can land it without re-reading the entire history.

---

## Safety summary

| Surface                           | Auto-write? | Confirmation phrase     |
| --------------------------------- | ----------- | ----------------------- |
| GL import (existing)              | yes, gated  | `IMPORT` (prod only)    |
| COA create (existing)             | yes, gated  | `CREATE ACCOUNTS`       |
| Opening balance JE (this PR)      | yes, gated  | `POST OPENING BALANCE`  |
| Ending TB reconciliation (this PR)| no          | n/a                     |
| Trust reconciliation (this PR)    | no — disabled at the module level | n/a |
| AR/AP unsupported strategies      | no — refused with a clear flash message | n/a |

The `import-to-qbo` route's `import_blocked_report_type` audit event
continues to fire for any non-GL upload attempt.

## Tests

`tests/smoke_migration_workflow.py` covers:

* W1 — opening TB unbalanced blocks the plan.
* W2 — opening TB rows that don't resolve to QBO block per-row.
* W3 — balanced plan produces a balanced JE payload with the right
  `TxnDate` and posting-type splits.
* W4 — ending TB reconciliation: match / diff / unexpected / missing
  buckets, plus the documented limitation note.
* W5 — trust reconciliation surfaces negative balances, missing IDs,
  and TB-mismatch warnings; posting flag stays `False`.
* W6 — AR/AP strategy validation accepts only safe ids; guidance reacts
  to country/basis/Clio; unsupported strategies have a refusal string.
* W7 — hierarchy detection: orphan + cycle blocked, top-level +
  qbo_existing_parent + in_plan_parent classified, deterministic create
  order with parents before children.
* W8 — parser hardening: money formats, footer / preamble skipping,
  combined account splitting.
* W9 — `/jobs/<id>/opening-balance` never calls `create_journal_entry`
  without the confirmation phrase, and never calls QBO when not
  connected even if the phrase is typed.
* W10 — trust posting remains intentionally disabled both at the
  module surface and via the existing `import-to-qbo` safety gate.

Existing smoke tests (`smoke_multi_report`, `smoke_coa_create`,
`smoke_cutover`, `smoke_persistence`, `smoke_security_hardening`, ...)
continue to pass.
