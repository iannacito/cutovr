"""Brand-name consistency smoke tests.

Run from project root:

    python3 tests/smoke_brand_name_consistency.py

Locks in the product-name wording across customer-facing surfaces:

  T1 The default app/company name surfaces as "PC Law Migrate" in body
     copy (page titles, meta description, footer copyright).
  T2 The header brand mark in the upper-left corner remains the
     uppercase "PC LAW MIGRATE" wordmark — that is the logo treatment.
  T3 The legacy italic 'PCLaw <em>Migrate</em>' wordmark is gone.
  T4 No customer-facing public page accidentally renders the previous
     "PCLaw Migrate" product name in body copy. References to the
     source software "PCLaw" (e.g. "PCLaw to QuickBooks", "PCLaw
     reports") are intentionally preserved.

The QBO API is not exercised; this test only renders public/HTML pages.
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
os.environ.setdefault("SECRET_KEY", "smoke-secret-brand-name-consistency-32c")

# Public pages that every customer can hit without authentication.
PUBLIC_PAGES = [
    "/",
    "/login",
    "/signup",
    "/pricing",
    "/onboarding",
    "/security",
    "/support",
    "/about",
    "/privacy",
    "/terms",
    "/quickbooks-guide",
]

import app as appmod  # noqa: E402


def _get(path, client=None):
    c = client or appmod.app.test_client()
    return c.get(path)


def t1_default_product_name_in_body_copy():
    """The product name "PC Law Migrate" should appear on key pages."""
    c = appmod.app.test_client()
    # Footer copyright includes company_name; login page is a known anchor.
    body = c.get("/login").get_data(as_text=True)
    assert "PC Law Migrate" in body, \
        "default APP_NAME/COMPANY_NAME should render as 'PC Law Migrate' on /login"

    # Title block uses {{ app_name }} — guard the landing page too.
    landing = c.get("/").get_data(as_text=True)
    assert "PC Law Migrate" in landing, \
        "landing page should render 'PC Law Migrate' (via app_name title/footer)"
    print("T1 OK: default product name renders as 'PC Law Migrate' in body copy")


def t2_header_logo_remains_uppercase():
    """The upper-left brand mark is the 'PC LAW MIGRATE' logo wordmark."""
    c = appmod.app.test_client()
    body = c.get("/login").get_data(as_text=True)
    assert 'class="brand-mark"' in body, "header brand-mark anchor should be present"
    assert "PC LAW MIGRATE" in body, \
        "header logo should render the uppercase 'PC LAW MIGRATE' wordmark"
    print("T2 OK: header logo renders uppercase 'PC LAW MIGRATE' wordmark")


def t3_legacy_italic_wordmark_is_gone():
    """The previous 'PCLaw <em>Migrate</em>' italic wordmark must be removed."""
    c = appmod.app.test_client()
    for path in ("/", "/login", "/pricing", "/onboarding"):
        body = c.get(path).get_data(as_text=True)
        assert "PCLaw <em>Migrate</em>" not in body, \
            f"{path} should not contain legacy italic 'PCLaw <em>Migrate</em>' wordmark"
    print("T3 OK: legacy italic 'PCLaw <em>Migrate</em>' wordmark is gone")


def t4_no_customer_facing_pclaw_migrate_product_name():
    """Public pages should not render the previous product name "PCLaw Migrate".

    References to the source software "PCLaw" (e.g. "PCLaw to QuickBooks",
    "PCLaw reports", "PCLaw account") remain intentionally — only the
    product-name two-word "PCLaw Migrate" form is forbidden.
    """
    c = appmod.app.test_client()
    offenders = []
    for path in PUBLIC_PAGES:
        r = c.get(path)
        if r.status_code != 200:
            continue
        body = r.get_data(as_text=True)
        if "PCLaw Migrate" in body:
            # Allow the legacy comparison if the page also explicitly notes
            # it as a legacy/forbidden token (we don't, today). For now any
            # occurrence is a regression.
            offenders.append(path)
    assert not offenders, (
        "Customer-facing pages still contain the legacy product name "
        f"'PCLaw Migrate' (should be 'PC Law Migrate'): {offenders}"
    )
    print(
        "T4 OK: no customer-facing public page renders the legacy "
        "'PCLaw Migrate' product name"
    )


def t5_pclaw_source_software_references_preserved():
    """Sanity: the source-software name 'PCLaw' (without ' Migrate' suffix)
    is still present where it should be — e.g. landing page hero, onboarding
    export instructions. This guards against an over-eager search/replace
    that would silently rename references to the legacy accounting product.
    """
    c = appmod.app.test_client()
    landing = c.get("/").get_data(as_text=True)
    assert "PCLaw" in landing, (
        "landing page should still reference the source software 'PCLaw' "
        "(e.g. 'PCLaw to QuickBooks')"
    )
    onboarding = c.get("/onboarding").get_data(as_text=True)
    assert "PCLaw" in onboarding, (
        "onboarding page should still reference 'PCLaw' in export instructions"
    )
    print("T5 OK: source-software 'PCLaw' references preserved on landing/onboarding")


if __name__ == "__main__":
    try:
        t1_default_product_name_in_body_copy()
        t2_header_logo_remains_uppercase()
        t3_legacy_italic_wordmark_is_gone()
        t4_no_customer_facing_pclaw_migrate_product_name()
        t5_pclaw_source_software_references_preserved()
        print("\nALL BRAND-NAME CONSISTENCY SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
