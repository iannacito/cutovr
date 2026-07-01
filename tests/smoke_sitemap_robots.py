"""Smoke tests for SEO infrastructure: sitemap.xml, robots.txt, and the
Open Graph / Twitter card tags shared across public pages.

Run from project root:

    python3 tests/smoke_sitemap_robots.py

Covers:
  T1 /sitemap.xml returns 200 XML listing every curated public page on the
     canonical host, and omits internal/operator/demo/migration app routes.
  T2 /robots.txt returns 200 text, allows crawling, disallows internal
     prefixes, points at the sitemap, and never blocks a public SEO page.
  T3 Public pages carry Open Graph + Twitter card tags (site name Cutovr,
     og:url == canonical) without inventing a broken og:image URL.
  T4 PR #118 head metadata (title, meta description, canonical, JSON-LD) is
     preserved on the SEO pages.
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
os.environ.setdefault("SECRET_KEY", "smoke-secret-sitemap")

import app as appmod  # noqa: E402
import branding  # noqa: E402

BASE = branding.PUBLIC_APP_URL

PUBLIC_PAGES = [
    "/",
    "/pclaw-to-quickbooks-online-migration",
    "/pc-law-to-quickbooks-migration",
    "/law-firm-accounting-migration",
    "/trust-accounting-migration",
    "/partners",
    "/about",
    "/security",
    "/support",
    "/privacy",
    "/terms",
]


def _get(path):
    return appmod.app.test_client().get(path)


def t1_sitemap():
    r = _get("/sitemap.xml")
    assert r.status_code == 200, f"/sitemap.xml -> {r.status_code}"
    assert "xml" in r.mimetype, f"sitemap mimetype {r.mimetype!r} not XML"
    body = r.get_data(as_text=True)
    assert body.lstrip().startswith("<?xml"), "sitemap missing XML declaration"
    assert "<urlset" in body and "sitemaps.org/schemas/sitemap" in body, \
        "sitemap missing urlset/namespace"
    for path in PUBLIC_PAGES:
        assert f"<loc>{BASE}{path}</loc>" in body, \
            f"sitemap missing public page {path}"
    # Internal routes must never appear.
    for internal in ("/operator", "/demo", "/login", "/dashboard",
                     "/quickbooks", "/jobs", "/healthz"):
        assert f"<loc>{BASE}{internal}</loc>" not in body, \
            f"sitemap leaked internal route {internal}"
    print("T1 OK: sitemap.xml lists all public pages, no internal routes")


def t2_robots():
    r = _get("/robots.txt")
    assert r.status_code == 200, f"/robots.txt -> {r.status_code}"
    assert r.mimetype == "text/plain", f"robots mimetype {r.mimetype!r}"
    body = r.get_data(as_text=True)
    assert "User-agent: *" in body, "robots missing user-agent"
    assert "Allow: /" in body, "robots should allow crawling"
    assert f"Sitemap: {BASE}/sitemap.xml" in body, "robots missing sitemap link"
    disallows = [ln.split(":", 1)[1].strip()
                 for ln in body.splitlines() if ln.startswith("Disallow:")]
    for prefix in ("/operator", "/demo", "/login", "/logout", "/quickbooks",
                   "/migration", "/integrations"):
        assert prefix in disallows, f"robots should disallow {prefix}"
    # No disallow rule may swallow a public SEO page.
    for page in PUBLIC_PAGES:
        for rule in disallows:
            assert not (page == rule or page.startswith(rule + "/")
                        or (rule != "/" and page.startswith(rule)
                            and page[len(rule):len(rule) + 1] in ("", "/"))), \
                f"robots rule {rule!r} would block public page {page!r}"
    print("T2 OK: robots.txt allows crawl, disallows internals, links sitemap")


def t3_open_graph_tags():
    for path in PUBLIC_PAGES:
        body = _get(path).get_data(as_text=True)
        assert 'property="og:site_name" content="Cutovr"' in body, \
            f"{path} missing og:site_name"
        assert 'property="og:title"' in body, f"{path} missing og:title"
        assert 'property="og:description"' in body, f"{path} missing og:description"
        assert 'name="twitter:card"' in body, f"{path} missing twitter:card"
        # og:url must match the canonical URL for the page.
        m_can = re.search(r'rel="canonical" href="([^"]+)"', body)
        m_og = re.search(r'property="og:url" content="([^"]+)"', body)
        assert m_can and m_og, f"{path} missing canonical/og:url"
        assert m_can.group(1) == m_og.group(1), \
            f"{path} og:url {m_og.group(1)!r} != canonical {m_can.group(1)!r}"
        # No invented/broken image URL.
        m_img = re.search(r'property="og:image" content="([^"]*)"', body)
        if m_img:
            assert m_img.group(1).startswith(("http://", "https://", "/")), \
                f"{path} has a malformed og:image {m_img.group(1)!r}"
    print("T3 OK: public pages have OG/Twitter tags; og:url matches canonical")


def t4_pr118_head_preserved():
    body = _get("/pclaw-to-quickbooks-online-migration").get_data(as_text=True)
    assert "<title>" in body and "PCLaw to QuickBooks Online Migration" in body, \
        "pillar title tag lost"
    assert 'name="description"' in body, "pillar meta description lost"
    assert 'rel="canonical"' in body, "pillar canonical lost"
    assert 'application/ld+json' in body, "pillar JSON-LD lost"
    print("T4 OK: PR #118 title/meta/canonical/JSON-LD preserved on SEO pages")


if __name__ == "__main__":
    try:
        t1_sitemap()
        t2_robots()
        t3_open_graph_tags()
        t4_pr118_head_preserved()
        print("\nALL SITEMAP/ROBOTS/OG SMOKE TESTS PASSED")
    finally:
        for path in (APP_DB, HIST_DB):
            try:
                os.unlink(path)
            except OSError:
                pass
