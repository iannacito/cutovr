"""Smoke tests for the May-28 workflow QA fixes.

Covers six concrete regressions Dan reported in the migration workflow:

  1. Item 1  — first-upload messaging is plain English, not an "error" /
     verification-style banner.
  2. Item 2  — after-mapping flash is action-oriented (info, not error)
     and avoids looping users back to the same alarming warning.
  3. Item 3  — short alias tokens ("ar", "ap") never silently map an
     unrelated PCLaw account (Wells Fargo Business LOC, Gross
     Salaries-Supp, Maintenance/Repair, etc.) to QuickBooks Accounts
     Receivable / Accounts Payable via substring containment, AND
     coincidental exact-name / exact-number matches to AR/AP accounts
     are vetoed unless the PCLaw name unambiguously indicates AR/AP.
  4. Item 4  — PCLaw Retained Earnings is blocked, not silently created
     as a parallel Equity account; the blocked reason guides the user
     to map it to QuickBooks' built-in Retained Earnings.
  5. Item 5  — Client / Vendor list uploads are recognised, surfaced
     with a "Coming next — not posted yet" status, and never mis-
     classified as Trust Listing / Trial Balance.
  6. Item 6  — Delete confirmation accepts the typed DELETE word
     consistently; legacy jobs with ``encrypted_file=None`` no longer
     surface a raw ``PosixPath / NoneType`` error to the customer.

Tests are pure / unit-style where possible. The Flask delete route is
exercised against the real app via the test client.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("UPLOAD_DIR", tempfile.mkdtemp(prefix="pclaw_uploads_qa_"))
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="pclaw_outputs_qa_"))
os.environ.setdefault("APP_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("IMPORT_HISTORY_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-workflow-qa")

import bulk_upload  # noqa: E402
import app as app_module  # noqa: E402
from coa_apply import map_pclaw_account_to_qbo_type  # noqa: E402


# ---------------------------------------------------------------------------
# Item 3 — AR / AP silent-fallback guard
# ---------------------------------------------------------------------------


def _make_qbo_accounts():
    """A QBO account list shaped like a fresh QuickBooks sandbox."""
    return [
        # Default QuickBooks AR account.
        {"Id": "qbo-ar", "Name": "Accounts Receivable (A/R)",
         "AccountType": "Accounts Receivable", "AcctNum": ""},
        # Default QuickBooks AP account.
        {"Id": "qbo-ap", "Name": "Accounts Payable (A/P)",
         "AccountType": "Accounts Payable", "AcctNum": ""},
        # Sample bank account — coincidentally numbered 2310 (matches
        # the user-reported Wells Fargo LOC AcctNum) but on a *bank*
        # type, not AR. Even an exact-number coincidence on a non-AR
        # account is fine; this exists so we can prove the AR safety
        # check is only filtering the AR side.
        {"Id": "qbo-bank", "Name": "Operating Bank",
         "AccountType": "Bank", "AcctNum": "1100"},
    ]


def i3_alias_tokens_do_not_collide_on_substrings():
    """``ar``/``ap`` substring containment never auto-suggests AR/AP."""
    qbo_accounts = _make_qbo_accounts()
    pclaw = [
        # Each tuple is (number, name) — none of these should be auto-
        # matched to AR or AP. They were the exact accounts that
        # silently mapped to "Accounts receivable — Acc..." in the
        # screenshots Dan attached.
        ("2310", "Wells Fargo Business LOC"),     # "ar" inside "fargo"
        ("2326", "Chase - 7649"),
        ("5130", "Gross Salaries-Supp"),          # "ar" inside "salaries"
        ("5500", "Maintenance/Repair"),           # "ap" inside "repair"
        ("3010", "Common Stock"),
        ("5075", "Business Development"),
    ]
    pclaw_accounts = [{"number": n, "name": nm} for n, nm in pclaw]
    saved_by_key = {}
    rows, summary = app_module._build_account_mapping_rows(
        pclaw_accounts=pclaw_accounts,
        qbo_accounts=qbo_accounts,
        saved_by_key=saved_by_key,
    )
    for r in rows:
        if r["current_qbo_id"] in ("qbo-ar", "qbo-ap"):
            raise AssertionError(
                f"Row {r['pclaw_number']} {r['pclaw_name']} was silently "
                f"auto-matched to a QBO {r['current_qbo_id']} account."
            )
    # All six should be unmatched — Item 3 requires uncertain rows stay
    # unmatched rather than defaulting.
    unmatched_names = [
        r["pclaw_name"] for r in rows
        if not r["is_saved"] and not r["is_suggestion"]
    ]
    if not all(name in unmatched_names for _, name in pclaw):
        raise AssertionError(
            f"Expected every test row to remain unmatched, got: "
            f"{unmatched_names}"
        )


def i3_exact_ar_name_still_matches():
    """Genuine AR rows DO match — the guard isn't an outright AR ban."""
    qbo_accounts = [
        # Use the QBO default name 'Accounts Receivable' so the exact-
        # name auto-match path can fire on a clear AR row.
        {"Id": "qbo-ar", "Name": "Accounts Receivable",
         "AccountType": "Accounts Receivable", "AcctNum": ""},
    ]
    pclaw_accounts = [{"number": "11000", "name": "Accounts Receivable"}]
    rows, _summary = app_module._build_account_mapping_rows(
        pclaw_accounts=pclaw_accounts,
        qbo_accounts=qbo_accounts,
        saved_by_key={},
    )
    if rows[0]["current_qbo_id"] != "qbo-ar":
        raise AssertionError(
            f"Legitimate 'Accounts Receivable' PCLaw row should have "
            f"matched the QBO AR account; got {rows[0]}"
        )


def i3_exact_acctnum_collision_with_ar_is_blocked():
    """Even an exact AcctNum match to a QBO AR account is blocked.

    Real-world scenario: QBO ships an AR account with no AcctNum, but
    if a customer (or sandbox) sets AcctNum=2310 on it AND PCLaw has
    a non-AR account 2310, the silent-default-to-AR is what scared Dan.
    The AR safety gate refuses unless the PCLaw name says AR.
    """
    qbo_accounts = [
        {"Id": "qbo-ar", "Name": "Accounts Receivable",
         "AccountType": "Accounts Receivable", "AcctNum": "2310"},
    ]
    pclaw_accounts = [{"number": "2310", "name": "Wells Fargo Business LOC"}]
    rows, _summary = app_module._build_account_mapping_rows(
        pclaw_accounts=pclaw_accounts,
        qbo_accounts=qbo_accounts,
        saved_by_key={},
    )
    if rows[0]["current_qbo_id"] is not None:
        raise AssertionError(
            "Wells Fargo Business LOC (AcctNum 2310) must not be silently "
            "auto-suggested to a QBO AR account."
        )


def i3_pclaw_name_strongly_implies_ar_helper():
    """Helper recognises real AR markers and rejects substrings."""
    fn = app_module._pclaw_name_strongly_implies_ar_ap
    assert fn("accountsreceivable") is True
    assert fn("accountreceivable") is True
    assert fn("accountspayable") is True
    # Substrings of "ar" / "ap" that historically misfired must NOT
    # trigger the helper.
    for token in (
        "wellsfargobusinessloc",
        "grosssalariessupp",
        "maintenancerepair",
        "commonstock",
        "businessdevelopment",
    ):
        if fn(token):
            raise AssertionError(
                f"PCLaw name {token!r} should NOT strongly imply AR/AP"
            )


# ---------------------------------------------------------------------------
# Item 4 — Retained Earnings safety
# ---------------------------------------------------------------------------


def i4_retained_earnings_is_blocked_not_silently_created():
    """PCLaw Retained Earnings never auto-creates a parallel QBO RE.

    The type-mapper returns ``decision='blocked'`` with a plain-English
    reason pointing the user at QuickBooks' built-in Retained Earnings.
    """
    row = {
        "account_number": "3200",
        "account_name": "Retained Earnings",
        "account_type": "Equity",
        "detail_type": "RetainedEarnings",
    }
    result = map_pclaw_account_to_qbo_type(row)
    if result["decision"] != "blocked":
        raise AssertionError(
            f"Expected RetainedEarnings to be blocked, got "
            f"{result['decision']!r}"
        )
    reason = result.get("blocked_reason") or ""
    if "Retained Earnings" not in reason or "QuickBooks" not in reason:
        raise AssertionError(
            f"Blocked reason should mention QuickBooks Retained Earnings; "
            f"got: {reason!r}"
        )
    # No raw QBO API jargon (AccountType / AccountSubType / RetainedEarnings
    # camelcase) in the customer-facing message.
    for jargon in ("AccountSubType", "AccountType", "Equity / RetainedEarnings"):
        if jargon in reason:
            raise AssertionError(
                f"Blocked reason should not contain API jargon: {jargon!r}"
            )


def i4_net_income_still_skipped():
    """Net Income remains short-circuited (separate from the RE fix)."""
    row = {"account_name": "Net Income (Loss)"}
    result = map_pclaw_account_to_qbo_type(row)
    if result["decision"] != "skipped":
        raise AssertionError(
            f"Expected Net Income to be skipped, got {result['decision']!r}"
        )


# ---------------------------------------------------------------------------
# Item 5 — Client / Vendor list recognition
# ---------------------------------------------------------------------------


def _write_csv(path: Path, rows: list[list[str]]):
    import csv
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow(r)


def i5_client_list_recognised_not_posted():
    """A PCLaw client list is classified as recognized_not_posted."""
    tmp = Path(tempfile.mkdtemp(prefix="pclaw_qa5_"))
    p = tmp / "Client_Listing_2026.csv"
    _write_csv(p, [
        ["Client ID", "Client Name", "Billing Address"],
        ["C-001", "Alpha LLC", "1 Main St"],
        ["C-002", "Beta LLC", "2 Main St"],
    ])
    res = bulk_upload.classify_csv(p, p.name)
    if res.status != bulk_upload.STATUS_RECOGNIZED_NOT_POSTED:
        raise AssertionError(
            f"Expected STATUS_RECOGNIZED_NOT_POSTED, got {res.status!r}"
        )
    if "Client" not in (res.report_label or ""):
        raise AssertionError(
            f"Expected 'Client list' label, got {res.report_label!r}"
        )
    # Customer-facing reason must avoid jargon ("QBO" abbreviation).
    if "QBO" in (res.reason or ""):
        raise AssertionError(
            f"Reason should use 'QuickBooks', not 'QBO'; got: {res.reason!r}"
        )


def i5_vendor_list_recognised_not_posted():
    tmp = Path(tempfile.mkdtemp(prefix="pclaw_qa5_"))
    p = tmp / "vendors_2026.csv"
    _write_csv(p, [
        ["Vendor ID", "Vendor Name", "Address"],
        ["V-001", "Acme Office Supplies", "100 First St"],
        ["V-002", "Beta Realty", "200 Second St"],
    ])
    res = bulk_upload.classify_csv(p, p.name)
    if res.status != bulk_upload.STATUS_RECOGNIZED_NOT_POSTED:
        raise AssertionError(
            f"Expected vendor list recognised; got {res.status!r}"
        )


def i5_summarize_bulk_surfaces_recognised_lists():
    results = [
        bulk_upload.ClassificationResult(
            filename="clients.csv",
            report_type=None,
            report_label="Client list",
            status=bulk_upload.STATUS_RECOGNIZED_NOT_POSTED,
        ),
        bulk_upload.ClassificationResult(
            filename="vendors.csv",
            report_type=None,
            report_label="Vendor list",
            status=bulk_upload.STATUS_RECOGNIZED_NOT_POSTED,
        ),
        bulk_upload.ClassificationResult(
            filename="gl.csv",
            report_type=bulk_upload.REPORT_GENERAL_LEDGER,
            report_label="General Ledger",
            status=bulk_upload.STATUS_CATEGORIZED,
        ),
    ]
    summary = bulk_upload.summarize_bulk(results)
    if summary.get("recognized_not_posted_count") != 2:
        raise AssertionError(
            f"Expected 2 recognised-not-posted entries; got "
            f"{summary.get('recognized_not_posted_count')!r}"
        )
    labels = {entry["label"] for entry in summary.get("recognized_not_posted", [])}
    if labels != {"Client list", "Vendor list"}:
        raise AssertionError(
            f"Expected labels Client list / Vendor list, got {labels}"
        )


# ---------------------------------------------------------------------------
# Item 6 — Delete confirmation flow
# ---------------------------------------------------------------------------


def _make_test_client_and_user():
    """Set up the app's test client and a logged-in user with a job."""
    from app import app, jobs, db
    import uuid

    # Reuse one firm/user across calls. ``create_firm_and_admin`` is the
    # supported helper.
    email = f"qa-{uuid.uuid4().hex[:8]}@example.com"
    firm_id, user_id = db.create_firm_and_admin(
        firm_name="QA Firm",
        email=email,
        password="not-a-real-password-just-for-test",
    )
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    return app, client, jobs, firm_id, user_id


def i6_delete_with_none_encrypted_file_does_not_500():
    """A legacy job with encrypted_file=None deletes cleanly.

    Reproduces the PosixPath / NoneType error Dan saw and verifies
    the new code path treats blank/None as 'no file on disk'.
    """
    app, client, jobs, firm_id, _user_id = _make_test_client_and_user()
    job_id = "qa-delete-1"
    jobs[job_id] = {
        "id": job_id,
        "firm_id": firm_id,
        "company": "QA Firm",
        "filename": "test.csv",
        "encrypted_file": None,          # the legacy bug trigger
        "encrypted_output": None,
        "status": "ready",
        "report_type": "general_ledger",
    }

    resp = client.post(
        f"/jobs/{job_id}/delete",
        data={"confirm_delete": "DELETE"},
        follow_redirects=False,
    )
    if resp.status_code not in (302, 303):
        raise AssertionError(
            f"Delete should redirect to dashboard; got {resp.status_code}"
        )
    # Job should be gone from the in-memory store.
    if job_id in jobs:
        raise AssertionError("Job should have been removed from jobs dict")
    # No PosixPath / NoneType error flash leaked to the user.
    with client.session_transaction() as sess:
        flashes = sess.get("_flashes") or []
    text = " ".join(msg for _cat, msg in flashes).lower()
    for forbidden in ("posixpath", "nonetype", "unsupported operand", "traceback"):
        if forbidden in text:
            raise AssertionError(
                f"Delete flash leaked debug language: {forbidden!r} in {text!r}"
            )


def i6_delete_rejects_missing_confirmation():
    """Empty / wrong confirmation text is rejected with a plain message."""
    app, client, jobs, firm_id, _user_id = _make_test_client_and_user()
    job_id = "qa-delete-2"
    jobs[job_id] = {
        "id": job_id,
        "firm_id": firm_id,
        "company": "QA Firm",
        "filename": "test.csv",
        "encrypted_file": "test.csv.enc",
        "status": "ready",
        "report_type": "general_ledger",
    }
    # Wrong confirmation word.
    resp = client.post(
        f"/jobs/{job_id}/delete",
        data={"confirm_delete": "no"},
        follow_redirects=False,
    )
    if resp.status_code not in (302, 303):
        raise AssertionError(
            f"Expected redirect on bad confirmation; got {resp.status_code}"
        )
    if job_id not in jobs:
        raise AssertionError(
            "Job should NOT have been deleted on unconfirmed delete"
        )


def i6_delete_accepts_lowercase_and_whitespace():
    """Confirmation is case-insensitive and tolerant of whitespace."""
    app, client, jobs, firm_id, _user_id = _make_test_client_and_user()
    job_id = "qa-delete-3"
    jobs[job_id] = {
        "id": job_id,
        "firm_id": firm_id,
        "company": "QA Firm",
        "filename": "test.csv",
        "encrypted_file": "test.csv.enc",
        "status": "ready",
        "report_type": "general_ledger",
    }
    resp = client.post(
        f"/jobs/{job_id}/delete",
        data={"confirm_delete": "  delete  "},
        follow_redirects=False,
    )
    if resp.status_code not in (302, 303):
        raise AssertionError(
            f"Expected redirect on case-insensitive confirmation; "
            f"got {resp.status_code}"
        )
    if job_id in jobs:
        raise AssertionError("Job should have been removed (lowercase delete)")


# ---------------------------------------------------------------------------
# Items 1 & 2 — Upload + mapping copy is plain English, never alarming
# ---------------------------------------------------------------------------


def i1_no_internal_jargon_in_status_labels():
    """The bulk-upload status labels do not use internal terms."""
    # Open the template to confirm no 'automap' / 'verification' / 'QBO'
    # appears in the new copy we added.
    text = (ROOT / "templates" / "bulk-upload-review.html").read_text()
    banned = ("Automap", "automap", "verification needed", " QBO ",
              "Verification Needed")
    for token in banned:
        if token in text:
            raise AssertionError(
                f"Banned internal jargon {token!r} found in "
                f"bulk-upload-review.html"
            )


def i2_account_mapping_blocker_flash_is_info_not_error():
    """Source-code check: blocked-rows flash uses plain-English info copy."""
    text = (ROOT / "app.py").read_text()
    if "We need a bit more information for" in text:
        raise AssertionError(
            "Old 'We need a bit more information' alarming flash text is "
            "still present in app.py; the Item 2 copy was not updated."
        )
    if "still need your choice" not in text:
        raise AssertionError(
            "Expected new plain-English copy 'still need your choice' not "
            "found in app.py."
        )


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


def main():
    tests = [
        ("i3_alias_tokens_do_not_collide_on_substrings",
         i3_alias_tokens_do_not_collide_on_substrings),
        ("i3_exact_ar_name_still_matches", i3_exact_ar_name_still_matches),
        ("i3_exact_acctnum_collision_with_ar_is_blocked",
         i3_exact_acctnum_collision_with_ar_is_blocked),
        ("i3_pclaw_name_strongly_implies_ar_helper",
         i3_pclaw_name_strongly_implies_ar_helper),
        ("i4_retained_earnings_is_blocked_not_silently_created",
         i4_retained_earnings_is_blocked_not_silently_created),
        ("i4_net_income_still_skipped", i4_net_income_still_skipped),
        ("i5_client_list_recognised_not_posted",
         i5_client_list_recognised_not_posted),
        ("i5_vendor_list_recognised_not_posted",
         i5_vendor_list_recognised_not_posted),
        ("i5_summarize_bulk_surfaces_recognised_lists",
         i5_summarize_bulk_surfaces_recognised_lists),
        ("i6_delete_with_none_encrypted_file_does_not_500",
         i6_delete_with_none_encrypted_file_does_not_500),
        ("i6_delete_rejects_missing_confirmation",
         i6_delete_rejects_missing_confirmation),
        ("i6_delete_accepts_lowercase_and_whitespace",
         i6_delete_accepts_lowercase_and_whitespace),
        ("i1_no_internal_jargon_in_status_labels",
         i1_no_internal_jargon_in_status_labels),
        ("i2_account_mapping_blocker_flash_is_info_not_error",
         i2_account_mapping_blocker_flash_is_info_not_error),
    ]
    passed = 0
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"FAIL  {name}: {exc}")
            continue
        passed += 1
        print(f"OK    {name}")
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
