# URGENT: TB Opening Balance Raw 500 Still Unresolved — Render Log Investigation Needed

**To:** iannacito  
**Subject:** TB opening-balance raw 500 — defensive fixes didn't resolve it; need actual Render logs  
**Date:** 2026-07-08  

---

## Problem

**Live URL:** `https://cutovr.com/jobs/job_20260708185820_PLJYSMWseLBP-m8u/opening-balance`

**Symptom:** Raw 500 error when trying to preview opening balance on a Trial Balance job.

**What we did:** Deployed commit `cedcde2` with three defensive fixes:
1. Wrapped `_job_trial_balance_rows()` in try/except → fallback to empty list
2. Wrapped `resolve_opening_balance_date()` in try/except → fallback with error message
3. Wrapped `_firm_latest_coa_state()` in try/except → fallback with empty COA state
4. Fixed cache eviction: `jobs.pop()` → `qbo_connections.pop()` at two sites

**Result:** Error still occurs. The 500 is happening **outside** the three code paths we hardened, OR one of our guards is itself raising an exception.

---

## What We Need

**Please check Render logs for the actual exception traceback:**

1. **Go to:** Render dashboard → cutovr → Logs
2. **Filter by:** Time around the 500 error (job created `2026-07-08 18:58:20` UTC)
3. **Search for:**
   - `job_20260708185820_PLJYSMWseLBP-m8u` (job ID)
   - `opening-balance` (route name)
   - `Traceback` or `Exception` (exception markers)
4. **Capture the full stack trace** — specifically:
   - Which file and line number raised the exception?
   - What is the exception type and message?
   - Which function in the stack is failing?

**Critical:** Do NOT guess at what might be wrong. The traceback is the single source of truth.

---

## Hypotheses (in order of likelihood)

If you can't find logs, here are fallback debugging steps:

### 1. **The exception handlers themselves have syntax errors**
   - One of the three new try/except blocks is malformed → Python raises `SyntaxError` or `IndentationError` before any route code runs → raw 500
   - **Check:** Run `python -c "import cutovr.app"` locally to verify no import errors
   - **Fix:** Look at lines 7116–7120, 7210–7221, 7243–7250 in `app.py` for indentation/bracket issues

### 2. **`_get_qbo_client()` is raising QBOAuthExpired AFTER our guard**
   - This route already has a guard for QBOAuthExpired at line ~7108, but maybe the guard isn't catching it
   - Or QBOAuthExpired is raised in a different code path
   - **Check:** Is line ~7103–7108 catching the exception correctly? Is QBOAuthExpired imported?
   - **Check logs for:** `QBOAuthExpired` exception

### 3. **A different unguarded call is raising** ⚠️ **HIGH PROBABILITY**
   - There are unguarded calls in `opening_balance_preview()` we didn't wrap:
     - **Line 7191:** `db.get_cutover_settings(user["firm_id"])` — NO guard, could raise on corrupt settings
     - **Line 7280-7282:** `db.list_account_mappings()` — NO guard, called before plan validation
     - **Line 7283:** `build_tb_stages()` — NO guard, could raise on invalid job/workflow state
     - **Line 7289:** `tb_stages_context()` — NO guard, could raise on invalid stages
     - **Line 7300:** `_hub_display_filename()` — NO guard, called in template context
   - **Check logs for:** The exact function name and line number in the traceback
   - **These are the most likely culprits** — all called AFTER our three guarded calls

### 4. **Job hydration is broken**
   - `_job_or_403()` → `_get_job(job_id)` → `db.hydrate_job()` fails
   - This is a 403 or error redirect, but could become raw 500 if `_job_or_403()` itself has a bug
   - **Check logs for:** `hydrate_job` or `_get_job` in the traceback

### 5. **Cache eviction change broke something**
   - The `qbo_connections.pop()` call is raising KeyError or has the wrong job_id
   - **Check:** Is `job_id` defined at lines 2328 and 7146? Is it the right variable?

### 6. **Missing import or function definition**
   - One of the functions we call has been renamed, deleted, or moved
   - **Check logs for:** `ImportError`, `NameError`, `AttributeError`
   - **Example:** Line 7279 imports `tb_workflow` — if that module doesn't exist or `build_tb_stages` was deleted, raw 500

---

## Next Steps

1. **Immediately:** Check Render logs for the actual traceback
2. **Share the traceback** with the exact file, line, function, and exception type
3. **Verify:**
   - Is the exception inside one of our three new guards?
   - Is the exception in a function we didn't wrap?
   - Is the exception in our exception handler itself?
4. **Fix:** Apply a targeted guard to the exact function raising the exception
5. **Test:** Reproduce locally with the live job data before re-deploying

---

## Context

This is the second iteration of debugging this route. The first fix (commit `740f50a`, early 2026-07-08) added guards to `preview_import()` and two other routes. This route (`opening_balance_preview()`) was believed to have three unguarded calls, so we added three guards (`cedcde2`). If those guards didn't work, it means:

- Either we wrapped the wrong functions
- Or the exception is happening in a fourth function we missed
- Or our guard code itself has a bug

**The only way forward is the actual Render traceback.**

---

## Attached Files & Code References

- Live job: `job_20260708185820_PLJYSMWseLBP-m8u`
- Commit deployed: `cedcde2`
- Route file: `cutovr/app.py` lines 7090–7315 (opening_balance_preview function)

### Guards Applied (cedcde2)
- Line 7116–7122: `_job_trial_balance_rows()` ✓
- Line 7149–7171: `_get_qbo_client()` ✓
- Line 7174–7186: `qbo.get_accounts()` ✓
- Line 7210–7221: `resolve_opening_balance_date()` ✓
- Line 7223–7235: `build_opening_balance_plan()` ✓
- Line 7243–7250: `_firm_latest_coa_state()` ✓
- Line 7251–7265: `validate_tb_against_coa()` ✓

### Unguarded Calls Found (likely culprits)
- **Line 7191:** `db.get_cutover_settings(user["firm_id"])` ⚠️ NOT GUARDED
- **Line 7280–7282:** `db.list_account_mappings()` ⚠️ NOT GUARDED
- **Line 7283:** `build_tb_stages()` ⚠️ NOT GUARDED
- **Line 7289:** `tb_stages_context()` ⚠️ NOT GUARDED
- **Line 7300:** `_hub_display_filename()` ⚠️ NOT GUARDED

---

**Please reply with the Render logs or let me know if you need help accessing them.** Once we have the traceback, the fix will be straightforward.

If the traceback points to one of the unguarded calls above (likely), I can apply guards immediately without waiting.
