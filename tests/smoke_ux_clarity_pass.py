"""UX clarity / restraint pass smoke tests.

Run from project root:

    python3 tests/smoke_ux_clarity_pass.py

Pins the clarity rules tightened in the "restraint pass" so they don't
regress. These are static-template + public-page checks (no QuickBooks
fixtures needed) so they stay fast and low-flake.

Checks:
  T1 No customer-facing template hard-codes a fake placeholder email
     (e.g. "@firm.example", "@pclaw-qbo.example"). Support contacts go
     through the guarded {{ support_email }} variable instead.
  T2 The account-mapping "Contact support" link is guarded by the
     real-support-email check, not a hard-coded mailto.
  T3 Customer-facing step templates avoid the worst raw-jargon phrases
     ("opening balance JE", "balanced journal entry that seeds",
     "double-post your opening trial balance").
  T4 The support assistant describes the six steps in the real order
     (Connect QuickBooks + match is Step 3, Review is Step 4).
  T5 Public Security / Privacy pages drop the internal "Fernet" library
     name while keeping the reassuring "AES-256".
  T6 Public pages render "PC Law Migrate" (not "PCLaw Migrate") and never
     surface customer-facing "QBO".
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMPLATES = ROOT / "templates"

os.environ["APP_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["IMPORT_HISTORY_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-ux-clarity-pass-32-chars-long")

import app as appmod  # noqa: E402
import support_assistant  # noqa: E402


# Customer-facing templates. Operator-/migration-console pages keep raw
# tokens (env var names, realm ids) on purpose, so they are excluded.
CUSTOMER_TEMPLATES = [
    "account-mapping.html",
    "coa-result.html",
    "coa-preview.html",
    "coa-confirm.html",
    "dashboard.html",
    "onboarding.html",
    "opening-balance.html",
    "preview-import.html",
    "reconcile-balances.html",
    "send-to-qbo.html",
    "support.html",
    "security.html",
    "privacy.html",
    "trust-reconciliation.html",
    "uploaded-reports.html",
    "quickbooks-guide.html",
]


def _read(name):
    return (TEMPLATES / name).read_text(encoding="utf-8")


def t1_no_placeholder_emails_in_customer_templates():
    # ".example" TLD placeholder emails must not be hard-coded into
    # customer copy. The app already guards real addresses behind
    # {{ support_email }} / {{ security_email }} with a
    # "your-domain.example" sentinel check, which is allowed.
    bad = re.compile(r"[\w.+-]+@[\w.-]*\.example\b")
    offenders = []
    for name in CUSTOMER_TEMPLATES:
        text = _read(name)
        for m in bad.finditer(text):
            addr = m.group(0)
            # The sentinel comparison string is allowed.
            if "your-domain.example" in addr:
                continue
            offenders.append(f"{name}: {addr}")
    assert not offenders, "placeholder emails leaked to customers: " + "; ".join(offenders)
    print("T1 OK: no fake placeholder emails in customer-facing templates")


def t2_account_mapping_support_link_is_guarded():
    text = _read("account-mapping.html")
    assert "support@pclaw-qbo.example" not in text, "hard-coded placeholder support email still present"
    # Contact-support mailto should use the guarded variable.
    assert "mailto:{{ support_email }}" in text, "contact-support link should use {{ support_email }}"
    assert 'your-domain.example" not in support_email' in text, "guard check missing around support link"
    print("T2 OK: account-mapping contact-support link is guarded by real-email check")


def t3_step_pages_avoid_worst_jargon():
    checks = {
        "opening-balance.html": [
            "Post opening balance JE",
            "balanced journal entry that seeds",
            "opening balance JournalEntry",
        ],
        "preview-import.html": [
            "double-post your opening trial balance",
        ],
    }
    offenders = []
    for name, phrases in checks.items():
        text = _read(name)
        for p in phrases:
            if p in text:
                offenders.append(f"{name}: {p!r}")
    assert not offenders, "raw jargon resurfaced: " + "; ".join(offenders)
    # And the plain-English replacements should be present.
    ob = _read("opening-balance.html")
    assert "Post starting balances" in ob, "expected plain-English 'Post starting balances' CTA"
    print("T3 OK: step pages avoid worst raw-jargon phrases, use plain English")


def t4_support_assistant_step_order_matches_real_flow():
    res = support_assistant.answer("how does the workflow work")
    ans = res["answer"]
    assert res["matched"], ans
    # Step 3 is Connect QuickBooks + match; Step 4 is review. The old copy
    # had these reversed, which contradicted the live stepper.
    assert "3) Connect QuickBooks and match" in ans, ans
    assert "4) Review" in ans, ans
    qbo = support_assistant.answer("how do I connect quickbooks")["answer"]
    assert "On Step 3" in qbo, qbo
    assert "On Step 4" not in qbo, qbo
    print("T4 OK: support assistant describes the six steps in the real order")


def t5_security_privacy_drop_internal_library_name():
    for name in ("security.html", "privacy.html"):
        text = _read(name)
        assert "Fernet" not in text, f"{name} still names the internal 'Fernet' library to customers"
        assert "AES-256" in text, f"{name} should keep the reassuring 'AES-256' wording"
    print("T5 OK: Security/Privacy keep AES-256 but drop internal 'Fernet' name")


def t6_public_pages_brand_and_no_qbo():
    c = appmod.app.test_client()
    pages = ["/", "/pricing", "/about", "/security", "/privacy", "/support", "/login", "/signup"]
    for path in pages:
        body = c.get(path).get_data(as_text=True)
        assert "PCLaw Migrate" not in body, f"{path} has 'PCLaw Migrate' product-name regression"
        # Customer-facing copy uses 'QuickBooks', never 'QBO'. (Operator
        # pages are not in this list.)
        assert "QBO" not in body, f"{path} surfaces customer-facing 'QBO'"
    print("T6 OK: public pages render correct brand and no customer-facing 'QBO'")


if __name__ == "__main__":
    t1_no_placeholder_emails_in_customer_templates()
    t2_account_mapping_support_link_is_guarded()
    t3_step_pages_avoid_worst_jargon()
    t4_support_assistant_step_order_matches_real_flow()
    t5_security_privacy_drop_internal_library_name()
    t6_public_pages_brand_and_no_qbo()
    print("ALL UX CLARITY PASS SMOKE TESTS PASSED")
