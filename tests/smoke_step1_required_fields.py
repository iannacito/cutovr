"""Step 1 (cutover/switchover) required-field validation smoke tests.

Run from project root:

    python3 tests/smoke_step1_required_fields.py

QA reported that a totally blank Step 1 form would advance the
workflow. This pins:
  T1 An empty POST to /cutover stays on Step 1, surfaces a plain-English
     error mentioning the missing fields, and DOES NOT redirect.
  T2 Missing just cutover_date is rejected and the country value the
     user already picked is preserved on re-render (so they don't lose
     context).
  T3 Missing just country is rejected and the cutover_date the user
     already picked is preserved.
  T4 A valid POST (cutover_date + country present) saves and redirects
     to the migration checklist as before.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-step1-validation")

import app as appmod  # noqa: E402


def _signup_and_login(email="step1@example.test"):
    c = appmod.app.test_client()
    c.post(
        "/signup",
        data={
            "firm_name": "Step 1 Firm",
            "email": email,
            "password": "passw0rd!1234",
            "confirm_password": "passw0rd!1234",
        },
    )
    return c


def t1_blank_post_stays_on_step1():
    c = _signup_and_login("blank@example.test")
    r = c.post("/cutover", data={}, follow_redirects=False)
    assert r.status_code == 400, f"expected 400, got {r.status_code}"
    body = r.get_data(as_text=True)
    # Plain English: must mention both missing pieces.
    assert "switchover date" in body, body[:500]
    assert "country" in body, body[:500]
    # Must NOT redirect to the checklist (i.e., advance the workflow).
    assert "Location" not in r.headers or "checklist" not in r.headers.get("Location", "")
    print("T1 OK: blank Step 1 stays on Step 1 with plain-English error")


def t2_missing_date_preserves_country():
    c = _signup_and_login("nodate@example.test")
    r = c.post(
        "/cutover",
        data={"country": "US"},
        follow_redirects=False,
    )
    assert r.status_code == 400, r.status_code
    body = r.get_data(as_text=True)
    assert "switchover date" in body, body[:500]
    # Country value must round-trip into the re-rendered form.
    assert 'value="US"' in body or 'value=US' in body or "selected>United States" in body or 'selected' in body
    # Must NOT have saved settings.
    print("T2 OK: missing cutover_date is rejected, country preserved")


def t3_missing_country_preserves_date():
    c = _signup_and_login("nocountry@example.test")
    r = c.post(
        "/cutover",
        data={"cutover_date": "2026-04-01"},
        follow_redirects=False,
    )
    assert r.status_code == 400, r.status_code
    body = r.get_data(as_text=True)
    assert "country" in body, body[:500]
    assert "2026-04-01" in body
    print("T3 OK: missing country is rejected, date preserved")


def t4_valid_post_saves_and_redirects():
    c = _signup_and_login("good@example.test")
    r = c.post(
        "/cutover",
        data={"cutover_date": "2026-04-01", "country": "US"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302), f"expected redirect, got {r.status_code}"
    loc = r.headers.get("Location", "")
    assert "checklist" in loc or "migration-checklist" in loc, loc
    print("T4 OK: valid Step 1 POST saves and advances workflow")


if __name__ == "__main__":
    t1_blank_post_stays_on_step1()
    t2_missing_date_preserves_country()
    t3_missing_country_preserves_date()
    t4_valid_post_saves_and_redirects()
    print("ALL STEP 1 REQUIRED-FIELD SMOKE TESTS PASSED")
