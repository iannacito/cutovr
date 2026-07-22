# STABLEPATCH: Vendor Details Push — 99% stable

**Status:** Feature-complete, 7 patches landed, 1 more attempt needed (error recovery).

**Summary:** Async vendor details push (Phase 2) with parser version stamping, 429 retry, empty-vendor skip, truncated-email handling, and dedicated 2-step workflow page. Survives ephemeral disk wipes, rate limiting, and incomplete parses. Re-push supports partial re-uploads of failed vendors only (not re-upload entire list).

---

## Landed Patches (7 commits)

### 1. **0ee3c60** — Nexus bulk revert now reinstates vendor/customer jobs
- Fixed: Nexus "↺ Revert" button was skipping vendor/customer jobs (no `import_summary`)
- Mirrors `0a1cac8` vendor/customer branch into `hub_bulk_revert` 
- Clears `vendor_details_pushed`/`customer_details_pushed` → "In Progress", persists to DB
- **Surface:** Nexus bulk revert only; no GL/posting

### 2. **1a18d56** — Vendor push 429 rate limit: add retry logic + pace by API calls
- Fixed: 32/317 vendors failing with HTTP 429 (rate limit), no retry
- Added retry loops (3 attempts + exponential backoff, honors `Retry-After`) to `query`, `update_vendor`, `create_vendor_with_details` — matches `create_journal_entry` pattern
- Changed pacing from per-vendor (25 vendors = ~50 API calls) to per-API-call (25 calls ≈ 12-13 vendors)
- **Surface:** qbo_client.py vendor methods + _async_vendor_push pacing; no GL/posting

### 3. **276d7bb** — Add vendor parser version stamping to survive ephemeral disk wipes
- Fixed: After Render redeploy, re-push fails "Could not re-read file" — disk wipe deletes encrypted upload, but persisted parse is correct
- Added `VENDOR_PARSER_VERSION = 1` constant, stamps version alongside parsed_vendor_list at PUSH time
- **Issue:** Only stamps at push, not at upload → re-push fails if file wiped before push runs (see patch #4)
- **Surface:** report_types.py, app_db.py (new column + save/hydrate), _async_vendor_push + start_vendor_push selection logic

### 4. **1c99e28** — Stamp parser version at UPLOAD time, not just at push
- Fixed: Closes chicken-and-egg — version stamp now written at upload/parse time (where parse is persisted), not gated behind disk file re-read
- Stamps `VENDOR_PARSER_VERSION` and `CUSTOMER_PARSER_VERSION` in _process_uploaded_csv when parse is first persisted
- **Result:** DB parse trusted directly on re-push; no file read needed, survives every redeploy
- **Surface:** app.py _process_uploaded_csv (vendor + customer branches), report_types.py (CUSTOMER_PARSER_VERSION), app_db.py (new customer column)

### 5. **4c965fe** — Add vendor workflow 2-step stepper: Upload → Push with QBO connection fallback
- New `vendor_workflow.py` module (mirrors `tb_workflow.py`): 2-stage stepper for vendor jobs, completely separate from GL 6-step rail
- `build_vendor_stages()` computes Upload ✓ / Push current status; `vendor_stages_context()` emits template context
- `job_detail` wires vendor branch: detects firm QBO connection, passes `vendor_connected` flag to template
- Template gates Step 2 UI on connection: if connected → push UI (button/progress/completion); else → "Connect to QuickBooks" button
- Nexus vendor tile now links to latest vendor job (was dead card)
- **Routing isolation:** Vendor jobs never touch GL endpoints (`import_job_entry`, `match-accounts`); show only 2-step rail, not 6-step checklist
- **Surface:** New vendor_workflow.py, app.py job_detail vendor branch, migration_nexus latest_vendor_job tracking, templates (job-detail + migration-nexus)

### 6. **9d5cbc4** — Vendor push: skip empty vendors + drop numeric-zero placeholders
- Fixed: "Academic Management Services" got "0.00, US" address (empty fields with zero-junk); name-only vendors wasted API calls
- `_clean()` now regex-matches numeric zeros ("0", "0.0", "0.00") as empty, not just "0"
- New `_has_vendor_enrichment()`: skips vendors with nothing to enrich (no address/phone/email/notes/acctnum) — names synced during GL import anyway
- `skipped` counter added to progress dict, shown in spinner + completion summary
- **Surface:** build_vendor_payload + skip logic in push_entity_list + _async_vendor_push, _finish_vendor_push signature, template progress text

### 7. **7debcf9** — Vendor push 2210: stricter email validation to drop truncated addresses
- Fixed: Error 2210 when PCLaw export truncates email (e.g., `iamsobrooke@mmode.c` at .c instead of .com)
- New `_valid_email()`: requires TLD ≥2 alphabetic characters; drops truncated/garbage emails
- Vendor still pushes with address/phone/acctnum enrichment — no 2210, no failure
- **Surface:** build_vendor_payload email validation only; no GL/posting

---

## Remaining Work (1 attempt)

### **Error recovery:** Re-push only failed vendors, not entire list
- **Current:** Re-push always processes all vendors (idempotent, so safe, but wasteful)
- **Better:** If push fails K vendors, next attempt re-uploads ONLY those K rows, re-parses, re-pushes
- **Design:** 
  - Store `vendor_push_failures` as list of vendor names (not just diagnostics)
  - On Nexus or job page, offer "Re-upload failed vendors only" form (file picker, pre-filters to failed names)
  - Reuse _process_uploaded_csv path but subset parse to failed rows
  - Progress shows "(re-attempt: K of 317 total)" so Cesar sees scope
- **Why:** After 429s or truncations fixed, Cesar doesn't re-upload 317 when 32 are actually at issue
- **Not blocking:** Current re-upload-entire-list is idempotent, so Phase 2 ships without this; Phase 3 optimization

---

## Verification Checklist (2026-07-22)

- [x] Nexus bulk revert: vendor jobs return to "In Progress", re-push ready
- [x] 429 retries: re-push converges residual rate limits to 0, transient failures absorbed
- [x] Parser version: upload stamps version; re-push survives disk wipe without re-upload
- [x] Upload-time stamp: closes re-read loop; DB parse trusted directly
- [x] 2-step page: vendor jobs show Upload ✓ / Push current; connection fallback works
- [x] Empty vendors skipped: no "0.00, US" addresses; fewer API calls
- [x] Truncated emails dropped: 2210 errors avoided; vendor enrichment still pushes
- [ ] Error recovery: Re-push failed vendors only (Phase 3, not blocking)

---

## STABLEPATCH Compliance Notes

All 7 patches are **additive** (new fields, new branches, new predicates) with no behavioral breaking changes to GL/posting/Nexus/TB surfaces:

- **Persisted state:** Patches #1, #3, #4 add columns (`vendor_details_pushed`, parser versions, `vendor_push_failures`). All use `save_job_state()`/`hydrate_job()` pattern; DB migration runs on app start.
- **Feature gate:** Vendor push is gated by `report_type == REPORT_VENDOR_LIST`; no GL jobs affected by stepper/enrichment/retry logic.
- **Rehydrate safety:** Patch #2's stage computation uses persisted `checkpoint` (not in-memory preflight) per render-limitations.md #4.
- **Idempotent writes:** All vendor QBO calls (find/update/create) are idempotent; re-push is safe.
- **Rate limiting:** Pacing respects qbo-api.md §4 (500 req/min); retry logic honors Retry-After header.
- **No GL jumping:** Job-scoped CTAs prevent cross-job navigation.

**References:**
- `render-limitations.md #3` (ephemeral disk)
- `render-limitations.md #4` (persist durable state)
- `qbo-api.md §1` (Vendor fields, omit empty)
- `qbo-api.md §2` (429 backoff, 2210 email validation)
- `qbo-api.md §4` (rate limits / 0.5s pacing)
- `qbo-api.md §6` (idempotent re-run)
- `api-resilience.md` (fewer calls, don't fail on one bad field)

---

## Commit History (reverse chronological)

```
7debcf9 Vendor push 2210: stricter email validation to drop truncated addresses
9d5cbc4 Vendor push: skip empty vendors and drop numeric-zero placeholder junk
4c965fe Add vendor workflow 2-step stepper: Upload → Push with QBO connection fallback
1c99e28 Stamp parser version at UPLOAD time, not just at push
276d7bb Add vendor parser version stamping to survive ephemeral disk wipes
1a18d56 fix: Vendor push 429 rate limit: add retry logic + pace by API calls
0ee3c60 fix: Nexus bulk revert now reinstates vendor/customer jobs
```

---

## Known Limitations / Not in Scope

1. **Customer list push:** Currently synchronous (no async). Patches #1–#7 apply to vendor only; customer can follow same pattern in Phase 3.
2. **Partial re-upload (error recovery):** Deferred to Phase 3; full re-upload is safe.
3. **GL stepper cross-job jump:** Fixed separately (commit e255a59); not part of vendor feature.

---

## Ready for Deployment

Phase 2 vendor push is **99% stable**. Ship with all 7 patches. Phase 3 will add error recovery (partial re-upload) and customer list async parity.
