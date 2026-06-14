"""Tests for multiple monthly General Ledger uploads.

A firm migrating a full year off PCLaw uploads one general ledger per month.
Those uploads must all coexist (the bulk path never supersedes its own
batch), an exact byte-for-byte re-upload should warn rather than silently
double, and Step 5 must only ever pick a *valid* active GL as its import
target — never a failed/ superseded/ unknown one.
"""

import app
import bulk_upload as bu
import demo_mode as dm


SUPERSEDED = dm.SUPERSEDED_STATUS_PREFIX + " job_x)"
ARCHIVED = dm.DEMO_ARCHIVED_STATUS_PREFIX + " run_x)"


# --- exact-duplicate detection --------------------------------------------


def test_find_duplicate_upload_matches_on_content_hash():
    existing = [
        {"id": "job_a", "file_sha256": "aaa", "status": "Imported"},
        {"id": "job_b", "file_sha256": "bbb", "status": "parsed"},
    ]
    hit = app.find_duplicate_upload("bbb", existing)
    assert hit is not None
    assert hit["id"] == "job_b"


def test_find_duplicate_upload_distinct_months_are_not_duplicates():
    existing = [
        {"id": "jan", "file_sha256": "hash_jan", "status": "parsed"},
        {"id": "feb", "file_sha256": "hash_feb", "status": "parsed"},
    ]
    # March has its own distinct content -> no duplicate.
    assert app.find_duplicate_upload("hash_mar", existing) is None


def test_find_duplicate_upload_ignores_superseded_and_archived():
    existing = [
        {"id": "old", "file_sha256": "dup", "status": SUPERSEDED},
        {"id": "gone", "file_sha256": "dup", "status": ARCHIVED},
    ]
    # The only matching jobs are inactive, so a re-upload is allowed clean.
    assert app.find_duplicate_upload("dup", existing) is None


def test_find_duplicate_upload_excludes_self():
    existing = [{"id": "me", "file_sha256": "x", "status": "parsed"}]
    assert app.find_duplicate_upload("x", existing, exclude_job_id="me") is None


def test_find_duplicate_upload_no_hash_returns_none():
    existing = [{"id": "a", "file_sha256": "x", "status": "parsed"}]
    assert app.find_duplicate_upload("", existing) is None
    assert app.find_duplicate_upload(None, existing) is None


# --- Step 5 import-target selection ---------------------------------------


def test_importable_gl_filter_excludes_failed_jobs():
    # The shared filter that _firm_importable_gl_jobs applies on top of
    # active-only filtering: failed uploads must drop out.
    jobs = [
        {"id": "good", "report_type": "general_ledger", "status": "parsed"},
        {"id": "bad", "report_type": "general_ledger",
         "status": "Error: We couldn't read the ledger"},
        {"id": "stale", "report_type": "general_ledger", "status": SUPERSEDED},
    ]
    active = dm.filter_active_jobs(jobs)
    importable = [j for j in active if not dm.is_failed_job(j)]
    ids = {j["id"] for j in importable}
    assert ids == {"good"}


def test_multiple_monthly_gls_all_active():
    """Several monthly GLs in one batch all remain active (none superseded)."""
    jobs = [
        {"id": "jan", "report_type": "general_ledger", "status": "parsed"},
        {"id": "feb", "report_type": "general_ledger", "status": "parsed"},
        {"id": "mar", "report_type": "general_ledger", "status": "Imported"},
    ]
    active = dm.filter_active_jobs(jobs)
    importable = [j for j in active if not dm.is_failed_job(j)]
    assert {j["id"] for j in importable} == {"jan", "feb", "mar"}


# --- bulk path keeps every monthly GL -------------------------------------


def test_bulk_required_reports_include_gl_once():
    # The bulk classifier expects at least one GL; multiple monthly GLs are
    # not treated as conflicting duplicates of each other the way a second
    # trial balance would be.
    assert bu.REPORT_GENERAL_LEDGER in bu.REQUIRED_REPORTS


def _gl_result(name):
    return bu.ClassificationResult(
        filename=name,
        report_type=bu.REPORT_GENERAL_LEDGER,
        report_label="General Ledger",
        confidence=bu.CONFIDENCE_HIGH,
        status=bu.STATUS_CATEGORIZED,
    )


def test_multiple_gl_files_not_flagged_as_duplicates():
    results = [_gl_result("gl_jan.csv"), _gl_result("gl_feb.csv"),
               _gl_result("gl_mar.csv")]
    bu.resolve_collisions(results)
    # All three stay categorized (kept), none demoted to duplicate.
    assert all(r.status == bu.STATUS_CATEGORIZED for r in results)
    # But each carries a confirm-the-months annotation.
    assert all(r.warning for r in results)


def test_multiple_coa_files_still_flagged_as_duplicates():
    coa = lambda n: bu.ClassificationResult(  # noqa: E731
        filename=n,
        report_type=bu.REPORT_CHART_OF_ACCOUNTS,
        report_label="Chart of Accounts",
        confidence=bu.CONFIDENCE_HIGH,
        status=bu.STATUS_CATEGORIZED,
    )
    results = [coa("coa1.csv"), coa("coa2.csv")]
    bu.resolve_collisions(results)
    # Two charts of accounts is still suspicious -> needs review.
    assert all(r.status == bu.STATUS_DUPLICATE for r in results)
