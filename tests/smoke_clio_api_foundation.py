"""Clio Accounting API v1 foundation smoke tests.

Run from project root:

    python3 tests/smoke_clio_api_foundation.py

This is the INTERNAL, back-pocket foundation for the future Clio Accounting
migration service. It must stay off the public site and must never regress the
public PC Law -> QuickBooks Online workflow. These tests cover the pieces added
on top of the PR #122 readiness lanes:

  T1  Capability registry: endpoint roadmap families + operations + statuses,
      write-support classification, serializable snapshot marked internal.
  T2  Adapter: live mode disabled by default -> writes return a structured
      *blocked* result (never silent success) + carry an idempotency key.
  T3  Adapter: even enabled+configured, unimplemented live client fails closed;
      config summary never leaks the token.
  T4  Payload builders: stable canonical payloads, idempotency metadata, and a
      real balancing invariant on journal entries.
  T5  Lane plans: both Clio lanes have data-flow steps and never post to QBO.
  T6  Operator readiness view: operator-gated (logged-out -> login, non-operator
      -> 404), noindex, shows dry-run + capability matrix.
  T7  Public non-exposure: landing, intake, sitemap, robots carry no Clio
      Accounting API/readiness exposure.
  T8  PC Law -> QBO default/NULL lane still posts to QuickBooks; Clio lanes stay
      blocked from QBO posting (guards the safety gate end to end).
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-clio-api-secret")
os.environ["OPERATOR_EMAILS"] = "op@cutovr.test"
# Ensure a clean, disabled-by-default adapter state.
for _k in ("CLIO_ACCOUNTING_API_ENABLED", "CLIO_ACCOUNTING_API_BASE_URL",
           "CLIO_ACCOUNTING_API_TOKEN"):
    os.environ.pop(_k, None)
for _k in ("SMTP_HOST", "MAIL_SERVER", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
    os.environ.pop(_k, None)

import app as appmod  # noqa: E402
import service_lanes as sl  # noqa: E402
import clio_accounting as ca  # noqa: E402
import clio_accounting_capabilities as caps  # noqa: E402
import clio_accounting_payloads as pb  # noqa: E402
import clio_accounting_lanes as lanes  # noqa: E402


def t1_capability_registry():
    snap = caps.registry_snapshot()
    assert snap["internal_only"] is True
    assert snap["docs_published"] is False
    assert snap["source"] == "assumed_from_roadmap"
    assert snap["max_page_size"] == 200

    families = {f.family for f in caps.families()}
    for expected in (
        "ledger_accounts", "journal_entries", "reports", "vendor_bills",
        "vendor_bill_payments", "clients", "matters", "vendors", "expenses",
    ):
        assert expected in families, f"missing family {expected}"

    # Specific roadmap statuses.
    assert caps.get_status("ledger_accounts.read") == caps.STATUS_READ_ONLY
    assert caps.get_status("ledger_accounts.create") == caps.STATUS_FEATURE_FLAG_DISABLED
    assert caps.get_status("ledger_accounts.deactivate") == caps.STATUS_FEATURE_FLAG_DISABLED
    assert caps.get_status("ledger_accounts.reactivate") == caps.STATUS_FEATURE_FLAG_DISABLED
    assert caps.get_status("journal_entries.destroy") == caps.STATUS_FEATURE_FLAG_DISABLED
    assert caps.get_status("reports.read") == caps.STATUS_READ_ONLY
    assert caps.get_status("reports.create") == caps.STATUS_FEATURE_FLAG_DISABLED
    assert caps.get_status("vendor_bills.write") == caps.STATUS_FEATURE_FLAG_DISABLED
    assert caps.get_status("vendor_bill_payments.read") == caps.STATUS_READ_ONLY
    assert caps.get_status("vendor_bill_payments.write") == caps.STATUS_PRODUCTION_PENDING
    assert caps.get_status("clients.read") == caps.STATUS_READ_ONLY
    assert caps.get_status("matters.read") == caps.STATUS_READ_ONLY
    assert caps.get_status("vendors.read") == caps.STATUS_READ_ONLY
    assert caps.get_status("vendors.write") == caps.STATUS_PRODUCTION_PENDING
    assert caps.get_status("expenses.write") == caps.STATUS_PRODUCTION_PENDING

    # Write classification (Clio axis): pending writes are NOT yet supported.
    assert caps.is_write_supported_by_clio("ledger_accounts.create") is True
    assert caps.is_write_supported_by_clio("vendor_bill_payments.write") is False
    assert caps.is_write_supported_by_clio("expenses.write") is False
    # A read op is never a "write supported".
    assert caps.is_write_supported_by_clio("clients.read") is False
    print("T1 OK: capability registry families, statuses, write classification")


def t2_adapter_blocks_writes_by_default():
    ad = ca.get_adapter()
    cfg = ad.config_summary()
    assert cfg["mode"] == "dry_run"
    assert cfg["live_writes_allowed"] is False

    writes = [
        ad.create_ledger_account({"name": "Cash"}),
        ad.update_ledger_account({"name": "Cash"}),
        ad.deactivate_ledger_account({"account_number": "1000"}),
        ad.reactivate_ledger_account({"account_number": "1000"}),
        ad.create_journal_entry({"lines": []}),
        ad.update_journal_entry({"lines": []}),
        ad.destroy_journal_entry({"id": "x"}),
        ad.create_report({"report_type": "trial_balance"}),
        ad.create_vendor_bill({"vendor_ref": "v"}),
        ad.create_vendor_bill_payment({"vendor_ref": "v"}),
        ad.create_vendor({"display_name": "V"}),
        ad.create_expense({"amount": 1}),
    ]
    for r in writes:
        assert r.status == ca.RESULT_BLOCKED, f"{r.operation} -> {r.status}"
        assert r.performed is False, f"{r.operation} claimed performed!"
        assert r.dry_run is True
        assert r.idempotency_key, f"{r.operation} missing idempotency key"
        assert "NOT sent" in r.message
    # Caller-supplied idempotency key is echoed back verbatim.
    r = ad.create_journal_entry({"lines": []}, idempotency_key="my-key-123")
    assert r.idempotency_key == "my-key-123"
    print("T2 OK: adapter disabled -> all writes structured-blocked, never performed")


def t3_adapter_fail_closed_and_no_secret_leak():
    # Enabled + configured (incl. a token) but the live client isn't built:
    # must still fail closed, never pretend to post.
    ad = ca.ClioAccountingAdapter(
        enabled=True, base_url="https://api.example.test", token="super-secret",
    )
    assert ad.live_writes_allowed is True
    cfg = ad.config_summary()
    assert cfg["mode"] == "live"
    assert cfg["token_configured"] is True
    # The token value must never appear in the secret-free summary.
    assert "super-secret" not in repr(cfg)
    r = ad.create_ledger_account({"name": "Cash"})
    assert r.performed is False
    assert r.status == ca.RESULT_BLOCKED
    assert "not implemented" in r.message.lower()
    # A Clio-unsupported write (pending) is blocked with a capability reason,
    # even in live mode.
    r2 = ad.create_expense({"amount": 5})
    assert r2.performed is False and r2.status == ca.RESULT_BLOCKED
    assert "does not yet support" in r2.message
    print("T3 OK: adapter fails closed in live mode; token never leaked")


def t4_payload_builders():
    la = pb.build_ledger_account(number="1000", name="Operating Cash",
                                 account_type="asset", source_ref="job1")
    assert la["account_number"] == "1000" and la["name"] == "Operating Cash"
    assert la["_meta"]["idempotency_key"]
    assert la["_meta"]["schema_version"] == pb.PAYLOAD_SCHEMA_VERSION
    assert la["_meta"]["assumed_schema"] is True

    # Balanced journal entry builds; unbalanced raises.
    je = pb.build_journal_entry(
        entry_date="2026-01-01",
        lines=[
            {"account_number": "1000", "debit": 100.0},
            {"account_number": "4000", "credit": 100.0},
        ],
        source_ref="job1",
    )
    assert je["_meta"]["idempotency_key"]
    assert len(je["lines"]) == 2
    try:
        pb.build_journal_entry(
            entry_date="2026-01-01",
            lines=[{"account_number": "1000", "debit": 100.0}],
        )
        raise AssertionError("unbalanced journal entry should have raised")
    except ValueError:
        pass

    # Deterministic when idempotency key is supplied (stable canonical output).
    a = pb.build_vendor_bill(vendor_ref="V1", bill_date="2026-02-01",
                             total=250, idempotency_key="k1", source_ref="j")
    b = pb.build_vendor_bill(vendor_ref="V1", bill_date="2026-02-01",
                             total=250, idempotency_key="k1", source_ref="j")
    assert a == b, "vendor bill builder not deterministic for fixed inputs"
    assert a["total"] == 250.0

    # Reference placeholder is unresolved until a Clio id is attached.
    ref = pb.build_reference(entity_type="vendor", display_name="Acme LLP")
    assert ref["resolved"] is False and ref["clio_id"] is None
    assert pb.is_reference_resolved(ref) is False
    ref["clio_id"] = "clio-123"
    ref["resolved"] = True
    assert pb.is_reference_resolved(ref) is True

    # Report + payment + expense builders carry idempotency meta + capability.
    for payload in (
        pb.build_report_request(report_type="trial_balance", as_of_date="2026-06-30"),
        pb.build_vendor_bill_payment(vendor_ref="V1", payment_date="2026-03-01", amount=100),
        pb.build_expense(expense_date="2026-03-01", amount=42.5),
    ):
        assert payload["_meta"]["idempotency_key"]

    assert pb.builder_operations_are_known()
    print("T4 OK: payload builders stable, idempotency meta, balance invariant")


def t5_lane_plans():
    for lane in (sl.PCLAW_TO_CLIO_ACCOUNTING, sl.QBO_TO_CLIO_ACCOUNTING):
        p = lanes.plan(lane)
        assert p["is_clio_accounting"] is True
        assert p["uses_qbo_posting"] is False, "Clio lane must not post to QBO"
        assert p["step_count"] >= 1
        # Every step targets a known capability.
        for step in p["steps"]:
            assert caps.capability(step["capability"]) is not None, step["capability"]
    # QBO lane covers the fuller mapping (bills, payments, expenses, reports).
    qbo_caps = {s["capability"] for s in lanes.plan(sl.QBO_TO_CLIO_ACCOUNTING)["steps"]}
    for expected in ("ledger_accounts.create", "journal_entries.create",
                     "vendor_bills.write", "expenses.write", "reports.create"):
        assert expected in qbo_caps, f"QBO plan missing {expected}"
    # Non-clio lane has no plan.
    assert lanes.steps(sl.PCLAW_TO_QBO) == []
    assert len(lanes.all_plans()) == 2
    print("T5 OK: both Clio lane plans present, capability-aligned, never post QBO")


def _signup(client, email, firm="Firm"):
    client.post("/signup", data={
        "firm_name": firm, "email": email,
        "password": "passw0rd!1234", "confirm_password": "passw0rd!1234",
    })
    return appmod.db.get_user_by_email(email)


def t6_operator_view_gated():
    # Logged out -> login redirect.
    anon = appmod.app.test_client()
    r = anon.get("/operator/clio-accounting", follow_redirects=False)
    assert r.status_code in (301, 302)
    assert "/login" in r.headers.get("Location", "")

    # Non-operator user -> 404 (panel existence not confirmed).
    civ = appmod.app.test_client()
    _signup(civ, "civilian@firm.test", "Civ Firm")
    r = civ.get("/operator/clio-accounting")
    assert r.status_code == 404, r.status_code

    # Operator -> 200 with capability matrix + dry-run + noindex.
    op = appmod.app.test_client()
    _signup(op, "op@cutovr.test", "Op Firm")
    r = op.get("/operator/clio-accounting")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'name="robots"' in body and "noindex" in body
    assert "dry_run" in body, "operator view should show dry-run mode"
    # Capability families render.
    assert "Ledger Accounts" in body and "Journal Entries" in body
    assert "Vendor Bill Payments" in body and "Expenses" in body
    # Assumed-from-roadmap framing is explicit; no posting action rendered.
    assert "assumed" in body.lower()
    assert "Send to QuickBooks" not in body
    # Lane plans render for both Clio lanes.
    assert "PC Law to Clio Accounting Readiness" in body
    assert "QuickBooks Online to Clio Accounting Readiness" in body
    print("T6 OK: operator readiness view gated, noindex, dry-run, capabilities")


def t7_public_non_exposure():
    c = appmod.app.test_client()  # logged out
    for path in ("/", "/intake", "/sitemap.xml", "/robots.txt"):
        body = c.get(path).get_data(as_text=True)
        low = body.lower()
        assert "/operator/clio-accounting" not in body, f"{path} exposes operator route"
        assert "clio accounting api" not in low, f"{path} exposes Clio API"
        assert "clio-accounting-readiness" not in low or path in ("/robots.txt",), \
            f"{path} exposes internal readiness path"
    # robots.txt should DISALLOW the operator prefix (internal), if it lists it.
    robots = c.get("/robots.txt").get_data(as_text=True)
    assert "/operator" in robots or "Disallow" in robots
    print("T7 OK: no public exposure of Clio API foundation / operator route")


def _make_gl_job(firm_id, user_id, job_id):
    appmod.db.upsert_job(
        job_id=job_id, firm_id=firm_id, user_id=user_id, company="Co",
        source_file="gl.csv", encrypted_file="gl.csv.enc",
        file_sha256="deadbeef", status="ready",
    )


def t8_qbo_gate_unchanged():
    # Default/NULL-lane firm still posts to QBO.
    c_def = appmod.app.test_client()
    u_def = _signup(c_def, "api-default@firm.test", "Default Firm")
    assert sl.uses_qbo_posting(appmod.db.resolve_service_lane_for_firm(u_def["firm_id"])) is True
    _make_gl_job(u_def["firm_id"], u_def["id"], "api-default-gl")
    r = c_def.get("/send-to-qbo", follow_redirects=False)
    assert "/clio-accounting-readiness" not in r.headers.get("Location", "")

    # Clio-lane firm stays blocked from QBO posting.
    c = appmod.app.test_client()
    u = _signup(c, "api-clio@firm.test", "Clio Firm")
    appmod.db.set_firm_service_lane(u["firm_id"], sl.QBO_TO_CLIO_ACCOUNTING)
    jid = "api-clio-gl"
    _make_gl_job(u["firm_id"], u["id"], jid)
    r = c.post(f"/jobs/{jid}/import-to-qbo", follow_redirects=False)
    loc = r.headers.get("Location", "")
    assert "/clio-accounting-readiness" in loc, loc
    print("T8 OK: PCLaw->QBO still posts; Clio lane still blocked from QBO")


if __name__ == "__main__":
    try:
        t1_capability_registry()
        t2_adapter_blocks_writes_by_default()
        t3_adapter_fail_closed_and_no_secret_leak()
        t4_payload_builders()
        t5_lane_plans()
        t6_operator_view_gated()
        t7_public_non_exposure()
        t8_qbo_gate_unchanged()
        print("\nALL CLIO API FOUNDATION SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
