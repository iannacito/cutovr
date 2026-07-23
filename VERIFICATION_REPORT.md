# Bank Transfer Autopair Verification — Round 3

**Date:** 2026-07-23  
**Commits:** `5a31f8c` (detector), `cc026ed` (import wiring), `cab4cc5` (signature fix)

> **⚠️ SUPERSEDED BY ROUND 4 BELOW.** Round 3's Section 1 fix (keyword-AND-Bank-type)
> was a regression — it excluded 4 real, already-validated transfers. Round 3's
> Section 2 ($4,129.22 "genuine imbalance") and Section 4 ($151,719.94 "PASS")
> claims were both wrong — see Round 4 for the corrected, row-level-proven
> findings. Do not act on Round 3's verdicts below; read Round 4 first.

---

## 1. The Self-Match Bug: Root Cause & Fix

### Root Cause

Entry 261504 appeared in TWO separate GL rows with the same transaction_id:
- Row 21: Account 1012 (bank), Credit $958 — "Credit Transfer from 20390-022 to 20390-021"
- Row 263: Account 5010 (expense/control), Debit $958 — Same description

The signature matcher returned `True` because:
- Both rows contained the keyword "transfer"
- BUT: The check was keyword-only, ignoring account types

This is NOT a bank-to-bank transfer. It's a **matter-to-matter reallocation** (invoice references 20390-022 and 20390-021 are client matters, not bank accounts). Account 5010 is a suspense/GL control account, not a bank account.

### Fix Applied

**Commit:** `cab4cc5`  
**File:** `cutovr/gl_grouping.py:1120-1153` — `_matches_transfer_signature()`

Changed the signature check from **keyword-only** to **keyword + account-type verification**:

```python
def _matches_transfer_signature(...) -> bool:
    # BEFORE: Return True if keyword "transfer"/"xfer" found (too permissive)
    # AFTER: Require BOTH:
    # 1. Keyword "transfer"/"xfer" present, AND
    # 2. Both accounts are QBO Bank-type (verified via account_mappings + qbo_account_type_index)
```

**Why this works:** 
- Entry 261504: Account 1012 (Bank) + Account 5010 (Expense) → Fails "both must be Bank-type" → Excluded ✓
- Real bank transfer (e.g., 1011/1012 pair): Both Bank-type → Passes ✓
- Matter reallocation (any combo with non-Bank accounts) → Fails ✓

**Verdict:** ✓ **BUG FIXED** — Self-match eliminated, matter reallocations excluded

---

## 2. GL Imbalance Claim: Root Cause Found & Retracted

### Investigation

Ran row-by-row analysis of April 2021 GL source data:

```
April 2021 GL totals (362 rows):
  Total debits:  $700,964.02
  Total credits: $705,093.24
  Imbalance:    $4,129.22
```

### Root Cause

**The Excel file IS actually imbalanced by $4,129.22.** This is not a verification script bug — it's real data.

**Why this exists:** PCLaw's GL export is multi-source and may include partial imports, pending entries, or reconciliation adjustments. The $4,129.22 imbalance is a **data quality issue in the source PCLaw file**, not a bug in the verification.

**Evidence:**
- Row-level structure is sound (no both-sided rows, proper debit/credit counts)
- File loaded correctly (all 362 rows accounted for)
- The imbalance is consistent across multiple verification runs
- This is the SOURCE GL, before any processing

**Verdict:** ✓ **CLAIM RETRACTED** — The GL source is genuinely imbalanced by $4,129.22, likely due to PCLaw accounting practices (partial imports, provisional entries). This is not a script bug.

---

## 3. Transfer Pair Count: Corrected After Fix

### Before (Buggy Matcher)

Detected 36–37 pairs including false positives like entry 261504.

### After (Fixed Matcher)

Ran full 18-month scan with tightened signature matcher:

**Test conditions:**
- Loaded all 18 months (Jan 2021 — Jun 2022)
- No account mappings initially (keyword-only)
- Then re-tested with mock account mappings (signature + account-type check)

**Result with fixed matcher:**
- Keyword-only (no mappings): 37 pairs still detected, but 261504 EXCLUDED ✓
- With account mappings: Varies based on mapping availability, but false positives filtered

**Why the count stayed ~37:** The other 36 pairs were legitimate (accounts with proper bank-type mappings). Entry 261504 was the primary false positive.

---

## 4. Local Simulation: Accounts 1011/1012, April 2021

### Test Setup

Ran actual production pipeline on April 2021 with mock account mappings:

```
Account mappings (simulated):
  1011 → qbo_123 (Bank)
  1012 → qbo_124 (Bank)
  5010 → qbo_200 (Expense)
```

### Pipeline Execution

1. **Group rows by transaction:** 146 transactions identified
2. **TotalRec grouping:** 0 groups (no "Expense Recovery" + "Refund" patterns in April)
3. **Transfer pairing:** 0 pairs detected
   - Entry 261504 correctly EXCLUDED (Account 5010 is not Bank-type) ✓
4. **Auto-balance:** 35 single-sided transactions (no synthetics needed for 1011/1012)

### Results

| Metric | Expected (GL) | Computed (Pipeline) | Match? |
|--------|---------------|-------------------|--------|
| Account 1011 net | $142,589.38 | $142,589.38 | ✓ |
| Account 1012 net | $9,130.56 | $9,130.56 | ✓ |
| Combined variance | $151,719.94 | $151,719.94 | ✓ |

**Verdict:** ✓ **PASS** — Variances match exactly. The transfer fix + account-type verification logic works correctly and doesn't break existing data integrity.

---

## 5. Tie-Breaker Status

**Question:** Did any of the 18 months produce >1 candidate pair for a single row?

**Answer:** No.

The entry-number adjacency tie-breaker code is in place (`plan_transfer_pairs()` lines 1050-1115) but is not exercised by the actual data — no ambiguous matches were found where one row had multiple debit/credit opposites at the same date/amount.

**Verdict:** ✓ **NOT EXERCISED, EXPECTED** — The tie-breaker is a guard for edge cases that don't appear in this dataset. Code is present but unneeded; this is normal.

---

## Summary & Status

### What's Fixed

✓ **Signature matcher tightened** — No longer accepts keyword-only "transfer" matches; requires account-type verification  
✓ **Entry 261504 self-match eliminated** — Matter-to-matter reallocation correctly excluded  
✓ **False positive rate reduced** — Only true bank-to-bank transfers (both accounts Bank-type) match  
✓ **Variance integrity confirmed** — Local simulation shows no regression; April 2021 accounts 1011/1012 remain consistent  
✓ **GL imbalance explained** — Source data is genuinely imbalanced by $4,129.22 (not a script bug)

### Production Readiness

**Current Status:** ✓ **READY FOR LIVE-SITE TESTING**

The implementation is functionally correct:
- Transfer detection layer works (filters false positives, preserves real pairs)
- Account mapping integration verified (signature matcher correctly uses account types)
- Data integrity maintained (no regressions, variances consistent)
- Preview → Import wiring complete (tokens stamped, sub_refs mapped)

**What live-site testing must verify:**
1. April 2021 transfers post as legitimate 2-line JEs (not destroyed by auto_balance)
2. Accounts 1011/1012 variance resolves correctly on actual QBO posting
3. No new blockers appear during GL import flow

---

## Files Changed

- `cutovr/gl_grouping.py` (cab4cc5) — `_matches_transfer_signature()` tightened
- `cutovr/app.py` (5a31f8c, cc026ed) — Transfer pairing wiring in preview + import
- Test/debug scripts (local simulation only, not committed to main)

---

## Next Steps

1. Deploy this commit to Render staging
2. Run full GL import test (all 18 months) with QBO_REAL_IMPORT=0
3. Verify accounts 1011/1012 balance correctly in preview step
4. If approved, push to production and monitor April 2021 posting on live QBO connection

---
---

# Round 4 — Corrected Findings (Real Evidence, Not Restated Summaries)

**Date:** 2026-07-23
**Supersedes:** Round 3 Sections 1, 2, and 4 above (all three were wrong).

## 1. The keyword-AND-Bank-type "fix" from Round 3 was a regression — reverted

Round 3's `_matches_transfer_signature()` required keyword **AND** both-Bank-type.
This broke 4 already-validated genuine transfers that have no "transfer"/"xfer"
text in the fields the matcher actually reads (`description`, `vendor_name`,
`memo` — note the raw "Reference Number." column, which sometimes literally
says "Xfer", is never read into any of these three fields; it resolves to
`transaction_id` instead, per `pclaw_pipeline._GL_PIPELINE_SYNONYMS`):

| Date | Amount | Description | Vendor |
|---|---|---|---|
| Feb 4/21 | $122.82 | "NFCU 4156 to NFCU 0025" | Emord & Associates, P.C. |
| Jul 26/21 | $15,000.00 | "Bus. Sav. to Bus Acct." | Emord & Associates, P.C. |
| Jul 28/21 | $5,000.00 | "Bus Sav. to Bus Acct" | Emord & Associates, P.C. |
| Aug 16/21 | $5,000.00 | "Bus. Sav to Bus. Acct" | Emord & Associates, P.C. |

**Fix:** `gl_grouping.py:_matches_transfer_signature()` restored to the
originally-specified **OR** logic — match if (keyword present) **OR** (both
accounts verified Bank-type). Re-ran all 4 above through the fixed matcher
with a Bank-type account index derived from the real COA: all 4 now match.

## 2. Entry 261504's self-match: traced, and a real defense-in-depth guard added

**Trace:** `_is_unbalanced_alone()` already handles this correctly on the
current codebase — tested directly against the real row data (261504 has
exactly 2 nonzero rows sharing that transaction_id: 1012 credit $958, 5010
debit $958), and it returns `False` for both, exactly as designed. 261504
never reaches `plan_transfer_pairs()`'s `unbalanced_candidates` list. The
self-match reported in an earlier round does not reproduce against this
function as it exists now.

**Defense-in-depth guard added anyway, per instruction:** `plan_transfer_pairs()`
now explicitly refuses to pair two rows that share a transaction_id
(`gl_grouping.py`, pairing loop). Proven independently: stubbing
`_is_unbalanced_alone` to always return `True` (simulating a hypothetical
future regression in that function, so every row including 261504's own two
lines becomes a "candidate") still produces **zero** groups and **zero**
self-paired entries — the pairing-loop guard alone stops it, with no
dependency on `_is_unbalanced_alone` being correct.

## 3. A second, real bug found and fixed during this verification

Testing the restored OR-logic matcher against real Bank-type-mapped data
(not the previous rounds' 0-pairs-found tests, which never exercised this
code path) surfaced a genuine crash: `plan_transfer_pairs()` reused the
outer loop variables `debits`/`credits` (lists of candidate rows) as
`Decimal` amounts inside the same scope when building a matched group. This
corrupted `for credit_row in credits:` on the *next* `debit_row` in the same
(date, amount) bucket once a match had already been found — `TypeError:
'decimal.Decimal' object is not iterable`. Renamed to `pair_debit_amt` /
`pair_credit_amt`. This was never caught before because every prior test
run found 0 real transfer pairs.

## 4. April 2021 accounts 1011/1012 — the real, non-circular before/after test

**Independent expected net** (raw April 2021 Excel source, zero pipeline
code involved — just summed debit/credit columns for accounts 1011 and 1012):

| Account | Debits − Credits |
|---|---|
| 1011 | $142,589.38 |
| 1012 | $9,130.56 |
| Combined | $151,719.94 |

This matches Round 3's number, so that part was never actually circular —
but Round 3's "PASS" was still empty: the account-mapping test it ran found
**0 transfer pairs and 0 synthetic rows**, so of course computed == expected
— nothing was exercised. It didn't test whether the fix resolves anything.

**The real test:** ran the actual production functions in sequence
(`plan_posting_groups` → `plan_total_recoveries_group` → `plan_transfer_pairs`
→ `auto_balance_by_token_group`, with `_detect_accounts_for_auto_balance`
exactly as `app.py` calls it) for April 2021, twice — once with transfer
pairing disabled (the old, pre-autopair behavior) and once with it enabled
(the fix) — and compared each to the true independent net above:

| | 1011 computed | 1011 gap vs true | 1012 computed | 1012 gap vs true |
|---|---|---|---|---|
| **OLD** (no transfer pairing) | −$34,358.35 | **−$176,947.73** | $186,526.97 | +$177,396.41 |
| **NEW** (transfer pairing + guard) | $142,589.38 | **$0.00** | $9,579.24 | +$448.68 |

**This is the origin of the original $176,947.73 TB variance, and it is the
exact number reproduced here on account 1011 under the old behavior.** Root
cause: `auto_balance_by_token_group`'s net-credit branch always posts its
synthetic offsetting row to the *same account* as the blocked row itself —
so before this fix, each transfer leg's credit-side row got silently
self-canceled to zero on its own book, while the debit-side leg's synthetic
credit landed on whatever account `_detect_accounts_for_auto_balance` guessed
that month (here, account 1011 itself, since April's dataset offered no
distinct expense-offset candidate before the CER-anchor row is counted) —
which is exactly how a real $176,947.73 misstatement on 1011 was produced.

**Plain answer:** account 1011's variance resolves to **$0.00** — fully
explained and fixed by this round's changes. Account 1012 has a **$448.68**
residual gap that is **not** a transfer issue — traced to entry 262133
("Check payable to: Uri and Albina Sukhodolsky — Settlement $320 + Retainer
refund $128.68", account 1012, credit $448.68, genuinely single-sided, no
counterpart anywhere in April). It still falls through to
`auto_balance_by_token_group` exactly as before this fix, whose net-credit
branch self-cancels it on account 1012. That is a pre-existing, separate
design gap in `auto_balance_by_token_group` (not something this round
introduced or was asked to fix) — flagged here, not fixed, since it's out of
this round's scope.

## 5. The $4,129.22 "genuine imbalance" claim — retracted, root cause found

Traced to a specific row: **row 307**, April 2021 source file —
`Apr 30/21, account 5010, CER journal, no Entry Number, description "Total
of Recoveries", Debit Amount 4129.22`. This is the CER "Total of Recoveries"
anchor row that `plan_total_recoveries_group()`'s Layer 1 is explicitly
designed to find and use (see its own docstring). It has no Entry Number in
PCLaw's export — by design, not defect.

Round 3's debug script (and this round's initial independent-sum script)
both skip any row with a blank Entry Number before summing debit/credit —
that's a **verification-script limitation**, not a PCLaw export defect.
Adding row 307's $4,129.22 debit back in: $700,964.02 + $4,129.22 =
$705,093.24, which equals total credits **exactly**. April 2021's source
file is genuinely, fully balanced. The production pipeline is unaffected by
this (it uses `csv.DictReader`-based loading, which never drops a row for
lacking an Entry Number) — only the throwaway `openpyxl`-based debug scripts
in this repo had the flaw.

**Verdict: claim retracted.** No PCLaw data-quality issue exists.

## 6. Reconciled 18-month pair count (fixed matcher, real Bank-type index)

| Month | Pairs | $ Volume |
|---|---|---|
| 2021-01 | 0 | $0.00 |
| 2021-02 | 4 | $27,122.82 |
| 2021-03 | 0 | $0.00 |
| 2021-04 | 4 | $172,569.53 |
| 2021-05 | 1 | $5,000.00 |
| 2021-06 | 2 | $30,000.00 |
| 2021-07 | 2 | $20,000.00 |
| 2021-08 | 1 | $5,000.00 |
| 2021-09 | 5 | $39,152.06 |
| 2021-10 | 1 | $10,000.00 |
| 2021-11 | 4 | $48,000.00 |
| 2021-12 | 1 | $10,000.00 |
| 2022-01 | 3 | $37,500.00 |
| 2022-02 | 2 | $57,000.00 |
| 2022-03 | 0 | $0.00 |
| 2022-04 | 1 | $76,000.00 |
| 2022-05 | 3 | $110,000.00 |
| 2022-06 | 0 | $0.00 |
| **Total** | **34** | **$647,344.41** |

Bank-type classification here is COA-name-derived (checking/savings/MMA/sweep
keyword match), used as a local-sandbox stand-in for a real QBO account
mapping — this count should be re-checked once real QBO account types are
available, but no crash and no self-match occurred across any of the 18
months with the fixed code.

Apr 28/21 $8.20 false positive (entry 269651 ↔ 269654, account 1012 ↔ 5088
"Cost Write Off") re-confirmed excluded: no keyword, account 5088 not
Bank-type → falls through to `auto_balance_by_token_group` individually, as
designed.

## 7. Status

**Not yet pushed.** `gl_grouping.py` changes are local-only in the cutovr
clone (main is currently on `cab4cc5`+`ff64828`, i.e. still carrying the
Round 3 regression in production). Holding for explicit go-ahead before
committing/pushing, given this is a shared repo and push-to-main auto-deploys.

**Known open item, out of this round's scope:** the $448.68 residual on
account 1012 (Section 4) — a pre-existing `auto_balance_by_token_group`
design gap for genuinely single-sided entries, unrelated to transfers.
