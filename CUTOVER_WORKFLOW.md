# Cutover Workflow

This document describes the PCLaw → QuickBooks Online migration
sequence the app is being built around. It is the partner to the
`/cutover` and `/migration-checklist` screens that landed in the
**Cutover Setup + Migration Checklist** PR.

This PR delivers the **foundation**: a place for firms to define their
migration context, a checklist that derives status from existing
work, and beginner-friendly accounting guidance throughout. Risky QBO
writes for opening balances, trust posting, and AR/AP migration are
**not yet built** and are tracked under *Planned next* below.

---

## Why a structured cutover

A PCLaw → QuickBooks migration goes wrong in predictable ways when
firms skip the prep work:

* **No defined cutover date.** Transactions live in both systems and
  reconciliation becomes impossible.
* **GL imported before opening balances.** QuickBooks has no historical
  starting point, so balances are wrong from day one.
* **AR / AP / trust posted blindly with the GL.** Client money and open
  invoices end up duplicated, mis-aged, or in the wrong account.
* **No ending TB check.** The firm has no proof that QuickBooks matches
  PCLaw after import.

The cutover setup screen forces a firm to make these decisions *before*
uploading anything, and the migration checklist surfaces them as
explicit steps.

---

## Vocabulary

| Term                        | Meaning                                                                                                                                                                                                                                            |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Cutover date**            | The day the firm switches from running the books in PCLaw to running them in QuickBooks Online. Transactions on or after this date should live in QBO; older history stays in PCLaw.                                                              |
| **Opening balance date**    | As-of date for the trial balance used to seed QuickBooks. Almost always the day before cutover (cutover 2026-04-01 → opening balance as of 2026-03-31).                                                                                            |
| **Period start / end**      | Bounds of the GL detail being migrated. Most firms migrate one fiscal year of detail; older history is summarized via the opening balance.                                                                                                         |
| **Accounting basis**        | Cash vs accrual. Drives AR/AP treatment — cash basis firms usually skip AR/AP migration entirely.                                                                                                                                                  |
| **Country**                 | Canada / US / Other. Drives tax handling (GST/HST/PST vs sales tax) and a few QBO quirks.                                                                                                                                                          |
| **Clio involved**           | Some firms run trust and billing in Clio and use QBO only for the GL. When true, trust postings should generally happen in Clio rather than QBO.                                                                                                   |

---

## Intended migration sequence

1. **Cutover setup.** Firm admin defines the items above on
   `/cutover`. Persisted per firm.
2. **Chart of accounts upload.** Upload the PCLaw COA as a Chart of
   Accounts report. The app already supports a dry-run preview that
   matches PCLaw accounts to existing QBO accounts and flags soft
   conflicts.
3. **Opening trial balance upload.** Upload the PCLaw TB as of the
   opening balance date. *Today* this is parsed and validated.
   *Planned* — auto-build the opening journal entry from this TB and
   post it to QuickBooks.
4. **QuickBooks connection.** Connect the QBO company that will receive
   the migrated data. Already built.
5. **Account mappings.** Map each PCLaw account number / name to a
   QuickBooks account. Already built.
6. **Dry-run preview.** Open the GL job and review the preflight /
   dry-run preview. Already built.
7. **General ledger import.** Post the period GL to QuickBooks Online.
   Already built, with duplicate protection and one-click reversal.
8. **Ending trial balance check.** Upload the PCLaw TB as of the period
   end and compare to QuickBooks. *Today* the file is parsed and
   validated. *Planned* — automated reconciliation report.
9. **Trust listing check.** Upload the PCLaw trust listing for
   validation. **Trust balances are never auto-posted** — they
   represent client money and need a deliberate per-matter posting
   strategy that the firm confirms.
10. **AR / AP strategy.** Driven by country + accounting basis. *Today*
    no AR/AP migration is performed automatically.
11. **Reconciliation report.** Download or view the reconciliation
    / verification output as proof QuickBooks matches PCLaw.

The `/migration-checklist` page surfaces these steps and derives
status from existing job, report-type, QBO connection, and mapping
records. Steps where the file can be uploaded today but where posting
is not yet built are flagged **Posting planned** in the UI.

---

## Built in this PR

* `cutover_settings` table (additive SQLite migration, backward
  compatible — firms without a row continue to work).
* `cutover_workflow.py` — status derivation + accounting guidance
  strings shared by routes and templates.
* `/cutover` (also reachable at `/migration-setup`) — protected form
  to create or update the firm's cutover context. Idempotent upsert.
* `/migration-checklist` — protected dashboard listing every step,
  with status derived from existing data and a "next recommended step"
  highlight.
* Dashboard next-step nudge — surfaces the first incomplete step and
  links to the matching action.
* Onboarding cross-link — logged-in onboarding page directs firms to
  cutover setup first.
* Nav bar entry — "Checklist" link in the logged-in nav.
* `CUTOVER_WORKFLOW.md` (this file).
* Smoke tests covering create/update, auth scoping, status
  derivation, dashboard next-step display, and backward compatibility
  for firms without settings.

## Planned next (NOT in this PR)

The following items require deliberate accounting decisions and QBO
writes. They are intentionally scoped out of this foundation PR.

* **COA creation in QuickBooks** — promote the COA dry-run preview to
  actual `Account` creates against the connected QBO realm.
* **Opening trial balance → opening journal entry** — generate a single
  JE that establishes opening balances in QuickBooks as of the opening
  balance date, with explicit confirmation and reversal support.
* **Ending TB reconciliation report** — automated diff between the
  ending PCLaw TB and the QuickBooks TB pulled via the QBO Reports
  API.
* **Trust listing reconciliation** — compare PCLaw trust listing to a
  QBO (or Clio) trust account ledger, surface differences per matter,
  but **never auto-post**. Posting is operator-confirmed.
* **AR / AP migration strategy** — at least two paths: summary opening
  JE by customer / vendor (accrual), and skip (cash). UI to pick the
  strategy per firm and execute it.
* **Reconciliation report download** — single PDF / CSV bundle that the
  firm can keep as audit evidence.

---

## Backward compatibility

* Existing firms with no `cutover_settings` row continue to work. The
  dashboard renders the "cutover setup" step as **Not started** and
  routes default to safe behavior.
* The schema change is purely additive (`CREATE TABLE IF NOT EXISTS`)
  and committed inside the existing `AppDB._migrate` pattern, so a
  redeploy onto an existing SQLite file is a no-op for everything
  except creating the new empty table.
* No existing route, form, or template was removed. The dashboard,
  onboarding, GL upload, multi-report support, mapping, QBO OAuth,
  import, duplicate protection, reversal, readiness, disconnect,
  operator panel, and reports all continue to function as before.

---

## Auditing

Every save on `/cutover` writes a `cutover_settings_saved` audit log
entry scoped to the firm + user that performed the save. The values
themselves are configuration the firm admin types in and are not
treated as secrets, but the audit row is useful for tracing when the
migration plan changed mid-project.
