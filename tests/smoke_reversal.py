"""Reversal smoke test.

Run from project root:

    python3 tests/smoke_reversal.py

Covers:
  T1 confirm_reverse missing → flash + no reversal posted.
  T2 No prior import → import_reversal_blocked + no QBO writes.
  T3 Happy path: each reversal payload has flipped Debit/Credit while
     preserving AccountRef + Entity. Original JEs stay; new reversal
     records exist; status badge shows "Reversed ✓"; audit logged.
  T4 Second reversal attempt → import_reversal_blocked, no extra QBO writes.
  T5 Cross-firm POST returns 404.
  T6 Demo mode (QBO_REAL_IMPORT off) → blocked, no QBO writes.
  T7 If QBO can't find an original JE Id mid-batch, the reversal is
     marked failed, partial state is persisted, and the audit log
     records import_reversal_failed.

QBO API is fully mocked.
"""

import io
import os
import sys
import tempfile
import time
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret")

import app as appmod  # noqa: E402

GL = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()

FAKE_ACCOUNTS = {
    "QueryResponse": {
        "Account": [
            {"Id": "A11", "Name": "Operating Bank", "AcctNum": "1000", "AccountType": "Bank", "Active": True},
            {"Id": "A12", "Name": "Trust Bank", "AcctNum": "1010", "AccountType": "Bank", "Active": True},
            {"Id": "A13", "Name": "Accounts Receivable", "AcctNum": "1100", "AccountType": "Accounts Receivable", "Active": True},
            {"Id": "A14", "Name": "Accounts Payable", "AcctNum": "2000", "AccountType": "Accounts Payable", "Active": True},
            {"Id": "A15", "Name": "Client Trust Liability", "AcctNum": "2100", "AccountType": "Other Current Liability", "Active": True},
            {"Id": "A16", "Name": "Owner Equity", "AcctNum": "3000", "AccountType": "Equity", "Active": True},
            {"Id": "A17", "Name": "Legal Fees Revenue", "AcctNum": "4000", "AccountType": "Income", "Active": True},
            {"Id": "A18", "Name": "Rent Expense", "AcctNum": "5000", "AccountType": "Expense", "Active": True},
            {"Id": "A19", "Name": "Filing Fees Expense", "AcctNum": "5200", "AccountType": "Expense", "Active": True},
        ]
    }
}

# Posted JE payloads (whatever the route sends to QBO).
posted = []
# Reverse-direction store: every JE the mock created, keyed by Id, so
# get_journal_entry can return them later.
created_jes = {}


def fake_create_je(self, p):
    n = len(posted) + 1
    je_id = str(900 + n)
    je = {
        "Id": je_id,
        "DocNumber": f"D{n}",
        "TxnDate": p["TxnDate"],
        "Line": list(p.get("Line") or []),
        "PrivateNote": p.get("PrivateNote"),
    }
    created_jes[je_id] = je
    posted.append({"id": je_id, "payload": p})
    return {"JournalEntry": je}


def fake_get_je(self, je_id):
    return created_jes.get(je_id)


def fake_query(self, sql):
    if "FROM JournalEntry" in sql and "Id =" in sql:
        je_id = sql.split("Id = '")[1].rstrip("'")
        je = created_jes.get(je_id)
        return {"QueryResponse": {"JournalEntry": [je]} if je else {}}
    return {"QueryResponse": {}}


def fake_company_info(self):
    return {"CompanyInfo": {"CompanyName": "Sandbox R", "LegalName": "X", "Country": "US"}}


def fake_token_exchange(code):
    return {
        "access_token": f"AT_{code}",
        "refresh_token": f"RT_{code}",
        "expires_at": "2099-01-01T00:00:00",
        "token_type": "bearer",
    }


def fake_find(self, n):
    return None


def fake_create_c(self, n):
    return {"Id": f"C_{n}", "DisplayName": n}


def fake_create_v(self, n):
    return {"Id": f"V_{n}", "DisplayName": n}


def signup(client, firm, email, password="passw0rd!1234"):
    return client.post(
        "/signup",
        data={"firm_name": firm, "email": email,
              "password": password, "confirm_password": password},
    )


def setup_and_import(client, firm, email):
    """Sign up, upload, complete OAuth, run a successful import. Returns job_id."""
    signup(client, firm, email)
    time.sleep(1.05)
    client.post(
        "/upload",
        data={"company_name": "Client", "ledger_file": (io.BytesIO(GL), "gl.csv")},
        content_type="multipart/form-data",
    )
    job_id = sorted(appmod.jobs.keys())[-1]
    with client.session_transaction() as s:
        s["pending_job_id"] = job_id
    client.get(f"/oauth/callback?code=X&state={job_id}&realmId=R-{job_id[-4:]}")
    client.post(f"/jobs/{job_id}/import-to-qbo")
    assert "Imported 5" in appmod.jobs[job_id]["status"], appmod.jobs[job_id]["status"]
    return job_id


def main():
    appmod.QBO_REAL_IMPORT = True
    base_patches = [
        mock.patch.object(appmod.QBOClient, "get_accounts", return_value=FAKE_ACCOUNTS),
        mock.patch.object(appmod.QBOClient, "create_journal_entry", new=fake_create_je),
        mock.patch.object(appmod.QBOClient, "get_journal_entry", new=fake_get_je),
        mock.patch.object(appmod.QBOClient, "query", new=fake_query),
        mock.patch.object(appmod.QBOClient, "get_company_info", new=fake_company_info),
        mock.patch.object(appmod.QBOClient, "find_customer_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "find_vendor_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "create_customer", new=fake_create_c),
        mock.patch.object(appmod.QBOClient, "create_vendor", new=fake_create_v),
        mock.patch.object(appmod.qbo_auth, "get_bearer_token", new=fake_token_exchange),
    ]
    for p in base_patches:
        p.start()

    try:
        # T1: confirm_reverse missing
        c = appmod.app.test_client()
        job_id = setup_and_import(c, "Firm A", "alice@a.test")
        before = len(posted)
        r = c.post(f"/jobs/{job_id}/reverse-import", data={}, follow_redirects=True)
        assert "Reversal not confirmed" in r.data.decode(), r.data[:300]
        assert len(posted) == before, "no JEs should have been posted"
        print("T1 OK: missing confirm_reverse rejected")

        # T2: no prior import
        c2 = appmod.app.test_client()
        signup(c2, "Firm B", "bob@b.test")
        time.sleep(1.05)
        c2.post(
            "/upload",
            data={"company_name": "B", "ledger_file": (io.BytesIO(GL), "g.csv")},
            content_type="multipart/form-data",
        )
        job_id_b = sorted(appmod.jobs.keys())[-1]
        before = len(posted)
        r = c2.post(f"/jobs/{job_id_b}/reverse-import",
                    data={"confirm_reverse": "REVERSE"}, follow_redirects=True)
        assert "Nothing to reverse" in r.data.decode()
        assert len(posted) == before
        firm_b = appmod.db.authenticate("bob@b.test", "passw0rd!1234")["firm_id"]
        assert any(a["action"] == "import_reversal_blocked"
                   for a in appmod.db.recent_audit_for_firm(firm_b, 50))
        print("T2 OK: no prior import → blocked")

        # T3: happy path
        before = len(posted)
        # Snapshot original JEs created during the import (Ids 901..905)
        original_ids = sorted(created_jes.keys())
        original_jes = {k: dict(created_jes[k]) for k in original_ids}
        r = c.post(f"/jobs/{job_id}/reverse-import",
                   data={"confirm_reverse": "REVERSE"}, follow_redirects=True)
        body = r.data.decode()
        assert "Reversal complete" in body, body[body.find("flash"):body.find("flash")+400]
        # Five new JEs were posted
        new_posts = posted[before:]
        assert len(new_posts) == 5, len(new_posts)
        # Verify each reversal swapped Debit/Credit while keeping AccountRef + Entity
        for entry in new_posts:
            payload = entry["payload"]
            for line in payload["Line"]:
                detail = line["JournalEntryLineDetail"]
                # Each line must still have AccountRef
                assert "AccountRef" in detail
                # PostingType is one of Debit/Credit
                assert detail["PostingType"] in ("Debit", "Credit")
            # Reversal note references the original. The prefix shouts
            # REVERSAL so the QBO Audit Log/PrivateNote field is unambiguous.
            assert "REVERSAL of PCLaw import" in payload["PrivateNote"]
            # DocNumber is set to "REV-<id>" so it's obvious in the
            # QuickBooks Journal report.
            assert payload.get("DocNumber", "").startswith("REV-")
            # Every line description starts with "REVERSAL" so the report
            # rows themselves are self-labeling.
            for line in payload["Line"]:
                assert (line.get("Description") or "").startswith("REVERSAL")
        # Compare flipped types line-for-line vs the original JEs.
        # DocNumber is "REV-<original_je_id>", which is the stable way to
        # tie a reversal payload back to its original entry.
        for entry in new_posts:
            doc = entry["payload"]["DocNumber"]
            assert doc.startswith("REV-"), doc
            orig_id = doc[len("REV-"):]
            orig = original_jes[orig_id]
            for orig_line, rev_line in zip(orig["Line"], entry["payload"]["Line"]):
                op = orig_line["JournalEntryLineDetail"]["PostingType"]
                rp = rev_line["JournalEntryLineDetail"]["PostingType"]
                assert (op == "Debit") != (rp == "Debit"), (op, rp)
                # AccountRef preserved
                assert orig_line["JournalEntryLineDetail"]["AccountRef"]["value"] == \
                       rev_line["JournalEntryLineDetail"]["AccountRef"]["value"]
                # Entity preserved when present (A/R or A/P lines)
                if "Entity" in orig_line["JournalEntryLineDetail"]:
                    assert rev_line["JournalEntryLineDetail"]["Entity"] == \
                           orig_line["JournalEntryLineDetail"]["Entity"]
        # Reversal row recorded
        last_imp = appmod.history.get_latest_completed_import_for_job(job_id)
        rev = appmod.history.get_reversal_for_import(last_imp["id"])
        assert rev and rev["status"] == "success"
        assert len(rev["transactions"]) == 5
        for t in rev["transactions"]:
            assert t["original_qbo_je_id"] in original_ids
            assert t["reversal_qbo_je_id"]
        # Audit logs
        firm_a = appmod.db.authenticate("alice@a.test", "passw0rd!1234")["firm_id"]
        actions = [a["action"] for a in appmod.db.recent_audit_for_firm(firm_a, 50)]
        assert "import_reversal_started" in actions and "import_reversal_success" in actions
        # Detail page shows "Reversed" status badge
        r = c.get(f"/jobs/{job_id}")
        assert "Reversed" in r.data.decode()
        print("T3 OK: reversal swapped Debit/Credit, preserved Entity, logged + persisted")

        # T4: second reversal attempt blocked
        before = len(posted)
        r = c.post(f"/jobs/{job_id}/reverse-import",
                   data={"confirm_reverse": "REVERSE"}, follow_redirects=True)
        assert "already reversed" in r.data.decode()
        assert len(posted) == before
        print("T4 OK: second reversal blocked, no extra JEs posted")

        # T5: cross-firm POST returns 404
        before = len(posted)
        r = c2.post(f"/jobs/{job_id}/reverse-import",
                    data={"confirm_reverse": "REVERSE"})
        assert r.status_code == 404
        assert len(posted) == before
        print("T5 OK: cross-firm reversal returns 404")

        # T6: demo mode rejects (QBO_REAL_IMPORT off). Use a fresh job
        # so the duplicate-guard check is irrelevant.
        appmod.QBO_REAL_IMPORT = False
        c3 = appmod.app.test_client()
        signup(c3, "Firm C", "carol@c.test")
        # Manually mint a successful "import" for this job by going through
        # a real import flow, which will demo-mode-skip the JE creation.
        # Instead we just hand-write an imports row so the route reaches
        # the QBO_REAL_IMPORT branch.
        time.sleep(1.05)
        c3.post(
            "/upload",
            data={"company_name": "C", "ledger_file": (io.BytesIO(GL), "g.csv")},
            content_type="multipart/form-data",
        )
        jid_c = sorted(appmod.jobs.keys())[-1]
        with c3.session_transaction() as s:
            s["pending_job_id"] = jid_c
        c3.get(f"/oauth/callback?code=X&state={jid_c}&realmId=R-c")
        # Insert a fake successful import so reversal sees something to reverse.
        firm_c = appmod.db.authenticate("carol@c.test", "passw0rd!1234")["firm_id"]
        appmod.history.record_import(
            job_id=jid_c, realm_id=appmod.qbo_connections[jid_c]["realm_id"],
            file_sha256="x", company_name="C",
            transaction_count=1, debit_total="1.00", credit_total="1.00",
            status="success",
            created_transactions=[{
                "transaction_id": "T1", "qbo_je_id": "999",
                "doc_number": "D", "txn_date": "2026-05-05"}],
        )
        before = len(posted)
        r = c3.post(f"/jobs/{jid_c}/reverse-import",
                    data={"confirm_reverse": "REVERSE"}, follow_redirects=True)
        assert "Demo mode" in r.data.decode()
        assert len(posted) == before
        actions = [a["action"] for a in appmod.db.recent_audit_for_firm(firm_c, 50)]
        assert "import_reversal_blocked" in actions
        print("T6 OK: demo mode blocks reversal, no JEs posted")

        # T7: mid-batch QBO 404 → reversal failed, partial state recorded
        appmod.QBO_REAL_IMPORT = True
        c4 = appmod.app.test_client()
        job_id_d = setup_and_import(c4, "Firm D", "dave@d.test")
        # Sabotage: make the second JE fetch return None
        original_get = fake_get_je
        sabotage_state = {"calls": 0}
        def sabotage_get(self, je_id):
            sabotage_state["calls"] += 1
            if sabotage_state["calls"] == 2:
                return None
            return original_get(self, je_id)
        with mock.patch.object(appmod.QBOClient, "get_journal_entry", new=sabotage_get):
            before = len(posted)
            r = c4.post(f"/jobs/{job_id_d}/reverse-import",
                        data={"confirm_reverse": "REVERSE"}, follow_redirects=True)
        body = r.data.decode()
        assert "Reversal failed" in body, body[body.find("flash"):body.find("flash")+300]
        new_posts = len(posted) - before
        assert new_posts == 1, new_posts  # only one reversal succeeded before abort
        # Reversal row recorded with status='failed'
        last_imp = appmod.history.get_latest_completed_import_for_job(job_id_d)
        rev = appmod.history.get_reversal_for_import(last_imp["id"])
        assert rev and rev["status"] == "failed"
        assert rev["error"]
        firm_d = appmod.db.authenticate("dave@d.test", "passw0rd!1234")["firm_id"]
        actions = [a["action"] for a in appmod.db.recent_audit_for_firm(firm_d, 50)]
        assert "import_reversal_failed" in actions
        print("T7 OK: QBO 404 mid-batch → reversal failed, partial state persisted")
    finally:
        for p in base_patches:
            p.stop()
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass

    print("\nALL REVERSAL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
