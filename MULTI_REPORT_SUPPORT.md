# Multi-report support

The migration product began as a General Ledger -> QuickBooks Online
JournalEntry import. Real-world cutovers involve four PCLaw reports the
firm typically exports together. This document describes which report
types this build understands, what each one does today, and what is
explicitly *not* yet implemented.

## Supported report types

| Report type | upload `report_type` value | QBO behavior | Notes |
| --- | --- | --- | --- |
| General Ledger | `general_ledger` | **Importable** | Each PCLaw transaction posts as one QBO JournalEntry after explicit confirmation. Existing behavior; unchanged. |
| Chart of Accounts | `chart_of_accounts` | **Preview only** | Parsed and compared against the connected QBO company's Account list. Shows matched accounts, would-be-creates, and soft conflicts. No QBO writes from the automated flow. |
| Trial Balance | `trial_balance` | **Validation only** | Parsed, totaled, and flagged if debits do not equal credits. Used to reconcile a posted GL import. Never auto-posted to QBO. |
| Trust Listing | `trust_listing` | **Validation only** | Parsed by client / matter with trust totals per bank account. Used to reconcile against the QBO trust liability and trust bank account balances. Never auto-posted to QBO. |

Upload-form selection is optional. When the user picks
"Auto-detect from CSV headers" the server inspects the headers and
chooses the best-fitting report type. If the headers match the legacy
GL format (transaction_id, date, account_number, account_name, debit,
credit) the GL path runs exactly as before — backward compatibility is
preserved for existing customers and tests.

## Column reference

The parsers accept common header variants (case-insensitive,
underscores / spaces / dashes are interchangeable). Listed first is the
canonical header.

### Chart of Accounts (`chart_of_accounts`)

| Column | Aliases | Required | Notes |
| --- | --- | --- | --- |
| `account_number` | `acct_num`, `number` | yes | Primary match key against QBO `AcctNum`. |
| `account_name` | `name` | yes | Fallback match key against QBO `Name`. |
| `account_type` | `type`, `category`, `pclaw_category`, `qbo_suggested_type` | recommended | Used for display + future create-account hint. |
| `qbo_suggested_detail_type` | `detail_type`, `sub_type` | optional | Used as the would-be-create detail-type hint. |
| `description` | `notes`, `memo` | optional | Free-text. |
| `active` | `status`, `is_active`, `enabled` | optional | `Yes/No`, `true/false`, `1/0`, or `A/I`. Defaults to active. |
| `opening_balance` | `balance` | optional | Display only. Never posted to QBO. |

### Trial Balance (`trial_balance`)

| Column | Aliases | Required | Notes |
| --- | --- | --- | --- |
| `account_number` | `acct_num`, `number` | yes | Display + reconciliation key. |
| `account_name` | `name` | yes | Display + reconciliation key. |
| `debit_balance` | `debit`, `debit_amount` | one of debit/credit or net required | Money cell. |
| `credit_balance` | `credit`, `credit_amount` | one of debit/credit or net required | Money cell. |
| `net_balance` | `balance` | optional | Used when debit/credit columns are absent. Positive net is treated as debit, negative as credit. |
| `as_of_date` | `period`, `period_end`, `date` | optional | Display only. |

The preflight checks debits == credits across the whole report and
calls out the out-of-balance amount if not.

### Trust Listing (`trust_listing`)

| Column | Aliases | Required | Notes |
| --- | --- | --- | --- |
| `trust_balance` | `balance`, `amount` | yes | Money cell. |
| `client_id` | `client_no`, `client_number` | at least one client/matter identifier required | Used as the primary client key. |
| `client_name` | `client` | (see above) | Display + fallback identifier. |
| `matter_id` | `matter_no`, `matter_number` | (see above) | Used as the primary matter key. |
| `matter_name` | `matter` | (see above) | Display. |
| `trust_bank_account` | `trust_account`, `bank_account`, `trust_bank` | optional | Used to subtotal per trust bank account. |
| `as_of_date` | `as_of`, `date`, `period_end` | optional | Display only. |

The preflight totals the trust balance across all rows, counts clients
and matters, breaks out totals per trust bank account, and flags any
negative balance (which would be a data quality issue worth
investigating before migration).

## QBO safety guarantees

These are tested by `tests/smoke_multi_report.py`:

- `POST /jobs/<id>/import-to-qbo` always 4xxs / redirects for
  report_type in {chart_of_accounts, trial_balance, trust_listing}
  *before* any QBO call. An audit event `import_blocked_report_type` is
  written.
- The Chart of Accounts preview (`/jobs/<id>/coa-preview`) only calls
  the QBO `query` endpoint to fetch the Account list. It does not call
  any `create_*` endpoint. The preview output is purely a comparison.
- Trial Balance and Trust Listing uploads never touch the QBO HTTP
  client. They only parse the uploaded CSV and write the preflight
  summary to the job dict.

## Validation report

`GET /jobs/<id>/validation-report.csv` returns a CSV whose body adapts
to the job's `report_type`:

- GL: classic Transactions / Lines / Debits / Credits / Balanced /
  Unique accounts block, optionally followed by the QBO mapping
  preview when QBO is connected.
- COA: accounts in file, type counts, duplicates, missing name/type
  warnings.
- Trial Balance: total debits, total credits, balanced, out-of-balance
  amount, rows missing account.
- Trust Listing: row count, distinct clients, distinct matters, total
  trust balance, negative-balance count, per-bank-account subtotals.

All cells are sanitized through `csv_safety.sanitize_csv_cell` to
neutralize spreadsheet formula injection from PCLaw-supplied text.

## Sample / template downloads

| Report | URL |
| --- | --- |
| General Ledger (small template) | `/onboarding/template.csv` |
| General Ledger (multi-transaction sample) | `/onboarding/sample.csv` |
| Chart of Accounts | `/onboarding/sample/chart_of_accounts.csv` |
| Trial Balance | `/onboarding/sample/trial_balance.csv` |
| Trust Listing | `/onboarding/sample/trust_listing.csv` |

The samples are the bundled `test_data/` demo files and contain only
obviously-fake data.

## Not (yet) implemented

Chart of Accounts QBO Account creation through the automated flow is
deliberately *not* in this pass. The dry-run preview is the safe first
step: it surfaces the would-be-creates without risk of an accidental
write into the wrong company. Real account creation requires:

- a confirmation route mirroring the GL `confirm_import=IMPORT` pattern,
- handling of QBO Account validation rules (AccountType / AccountSubType
  compatibility, parent-account ordering),
- per-firm undo semantics that match the GL reversal workflow.

When this is built, the COA preview page will gain an "Apply to QBO"
button gated behind the same explicit-confirmation pattern the GL
import uses today.

A/R aging and A/P aging dedicated parsers are also future work — for
now those amounts arrive in QBO via the GL import when individual
transactions post to the QBO `Accounts Receivable` / `Accounts Payable`
accounts. The pipeline already auto-creates Customer / Vendor entities
on those rows.
