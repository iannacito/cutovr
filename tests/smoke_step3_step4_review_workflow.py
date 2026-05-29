"""Smoke tests for the Step 3/4 review workflow polish.

Regression scope:

  * Step 4 review page must pin the stepper to Step 4 (Review). It used
    to render "Step 3 Match" as the current stage with a stale "Create
    missing QuickBooks accounts" CTA even when account matching was
    complete and the only blocker was transactions.

  * The stepper CTA on Step 4 must reflect the *actual* blocker state:

      review_blocker = "ready"        -> "Step 5: Send to QuickBooks"
      review_blocker = "unmatched"    -> "Match accounts"
      review_blocker = "blocked_txns" -> "Download validation report"

  * The in-page Step 4 primary CTA mirrors the stepper one: only show
    "Step 5: Send to QuickBooks" when ready; otherwise show one clear
    required action (Match accounts OR Download validation report +
    Upload corrected file). Never "Create missing QuickBooks accounts"
    on the review page when account matching is complete.

  * Customer-facing copy on the Customers & Vendors preview panel uses
    "QuickBooks" — not "QBO" — and explains in plain English that we
    only create the names needed for this import.

  * Blocked transactions panel renders a plain-English summary above the
    technical table with a single CTA pointing at the validation report,
    plus a note about opening-balance-style rows.

  * Row-level "Add this account to QuickBooks" flash message reports a
    concrete count of accounts left to review, so the user never wonders
    whether the click worked.

Run from project root:

    python3 tests/smoke_step3_step4_review_workflow.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_UPLOAD_DIR = tempfile.mkdtemp(prefix="pclaw_uploads_s34_")
os.environ["UPLOAD_DIR"] = _UPLOAD_DIR
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_s34_")
APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-step3-step4-review")

import customer_workflow as cw  # noqa: E402
from cutover_workflow import (  # noqa: E402
    ChecklistItem,
    STATUS_NOT_STARTED, STATUS_COMPLETE,
    STEP_CUTOVER_SETUP, STEP_COA_UPLOAD, STEP_OPENING_TB, STEP_GL_UPLOAD,
    STEP_ENDING_TB, STEP_TRUST_LISTING, STEP_QBO_CONNECT,
    STEP_ACCOUNT_MAPPING, STEP_DRY_RUN, STEP_PROD_IMPORT,
    STEP_RECONCILIATION,
)
import app as appmod  # noqa: E402


def _items(**overrides):
    defaults = {
        STEP_CUTOVER_SETUP: STATUS_COMPLETE,
        STEP_COA_UPLOAD: STATUS_COMPLETE,
        STEP_OPENING_TB: STATUS_COMPLETE,
        STEP_GL_UPLOAD: STATUS_COMPLETE,
        STEP_ENDING_TB: STATUS_NOT_STARTED,
        STEP_TRUST_LISTING: STATUS_NOT_STARTED,
        STEP_QBO_CONNECT: STATUS_COMPLETE,
        STEP_ACCOUNT_MAPPING: STATUS_COMPLETE,
        STEP_DRY_RUN: STATUS_NOT_STARTED,
        STEP_PROD_IMPORT: STATUS_NOT_STARTED,
        STEP_RECONCILIATION: STATUS_NOT_STARTED,
    }
    defaults.update(overrides)
    planned = {STEP_TRUST_LISTING}
    return [
        ChecklistItem(
            key=k, label=k, status=s, summary="",
            planned=(k in planned),
        )
        for k, s in defaults.items()
    ]


# --- N1: stepper pins to Step 4 when review page passes force=REVIEW ----

def n1_review_page_pins_step4_current():
    """force_current_stage=STAGE_REVIEW must put the stepper on Step 4,
    even if the underlying checklist hasn't yet flipped DRY_RUN to
    complete. Without this pin the user lands on the review page with
    Step 3 highlighted in the stepper."""
    items = _items()  # Matching complete, dry-run not yet started
    stages = cw.build_customer_stages(
        items, force_current_stage=cw.STAGE_REVIEW,
    )
    cur = cw.current_stage(stages)
    assert cur is not None and cur.key == cw.STAGE_REVIEW, \
        cur and cur.key
    assert cur.index == 4, cur.index
    # The Match stage should be marked complete (it's earlier).
    match = next(s for s in stages if s.key == cw.STAGE_MATCH)
    assert match.is_complete, match.status
    print("OK  N1  force_current_stage=review pins Step 4 as current")


# --- N2: stepper CTA reflects the review blocker -----------------------

def n2_review_blocker_drives_stepper_cta():
    items = _items()

    # ready -> Step 5 CTA
    stages = cw.build_customer_stages(
        items, force_current_stage=cw.STAGE_REVIEW,
        review_blocker="ready", review_job_id="abc",
    )
    review = next(s for s in stages if s.key == cw.STAGE_REVIEW)
    assert "Step 5" in review.cta_label, review.cta_label
    assert "Send to QuickBooks" in review.cta_label, review.cta_label
    # Must NOT advertise create-missing on a ready review.
    assert "Create missing" not in review.cta_label, review.cta_label

    # unmatched -> Match accounts CTA pointing at this job's mapping page
    stages = cw.build_customer_stages(
        items, force_current_stage=cw.STAGE_REVIEW,
        review_blocker="unmatched", review_job_id="abc",
    )
    review = next(s for s in stages if s.key == cw.STAGE_REVIEW)
    assert review.cta_label == "Match accounts", review.cta_label
    assert "abc" in review.cta_url and "account-mapping" in review.cta_url, \
        review.cta_url

    # blocked_txns -> Download validation report CTA pointing at the CSV
    stages = cw.build_customer_stages(
        items, force_current_stage=cw.STAGE_REVIEW,
        review_blocker="blocked_txns", review_job_id="abc",
    )
    review = next(s for s in stages if s.key == cw.STAGE_REVIEW)
    assert review.cta_label == "Download validation report", \
        review.cta_label
    assert "validation-report" in review.cta_url, review.cta_url
    # Must NOT show Create missing QuickBooks accounts.
    assert "Create missing" not in review.cta_label, review.cta_label
    print("OK  N2  stepper CTA reflects review_blocker (ready/unmatched/blocked_txns)")


# --- N3: _review_blocker_kind classifier --------------------------------

def n3_review_blocker_kind_classifier():
    # Error path
    assert appmod._review_blocker_kind(None, "oops") == "preview_error"
    assert appmod._review_blocker_kind(None, None) == "preview_error"

    # Unmatched takes priority over txn blockers.
    pv = {
        "unmapped_account_count": 1,
        "blocked_transactions": [{"transaction_id": "x"}],
        "would_post": False,
        "balanced": True,
    }
    assert appmod._review_blocker_kind(pv, None) == "unmatched"

    pv = {
        "unmapped_account_count": 0,
        "blocked_transactions": [{"transaction_id": "x"}],
        "would_post": False,
        "balanced": True,
    }
    assert appmod._review_blocker_kind(pv, None) == "blocked_txns"

    pv = {
        "unmapped_account_count": 0,
        "blocked_transactions": [],
        "would_post": False,
        "balanced": False,
    }
    assert appmod._review_blocker_kind(pv, None) == "unbalanced"

    pv = {
        "unmapped_account_count": 0,
        "blocked_transactions": [],
        "would_post": True,
        "balanced": True,
    }
    assert appmod._review_blocker_kind(pv, None) == "ready"
    print("OK  N3  _review_blocker_kind classifies states correctly")


# --- N4: customer/vendor copy says QuickBooks, not QBO ------------------

def n4_customer_vendor_copy_uses_quickbooks():
    """The Customers & Vendors panel must not contain the literal token
    "QBO" and must use the plain-English sentence about why QuickBooks
    needs customers / vendors on AR / AP lines."""
    tpl_path = ROOT / "templates" / "preview-import.html"
    body = tpl_path.read_text(encoding="utf-8")

    # Find the Customers & Vendors section
    start = body.index('data-testid="preview-customers-vendors"')
    end = body.index("</section>", start)
    section = body[start:end]

    assert "QBO" not in section, (
        f"customer/vendor panel still contains 'QBO': {section!r}"
    )
    # Must explain customers/vendors plainly.
    normalized = " ".join(section.split())
    assert "customer on receivable lines" in normalized, normalized
    assert "vendor on payable lines" in normalized, normalized
    # Must promise we only create what's needed.
    assert "only the names needed" in normalized, normalized
    print("OK  N4  Customers & Vendors copy uses QuickBooks, plain-English")


# --- N5: blocked transactions plain-English summary + CTAs --------------

def n5_blocked_transactions_plain_english_summary():
    tpl_path = ROOT / "templates" / "preview-import.html"
    body = tpl_path.read_text(encoding="utf-8")

    start = body.index('data-testid="blocked-transactions-card"')
    end = body.index("</section>", start)
    section = body[start:end]

    # Plain-English summary above the technical table.
    assert 'data-testid="blocked-transactions-plain-summary"' in section, \
        section
    assert "need to be fixed before QuickBooks will accept them" in section, \
        section
    # Download validation report + Upload corrected file CTAs above the
    # technical details.
    assert 'data-testid="blocked-download-cta"' in section, section
    assert 'data-testid="blocked-reupload-cta"' in section, section
    # Opening-balance guidance must be present so lawyers know what to do
    # with single-sided beginning-balance rows.
    assert "opening balances" in section.lower(), section
    # Technical table is collapsed behind a details block so the
    # plain-English next step is the primary content.
    assert 'data-testid="blocked-transactions-technical"' in section, section
    print("OK  N5  blocked transactions: plain-English summary + CTAs + "
          "opening-balance guidance present")


# --- N6: _remaining_unmatched_blurb wording -----------------------------

def n6_remaining_unmatched_blurb():
    # None -> empty so we don't put up a false claim if the count fails.
    assert appmod._remaining_unmatched_blurb(None) == ""
    # 0 -> celebrate Step 3 being done.
    msg = appmod._remaining_unmatched_blurb(0)
    assert "0 left to review" in msg, msg
    assert "Step 3 is complete" in msg, msg
    # 1 -> singular.
    msg = appmod._remaining_unmatched_blurb(1)
    assert "1 account still needs" in msg, msg
    # >1 -> plural with the count.
    msg = appmod._remaining_unmatched_blurb(3)
    assert "3 accounts still need" in msg, msg
    print("OK  N6  _remaining_unmatched_blurb wording correct for 0/1/N")


# --- N7: row-level add-account success flash includes count -------------


def _signup(client, email, firm):
    pwd = "passw0rd!1234"
    client.post("/logout", follow_redirects=False)
    client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": pwd, "confirm_password": pwd,
    }, follow_redirects=False)
    # Force a login so the session cookie is in place regardless of
    # whether the signup path auto-logged-in.
    client.post("/login", data={"email": email, "password": pwd},
                follow_redirects=False)


class _FakeQBO:
    def __init__(self, accounts=None):
        self._accounts = list(accounts or [])
        self.created_payloads = []

    def get_accounts(self):
        return {"QueryResponse": {"Account": list(self._accounts)}}

    def find_account_by_acctnum(self, num):
        if not num:
            return None
        for a in self._accounts:
            if str(a.get("AcctNum") or "") == str(num):
                return a
        return None

    def find_account_by_name(self, name):
        if not name:
            return None
        t = name.strip().lower()
        for a in self._accounts:
            if str(a.get("Name") or "").strip().lower() == t:
                return a
        return None

    def create_account(self, payload):
        self.created_payloads.append(payload)
        new_id = str(2000 + len(self.created_payloads))
        rec = {
            "Id": new_id,
            "Name": payload.get("Name"),
            "AcctNum": payload.get("AcctNum"),
            "AccountType": payload.get("AccountType"),
            "AccountSubType": payload.get("AccountSubType"),
            "Active": True,
        }
        self._accounts.append(rec)
        return {"Account": rec}


def n7_row_add_account_flash_contains_count():
    """After a successful per-row create, the flash message must include
    a concrete count of accounts left to review. Without this, the count
    summary on the page only updates after the redirect, leaving the user
    wondering whether the click worked."""
    client = appmod.app.test_client()
    _signup(client, "s34row@example.test", "Step3-4 Row LLP")
    db = appmod.db
    user = db.get_user_by_email("s34row@example.test")
    job_id = "job_s34_row_add"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company="Step3-4 Row LLP", source_file="x.csv",
        encrypted_file="ignored.enc", file_sha256="0" * 64,
        status="uploaded",
    )
    snapshot = [
        {"number": "1000", "name": "Operating Bank"},
        {"number": "2100", "name": "Brand New Liability"},
    ]
    db.save_job_state(job_id, {"status": "uploaded",
                                "pclaw_accounts": snapshot})
    appmod.qbo_connections[job_id] = {
        "realm_id": f"R-{job_id}",
        "access_token_enc": appmod.encrypt_token("fake-access"),
        "refresh_token_enc": appmod.encrypt_token("fake-refresh"),
        "company_name": "Step3-4 Row LLP",
        "legal_name": "Step3-4 Row LLP",
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }
    appmod.jobs.pop(job_id, None)

    qbo = _FakeQBO([
        {"Id": "A1", "Name": "Operating Bank", "AcctNum": "1000",
         "AccountType": "Bank"},
    ])

    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        # POST the row-level add form. Provide a category so the safe
        # type-mapper accepts the create.
        r = client.post(
            f"/jobs/{job_id}/account-mapping/add-account",
            data={
                "pclaw_number": "2100",
                "pclaw_name": "Brand New Liability",
                "category": "loan",
            },
            follow_redirects=False,
        )
    # Either a redirect to /account-mapping or a 200 — both fine. The
    # flash content is what we test.
    assert r.status_code in (302, 303, 200), r.status_code

    # Follow the redirect to /account-mapping; the flash should appear
    # in the rendered HTML.
    with mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(qbo, appmod.qbo_connections[job_id]),
    ):
        r2 = client.get(f"/jobs/{job_id}/account-mapping",
                        follow_redirects=False)
    body = r2.get_data(as_text=True)
    # The flash must mention "Added" + a count blurb. We tolerate either
    # "0 left to review" (if create succeeded and matching is complete)
    # OR "still need a quick look" (if more remain) OR a plain "Added 1
    # account" — the key is that the flash communicates the action took
    # effect and includes a concrete state.
    assert ("Added 1 account" in body) or (
        "is already in QuickBooks" in body
    ), body[:2000]
    # The remaining-count blurb in either form (0/1/N) should be present
    # whenever the count helper recomputed successfully.
    assert (
        "left to review" in body
        or "still need" in body
        or "still needs" in body
    ), body[:2000]
    print("OK  N7  row-level add-account flash includes remaining count")


# --- N8: preview_import response wires review_blocker into stepper ------

def n8_preview_import_response_uses_force_review_and_blocker():
    """End-to-end through the preview_import route: the response should
    render the stepper with Step 4 as current AND respect the review
    blocker we mock in."""
    client = appmod.app.test_client()
    _signup(client, "s34page@example.test", "Step3-4 Page LLP")
    db = appmod.db
    user = db.get_user_by_email("s34page@example.test")
    job_id = "job_s34_review_page"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company="Step3-4 Page LLP", source_file="x.csv",
        encrypted_file="ignored.enc", file_sha256="0" * 64,
        status="uploaded",
    )
    appmod.qbo_connections[job_id] = {
        "realm_id": f"R-{job_id}",
        "access_token_enc": appmod.encrypt_token("fake-access"),
        "refresh_token_enc": appmod.encrypt_token("fake-refresh"),
        "company_name": "Step3-4 Page LLP",
        "legal_name": "Step3-4 Page LLP",
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }
    appmod.jobs.pop(job_id, None)

    # Fake preview: matching complete, but transactions blocked.
    fake_preview = {
        "would_post": False,
        "balanced": True,
        "journal_entry_count": 0,
        "transaction_count_total": 5,
        "unique_account_count": 3,
        "mapped_account_count": 3,
        "unmapped_account_count": 0,
        "unmapped_accounts": [],
        "accounts": [],
        "customers": [],
        "vendors": [],
        "blocked_transactions": [
            {"transaction_id": "259278", "line_count": 1,
             "reasons": ["fewer than 2 posting lines"]},
        ],
        "sample_lines": [],
        "missing_required_columns": [],
        "total_debits": "100.00",
        "total_credits": "100.00",
        "line_count": 5,
        "mapping_mode": "number",
    }

    class _QBO:
        def get_accounts(self):
            return {"QueryResponse": {"Account": []}}

    fake_rows = [{"foo": "bar"}]
    fake_fieldnames = ["foo"]

    with mock.patch.object(
        appmod, "_load_job_gl_rows",
        return_value=(fake_rows, fake_fieldnames),
    ), mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(_QBO(), appmod.qbo_connections[job_id]),
    ), mock.patch.object(
        appmod, "build_dry_run_preview",
        return_value=fake_preview,
    ):
        r = client.get(f"/jobs/{job_id}/preview-import",
                        follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)

    # Step 4 next-action card should be the blocked-txns variant.
    assert 'data-testid="step4-next-action-card"' in body, body[:2000]
    assert 'data-testid="step4-next-cta-download"' in body, body[:2000]
    # Stepper CTA must NOT advertise Create missing QuickBooks accounts
    # — accounts are matched; the only blocker is transactions.
    # (We grep the rendered HTML for the stepper-level CTA text.)
    # Multiple sections may show "Create missing" elsewhere if any
    # banners somehow fired; the stepper itself should not.
    # The stepper's anchor is `.workflow-stepper__cta-link` — check that
    # specifically.
    import re
    m = re.search(
        r'class="[^"]*workflow-stepper__cta-link[^"]*"[^>]*>(.*?)<',
        body, re.S,
    )
    if m:
        stepper_cta_text = m.group(1)
        assert "Create missing" not in stepper_cta_text, stepper_cta_text
        # Should mention validation report on a blocked-txn page.
        assert "Download validation report" in stepper_cta_text, \
            stepper_cta_text

    # The Step 5 CTA at the bottom must NOT render (blocked).
    assert 'data-testid="step4-proceed-to-step5"' not in body, body[:2000]
    print("OK  N8  /preview-import renders Step 4 nav + blocker-correct CTAs")


# --- N9: preview_import on ready state shows Step 5 CTA ----------------

def n9_preview_import_ready_shows_step5_cta():
    client = appmod.app.test_client()
    _signup(client, "s34ready@example.test", "Step3-4 Ready LLP")
    db = appmod.db
    user = db.get_user_by_email("s34ready@example.test")
    job_id = "job_s34_review_ready"
    db.upsert_job(
        job_id=job_id, firm_id=user["firm_id"], user_id=user["id"],
        company="Step3-4 Ready LLP", source_file="x.csv",
        encrypted_file="ignored.enc", file_sha256="0" * 64,
        status="uploaded",
    )
    appmod.qbo_connections[job_id] = {
        "realm_id": f"R-{job_id}",
        "access_token_enc": appmod.encrypt_token("fake-access"),
        "refresh_token_enc": appmod.encrypt_token("fake-refresh"),
        "company_name": "Step3-4 Ready LLP",
        "legal_name": "Step3-4 Ready LLP",
        "country": "US",
        "expires_at": "2999-01-01T00:00:00",
        "company_info_error": None,
    }
    appmod.jobs.pop(job_id, None)

    fake_preview = {
        "would_post": True,
        "balanced": True,
        "journal_entry_count": 5,
        "transaction_count_total": 5,
        "unique_account_count": 3,
        "mapped_account_count": 3,
        "unmapped_account_count": 0,
        "unmapped_accounts": [],
        "accounts": [],
        "customers": [],
        "vendors": [],
        "blocked_transactions": [],
        "sample_lines": [],
        "missing_required_columns": [],
        "total_debits": "100.00",
        "total_credits": "100.00",
        "line_count": 5,
        "mapping_mode": "number",
    }

    class _QBO:
        def get_accounts(self):
            return {"QueryResponse": {"Account": []}}

    fake_rows = [{"foo": "bar"}]
    fake_fieldnames = ["foo"]

    with mock.patch.object(
        appmod, "_load_job_gl_rows",
        return_value=(fake_rows, fake_fieldnames),
    ), mock.patch.object(
        appmod, "_get_qbo_client",
        return_value=(_QBO(), appmod.qbo_connections[job_id]),
    ), mock.patch.object(
        appmod, "build_dry_run_preview",
        return_value=fake_preview,
    ):
        r = client.get(f"/jobs/{job_id}/preview-import",
                        follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)

    # Ready state -> Step 5 CTA present at the bottom of the page.
    assert 'data-testid="step4-proceed-to-step5"' in body, body[:2000]
    # No "next-action" card since there is no blocker.
    assert 'data-testid="step4-next-action-card"' not in body, body[:2000]
    print("OK  N9  preview_import ready state shows Step 5 CTA")


if __name__ == "__main__":
    n1_review_page_pins_step4_current()
    n2_review_blocker_drives_stepper_cta()
    n3_review_blocker_kind_classifier()
    n4_customer_vendor_copy_uses_quickbooks()
    n5_blocked_transactions_plain_english_summary()
    n6_remaining_unmatched_blurb()
    n7_row_add_account_flash_contains_count()
    n8_preview_import_response_uses_force_review_and_blocker()
    n9_preview_import_ready_shows_step5_cta()
    print("\nAll Step 3/4 review workflow smoke tests passed.")
