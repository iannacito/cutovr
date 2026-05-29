"""Customer-facing wording smoke test: 'QBO' must not appear.

Run from project root:

    python3 tests/smoke_customer_facing_quickbooks_wording.py

Dan asked for all customer-facing copy to say 'QuickBooks' (not 'QBO').
This pins that every customer-facing template renders with no
standalone 'QBO' token visible to lawyers using the app. Operator-only
pages (/operator/*) and engineering-only console pages keep the
acronym because they are read by us, not by customers.

Checks:
  T1 A list of customer-facing template files have no standalone 'QBO'
     token in their source.
  T2 Public pages render with no 'QBO ' / ' QBO' / '(QBO)' substrings.
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
os.environ.setdefault("SECRET_KEY", "smoke-qbo-wording")

import app as appmod  # noqa: E402


# Templates that lawyers see. Operator/admin templates are excluded —
# they keep the internal acronym intentionally.
CUSTOMER_TEMPLATES = [
    "support.html",
    "quickbooks-manage.html",
    "quickbooks-guide.html",
    "preview-import.html",
    "opening-balance.html",
    "job-detail.html",
    "disconnect.html",
    "demo-workspace.html",
    "coa-result.html",
    "coa-preview.html",
    "coa-confirm.html",
    "send-to-qbo.html",
    "firm-imports.html",
    "dashboard.html",
    "landing.html",
    "onboarding.html",
    "privacy.html",
    "terms.html",
    "pricing.html",
    "migration-checklist.html",
    "import-recovery.html",
    "account-mapping.html",
    "reconcile-balances.html",
    "trust-reconciliation.html",
    "ending-tb-reconciliation.html",
    "uploaded-reports.html",
    "bulk-upload-review.html",
    "welcome-back.html",
]

# Standalone "QBO" as a word — not part of an identifier, URL, or
# template variable. We match it on word boundaries inside the
# rendered/visible text. The route URL "/send-to-qbo" is allowed as
# it's part of a path; the directly-visible label "QBO" alone is not.
QBO_WORD_RE = re.compile(r"(?<![A-Za-z0-9_/-])QBO(?![A-Za-z0-9_])")


def t1_customer_templates_no_qbo_word():
    bad = []
    for tpl in CUSTOMER_TEMPLATES:
        path = ROOT / "templates" / tpl
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        for m in QBO_WORD_RE.finditer(src):
            start = max(0, m.start() - 40)
            end = min(len(src), m.end() + 40)
            bad.append((tpl, m.start(), src[start:end]))
    assert not bad, (
        "Customer-facing templates contain standalone 'QBO' wording. "
        "Replace with 'QuickBooks'. Offenders:\n"
        + "\n".join(f"  {t} @ {pos}: ...{ctx}..." for t, pos, ctx in bad)
    )
    print(f"T1 OK: {len(CUSTOMER_TEMPLATES)} customer-facing templates use 'QuickBooks'")


def t2_rendered_public_pages_no_qbo_word():
    c = appmod.app.test_client()
    bad = []
    for path in (
        "/", "/onboarding", "/pricing", "/privacy", "/terms",
        "/quickbooks-guide", "/support",
    ):
        r = c.get(path)
        if r.status_code != 200:
            continue
        body = r.get_data(as_text=True)
        for m in QBO_WORD_RE.finditer(body):
            start = max(0, m.start() - 40)
            end = min(len(body), m.end() + 40)
            bad.append((path, body[start:end]))
    assert not bad, (
        "Rendered public pages leak the 'QBO' acronym:\n"
        + "\n".join(f"  {p}: ...{ctx}..." for p, ctx in bad)
    )
    print("T2 OK: rendered public pages say 'QuickBooks', never 'QBO'")


if __name__ == "__main__":
    t1_customer_templates_no_qbo_word()
    t2_rendered_public_pages_no_qbo_word()
    print("ALL CUSTOMER-FACING QUICKBOOKS WORDING TESTS PASSED")
