"""Smoke tests for workflow-state, copy, and CTA-alignment regressions
reported after PR #53.

Covers
------
  W1  Step 5 success flash text never names Step 6 directly. (If a user
      navigates from Step 5 to an earlier page before the flash is
      consumed, the message must not say "ready to move on to Step 6"
      from inside an earlier-step page.)
  W2  The Connected-to-QuickBooks success flash text does not include
      the realm id. The realm id is a long technical identifier that
      belongs in support/operator views only.
  W3  Step 3 "all matched" complete card lays out Back on the left and
      Proceed on the right (matches the rest of the workflow).
  W4  Step 5 "already-imported" success card lays out the secondary
      link on the left and the primary Proceed-to-Step-6 CTA on the
      right.
  W5  Final-balance-check (Step 6) page does NOT show the QuickBooks
      realm id in the prominent "At a glance" card. The realm id only
      appears inside a quiet <details> disclosure (or not at all).
  W6  After a completed migration with optional reports uploaded but
      not separately posted, the Step 6 reconciliation summary does
      NOT mark them as "Pending" — they roll up as "Skipped" with a
      friendly explanation, so the customer doesn't see required-looking
      pending blockers on a finished migration.
  W7  Reconciliation line labels are customer-friendly plain English
      (no "Imported to QuickBooks" / "Account mapping" jargon).

Run from project root::

    python3 tests/smoke_workflow_state_and_copy_cleanup.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="pclaw_uploads_")
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="pclaw_outputs_")
os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-workflow-state-cleanup")

import app as appmod  # noqa: E402
import final_report  # noqa: E402


def w1_step5_success_flash_does_not_name_step6():
    """The Step 5 import success flash must be self-contained. If the
    user navigates away to an earlier-step page before consuming the
    flash, they should not see "you're ready to move on to Step 6" on
    e.g. the Step 3 match-accounts page — that contradicts the page's
    own state."""
    import re
    with open(ROOT / "app.py", "r") as f:
        src = f.read()
    # The legacy phrasing leaked Step 6 wording into the flash; it must
    # be gone from app.py.
    assert "ready to move on to Step 6" not in src, (
        "Step 5 import success flash still mentions 'move on to Step 6' "
        "verbatim — that leaks onto earlier-step pages when a user "
        "navigates back before the flash is consumed."
    )
    # The new flash should refer to the action by name, not by step
    # number, so the message reads cleanly on any page.
    assert "final balance check" in src.lower(), (
        "Step 5 import success flash should refer to the action "
        "(\"final balance check\") rather than \"Step 6\"."
    )
    print("OK  W1  Step 5 success flash no longer names Step 6 by number")


def w2_connect_qbo_flash_does_not_show_realm_id():
    """The Connected-to-QuickBooks success flash must not include the
    raw realm id. Realm ids are long, opaque numeric strings that mean
    nothing to a lawyer and look like garbage in a banner."""
    with open(ROOT / "app.py", "r") as f:
        src = f.read()
    # The literal "(realmId {realm_id})" formatter must be gone from
    # the user-facing flash. Internal audit logging is allowed to keep
    # the realm id.
    assert "(realmId {realm_id})" not in src, (
        "The connect-success flash still includes 'realmId {realm_id}'."
    )
    # The friendly form must still reference the company by name.
    assert "Connected to QuickBooks:" in src
    print("OK  W2  Connect-success flash hides the realm id")


def w3_step3_complete_card_back_left_next_right():
    """In account-mapping.html, the step-complete card must have the
    Back link in DOM order before the Proceed CTA. The two have
    distinct data-testids so we can assert ordering reliably."""
    body = (ROOT / "templates" / "account-mapping.html").read_text()
    back_idx = body.find('data-testid="step3-back-cta"')
    next_idx = body.find('data-testid="step3-next-cta"')
    assert back_idx > 0 and next_idx > 0, (back_idx, next_idx)
    assert back_idx < next_idx, (
        "Step 3 complete card: back CTA must appear before proceed CTA "
        "so the rendered layout puts Back on the left, Proceed on the "
        "right."
    )
    print("OK  W3  Step 3 complete card: back-left, next-right")


def w4_step5_already_imported_back_left_next_right():
    """In send-to-qbo.html, the already-imported success block must put
    the secondary link (Open the job) before the primary Step 6 CTA so
    the layout matches the rest of the workflow."""
    body = (ROOT / "templates" / "send-to-qbo.html").read_text()
    # Restrict to the already-imported block.
    start = body.find('data-testid="already-imported"')
    assert start > 0, "send-to-qbo.html missing already-imported block"
    block = body[start:start + 4000]
    open_idx = block.find("Open the job")
    cta_idx = block.find('data-testid="step5-next-cta"')
    assert open_idx > 0 and cta_idx > 0, (open_idx, cta_idx)
    assert open_idx < cta_idx, (
        "Step 5 already-imported card: secondary link must appear "
        "before the primary CTA so the rendered layout puts the "
        "less-emphatic action on the left."
    )
    print("OK  W4  Step 5 already-imported: back-left, next-right")


def w5_step6_at_a_glance_hides_realm_id():
    """The Step 6 "At a glance" card lists the QBO company by name —
    the realm id may only appear inside a <details> disclosure or not
    at all. The template must not fall back to printing the realm id
    next to / instead of the company name."""
    body = (ROOT / "templates" / "reconcile-balances.html").read_text()
    # The dl#step6-at-a-glance must not contain summary.qbo_realm_id.
    glance_start = body.find('data-testid="step6-at-a-glance"')
    assert glance_start > 0, "step6 at-a-glance card missing"
    glance_end = body.find("</dl>", glance_start)
    glance_block = body[glance_start:glance_end]
    assert "qbo_realm_id" not in glance_block, (
        "Step 6 'At a glance' card still references the realm id — "
        "move it into the quiet technical-details disclosure."
    )
    # The realm id may still appear inside a <details> block.
    print("OK  W5  Step 6 at-a-glance hides realm id (allowed inside <details>)")


def w6_completed_migration_skips_optional_uploaded_reports():
    """When the GL has imported successfully and the firm uploaded
    starting / ending TB + trust listing files but never ran the
    optional opening-balance JE / TB recon / trust recon, those lines
    must roll up as "skipped" (with a friendly explanation), not
    "pending" — otherwise the completed migration looks broken."""
    summary = final_report.build_reconciliation_summary(
        firm_name="Done LLP",
        cutover={"cutover_date": "2026-04-01"},
        jobs=[
            # GL is imported.
            {"id": "gl", "report_type": "general_ledger",
             "import_summary": {"qbo_je_count": 12,
                                "source_transaction_count": 12,
                                "balanced": True}},
            # Starting balances uploaded but not posted as JE.
            {"id": "tb1", "report_type": "trial_balance",
             "status": "uploaded"},
            # Ending balances uploaded but no recon ran.
            {"id": "tb2", "report_type": "trial_balance",
             "status": "uploaded"},
            # Trust listing uploaded but no recon ran.
            {"id": "tl", "report_type": "trust_listing",
             "status": "uploaded"},
        ],
        qbo_connections=[{"company_name": "Demo QBO", "realm_id": "R1"}],
        account_mapping_count=8,
    )
    by_key = {ln.key: ln for ln in summary.lines}
    assert summary.overall_status == final_report.STATUS_COMPLETED, summary.overall_status
    for key in ("starting_balances", "ending_balance", "client_trust"):
        ln = by_key[key]
        assert ln.status == final_report.STATUS_SKIPPED, (
            f"After a completed migration, optional line '{key}' should "
            f"be 'skipped', not '{ln.status}'. Detail: {ln.detail!r}"
        )
        # The detail must explain plainly — no "not yet" / "still
        # pending" language that suggests the migration is blocked.
        assert "pending" not in ln.detail.lower(), (
            f"Line '{key}' skipped-status detail must avoid 'pending' "
            f"wording on a completed migration: {ln.detail!r}"
        )
    print("OK  W6  Completed migration: optional uploaded reports are 'skipped', not 'pending'")


def w7_reconciliation_line_labels_are_plain_english():
    """Customer-facing reconciliation labels must avoid accounting
    jargon. The user complained that "Imported to QuickBooks" /
    "Account mapping" / etc. were confusing on the success screen."""
    summary = final_report.build_reconciliation_summary(
        firm_name="Plain LLP",
        cutover={"cutover_date": "2026-04-01"},
        jobs=[
            {"id": "gl", "report_type": "general_ledger",
             "import_summary": {"qbo_je_count": 1,
                                "source_transaction_count": 1,
                                "balanced": True}},
        ],
        qbo_connections=[],
        account_mapping_count=1,
    )
    labels = {ln.key: ln.label for ln in summary.lines}
    # Friendly customer-facing names — these match the user requirements:
    #   account list, starting balances, transaction history, final
    #   balance check, client trust balances.
    assert labels["import"] == "Transaction history imported", labels["import"]
    assert labels["accounts"] == "Accounts matched", labels["accounts"]
    assert labels["starting_balances"] == "Starting balances", labels["starting_balances"]
    assert labels["ending_balance"] == "Final balance check", labels["ending_balance"]
    assert labels["client_trust"] == "Client trust balances", labels["client_trust"]
    # Legacy jargon must be gone.
    for ln in summary.lines:
        assert ln.label != "Imported to QuickBooks", (
            "Legacy 'Imported to QuickBooks' label still present.")
        assert ln.label != "Account mapping", (
            "Legacy 'Account mapping' label still present.")
    print("OK  W7  Reconciliation line labels use plain English")


def main():
    w1_step5_success_flash_does_not_name_step6()
    w2_connect_qbo_flash_does_not_show_realm_id()
    w3_step3_complete_card_back_left_next_right()
    w4_step5_already_imported_back_left_next_right()
    w5_step6_at_a_glance_hides_realm_id()
    w6_completed_migration_skips_optional_uploaded_reports()
    w7_reconciliation_line_labels_are_plain_english()
    print("\nALL WORKFLOW-STATE / COPY-CLEANUP SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
