"""Customer-facing brand smoke tests for "PC Law Migrate".

The product/service must render as "PC Law Migrate" across customer
pages, with the upper-left brand mark in all caps ("PC LAW MIGRATE").
Source-software references like "PCLaw reports" / "PCLaw account list"
must remain unchanged.

Run from project root:

    python3 tests/smoke_brand_pc_law_migrate.py
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-for-brand-pc-law-migrate-32chars")

import app as appmod  # noqa: E402


# Customer-facing public pages where the product name should render
# correctly. We exclude operator-only pages (which keep raw/internal
# tokens) and the dynamic guide pages that reference PCLaw source
# software as well.
PUBLIC_PAGES = [
    "/",
    "/login",
    "/signup",
    "/pricing",
    "/about",
    "/security",
    "/privacy",
    "/terms",
    "/support",
    "/onboarding",
    "/quickbooks-guide",
]


# Regex matching the compact (no-space) brand string, so we catch both
# "PCLaw Migrate" and Pclaw Migrate / PCLAW MIGRATE bare-form mistakes
# but NOT the legitimate "PC Law Migrate".
_COMPACT_BRAND_RE = re.compile(r"PCLaw\s+Migrate|Pclaw\s+Migrate|PCLAW\s+MIGRATE")


def t1_public_pages_use_pc_law_migrate():
    c = appmod.app.test_client()
    for path in PUBLIC_PAGES:
        r = c.get(path)
        assert r.status_code in (200, 302), f"{path} -> {r.status_code}"
        if r.status_code == 302:
            continue
        body = r.get_data(as_text=True)
        # Customer pages must display the correct product name at least
        # once (some via {{ app_name }} interpolation; some inline copy).
        assert "PC Law Migrate" in body, f"{path} missing 'PC Law Migrate'"
        # Compact "PCLaw Migrate" must not leak into customer copy.
        # Exception: the brand mark on the logo is rendered as all-caps
        # "PC LAW MIGRATE" which is allowed.
        matches = _COMPACT_BRAND_RE.findall(body)
        # Allow occurrences inside the brand-mark element (already
        # rendered as "PC LAW <em>MIGRATE</em>" — that's "PC LAW MIGRATE"
        # when stripped of tags, with a space).
        if matches:
            # The regex requires \s+ between tokens so the rendered
            # brand "PC LAW <em>MIGRATE</em>" won't match — confirm.
            assert False, (
                f"{path} contains compact brand string(s): {matches}"
            )
    print(f"T1 OK: {len(PUBLIC_PAGES)} public pages render 'PC Law Migrate'")


def t2_brand_mark_is_all_caps():
    c = appmod.app.test_client()
    body = c.get("/login").get_data(as_text=True)
    assert "PC LAW <em>MIGRATE</em>" in body, \
        "header brand mark should render as 'PC LAW MIGRATE'"
    print("T2 OK: header brand mark is 'PC LAW MIGRATE'")


def t3_pclaw_source_references_preserved():
    """Source-system references must keep the compact 'PCLaw' spelling."""
    c = appmod.app.test_client()
    guide = c.get("/quickbooks-guide").get_data(as_text=True)
    # The guide describes PCLaw reports + data — these must stay.
    for phrase in ("PCLaw account list", "PCLaw export", "PCLaw closing"):
        assert phrase in guide, f"guide should preserve source phrase '{phrase}'"
    print("T3 OK: PCLaw source-system references preserved on guide page")


def t4_footer_company_name():
    c = appmod.app.test_client()
    body = c.get("/login").get_data(as_text=True)
    assert "PC Law Migrate" in body, "footer/company name should be 'PC Law Migrate'"
    print("T4 OK: footer company name is 'PC Law Migrate'")


def t5_manifest_and_icons_use_pc_law_migrate():
    c = appmod.app.test_client()
    manifest = c.get("/static/site.webmanifest").get_data(as_text=True)
    assert "PC Law Migrate" in manifest
    fav = c.get("/static/favicon.svg").get_data(as_text=True)
    assert "PC Law Migrate" in fav
    icon = c.get("/static/icon-512.svg").get_data(as_text=True)
    assert "PC Law Migrate" in icon
    print("T5 OK: manifest + SVG icons reference 'PC Law Migrate'")


if __name__ == "__main__":
    t1_public_pages_use_pc_law_migrate()
    t2_brand_mark_is_all_caps()
    t3_pclaw_source_references_preserved()
    t4_footer_company_name()
    t5_manifest_and_icons_use_pc_law_migrate()
    print("\nAll PC Law Migrate brand smoke tests OK.")
