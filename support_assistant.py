"""Deterministic FAQ assistant for the support widget.

A small, dependency-free intent matcher that answers the most common
customer and prospect questions about Cutovr. Used by the
floating "Need help?" widget surfaced from _base.html on every page.

Design notes:
  * We deliberately do NOT call an LLM here. Answers are short, factual,
    reviewed copy. If a future deploy wants to wire up an LLM, it can
    read SUPPORT_ASSISTANT_LLM_ENABLED and swap in its own handler; that
    is intentionally out of scope for the widget itself.
  * The assistant never claims to access a customer's QuickBooks, PCLaw
    files, account, or personal data. It guides; it does not diagnose.
  * Every miss falls back to a "contact support" message so the user is
    never stuck.

Matching strategy (still fully deterministic):
  * The query is lowercased, punctuation-stripped, and tokenized.
  * A synonym map folds common phrasings onto canonical tokens (e.g.
    "qbo" -> "quickbooks", "can't connect" -> "connect"), so a lawyer's
    everyday wording lands on the right intent.
  * Each intent declares weighted keyword groups. A "strong" hit (a term
    that strongly signals one intent, like "trust" or "trial balance")
    outscores several generic hits, so a specific query beats a vague one.
  * Multi-word phrases are matched as ordered subsequences against the
    token stream so "connect quickbooks" and "quickbooks connect" both
    count without exploding the keyword lists.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

import branding


# ---------------------------------------------------------------------------
# Synonyms / normalization
# ---------------------------------------------------------------------------
# Map of raw token -> canonical token. Applied after tokenizing so that
# common abbreviations and spellings collapse onto the words our intents
# actually key on. This is the bulk of the "fuzzy" behavior; keep it broad
# but unambiguous (never map a word to something that changes its meaning).
_SYNONYMS: Dict[str, str] = {
    # QuickBooks variants
    "qbo": "quickbooks",
    "qb": "quickbooks",
    "quickbook": "quickbooks",
    "quickbooksonline": "quickbooks",
    "intuit": "quickbooks",
    # PCLaw variants
    "pclaw": "pclaw",
    "pc": "pclaw",
    "law": "pclaw",
    # connection
    "connecting": "connect",
    "connected": "connect",
    "connection": "connect",
    "reconnect": "connect",
    "link": "connect",
    "linking": "connect",
    "redirect": "connect",
    "redirected": "connect",
    "oauth": "connect",
    "login": "login",
    "signin": "login",
    "sign": "login",
    # accounts / matching
    "accounts": "account",
    "matching": "match",
    "matched": "match",
    "mapping": "match",
    "map": "match",
    "mapped": "match",
    "missing": "missing",
    "unmatched": "missing",
    "unmapped": "missing",
    # reports / files
    "reports": "report",
    "file": "file",
    "files": "file",
    "csv": "csv",
    "export": "report",
    "exports": "report",
    "exported": "report",
    "exporting": "report",
    "spreadsheet": "csv",
    "upload": "upload",
    "uploads": "upload",
    "uploaded": "upload",
    "uploading": "upload",
    "reupload": "upload",
    "add": "upload",
    "another": "more",
    "additional": "more",
    "extra": "more",
    "forgot": "forgot",
    "forgotten": "forgot",
    # validation / errors
    "blocked": "blocked",
    "block": "blocked",
    "rejected": "blocked",
    "skipped": "blocked",
    "error": "error",
    "errors": "error",
    "failed": "error",
    "failing": "error",
    "fails": "error",
    "fail": "error",
    "broken": "error",
    "invalid": "error",
    "stuck": "stuck",
    "problem": "error",
    "issue": "error",
    "wont": "error",
    # trust
    "trust": "trust",
    "iolta": "trust",
    "retainer": "trust",
    # trial balance / reconcile
    "trial": "trial",
    "balance": "balance",
    "balances": "balance",
    "reconcile": "reconcile",
    "reconciliation": "reconcile",
    "reconciling": "reconcile",
    "totals": "balance",
    "total": "balance",
    # data safety
    "delete": "delete",
    "deletes": "delete",
    "deleted": "delete",
    "deleting": "delete",
    "overwrite": "delete",
    "change": "change",
    "changes": "change",
    "modify": "change",
    "alter": "change",
    "remove": "delete",
    "destroy": "delete",
    # security/privacy
    "secure": "secure",
    "security": "secure",
    "privacy": "privacy",
    "private": "privacy",
    "encrypt": "secure",
    "encrypted": "secure",
    "encryption": "secure",
    "safe": "secure",
    "retention": "retention",
    "retain": "retention",
    "stored": "retention",
    "store": "retention",
    "keep": "retention",
    "gdpr": "privacy",
    # pricing
    "price": "price",
    "pricing": "price",
    "prices": "price",
    "cost": "price",
    "costs": "price",
    "fee": "price",
    "fees": "price",
    "charge": "price",
    "pay": "price",
    "payment": "price",
    "essential": "essential",
    "essentials": "essential",
    "standard": "standard",
    "complete": "complete",
    "tier": "tier",
    "tiers": "tier",
    "plan": "tier",
    "plans": "tier",
    "package": "tier",
    "packages": "tier",
    "subscription": "subscription",
    # password
    "password": "password",
    "passwords": "password",
    "reset": "reset",
    "resetting": "reset",
    "locked": "login",
    "lockout": "login",
    # demo
    "demo": "demo",
    "test": "demo",
    "sandbox": "demo",
    "sample": "demo",
    "trial run": "demo",
    "production": "production",
    "real": "production",
    "live": "production",
    # final report
    "summary": "summary",
    "pdf": "pdf",
    "receipt": "receipt",
    "download": "download",
    "email": "email",
    "emailed": "email",
    # reverse
    "reverse": "reverse",
    "reversal": "reverse",
    "undo": "reverse",
    "rollback": "reverse",
    "rollbacks": "reverse",
    "mistake": "reverse",
    # general
    "steps": "step",
    "workflow": "step",
    "process": "step",
    "work": "step",
    "works": "step",
    "how": "how",
    "help": "help",
    "support": "support",
    "contact": "contact",
}


# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------
# Each intent is (topic_id, groups, answer). `groups` is a list of
# (weight, terms) tuples. A term is either a single canonical token or a
# space-joined phrase (matched as an ordered subsequence of the canonical
# token stream). The intent's score is the sum of the weights of the
# groups that hit at least once. A "strong" group (weight >= 3) is enough
# on its own to win against generic chatter.
#
# The tuple shape kept here is intentionally (topic, keyword_groups,
# answer); the smoke test iterates `_FAQ` as `for _topic, _kw, text`, so
# any future change must keep `text` as the third element.
Group = Tuple[int, List[str]]

_FAQ: List[Tuple[str, List[Group], str]] = [
    (
        "what_is",
        [
            (3, ["what is", "what does", "tell me about", "explain", "about"]),
            (1, ["pclaw", "quickbooks", "product", "service", "do", "tool"]),
        ],
        (
            "Cutovr moves your firm's accounting history out of "
            "PCLaw and into QuickBooks — chart of accounts, opening "
            "balances, general ledger, trial balance, and A/R / A/P. You "
            "upload your PCLaw reports, match accounts, preview, then post. "
            "Nothing reaches QuickBooks until you confirm."
        ),
    ),
    (
        "steps",
        [
            (3, ["step", "how it work", "order", "what happens"]),
            (1, ["how", "first", "next", "begin", "start"]),
        ],
        (
            "It's six short steps: 1) Set up your migration, 2) Upload your "
            "PCLaw reports, 3) Connect QuickBooks and match your accounts, "
            "4) Review everything before posting, 5) Send to QuickBooks, "
            "6) Check the final balances. You can pause and come back — your "
            "progress is saved."
        ),
    ),
    (
        "reports_needed",
        [
            (3, ["report", "which file", "what file"]),
            (2, ["csv", "file"]),
            (1, ["upload", "need", "which", "what"]),
        ],
        (
            "From PCLaw, export: Chart of Accounts, Trial Balance (as of "
            "your cutover date), the General Ledger for the history you want "
            "to bring over, and a Client Trust listing if you carry trust "
            "balances. CSV is the safest format. The Onboarding page has "
            "templates and column tips."
        ),
    ),
    (
        "more_reports",
        [
            (4, ["forgot report", "forgot upload", "more report", "add report",
                 "upload more", "another report", "forgot file", "forgot to upload"]),
            (2, ["more", "missing report"]),
            (1, ["upload", "report", "file"]),
        ],
        (
            "No problem — you can add more reports any time before you post. "
            "Open your migration, go to the Upload step, and add the extra "
            "file; it slots in alongside what you already uploaded. If you "
            "already posted, you can reverse the import, add the report, and "
            "re-run."
        ),
    ),
    (
        "quickbooks_connect",
        [
            (4, ["connect quickbooks", "quickbooks connect", "cant connect", "connect"]),
            (3, ["redirect"]),
            (2, ["quickbooks"]),
        ],
        (
            "On Step 3 you click \"Connect QuickBooks\" and sign in with "
            "Intuit. You'll need an active QuickBooks Online subscription. If "
            "the sign-in bounces back or the redirect fails, try again in a "
            "fresh browser tab and make sure pop-ups aren't blocked. You can "
            "disconnect or reconnect any time from the QuickBooks page."
        ),
    ),
    (
        "matching",
        [
            (4, ["match account", "account match", "missing account", "create account"]),
            (3, ["match", "account number"]),
            (2, ["account", "missing"]),
        ],
        (
            "On the Match accounts step, each PCLaw account is shown next to "
            "a suggested QuickBooks account. Accept the suggestion, pick a "
            "different account, or create a new one — new accounts are only "
            "created after you type CREATE ACCOUNTS to confirm. If an account "
            "looks missing, it usually just needs to be matched or created "
            "here; account numbers carry over when QuickBooks has them "
            "enabled."
        ),
    ),
    (
        "blocked",
        [
            (4, ["blocked", "validation"]),
            (3, ["error", "stuck"]),
            (1, ["why", "cant", "wont"]),
        ],
        (
            "A blocked or skipped row means a check didn't pass yet — usually "
            "an account that isn't matched, a date outside your migration "
            "window, or a row that doesn't balance. The review screen lists "
            "each one in plain English with what to fix. Nothing is sent to "
            "QuickBooks while anything is still blocked."
        ),
    ),
    (
        "trust",
        [
            (4, ["trust"]),
            (1, ["balance", "listing", "client"]),
        ],
        (
            "If your firm carries trust (IOLTA) balances, export the Client "
            "Trust listing from PCLaw and upload it with your other reports. "
            "Cutovr keeps trust balances separate and reconciles the "
            "trust total so client funds line up before anything posts. If "
            "the trust total doesn't match, the review step flags it."
        ),
    ),
    (
        "trial_balance",
        [
            (4, ["trial balance", "final balance", "trial", "reconcile"]),
            (2, ["balance"]),
            (1, ["check", "match"]),
        ],
        (
            "After you post (Step 6), Cutovr compares your QuickBooks "
            "totals against the PCLaw Trial Balance you uploaded and shows any "
            "differences line by line. If the final balances tie out, you're "
            "done; if they don't, the report points you at the accounts to "
            "review."
        ),
    ),
    (
        "data_safety",
        [
            (4, ["delete", "change", "overwrite"]),
            (2, ["existing", "current"]),
            (1, ["quickbooks", "data"]),
        ],
        (
            "It only adds entries you've reviewed — it never deletes or edits "
            "the data already in your QuickBooks company. Nothing is posted "
            "until you type your confirmation, and any import can be reversed "
            "in one click, which posts offsetting entries QuickBooks treats "
            "as fully auditable."
        ),
    ),
    (
        "reverse",
        [
            (4, ["reverse", "undo", "rollback"]),
            (2, ["wrong company", "start over", "mistake"]),
        ],
        (
            "If an import went to the wrong company or you want to start over, "
            "open the migration, scroll to Reverse this import, type REVERSE, "
            "and click the button. QuickBooks gets offsetting entries; the "
            "originals stay visible for audit."
        ),
    ),
    (
        "demo",
        [
            (4, ["demo"]),
            (2, ["production", "real"]),
            (1, ["try", "before"]),
        ],
        (
            "You can try the whole workflow with the built-in demo dataset "
            "before running a real migration — the Onboarding page has sample "
            "CSVs. Demo and real migrations never share data; they're separate "
            "workspaces, so nothing you do in the demo touches your real "
            "QuickBooks company."
        ),
    ),
    (
        "security",
        [
            (4, ["secure", "privacy"]),
            (3, ["retention"]),
            (1, ["data", "token", "store"]),
        ],
        (
            "Your uploaded files and QuickBooks tokens are encrypted at rest, "
            "and we never post to QuickBooks without your typed confirmation. "
            "Data is kept only as long as needed to run and support your "
            "migration, and you can disconnect QuickBooks any time. Full "
            "details are on the Security and Privacy pages in the footer."
        ),
    ),
    (
        "pricing",
        [
            (4, ["price", "subscription"]),
            (3, ["tier", "essential", "standard", "complete"]),
            (1, ["how much"]),
        ],
        (
            "Pricing is scoped per migration, because report quality, how much "
            "history you bring over, and trust-accounting needs vary by firm. "
            "Book a discovery call and we'll review your migration needs and "
            "provide a clear quote on the call. See the Pricing page for how "
            "it works."
        ),
    ),
    (
        "login",
        [
            (4, ["forgot password", "reset password", "password"]),
            (3, ["login", "reset"]),
            (1, ["account", "cant"]),
        ],
        (
            "Use the \"Forgot password?\" link on the sign-in page to get a "
            "reset email, then set a new password and sign in. If the reset "
            "email doesn't arrive, check spam first, then contact support. "
            "For your security we can't see or set your password for you."
        ),
    ),
    (
        "final_report",
        [
            (4, ["final report", "summary", "pdf", "receipt"]),
            (2, ["download", "email"]),
            (1, ["report"]),
        ],
        (
            "After Step 6 you can download a PDF migration summary listing "
            "every entry posted, every account matched, and any rows that "
            "were skipped — and email a copy to your bookkeeper right from "
            "the page."
        ),
    ),
    (
        "support",
        [
            (4, ["contact", "talk to", "real person", "human"]),
            (2, ["support", "help"]),
            (1, ["email"]),
        ],
        # Sentinel: resolved to the live support copy at call time so a
        # changed SUPPORT_EMAIL (or a placeholder mailbox) is reflected.
        "__SUPPORT__",
    ),
]


# ---------------------------------------------------------------------------
# Answers that depend on runtime config (support email) are templated at
# call time so a changed SUPPORT_EMAIL is always reflected.
# ---------------------------------------------------------------------------
def _support_answer() -> str:
    if branding.is_placeholder_email(branding.SUPPORT_EMAIL):
        return (
            "I'm the built-in assistant for general questions. For anything "
            "I can't answer, your migration page shows the exact next step "
            "for each stage. A human support contact is set up at launch — "
            "until then, the in-app guidance on each step is the fastest path."
        )
    return (
        f"For anything beyond this assistant, email {branding.SUPPORT_EMAIL}. "
        "Include your migration reference (top of your migration page) and "
        "the exact action and error text. We reply during business hours."
    )


def _fallback_answer() -> str:
    if branding.is_placeholder_email(branding.SUPPORT_EMAIL):
        return (
            "I'm a small built-in assistant, so I can answer general "
            "questions about Cutovr but I can't see your account or "
            "your QuickBooks data. Try rephrasing, or follow the step-by-step "
            "guidance on your migration page — it shows what to do next."
        )
    return (
        "I'm a small built-in assistant, so I can answer general questions "
        "about Cutovr but I can't see your account or your "
        f"QuickBooks data. If this didn't answer your question, email "
        f"{branding.SUPPORT_EMAIL} and a real person will follow up."
    )


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    """Lowercase, split on non-alphanumerics, then fold synonyms."""
    raw = _TOKEN_RE.findall(text.lower())
    return [_SYNONYMS.get(tok, tok) for tok in raw]


def _phrase_hits(phrase_tokens: List[str], stream: List[str]) -> bool:
    """True if phrase_tokens appear as an ordered (not necessarily
    contiguous) subsequence of stream. For a single token this is just
    membership."""
    if not phrase_tokens:
        return False
    it = iter(stream)
    return all(tok in it for tok in phrase_tokens)


def _score_intent(groups: List[Group], stream: List[str], token_set: set) -> int:
    score = 0
    for weight, terms in groups:
        for term in terms:
            term_tokens = _tokenize(term)
            if len(term_tokens) == 1:
                if term_tokens[0] in token_set:
                    score += weight
                    break
            elif _phrase_hits(term_tokens, stream):
                score += weight
                break
    return score


def _resolve_answer(text: str) -> str:
    return _support_answer() if text == "__SUPPORT__" else text


def answer(query: str) -> dict:
    """Return a dict {topic, answer, matched: bool} for a user query.

    Always returns a usable answer. On no confident match, returns the
    fallback pointing to support. Never raises.
    """
    if not query or not isinstance(query, str):
        return {"topic": "fallback", "answer": _fallback_answer(), "matched": False}

    stream = _tokenize(query)
    token_set = set(stream)
    if not token_set:
        return {"topic": "fallback", "answer": _fallback_answer(), "matched": False}

    best_topic = None
    best_score = 0
    best_text = None
    for topic, groups, text in _FAQ:
        score = _score_intent(groups, stream, token_set)
        if score > best_score:
            best_score = score
            best_topic = topic
            best_text = text

    # Require a minimum confidence so a single stray generic token (e.g.
    # "the", which won't match anyway, or a lone weak keyword) doesn't
    # force a possibly-wrong intent. Weak groups are weight 1; a real hit
    # is almost always >= 2.
    if best_score >= 2 and best_topic is not None:
        return {
            "topic": best_topic,
            "answer": _resolve_answer(best_text),
            "matched": True,
        }
    return {"topic": "fallback", "answer": _fallback_answer(), "matched": False}


def suggested_topics() -> List[dict]:
    """Short list of clickable starter prompts shown in the widget UI."""
    return [
        {"label": "What does Cutovr do?", "query": "what is cutovr"},
        {"label": "Which reports do I need?", "query": "which reports do I need"},
        {"label": "How do the steps work?", "query": "how does the migration work step by step"},
        {"label": "How do I connect QuickBooks?", "query": "how do I connect quickbooks"},
        {"label": "An account looks missing", "query": "missing account match"},
        {"label": "Is my data secure?", "query": "is my data secure and private"},
        {"label": "What does it cost?", "query": "how much does it cost"},
        {"label": "I forgot my password", "query": "forgot password reset"},
    ]
