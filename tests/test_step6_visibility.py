"""Regression tests for Step 6 (Reconcile balances) visibility / reachability.

These exercise the pure projection layer (cutover_workflow.build_checklist
-> customer_workflow.build_customer_stages) plus final_report's
reconciliation summary, which together decide whether a lawyer can see and
reach Step 6 on the main site.

The bug these guard against: once a firm uploaded several monthly general
ledgers, a stale ``unmapped_accounts`` snapshot on a *later* monthly file
forced the stepper back to Step 3 (match_blocked) and hid Step 6 entirely —
even though an earlier monthly GL had already been imported to QuickBooks
and the reconciliation summary's import line read "completed".
"""

import customer_workflow as cw
import cutover_workflow as cwf
import final_report as fr


def _setup_cutover():
    return {
        "cutover_date": "2026-04-01",
        "country": "CA",
        "accounting_basis": "cash",
    }


def _imported_gl(**extra):
    job = {
        "report_type": "general_ledger",
        "status": "Imported",
        "preflight": {"ready": True},
        "import_summary": {"qbo_je_count": 3, "reconciliation_built": True},
    }
    job.update(extra)
    return job


def _reconcile_summary(jobs, cutover=None, mapping_count=4):
    return fr.build_reconciliation_summary(
        firm_name="Test Firm",
        cutover=cutover or _setup_cutover(),
        jobs=jobs,
        qbo_connections=[{"company_name": "Test QBO", "realm_id": "1"}],
        account_mapping_count=mapping_count,
    )


def test_step6_current_for_valid_imported_state():
    """A migration with all uploads + an imported GL reaches Reconcile."""
    cutover = _setup_cutover()
    jobs = [
        {"report_type": "chart_of_accounts",
         "coa_create_history": [{"created_count": 5}]},
        {"report_type": "trial_balance", "status": "Trial Balance validated"},
        _imported_gl(),
    ]
    items = cwf.build_checklist(
        cutover, jobs, has_qbo_connection=True, account_mapping_count=4)
    stages = cw.build_customer_stages(items, has_jobs=True)
    current = cw.current_stage(stages)
    assert current is not None
    assert current.key == cw.STAGE_RECONCILE


def test_step6_reachable_from_summary_when_import_complete():
    summary = _reconcile_summary([_imported_gl()])
    import_line = next(l for l in summary.lines if l.key == "import")
    assert import_line.status == fr.STATUS_COMPLETED
    assert summary.overall_status == fr.STATUS_COMPLETED


def test_step6_not_hidden_by_unrelated_failed_upload():
    """A failed / unknown upload must not block reconciliation."""
    cutover = _setup_cutover()
    jobs = [
        {"report_type": "chart_of_accounts",
         "coa_create_history": [{"created_count": 5}]},
        {"report_type": "trial_balance", "status": "Trial Balance validated"},
        _imported_gl(),
        {"report_type": "unknown",
         "status": "Error: We couldn't read the ledger"},
    ]
    items = cwf.build_checklist(
        cutover, jobs, has_qbo_connection=True, account_mapping_count=4)
    stages = cw.build_customer_stages(items, has_jobs=True)
    assert cw.current_stage(stages).key == cw.STAGE_RECONCILE

    summary = _reconcile_summary(jobs)
    import_line = next(l for l in summary.lines if l.key == "import")
    assert import_line.status == fr.STATUS_COMPLETED


def test_step6_forced_visible_even_without_optional_opening_tb():
    """The Reconcile route anchors the stepper to Step 6 whenever the
    import line is complete, so a missing *optional* opening trial balance
    can't leave the rail stuck on an earlier stage and hide Step 6.
    """
    cutover = _setup_cutover()
    jobs = [
        {"report_type": "chart_of_accounts",
         "coa_create_history": [{"created_count": 5}]},
        _imported_gl(),  # no opening TB on file
    ]
    items = cwf.build_checklist(
        cutover, jobs, has_qbo_connection=True, account_mapping_count=4)
    summary = _reconcile_summary(jobs)
    import_line = next(l for l in summary.lines if l.key == "import")
    assert import_line.status == fr.STATUS_COMPLETED
    # Mirror _build_reconcile_view: import complete -> force Reconcile.
    stages = cw.build_customer_stages(
        items, has_jobs=True, match_blocked=False,
        force_current_stage=cw.STAGE_RECONCILE,
    )
    assert cw.current_stage(stages).key == cw.STAGE_RECONCILE


def test_step6_visible_with_multi_gl_when_one_imported():
    """Multi-GL: one monthly GL imported, another still flagged unmapped.

    The imported GL proves the QuickBooks company has the accounts, so the
    stepper must NOT be forced back to Match. With force_current_stage
    anchored to Reconcile (as _build_reconcile_view does when the import
    line is complete), Step 6 stays visible.
    """
    cutover = _setup_cutover()
    jobs = [
        {"report_type": "chart_of_accounts",
         "coa_create_history": [{"created_count": 5}]},
        _imported_gl(),  # month 1 imported
        {"report_type": "general_ledger", "status": "ready",
         "preflight": {"ready": True},
         "unmapped_accounts": ["6000 Office Supplies"]},  # month 2 snapshot
    ]
    items = cwf.build_checklist(
        cutover, jobs, has_qbo_connection=True, account_mapping_count=4)
    # Mirror the route: import complete -> not match_blocked, anchor Reconcile.
    stages = cw.build_customer_stages(
        items, has_jobs=True, match_blocked=False,
        force_current_stage=cw.STAGE_RECONCILE,
    )
    current = cw.current_stage(stages)
    assert current is not None
    assert current.key == cw.STAGE_RECONCILE
    # Every earlier stage must be complete so the rail reads sensibly.
    earlier = [s for s in stages if s.index < current.index]
    assert all(s.is_complete for s in earlier)


def test_step6_blocked_before_any_import():
    """No GL imported yet -> Step 6 not reachable; stepper not on Reconcile."""
    cutover = _setup_cutover()
    jobs = [
        {"report_type": "chart_of_accounts",
         "coa_create_history": [{"created_count": 5}]},
        {"report_type": "general_ledger", "status": "ready",
         "preflight": {"ready": True}},
    ]
    summary = _reconcile_summary(jobs)
    import_line = next(l for l in summary.lines if l.key == "import")
    assert import_line.status != fr.STATUS_COMPLETED

    items = cwf.build_checklist(
        cutover, jobs, has_qbo_connection=True, account_mapping_count=4)
    stages = cw.build_customer_stages(items, has_jobs=True)
    current = cw.current_stage(stages)
    assert current is None or current.key != cw.STAGE_RECONCILE
