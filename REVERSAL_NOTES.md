# Reversing an import

This sprint adds an accounting-safe **rollback** for a completed import.
The original JournalEntry records in QuickBooks stay where they are; we
post offsetting records that net them to zero. This matches how a human
bookkeeper would correct a misposted batch and is the only safe way to
undo an import without leaving an audit gap.

## What "reversal" means

For each JournalEntry we created during the original import, we POST a
new JournalEntry to QuickBooks with:

- the **same** lines (same `Amount`, same `AccountRef`, same `Entity`),
- **swapped** `PostingType` — every Debit becomes a Credit and vice versa,
- `TxnDate = today (UTC)` so the offset shows up in the current period,
- `PrivateNote = "Reversal of PCLaw import (job ...); original QBO JournalEntry Id=..."`.

We do **not** call any delete/void endpoint. The original entries stay
visible in QBO's Reports → Journal so an auditor can see both sides of
the correction.

## How to use it

1. Run an import (the existing flow — Connect → Map accounts → Import).
2. On the job detail page, scroll to the new **Reverse this import** panel.
3. Type `REVERSE` (uppercase, no quotes) in the confirm box. This is
   intentional — a single misclick won't trigger a reversal.
4. Click **Reverse import**.
5. The page reloads with a green flash and a list of the reversal Ids
   created in QBO.

After reversal:

- The button is gone. The panel now shows the reversal status, when it
  ran, and the reversal Ids.
- A second `/jobs/<id>/reverse-import` POST is rejected as a duplicate
  (idempotency: 1 import → at most 1 reversal).
- The dashboard's recent activity log records:
  `import_reversal_started`, then `import_reversal_success` (or
  `import_reversal_failed` / `import_reversal_blocked`).

## Idempotency and safety

- **`confirm_reverse=REVERSE`** must be present on the POST. CSRF
  protection still applies.
- **`QBO_REAL_IMPORT=1`** is required, the same gate that protects the
  original import.
- The route refuses if the job has no `success` import yet.
- The route refuses if a reversal already exists for the latest import.
  The DB enforces it via `UNIQUE (import_id)` on `import_reversals`.
- If QBO returns 404 for an original JE Id (someone deleted it manually),
  we abort, **persist the partial state**, log
  `import_reversal_failed`, and tell the user how many reversals were
  created before the abort.
- Token refresh runs before the reversal, same as the import flow. An
  expired refresh token surfaces the friendly "Please reconnect" flash.

## What gets stored

```
data/import_history.sqlite3
├─ imports                  (unchanged from Phase 2)
├─ imported_transactions    (unchanged)
├─ imported_entities        (unchanged)
├─ import_reversals         NEW — 1 row per reversal
└─ reversed_transactions    NEW — N rows linking original_qbo_je_id ↔ reversal_qbo_je_id
```

`import_reversals` columns: `id`, `import_id` (UNIQUE), `job_id`,
`firm_id`, `realm_id`, `status` ('success'|'failed'), `reversed_at`,
`created_by_user_id`, `error`.

`reversed_transactions` columns: `reversal_id`, `transaction_id`,
`original_qbo_je_id`, `reversal_qbo_je_id`, `reversal_doc_number`,
`reversal_txn_date`.

The new tables are created the first time the app starts after this
deploy. No manual migration step is needed.

## Limitations

- **No "reverse one transaction".** Reversal is all-or-nothing for the
  entire import. If you need to undo just one JE, do it manually in QBO
  and don't reverse the whole batch.
- **No re-import after reversal.** The duplicate guard still blocks
  re-uploading the same `file_sha256` or any of the same
  `transaction_id` values into the same realm. To re-import, edit the
  CSV (change either the file content or the transaction_ids) — or, for
  testing, manually `DELETE` the row in `imports` for that
  `file_sha256` + `realm_id`. Removing duplicate-guard rows is by
  design: the reversal mechanism is for fixing wrong-period or
  wrong-company imports, not for re-running.
- **Reversal date is today.** We don't expose a configurable date yet.
  If you need to backdate the offset to match the original period,
  edit the reversal JE's `TxnDate` directly in QBO afterwards.
- **Customer/Vendor entities are not deleted.** Any A/R or A/P lines on
  the reversal still need an `Entity`, which the swap preserves. The
  customers and vendors created during the original import stay in QBO.

## Run / test

The new offline test suite covers reversal end-to-end:

```bash
python3 tests/smoke_reversal.py
```

It mocks the QBO `get_journal_entry` and `create_journal_entry` calls,
runs an import, reverses it, asserts each reversal payload has flipped
PostingType, asserts the second reversal attempt is blocked, and
asserts the reversal records survive a simulated restart.
