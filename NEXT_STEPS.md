# Next Steps: Real QuickBooks Sandbox Import

This update replaces the old "demo mode" Import to QuickBooks button with a
real call to the QuickBooks Online sandbox API. It creates `JournalEntry`
records using the OAuth access token and `realmId` your app already obtains
during the Connect to QuickBooks flow.

## What changed

| File | Why |
| --- | --- |
| `qbo_client.py` (new) | Tiny QBO API client. Queries accounts and creates JournalEntry records. |
| `pclaw_pipeline.py` (new) | Groups GL rows by `transaction_id`, validates that each group balances, maps PCLaw accounts to QBO accounts, and builds JournalEntry payloads. |
| `app.py` | The `/jobs/<job_id>/import-to-qbo` route now calls QBO for real when `QBO_REAL_IMPORT=1`. Default is still demo so existing behavior doesn't change until you opt in. |
| `templates/job-detail.html` | The Import to QuickBooks button now shows whether real import is enabled, and lists created JournalEntry IDs after import. |
| `test_data/` (new) | Sample PCLaw reports copied from the starter kit (incl. a balanced GL with `transaction_id`). |

The original encryption, OAuth, parsing, and CSV export all work as before.

## How real import works

1. User uploads a PCLaw CSV. The original is stored encrypted on disk.
2. User connects to QuickBooks Online (existing OAuth flow). Tokens are encrypted at rest.
3. User clicks **Import to QuickBooks**.
4. If `QBO_REAL_IMPORT=1`:
   - The original CSV is decrypted in memory to a temp file.
   - `qbo_client.QBOClient.get_accounts()` fetches the QBO chart of accounts.
   - Account mapping is attempted by `AcctNum` first, then by `Name`.
   - **If the CSV uses the rich GL format** (`transaction_id, date, account_number, account_name, debit, credit`):
     rows are grouped by `transaction_id`, each group is validated to balance,
     and one `JournalEntry` is POSTed per transaction.
   - **If the CSV is the simple flat format** (no `transaction_id`):
     a single $1.00 test JournalEntry is created using two existing QBO
     accounts. This proves the connection works without faking a real import.
   - **If accounts cannot be mapped**: the import is blocked and the UI shows
     exactly which PCLaw accounts have no QBO match. Nothing is written.
5. If `QBO_REAL_IMPORT` is unset (default), the button still simulates the
   import the same way it always did.

## Run it locally (beginner-friendly)

```bash
cd ~/workspace/pclaw-qbo-v2

# 1. (One-time) create a virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Set your Intuit sandbox app credentials in this shell.
#    Get these from https://developer.intuit.com/ -> "My Apps" -> your app -> Keys & OAuth.
export QBO_CLIENT_ID="<your sandbox client id>"
export QBO_CLIENT_SECRET="<your sandbox client secret>"
export QBO_REDIRECT_URI="http://localhost:5000/oauth/callback"
export QBO_ENVIRONMENT="sandbox"

# 3. Set a stable encryption key so encrypted files survive restarts.
#    (If you skip this, a new random key is used each run and old encrypted files become unreadable.)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
export ENCRYPTION_KEY="<paste the key from above>"

# 4. Turn ON real QBO writes.
export QBO_REAL_IMPORT=1

# 5. Start the app.
python app.py
# -> http://localhost:5000
```

Then in the browser:

1. Upload `test_data/02_general_ledger.csv` (this has `transaction_id` and balances).
2. Click **Connect to QuickBooks** and complete the Intuit consent screen.
3. Click **Import to QuickBooks**.
4. Open your QuickBooks sandbox company and look at Reports -> Journal. You
   should see five new entries (one per `JE-####`).

## Mapping accounts: what to do if import is blocked

The QuickBooks sandbox company comes with its own chart of accounts. Most of
the names in the sample GL (e.g. `Operating Bank`, `Trust Bank`,
`Client Trust Liability`) won't exist there by default. If the import is
blocked with "these PCLaw accounts have no match", you have two options:

1. **Easiest** - in QBO, open Accounting -> Chart of Accounts and add new
   accounts whose **Name** matches the PCLaw `account_name` exactly. Then
   click Import again.
2. **Better long-term** - turn on account numbers in QBO (Settings -> Account
   and Settings -> Advanced -> Chart of accounts -> Enable account numbers)
   and assign each QBO account the same `Number` as the PCLaw
   `account_number`. The pipeline prefers numbers when they're present.

## Quick smoke test without the UI

```bash
# In the project root, with the venv active:
python - <<'PY'
from pclaw_pipeline import (
    load_general_ledger_csv,
    group_rows_by_transaction,
    validate_transaction_group,
)

rows = load_general_ledger_csv("test_data/02_general_ledger.csv")
groups = group_rows_by_transaction(rows)
for tid, grp in groups.items():
    validate_transaction_group(tid, grp)
    print(f"{tid}: {len(grp)} lines, balanced")
PY
```

Expected output:

```
JE-0001: 2 lines, balanced
JE-0002: 2 lines, balanced
JE-0003: 2 lines, balanced
JE-0004: 2 lines, balanced
JE-0005: 2 lines, balanced
```

## A/R and A/P journal lines need a Customer or Vendor

QuickBooks rejects a `JournalEntry` line that posts to **Accounts Receivable**
or **Accounts Payable** unless the line includes an `Entity` (a Customer for
A/R, a Vendor for A/P). The error you'd see otherwise is:

> Business Validation Error: When you use Accounts Receivable, you must
> choose a customer in the Name field.

This app handles that automatically:

1. After fetching the chart of accounts, the importer indexes each QBO
   account's `AccountType`.
2. While building each `JournalEntry`, any line whose account type is
   `Accounts Receivable` is tagged for a Customer entity, and any
   `Accounts Payable` line is tagged for a Vendor.
3. Just before POSTing, the importer resolves each tag to a real QBO
   entity Id - reusing one if it already exists, otherwise creating it on
   the fly. The `Entity` block is then injected into the line.

### Where the entity name comes from

Per row, in priority order:

- A/R lines: `customer_name` -> `client_name` -> `client_id` -> `matter_id` -> `PCLaw Test Customer`.
- A/P lines: `vendor_name` -> `vendor` -> `PCLaw Test Vendor`.

So you can either:

- Add a `customer_name` and/or `vendor_name` column to your GL CSV (already
  done in `test_data/02_general_ledger.csv`), or
- Leave them blank and the importer will create one shared
  `PCLaw Test Customer` and `PCLaw Test Vendor` in your sandbox.

Either way, no manual setup is required for the MVP.

## Safety notes

- No Intuit secrets are committed to source. They come from environment variables.
- OAuth tokens remain Fernet-encrypted at rest (existing `encryption.py`).
- The original encrypted upload is decrypted only to a tempfile during import,
  then deleted in a `finally` block.
- If `QBO_REAL_IMPORT` is unset, no API calls are made on import - the app
  behaves exactly like before this change.
