"""Smoke tests for the public SEO content pages.

Run from project root:

    python3 tests/smoke_seo_pages.py

Covers:
  T1 Each SEO route returns 200 with its required H1 and title tag.
  T2 Every SEO page renders a canonical URL, JSON-LD structured data, and the
     "Book a discovery call" CTA that routes through discovery_call_href.
  T3 Internal linking: the pillar page links to all four supporting pages, and
     the homepage links to the pillar page.
  T4 The trust page carries the required "rely on your accountant / compliance
     advisor" disclaimer.
  T5 Brand-safety guards: no "trusted by Clio" claims and no fabricated
     SOC 2 / encryption-standard / compliance-certification claims.
  T6 No public pricing amounts leak onto any SEO page.
  T8 The PC Law reports resource article links back to the pillar, service, and
     trust pages plus the discovery CTA, carries a "requirements vary" /
     "not advice" disclaimer, and the homepage links to the article.
"""

import os
import re
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
os.environ.setdefault("SECRET_KEY", "smoke-secret-seo")

import app as appmod  # noqa: E402


# path -> (title tag fragment, H1 fragment)
PAGES = {
    "/pclaw-to-quickbooks-online-migration": (
        "PCLaw to QuickBooks Online Migration: The Complete Guide (2026)",
        "The Complete Guide to Migrating from PCLaw to QuickBooks Online",
    ),
    "/pc-law-to-quickbooks-migration": (
        "PC Law to QuickBooks migration for law firms",
        "PC Law to QuickBooks migration,",
    ),
    "/law-firm-accounting-migration": (
        "Law firm accounting migration services",
        "Law firm accounting migration",
    ),
    "/trust-accounting-migration": (
        "Trust accounting migration for law firms",
        "Trust-related migration records",
    ),
    "/partners": (
        "Legal software migration partner for vendors",
        "A migration partner for",
    ),
    "/resources/pc-law-reports-needed-for-quickbooks-migration": (
        "PC Law reports needed for QuickBooks migration | Cutovr",
        "The PC Law reports you need before a QuickBooks migration",
    ),
}

# The resource article route; kept as a name so the tests below can be
# specific about the article's own internal links, disclaimer, and CTA.
ARTICLE_PATH = "/resources/pc-law-reports-needed-for-quickbooks-migration"


def _get(path):
    return appmod.app.test_client().get(path)


def t1_pages_render_with_title_and_h1():
    for path, (title, h1) in PAGES.items():
        r = _get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        body = r.get_data(as_text=True)
        assert f"<title>{title}" in body or f">{title}<" in body, \
            f"{path} missing title tag fragment {title!r}"
        assert h1 in body, f"{path} missing H1 fragment {h1!r}"
    print("T1 OK: all SEO pages render 200 with required title tag + H1")


def t2_seo_metadata_and_cta():
    for path in PAGES:
        body = _get(path).get_data(as_text=True)
        assert 'rel="canonical"' in body, f"{path} missing canonical link"
        assert path in body, f"{path} canonical should reference its own path"
        assert 'application/ld+json' in body, f"{path} missing JSON-LD"
        assert '"@type": "BreadcrumbList"' in body, f"{path} missing Breadcrumb schema"
        assert "Book a discovery call" in body or "partner discovery call" in body, \
            f"{path} missing discovery-call CTA"
        # The CTA routes through the in-app fallback form when no Calendly URL
        # is configured (test env), so the request form route must be present.
        assert "/pricing/quote-request" in body, \
            f"{path} discovery CTA should fall back to the request form when unset"
    print("T2 OK: every SEO page has canonical, JSON-LD (Breadcrumb), and discovery CTA")


def t3_internal_linking():
    pillar = _get("/pclaw-to-quickbooks-online-migration").get_data(as_text=True)
    for target in (
        "/pc-law-to-quickbooks-migration",
        "/law-firm-accounting-migration",
        "/trust-accounting-migration",
        "/partners",
    ):
        assert f'href="{target}"' in pillar, f"pillar page missing link to {target}"
    home = _get("/").get_data(as_text=True)
    assert 'href="/pclaw-to-quickbooks-online-migration"' in home, \
        "homepage should link to the pillar guide"
    print("T3 OK: pillar links to all supporting pages; homepage links to pillar")


def t4_trust_disclaimer():
    body = _get("/trust-accounting-migration").get_data(as_text=True)
    assert 'data-testid="trust-disclaimer"' in body, "trust page missing disclaimer block"
    lowered = body.lower()
    assert "accountant" in lowered and "compliance" in lowered, \
        "trust disclaimer should point firms to their accountant / compliance advisor"
    assert "legal, accounting, or compliance advice" in lowered, \
        "trust disclaimer should state it is not professional advice"
    print("T4 OK: trust page carries the accountant/compliance disclaimer")


def t5_brand_safety():
    for path in PAGES:
        lowered = _get(path).get_data(as_text=True).lower()
        assert "trusted by clio" not in lowered, f"{path} must not claim 'trusted by Clio'"
        # Avoid unsupported compliance/security certification claims.
        for banned in ("soc 2", "soc2", "hipaa certified", "iso 27001",
                       "bank-level encryption", "military-grade"):
            assert banned not in lowered, f"{path} contains unsupported claim {banned!r}"
    print("T5 OK: no 'trusted by Clio' or unsupported compliance/security claims")


def t6_no_public_pricing():
    for path in PAGES:
        body = _get(path).get_data(as_text=True)
        assert not re.search(r"\$\s*\d", body), f"{path} must not show public pricing amounts"
    print("T6 OK: no public pricing amounts on any SEO page")


def t7_readability_layout_hook():
    # The `seo-page` class carries the long-form spacing/readability styles.
    # Guard it so the layout hook can't silently disappear from a page.
    for path in PAGES:
        body = _get(path).get_data(as_text=True)
        assert "page--prose seo-page" in body, \
            f"{path} missing the 'seo-page' readability layout class"
    print("T7 OK: every SEO page carries the seo-page readability layout class")


def t8_reports_article():
    body = _get(ARTICLE_PATH).get_data(as_text=True)
    # Internal links back into the pillar / service / trust pages.
    for target in (
        "/pclaw-to-quickbooks-online-migration",
        "/pc-law-to-quickbooks-migration",
        "/trust-accounting-migration",
    ):
        assert f'href="{target}"' in body, f"article missing link to {target}"
    # Discovery CTA falls back to the in-app request form when unset.
    assert "/pricing/quote-request" in body, "article missing discovery/quote CTA"
    assert "Book a discovery call" in body, "article missing discovery-call button"
    # BlogPosting article schema present alongside the breadcrumb.
    assert '"@type": "BlogPosting"' in body, "article missing BlogPosting JSON-LD"
    # "Requirements vary" / not-advice disclaimer.
    assert 'data-testid="article-disclaimer"' in body, "article missing disclaimer block"
    lowered = re.sub(r"\s+", " ", body.lower())
    assert "vary by firm" in lowered, "article disclaimer should note requirements vary by firm"
    assert "not legal, accounting, or compliance advice" in lowered, \
        "article disclaimer should state it is not professional advice"
    # No public pricing on the article.
    assert not re.search(r"\$\s*\d", body), "article must not show pricing amounts"
    # Homepage links to the article.
    home = _get("/").get_data(as_text=True)
    assert f'href="{ARTICLE_PATH}"' in home, "homepage should link to the reports article"
    print("T8 OK: reports article links back, has BlogPosting schema, disclaimer, no pricing")


if __name__ == "__main__":
    try:
        t1_pages_render_with_title_and_h1()
        t2_seo_metadata_and_cta()
        t3_internal_linking()
        t4_trust_disclaimer()
        t5_brand_safety()
        t6_no_public_pricing()
        t7_readability_layout_hook()
        t8_reports_article()
        print("\nALL SEO-PAGE SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
