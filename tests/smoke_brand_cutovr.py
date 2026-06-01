"""Customer-facing brand smoke tests for "Cutovr".

The product/service must render as "Cutovr" across customer pages, with
the upper-left brand mark in all caps ("CUTOVR", rendered as the split
"CUT<em>OVR</em>"). Source-software references like "PCLaw reports" /
"PCLaw account list" must remain unchanged — Cutovr migrates *from* PCLaw,
so the legacy software name is still legitimate copy.

This test also guards against regressions back to the old product names
("PC Law Migrate", "PC LAW MIGRATE", "PCLaw Migrate").

Run from project root:

    python3 tests/smoke_brand_cutovr.py
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
os.environ.setdefault("SECRET_KEY", "smoke-secret-for-brand-cutovr-aaaaaaaa32chars")

import app as appmod  # noqa: E402


# Customer-facing public pages where the product name should render
# correctly. We exclude operator-only pages (which keep raw/internal
# tokens).
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


# Old product-name brand strings that must NOT leak into customer copy
# after the Cutovr rebrand. Note the word boundary / spacing so we catch
# the product name but never the legitimate source-software term "PCLaw"
# (as in "PCLaw reports").
_LEGACY_BRAND_RE = re.compile(
    r"PC\s*Law\s+Migrate|PCLaw\s+Migrate|PC\s+LAW\s+MIGRATE|Pclaw\s+Migrate",
    re.IGNORECASE,
)


def t1_public_pages_use_cutovr():
    c = appmod.app.test_client()
    for path in PUBLIC_PAGES:
        r = c.get(path)
        assert r.status_code in (200, 302), f"{path} -> {r.status_code}"
        if r.status_code == 302:
            continue
        body = r.get_data(as_text=True)
        # "Cutovr" appears at least once per customer page — via the footer
        # company name, the brand mark, or inline {{ app_name }} copy.
        assert "Cutovr" in body, f"{path} missing 'Cutovr'"
        # No legacy product name may leak into customer copy.
        matches = _LEGACY_BRAND_RE.findall(body)
        assert not matches, f"{path} contains legacy brand string(s): {matches}"
    print(f"T1 OK: {len(PUBLIC_PAGES)} public pages render 'Cutovr' with no legacy brand")


def t2_brand_mark_is_all_caps():
    c = appmod.app.test_client()
    body = c.get("/login").get_data(as_text=True)
    assert "CUT<em>OVR</em>" in body, \
        "header brand mark should render as the all-caps split 'CUT<em>OVR</em>'"
    print("T2 OK: header brand mark is all-caps 'CUTOVR'")


def t3_pclaw_source_references_preserved():
    """Source-system references must keep the 'PCLaw' spelling."""
    c = appmod.app.test_client()
    guide = c.get("/quickbooks-guide").get_data(as_text=True)
    # The guide describes PCLaw reports + data — these must stay because
    # the legacy software name is correct, factual copy.
    for phrase in ("PCLaw account list", "PCLaw export", "PCLaw closing"):
        assert phrase in guide, f"guide should preserve source phrase '{phrase}'"
    print("T3 OK: PCLaw source-system references preserved on guide page")


def t4_footer_company_name():
    c = appmod.app.test_client()
    body = c.get("/login").get_data(as_text=True)
    assert "Cutovr" in body, "footer/company name should be 'Cutovr'"
    print("T4 OK: footer company name is 'Cutovr'")


def t5_manifest_and_icons_use_cutovr():
    c = appmod.app.test_client()
    manifest = c.get("/static/site.webmanifest").get_data(as_text=True)
    assert "Cutovr" in manifest
    assert "PC Law Migrate" not in manifest and "PCLaw Migrate" not in manifest
    fav = c.get("/static/favicon.svg").get_data(as_text=True)
    assert "Cutovr" in fav
    icon = c.get("/static/icon-512.svg").get_data(as_text=True)
    assert "Cutovr" in icon
    print("T5 OK: manifest + SVG icons reference 'Cutovr'")


if __name__ == "__main__":
    t1_public_pages_use_cutovr()
    t2_brand_mark_is_all_caps()
    t3_pclaw_source_references_preserved()
    t4_footer_company_name()
    t5_manifest_and_icons_use_cutovr()
    print("\nAll Cutovr brand smoke tests OK.")
