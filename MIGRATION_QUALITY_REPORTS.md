# Migration quality reports

This document explains the three customer-confidence layers built on top of
the PCLaw → QuickBooks Online importer:

1. **Dry-run import preview** (non-destructive)
2. **Validation report** (CSV download)
3. **Post-import reconciliation report** (CSV download)

All three are surfaced on the job detail page in a clear migration sequence:

> **Validate → Map → Preview → Connect QBO → Confirm Import → Reconcile / Reverse**

They are intended to give law-firm operators — who are usually not
developers — a way to *see what will happen* before they post real journal
entries, and to *prove what happened* afterwards.

---

## 1. Dry-run import preview

**Route:** `GET /jobs/<job_id>/preview-import`
**Template:** `templates/preview-import.html`
**Logic:** `migration_quality.build_dry_run_preview(rows, qbo_accounts, saved_mappings)`

### What it does

Renders the same parsing + mapping pipeline the real importer uses, but
**stops short of POSTing**. The user sees:

- Number of JournalEntry records that would be created
- Total source debits + credits
- Whether the file balances
- Unique account count, mapped vs unmapped
- Customers and vendors that would be looked up / **created** in QBO
  (required by QBO for A/R and A/P lines)
- A blocked-transactions list (unbalanced txns, rows with both debit and
  credit, etc.)
- A small sample of journal-entry lines so the user can sanity-check the
  posting types and amounts

### What it does *not* do

- Never calls `QBOClient.create_journal_entry`
- Never calls `QBOClient.create_customer` / `create_vendor`
- Only calls `QBOClient.get_accounts()`, which is a read-only QBO query

The smoke test `tests/smoke_migration_quality.py` (T1) wraps the create
methods in `mock.patch(..., side_effect=AssertionError(...))` and walks
the preview path. The assertion fires if any write endpoint is touched.

### CTA placement

The job detail page now shows a six-step migration sequence at the top of
the page with a primary **Preview import** button before the QBO
connect/import controls. The preview page itself surfaces a **Download
validation report (CSV)** button and a deep-link back to **Map accounts**.

---

## 2. Validation report (CSV)

**Route:** `GET /jobs/<job_id>/validation-report.csv`
**Logic:** `migration_quality.render_validation_csv(job, preflight, preview)`

### Contents

A two-column **Field, Value** key/value table followed by a per-account
detail block. It includes:

- Job id, file name, firm-supplied company, created-at
- File SHA-256 (so the user can confirm which file the report refers to)
- Preflight metrics: transaction count, line count, debit/credit totals,
  balanced status, unique account count, missing required columns,
  rows-missing-account / rows-missing-date counts
- Mapping preview: mapping mode (number vs name), mapped count, unmapped
  count, unmapped list, customers/vendors needed, would-post verdict
- Last validation error (if any) translated through the friendly
  validation message helper
- Last import id and status if a prior import attempt exists
- A per-account detail table: PCLaw account, mapping key, mapped?, QBO
  account id, QBO account name, line count

### Security

- **Auth:** `_job_or_403(job_id)` enforces login and firm scoping. An
  unauthenticated user gets a 302 to login; a different firm gets a 404
  (we deliberately avoid 403 to not leak existence).
- **CSV injection protection:** every cell is run through
  `csv_safety.sanitize_csv_cell` (`csv_safety.py`). Cells beginning with
  `=`, `+`, `-`, `@`, TAB, or CR get a single leading tick prepended —
  the OWASP-recommended marker. Excel/Sheets/Calc strip the tick on
  display, so the user sees the original text without ever evaluating
  it as a formula. The smoke test (T3) uploads a GL with a
  `=cmd|' /C calc'!A0` description payload and asserts no exported cell
  begins with `=`.
- **Auditing:** every download writes a `validation_report_download`
  audit entry tied to the job_id and user.

The report is best-effort even when QuickBooks is down — if the QBO
chart-of-accounts call fails, we skip the mapping preview block but
still emit the validation portion so the customer can hand the file to
their accountant.

---

## 3. Post-import reconciliation report

**Route:** `GET /jobs/<job_id>/reconciliation-report.csv`
**Logic:** `migration_quality.build_reconciliation_report` +
`migration_quality.render_reconciliation_csv`

### When it is available

Only after a successful import. We check
`history.get_latest_completed_import_for_job(job_id)`; if no record
exists we flash a friendly message and redirect to the job page. We do
not hand out empty reports.

### Contents

- Job id, QuickBooks company name, import id, imported-at (UTC)
- Status, created JE count, total posted debits + credits
- Intuit support reference (`intuit_tid`) when one is attached to the
  job — useful when contacting Intuit support
- Per-JE detail table: PCLaw transaction id, QBO JournalEntry Id,
  QBO DocNumber, txn date
- Verification block when present: re-fetched QBO debit/credit totals,
  JE count match, debits/credits match, JE ids missing in QBO,
  verification timestamp
- Reversal block when present: reversal status, reversed-at, error

Like the validation report, every cell is sanitized through
`csv_safety.sanitize_csv_cell`, the route is protected with
`_job_or_403`, and the download writes a `reconciliation_report_download`
audit entry.

### CTA placement

- A **Download reconciliation report (CSV)** button appears in the
  *Import receipt* card immediately after a successful import.
- The migration-sequence stepper at the top of the job detail page
  surfaces the same button under **Step 6 · Reconcile / Reverse**.

---

## What this does NOT change

This work is intentionally narrow. It does not:

- Change the QBO OAuth flow or token storage
- Change the duplicate-import guard, the reversal flow, or any audit-log
  schema
- Add or remove any QBO API calls during the real import path
- Touch the PCLaw parser or expand parser coverage
- Modify production-mode confirmation gates (typing `IMPORT` is still
  required before a real production post)
- Modify the operator panel, readiness page, landing page, or auth flows

The preview and reports re-use existing parsed job data, the existing
chart-of-accounts call, and the existing import history. No new database
tables, no new env vars, no new dependencies.

---

## Tests

`tests/smoke_migration_quality.py` covers:

- **T1** Dry-run preview does not call any QBO write/create endpoint.
- **T2** Preview surface includes JE count, mapping status, customers
  needed.
- **T3** Validation CSV is downloadable, has correct content-type +
  attachment header, and sanitizes formula-injection payloads in
  user-controlled description fields.
- **T3b** Validation report enforces auth (302 to login for anonymous)
  and firm scoping (404 for other firms).
- **T4** Reconciliation report requires a completed import; once one
  exists, it includes the created QBO JE ids; it is also firm-scoped.
- **T5** Job-detail page surfaces the new CTAs: Preview import,
  Download validation report, Download reconciliation report, and the
  Migration sequence stepper.

Run it from the repo root:

```bash
python3 tests/smoke_migration_quality.py
```

The existing smoke suites continue to pass — see
`tests/smoke_persistence.py`, `tests/smoke_auth.py`,
`tests/smoke_csrf.py`, `tests/smoke_reversal.py`,
`tests/smoke_security_hardening.py`, `tests/smoke_beta_safety.py`.
