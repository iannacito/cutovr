"""Phase 2 smoke test (mocked QBO).

Run from the project root:

    python3 tests/smoke_phase2.py

Exits 0 on success. Verifies:
  T1 import + history + auto-verify
  T2 file-hash duplicate guard
  T3 transaction_id duplicate guard (different file content, same JE ids)
  T4 manual /verify route
  T5 demo mode untouched
  T6 different realm not blocked
  T7 verification flags missing JE in QBO
  T8 ImportHistory unit tests

The QBO API is fully mocked; nothing leaves the machine.
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

os.environ.setdefault("APP_SECRET", "smoke-test")
TMPDB = tempfile.mktemp(suffix=".sqlite3")
TMP_APP_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = TMPDB
os.environ["APP_DB"] = TMP_APP_DB

import os as _os
_os.environ.setdefault("CSRF_DISABLE", "1")
import app as appmod  # noqa: E402

client = appmod.app.test_client()


def _ensure_logged_in():
    """Sign up + log in once so the auth-protected routes work."""
    if appmod.db.authenticate("smoke@example.test", "passw0rd!1234"):
        client.post("/login", data={"email": "smoke@example.test", "password": "passw0rd!1234"})
        return
    client.post(
        "/signup",
        data={
            "firm_name": "Smoke Firm",
            "email": "smoke@example.test",
            "password": "passw0rd!1234",
            "confirm_password": "passw0rd!1234",
        },
    )


_ensure_logged_in()
GL = (ROOT / "test_data" / "02_general_ledger.csv").read_bytes()

FAKE_ACCOUNTS = {
    "QueryResponse": {
        "Account": [
            {"Id": "11", "Name": "Operating Bank", "AcctNum": "1000", "AccountType": "Bank", "Active": True},
            {"Id": "12", "Name": "Trust Bank", "AcctNum": "1010", "AccountType": "Bank", "Active": True},
            {"Id": "13", "Name": "Accounts Receivable", "AcctNum": "1100", "AccountType": "Accounts Receivable", "Active": True},
            {"Id": "14", "Name": "Accounts Payable", "AcctNum": "2000", "AccountType": "Accounts Payable", "Active": True},
            {"Id": "15", "Name": "Client Trust Liability", "AcctNum": "2100", "AccountType": "Other Current Liability", "Active": True},
            {"Id": "16", "Name": "Owner Equity", "AcctNum": "3000", "AccountType": "Equity", "Active": True},
            {"Id": "17", "Name": "Legal Fees Revenue", "AcctNum": "4000", "AccountType": "Income", "Active": True},
            {"Id": "18", "Name": "Rent Expense", "AcctNum": "5000", "AccountType": "Expense", "Active": True},
            {"Id": "19", "Name": "Filing Fees Expense", "AcctNum": "5200", "AccountType": "Expense", "Active": True},
        ]
    }
}

posted = []
created_jes = {}


def fake_je(self, p):
    n = len(posted) + 1
    je = {"Id": str(900 + n), "DocNumber": f"D{n}", "TxnDate": p["TxnDate"], "Line": list(p["Line"])}
    created_jes[je["Id"]] = je
    posted.append(je["Id"])
    return {"JournalEntry": je}


def fake_query(self, sql):
    if "FROM JournalEntry" in sql and "Id =" in sql:
        je_id = sql.split("Id = '")[1].rstrip("'")
        je = created_jes.get(je_id)
        return {"QueryResponse": {"JournalEntry": [je]} if je else {}}
    return {"QueryResponse": {}}


def fake_find(self, n):
    return None


def fake_create_c(self, n):
    return {"Id": f"C_{n}", "DisplayName": n}


def fake_create_v(self, n):
    return {"Id": f"V_{n}", "DisplayName": n}


def setup_job(name, content, realm="REALM-1", company="Sandbox A"):
    time.sleep(1.05)  # job_id timestamp has 1s resolution; avoid collision
    client.post(
        "/upload",
        data={"company_name": "Co", "email": "a@b.c", "ledger_file": (io.BytesIO(content), name)},
        content_type="multipart/form-data",
    )
    jid = sorted(appmod.jobs.keys())[-1]
    appmod.qbo_connections[jid] = {
        "realm_id": realm,
        "company_name": company,
        "access_token_enc": appmod.encrypt_token("a"),
        "refresh_token_enc": appmod.encrypt_token("b"),
        "expires_at": "2099-01-01",
        "connected_at": "x",
    }
    appmod.jobs[jid]["qbo_connected"] = True
    return jid


def main():
    patches = [
        mock.patch.object(appmod.QBOClient, "get_accounts", return_value=FAKE_ACCOUNTS),
        mock.patch.object(appmod.QBOClient, "create_journal_entry", new=fake_je),
        mock.patch.object(appmod.QBOClient, "query", new=fake_query),
        mock.patch.object(appmod.QBOClient, "find_customer_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "find_vendor_by_name", new=fake_find),
        mock.patch.object(appmod.QBOClient, "create_customer", new=fake_create_c),
        mock.patch.object(appmod.QBOClient, "create_vendor", new=fake_create_v),
    ]
    for p in patches:
        p.start()
    try:
        # T1
        appmod.QBO_REAL_IMPORT = True
        j1 = setup_job("gl.csv", GL)
        client.post(f"/jobs/{j1}/import-to-qbo")
        assert "Imported 5" in appmod.jobs[j1]["status"]
        assert appmod.jobs[j1]["verification"]["status"] == "ok"
        hist = appmod.history.get_history_for_job(j1)
        assert len(hist) == 1 and hist[0]["status"] == "success"
        print("T1 OK")

        # T2
        j2 = setup_job("same.csv", GL)
        before = len(posted)
        resp = client.post(f"/jobs/{j2}/import-to-qbo", follow_redirects=True)
        assert appmod.jobs[j2]["status"] == "Duplicate blocked"
        assert len(posted) == before
        assert "Duplicate import blocked" in resp.data.decode()
        print("T2 OK")

        # T3
        j3 = setup_job("mod.csv", GL.replace(b"DEP-001", b"DEP-XXX"))
        before = len(posted)
        resp = client.post(f"/jobs/{j3}/import-to-qbo", follow_redirects=True)
        assert appmod.jobs[j3]["status"] == "Duplicate blocked"
        assert len(posted) == before
        print("T3 OK")

        # T4
        resp = client.post(f"/jobs/{j1}/verify", follow_redirects=True)
        assert "Verification OK" in resp.data.decode()
        print("T4 OK")

        # T5
        appmod.QBO_REAL_IMPORT = False
        j5 = setup_job(
            "d.csv",
            b"transaction_id,date,account_number,account_name,debit,credit\nT,2026-05-05,1,A,1,0\nT,2026-05-05,2,B,0,1\n",
        )
        resp = client.post(f"/jobs/{j5}/import-to-qbo", follow_redirects=True)
        assert "Demo mode: no journal" in resp.data.decode()
        print("T5 OK")

        # T6
        appmod.QBO_REAL_IMPORT = True
        j6 = setup_job("gl2.csv", GL, realm="REALM-2", company="Sandbox B")
        before = len(posted)
        client.post(f"/jobs/{j6}/import-to-qbo")
        assert "Imported 5" in appmod.jobs[j6]["status"]
        assert len(posted) == before + 5
        print("T6 OK")

        # T7
        j7 = setup_job("gl3.csv", GL, realm="REALM-3", company="Sandbox C")
        client.post(f"/jobs/{j7}/import-to-qbo")
        sabotage_id = appmod.jobs[j7]["qbo_results"][0]["Id"]
        created_jes.pop(sabotage_id)
        client.post(f"/jobs/{j7}/verify")
        v = appmod.jobs[j7]["verification"]
        assert v["status"] == "mismatch"
        assert sabotage_id in v["not_found_ids"]
        print("T7 OK")

        # T8
        import import_history

        ih = import_history.ImportHistory(tempfile.mktemp(suffix=".sqlite3"))
        h = "aaa"
        assert ih.has_completed_import(h, "R") is None
        ih.record_import(
            job_id="j",
            realm_id="R",
            file_sha256=h,
            company_name="C",
            transaction_count=2,
            debit_total="10",
            credit_total="10",
            status="success",
            created_transactions=[
                {"transaction_id": "T1", "qbo_je_id": "1", "doc_number": "D1", "txn_date": "x"},
                {"transaction_id": "T2", "qbo_je_id": "2", "doc_number": "D2", "txn_date": "x"},
            ],
            created_entities=[("Customer", "Acme", "C1")],
        )
        assert ih.has_completed_import(h, "R") is not None
        assert ih.has_completed_import(h, "OTHER") is None
        assert ih.has_completed_transactions(["T1", "T2", "T3"], "R") == {"T1", "T2"}
        print("T8 OK")
    finally:
        for p in patches:
            p.stop()
        for p in (TMPDB, TMP_APP_DB):
            try:
                os.unlink(p)
            except OSError:
                pass

    print("\nALL PHASE 2 SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
