"""Onboarding CSV/template/sample download smoke tests.

Run from project root:

    python3 tests/smoke_onboarding_downloads.py

Pins the QA-reported requirement that every CSV download link on the
public Onboarding page returns an actual downloadable CSV — not a 404,
HTML error page, or empty body.

Covers:
  T1 /onboarding/template.csv -> 200, text/csv, attachment header,
     pclaw_qbo_template.csv filename, non-empty body, required header
     columns present.
  T2 /onboarding/sample.csv  -> same checks for the larger sample GL.
  T3 /onboarding/sample/<report_type>.csv for each supported report
     type (chart_of_accounts, trial_balance, trust_listing) -> 200,
     text/csv, attachment, non-empty body.
  T4 Unsupported report type -> 404.
  T5 Onboarding HTML page actually links to every URL above (no
     silent dead-link regression).
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
os.environ.setdefault("SECRET_KEY", "smoke-onboarding-downloads")

import app as appmod  # noqa: E402


def _assert_csv_download(r, expected_filename, must_contain_header=None):
    assert r.status_code == 200, f"expected 200, got {r.status_code}"
    assert r.mimetype == "text/csv", f"expected text/csv, got {r.mimetype}"
    cd = r.headers.get("Content-Disposition", "")
    assert "attachment" in cd, f"missing attachment header: {cd}"
    assert expected_filename in cd, f"missing filename {expected_filename}: {cd}"
    body = r.get_data(as_text=True)
    assert body.strip(), "empty CSV body"
    # First non-empty line is the header. Make sure it actually looks
    # like CSV (has a comma) and contains the expected column if asked.
    first = next((line for line in body.splitlines() if line.strip()), "")
    assert "," in first, f"first line is not CSV-shaped: {first!r}"
    if must_contain_header:
        for col in must_contain_header:
            assert col in first, f"missing column {col!r} in header {first!r}"


def t1_template_csv():
    c = appmod.app.test_client()
    r = c.get("/onboarding/template.csv")
    _assert_csv_download(
        r,
        "pclaw_qbo_template.csv",
        must_contain_header=(
            "transaction_id", "date", "account_number",
            "account_name", "debit", "credit",
        ),
    )
    print("T1 OK: /onboarding/template.csv downloads a valid CSV")


def t2_sample_csv():
    c = appmod.app.test_client()
    r = c.get("/onboarding/sample.csv")
    _assert_csv_download(
        r,
        "pclaw_qbo_sample_general_ledger.csv",
        must_contain_header=("transaction_id", "account_number"),
    )
    print("T2 OK: /onboarding/sample.csv downloads a valid CSV")


def t3_sample_report_csvs():
    c = appmod.app.test_client()
    cases = {
        "chart_of_accounts": "pclaw_qbo_sample_chart_of_accounts.csv",
        "trial_balance": "pclaw_qbo_sample_trial_balance.csv",
        "trust_listing": "pclaw_qbo_sample_trust_listing.csv",
    }
    for report_type, filename in cases.items():
        r = c.get(f"/onboarding/sample/{report_type}.csv")
        _assert_csv_download(r, filename)
    print("T3 OK: every supported /onboarding/sample/<report_type>.csv downloads")


def t4_unsupported_report_type_404s():
    c = appmod.app.test_client()
    r = c.get("/onboarding/sample/etc-passwd.csv")
    assert r.status_code == 404, r.status_code
    print("T4 OK: unsupported report type returns 404")


def t5_onboarding_page_links_to_every_download():
    c = appmod.app.test_client()
    body = c.get("/onboarding").get_data(as_text=True)
    for url in (
        "/onboarding/template.csv",
        "/onboarding/sample.csv",
        "/onboarding/sample/chart_of_accounts.csv",
        "/onboarding/sample/trial_balance.csv",
        "/onboarding/sample/trust_listing.csv",
    ):
        assert url in body, f"onboarding page missing link to {url}"
    print("T5 OK: onboarding HTML page links to every download")


if __name__ == "__main__":
    t1_template_csv()
    t2_sample_csv()
    t3_sample_report_csvs()
    t4_unsupported_report_type_404s()
    t5_onboarding_page_links_to_every_download()
    print("ALL ONBOARDING DOWNLOAD SMOKE TESTS PASSED")
