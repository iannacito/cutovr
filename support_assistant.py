"""Deterministic FAQ assistant for the support widget.

A small, dependency-free decision tree that answers the most common
customer questions about PC Law Migrate. Used by the floating
"Need help?" widget surfaced from _base.html on every page.

Design notes:
  * We deliberately do NOT call an LLM here. Answers are short, factual,
    and reviewed copy. If a future deploy wants to wire up an LLM, it
    can read SUPPORT_ASSISTANT_LLM_ENABLED and swap in its own handler;
    that is intentionally out of scope for the widget itself.
  * The assistant never claims to access a customer's QuickBooks, PCLaw
    files, account, or personal data. It guides; it does not diagnose.
  * Every miss falls back to a "contact support" message so the user
    is never stuck.
"""

from __future__ import annotations

import re
from typing import List, Tuple

import branding


# A keyword-based FAQ. Each entry is (topic_id, keyword_patterns, answer).
# We match keywords against a lowercased + punctuation-stripped version of
# the user query. Multiple matches are scored by number of distinct
# keywords hit so a more specific query wins over a generic one.
_FAQ: List[Tuple[str, List[str], str]] = [
    (
        "what_is",
        ["what", "is", "pc law migrate", "pclaw migrate", "do", "does", "product", "service"],
        (
            "PC Law Migrate moves your firm's accounting history out of PCLaw "
            "and into QuickBooks Online — chart of accounts, opening balances, "
            "general ledger, trial balance, and A/R / A/P. You upload PCLaw "
            "reports, match accounts, preview, then post. Nothing is sent to "
            "QuickBooks until you confirm."
        ),
    ),
    (
        "reports_needed",
        ["report", "reports", "files", "export", "need", "upload", "which", "what"],
        (
            "From PCLaw you'll typically export: Chart of Accounts, Trial "
            "Balance (as of cutover), General Ledger for the history you want "
            "to bring over, and a Client Trust listing if you carry trust "
            "balances. CSV is the safest format. The Onboarding page has "
            "templates and column tips."
        ),
    ),
    (
        "steps",
        ["step", "steps", "workflow", "how", "process", "order"],
        (
            "The flow is six short steps: 1) Upload your PCLaw reports, "
            "2) Match your accounts, 3) Review the Trial Balance, 4) Connect "
            "QuickBooks, 5) Send the entries, 6) Reconcile the balances. "
            "You can pause and come back — your progress is saved."
        ),
    ),
    (
        "quickbooks",
        ["quickbooks", "qbo", "connect", "connection", "intuit", "oauth"],
        (
            "You'll need an active QuickBooks Online subscription. On Step 4 "
            "you click \"Connect QuickBooks\" and sign in with Intuit. We "
            "store only the short-lived access token, encrypted. You can "
            "disconnect any time from the QuickBooks page."
        ),
    ),
    (
        "security",
        ["secure", "security", "privacy", "encrypt", "encryption", "data", "safe", "private"],
        (
            "Your files and QuickBooks tokens are encrypted at rest. We "
            "never post to QuickBooks without your typed confirmation. "
            "You can reverse an import with one click. Full details are on "
            "the Security and Privacy pages linked in the footer."
        ),
    ),
    (
        "pricing",
        ["price", "pricing", "cost", "how much", "fee", "subscription"],
        (
            "Pricing starts at $799 with no subscription — one flat fee per "
            "migration. The Complete tier covers 3+ years of history and is "
            "quoted per firm. See the Pricing page for the full breakdown."
        ),
    ),
    (
        "support",
        ["support", "help", "contact", "email", "talk", "person"],
        (
            f"For anything beyond this assistant, email "
            f"{branding.SUPPORT_EMAIL}. Include your migration reference "
            f"(top of your migration page) and the exact action and error "
            f"text. We reply during business hours."
        ),
    ),
    (
        "stuck",
        ["stuck", "blocked", "error", "fail", "failing", "broken", "issue", "problem", "wrong"],
        (
            "If a step is stuck, the page usually shows what's missing — a "
            "report not uploaded yet, an account not matched, or a "
            "QuickBooks connection that needs to be reconnected. If the "
            f"page doesn't explain it clearly, email {branding.SUPPORT_EMAIL} "
            f"with the migration reference and what you were trying to do."
        ),
    ),
    (
        "demo",
        ["demo", "test", "sandbox", "try", "sample", "production"],
        (
            "You can try the workflow with the built-in demo dataset before "
            "running a real migration. The Onboarding page has sample CSVs. "
            "Demo and production runs never share data — they're separate "
            "workspaces."
        ),
    ),
    (
        "final_report",
        ["report", "pdf", "summary", "final", "email", "download", "receipt"],
        (
            "After Step 6 you can download a PDF migration summary listing "
            "every entry posted, every account matched, and any rows that "
            "were skipped. The page also lets you email a copy to your "
            "bookkeeper."
        ),
    ),
    (
        "matching",
        ["match", "matching", "map", "mapping", "account", "accounts", "missing", "create"],
        (
            "On the Match accounts step you'll see each PCLaw account "
            "alongside a suggested QuickBooks account. You can accept the "
            "suggestion, pick a different account, or create a new one — "
            "but new accounts only get created after you type "
            "CREATE ACCOUNTS to confirm."
        ),
    ),
    (
        "reverse",
        ["reverse", "undo", "rollback", "mistake", "wrong company"],
        (
            "If an import went to the wrong company or you want to start "
            "over, open the migration, scroll to Reverse this import, type "
            "REVERSE, and click the button. QuickBooks gets offsetting "
            "entries; the originals stay visible for audit."
        ),
    ),
]


_FALLBACK = (
    "I'm a small built-in assistant, so I can answer general questions "
    "about PC Law Migrate but I can't see your account or your QuickBooks "
    f"data. If this didn't answer your question, email {{support_email}} "
    "and a real person will follow up."
)


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


def answer(query: str) -> dict:
    """Return a dict {topic, answer, matched: bool} for a user query.

    Always returns a usable answer. On no match, returns the fallback
    pointing to support email. Never raises.
    """
    fallback = _FALLBACK.format(support_email=branding.SUPPORT_EMAIL)
    if not query or not isinstance(query, str):
        return {"topic": "fallback", "answer": fallback, "matched": False}

    tokens = set(_tokenize(query))
    if not tokens:
        return {"topic": "fallback", "answer": fallback, "matched": False}

    best_topic = None
    best_score = 0
    best_answer = fallback
    for topic, keywords, text in _FAQ:
        score = 0
        for kw in keywords:
            kw_tokens = set(_tokenize(kw))
            if kw_tokens and kw_tokens.issubset(tokens):
                score += len(kw_tokens)
        if score > best_score:
            best_score = score
            best_topic = topic
            best_answer = text

    if best_score >= 1 and best_topic is not None:
        return {"topic": best_topic, "answer": best_answer, "matched": True}
    return {"topic": "fallback", "answer": fallback, "matched": False}


def suggested_topics() -> List[dict]:
    """Short list of clickable starter prompts shown in the widget UI."""
    return [
        {"label": "What does PC Law Migrate do?", "query": "what is pc law migrate"},
        {"label": "Which reports do I need?", "query": "which reports do I need"},
        {"label": "How do the steps work?", "query": "how does the workflow work"},
        {"label": "How do I connect QuickBooks?", "query": "how do I connect quickbooks"},
        {"label": "Is my data secure?", "query": "is my data secure"},
        {"label": "What does it cost?", "query": "what does it cost"},
    ]
