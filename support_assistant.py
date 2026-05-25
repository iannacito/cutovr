"""Customer-facing support assistant for PCLaw Migrate.

Answers common questions about the migration product from a curated, in-app
knowledge base. The assistant is designed to be useful with zero AI
configuration: the curated FAQ + lightweight keyword retrieval gives a
sensible answer to most questions a lawyer-customer would ask.

If an AI provider key is configured in the environment, the same module can
forward the question to a hosted LLM and return its answer. Otherwise the
fallback retriever runs.

Environment variables (all optional):

  SUPPORT_AI_API_KEY      Generic key used by the assistant. If set, takes
                          precedence and selects the provider based on
                          SUPPORT_AI_PROVIDER (default: "openai").
  OPENAI_API_KEY          Recognised as a fallback key if SUPPORT_AI_API_KEY
                          is not set. Implies provider = "openai".
  SUPPORT_AI_PROVIDER     "openai" (default) or "none". Set to "none" to
                          force fallback even when a key is present.
  SUPPORT_AI_MODEL        Override model name. Default: "gpt-4o-mini".
  SUPPORT_AI_TIMEOUT      HTTP timeout for the upstream call, seconds.
                          Default: 12.

Guardrails:

  * The assistant never claims to give legal or accounting advice.
  * Unknown questions are answered with a concise fallback that points the
    customer at the support mailbox.
  * Secrets are read from os.environ inside this module and are never
    placed in responses returned to the client.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SUPPORT_AI_PROVIDER_ENV = "SUPPORT_AI_PROVIDER"
SUPPORT_AI_KEY_ENV = "SUPPORT_AI_API_KEY"
OPENAI_KEY_ENV = "OPENAI_API_KEY"
SUPPORT_AI_MODEL_ENV = "SUPPORT_AI_MODEL"
SUPPORT_AI_TIMEOUT_ENV = "SUPPORT_AI_TIMEOUT"

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT_SECONDS = 12
MAX_QUESTION_CHARS = 600

LEGAL_OR_ACCOUNTING_DISCLAIMER = (
    "I can help with general PCLaw Migrate questions, but I'm not a substitute "
    "for legal or accounting advice. For firm-specific decisions, please check "
    "with your accountant or bookkeeper."
)

UNKNOWN_ANSWER_TEMPLATE = (
    "I don't have a confident answer for that one yet. For a hands-on response, "
    "email {support_email} with a short description of what you're trying to do."
)


# ---------------------------------------------------------------------------
# Curated knowledge base. Lawyers, not accountants — keep it plain.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FaqEntry:
    topic: str
    title: str
    keywords: Tuple[str, ...]
    answer: str


FAQ: Tuple[FaqEntry, ...] = (
    FaqEntry(
        topic="overview",
        title="What does PCLaw Migrate do?",
        keywords=(
            "pclaw migrate", "the product", "the service",
            "overview", "purpose", "product", "service", "about",
            "what does this", "what is this",
        ),
        answer=(
            "PCLaw Migrate moves your firm's accounting history out of PCLaw and "
            "into QuickBooks Online. You upload a few reports from PCLaw, we map "
            "the accounts to QuickBooks, and we post the entries so the books "
            "tie out. Most firms get through it in well under an hour."
        ),
    ),
    FaqEntry(
        topic="reports",
        title="Which PCLaw reports do I need?",
        keywords=(
            "report", "reports", "csv", "export", "file", "files",
            "general ledger", "gl", "chart of accounts", "coa",
            "trial balance", "tb", "trust", "listing", "what do i need",
            "upload",
        ),
        answer=(
            "You need four exports from PCLaw, all as CSV:\n"
            "• General Ledger — every journal entry for the period.\n"
            "• Chart of Accounts — the list of your PCLaw accounts.\n"
            "• Trial Balance — closing balances at your cutover date.\n"
            "• Trust Listing — client trust balances at your cutover date.\n"
            "The onboarding page has sample files and step-by-step PCLaw export "
            "instructions."
        ),
    ),
    FaqEntry(
        topic="steps",
        title="What are the steps in a migration?",
        keywords=(
            "step", "steps", "process", "workflow", "how does it work",
            "how do i start", "checklist", "phases", "order",
        ),
        answer=(
            "There are six steps, each on its own page:\n"
            "1. Onboarding — confirm what you need from PCLaw.\n"
            "2. Upload reports — General Ledger, Chart of Accounts, Trial Balance, Trust Listing.\n"
            "3. Connect QuickBooks Online.\n"
            "4. Match accounts — line up PCLaw accounts to QBO accounts.\n"
            "5. Send to QuickBooks — we post the entries.\n"
            "6. Reconcile balances — the closing balances in QBO match PCLaw and "
            "you get a final report."
        ),
    ),
    FaqEntry(
        topic="quickbooks",
        title="How does the QuickBooks connection work?",
        keywords=(
            "quickbooks", "qbo", "connect", "connection", "oauth", "intuit",
            "sign in to quickbooks", "authorize",
        ),
        answer=(
            "We connect with QuickBooks Online's official Intuit OAuth flow. "
            "You click Connect, sign in to QuickBooks, and grant access to the "
            "specific company you want to migrate into. We only post journal "
            "entries — we never read your QuickBooks payroll or banking "
            "credentials. You can disconnect any time from the QuickBooks page."
        ),
    ),
    FaqEntry(
        topic="account-matching",
        title="How does account matching work?",
        keywords=(
            "match", "matching", "map", "mapping", "accounts", "account",
            "chart of accounts", "coa", "unmapped", "create account",
        ),
        answer=(
            "Each PCLaw account needs to point at a QuickBooks account. We try "
            "to match by account number first, then by name. Anything we can't "
            "match shows up on the Match accounts page so you can pick the right "
            "QBO account — or have us create a new one with the same name and "
            "number. Nothing posts to QuickBooks until every account is matched."
        ),
    ),
    FaqEntry(
        topic="trust",
        title="How are trust balances handled?",
        keywords=(
            "trust", "iolta", "client trust", "trust account", "trust balance",
            "trust listing", "client funds", "retainer",
        ),
        answer=(
            "Your Trust Listing gives us each client's trust balance at your "
            "cutover date. We post those as opening balances against the trust "
            "liability account in QuickBooks, so what's in trust in QBO matches "
            "what was in PCLaw. If a client trust balance doesn't tie out, the "
            "Reconcile Balances step flags it before you finish."
        ),
    ),
    FaqEntry(
        topic="reconciliation",
        title="What is the final reconciliation?",
        keywords=(
            "reconcile", "reconciliation", "balances", "tie out", "match",
            "final", "verify", "step 6", "check", "compare",
        ),
        answer=(
            "After everything is posted, the Reconcile Balances page compares "
            "the closing balances in QuickBooks against the Trial Balance and "
            "Trust Listing you uploaded. If a number is off, you'll see exactly "
            "which account and by how much before you sign off."
        ),
    ),
    FaqEntry(
        topic="final-report",
        title="How do I get the final migration report?",
        keywords=(
            "final report", "report", "email", "summary", "pdf", "send me",
            "completion", "wrap up",
        ),
        answer=(
            "When the migration is complete, the Reconcile Balances page shows "
            "the final report on-screen and offers to email a copy to the "
            "address on your account. The report lists what was imported, the "
            "reconciliation result, and the QuickBooks company it went to."
        ),
    ),
    FaqEntry(
        topic="pricing",
        title="How much does this cost?",
        keywords=(
            "price", "pricing", "cost", "billing", "fee", "subscription",
            "how much", "charge", "invoice", "pay",
        ),
        answer=(
            "Pricing depends on the size of your ledger and how many companies "
            "you're migrating. For a current quote, email {support_email} with "
            "a rough idea of your firm's size and we'll get back to you with a "
            "fixed price."
        ),
    ),
    FaqEntry(
        topic="security",
        title="How do you keep my data safe?",
        keywords=(
            "security", "secure", "safe", "privacy", "private", "encryption",
            "encrypted", "protect", "data", "store", "retain", "delete",
        ),
        answer=(
            "Uploaded files are stored encrypted at rest. QuickBooks tokens are "
            "encrypted before they touch our database. We only access the "
            "QuickBooks company you authorise, and you can disconnect at any "
            "time from the QuickBooks page. The Privacy page has the full "
            "details."
        ),
    ),
    FaqEntry(
        topic="support",
        title="When should I contact a human?",
        keywords=(
            "support", "contact", "help", "human", "email", "talk", "stuck",
            "broken", "error", "issue", "bug", "problem",
        ),
        answer=(
            "If anything looks wrong — numbers that don't tie out, an error "
            "during import, a QuickBooks account you can't match — email "
            "{support_email} with the job ID from the top of the page and we'll "
            "take a look. Please don't paste raw QuickBooks tokens or full "
            "ledger contents into email."
        ),
    ),
    FaqEntry(
        topic="legal-disclaimer",
        title="Is this legal or accounting advice?",
        keywords=(
            "legal advice", "accounting advice", "advice", "bookkeeper",
            "accountant", "cpa", "tax", "compliance",
        ),
        answer=(
            "No. PCLaw Migrate is a tool that moves accounting data between "
            "PCLaw and QuickBooks Online. For firm-specific accounting or legal "
            "decisions — what account something should book to, how to handle a "
            "trust adjustment, anything tax-related — please check with your "
            "accountant or bookkeeper."
        ),
    ),
    FaqEntry(
        topic="reversal",
        title="What if I imported into the wrong QuickBooks company?",
        keywords=(
            "wrong", "reverse", "reversal", "undo", "delete", "rollback",
            "mistake", "wrong company", "wrong qbo",
        ),
        answer=(
            "Open the job, scroll to Reverse this import, type REVERSE, and "
            "click the button. We post offsetting journal entries in QuickBooks "
            "(DocNumber starting REV-) so the books net to zero. The originals "
            "stay visible for audit."
        ),
    ),
)


# Words we strip when tokenising the customer's question. Kept small — we
# want to keep words like "trust" and "report" intact.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "does",
    "for", "from", "have", "how", "i", "if", "in", "is", "it", "me", "my",
    "of", "on", "or", "should", "so", "that", "the", "then", "to", "was",
    "we", "what", "when", "where", "which", "who", "why", "will", "with",
    "you", "your", "this", "there", "can", "could", "would",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]+")


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if t.lower() not in _STOPWORDS]


def _score(question: str, entry: FaqEntry) -> float:
    tokens = _tokenize(question)
    raw = (question or "").lower()
    if not tokens and not raw:
        return 0.0
    score = 0.0
    for kw in entry.keywords:
        kw_l = kw.lower()
        # Phrase match (multi-word keyword) is worth more than single word
        # hits, because phrases are how customers actually ask things.
        # Phrases are matched against the raw lowercased question so that
        # stopwords like "what" and "does" don't break "what does" matches.
        if " " in kw_l:
            if kw_l in raw:
                score += 3.0
        elif kw_l in tokens:
            score += 1.0
    return score


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class AssistantAnswer:
    """What the assistant returns to the route handler.

    source: "faq" (matched a curated entry), "ai" (LLM responded),
            "fallback" (no match, unknown-question template),
            "guardrail" (legal/accounting deflection only).
    confident: True when we believe the answer is on-topic. UI may use this
               to decide whether to show the support-email pointer.
    topic: For "faq" answers, the FAQ topic. None otherwise.
    """

    answer: str
    source: str
    confident: bool
    topic: Optional[str] = None


def _support_email() -> str:
    try:
        import branding  # local import to avoid hard coupling at module load
        return branding.SUPPORT_EMAIL or "support@pclawmigrate.com"
    except Exception:  # noqa: BLE001
        return "support@pclawmigrate.com"


def _format(template: str) -> str:
    return template.format(support_email=_support_email())


def _looks_like_legal_or_accounting_advice(question: str) -> bool:
    """Heuristic: is the customer asking us to make a specific accounting
    or legal judgment for their firm? We don't refuse — we just lead with
    the disclaimer.
    """
    q = (question or "").lower()
    triggers = (
        "should i book", "should we book",
        "is it legal", "are we allowed", "am i allowed",
        "tax", "irs", "cra", "deductib", "audit risk",
        "which account should",
        "is this gaap", "gaap", "as a fiduciary",
    )
    return any(t in q for t in triggers)


def _retrieve(question: str) -> Optional[FaqEntry]:
    if not question or not question.strip():
        return None
    scored = sorted(
        ((entry, _score(question, entry)) for entry in FAQ),
        key=lambda pair: pair[1],
        reverse=True,
    )
    if not scored:
        return None
    best, best_score = scored[0]
    if best_score < 1.0:
        return None
    return best


def _ai_config() -> Optional[dict]:
    """Return active AI config, or None for fallback mode.

    Never returns the key in any value used downstream beyond the HTTP
    Authorization header — callers must not log this dict.
    """
    provider = (os.environ.get(SUPPORT_AI_PROVIDER_ENV) or "openai").strip().lower()
    if provider == "none":
        return None
    key = (os.environ.get(SUPPORT_AI_KEY_ENV) or os.environ.get(OPENAI_KEY_ENV) or "").strip()
    if not key:
        return None
    if provider != "openai":
        # Only openai is wired up server-side. Other providers fall back so
        # we don't ship half-working integrations.
        log.warning("support_assistant: unsupported provider %r, using fallback", provider)
        return None
    model = (os.environ.get(SUPPORT_AI_MODEL_ENV) or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    try:
        timeout = float(os.environ.get(SUPPORT_AI_TIMEOUT_ENV) or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    return {"provider": provider, "key": key, "model": model, "timeout": timeout}


def ai_mode_enabled() -> bool:
    """Public predicate so the UI / health probe can show 'AI on' vs 'FAQ only'.

    Does not return the secret itself.
    """
    return _ai_config() is not None


def _system_prompt() -> str:
    """Prompt used when AI mode is enabled. Anchored on the curated FAQ so
    the LLM stays on-topic and doesn't invent product behavior.
    """
    faq_block = "\n\n".join(
        f"Q: {entry.title}\nA: {_format(entry.answer)}"
        for entry in FAQ
    )
    return (
        "You are the customer-support assistant for PCLaw Migrate, a tool "
        "that migrates law-firm accounting data from PCLaw into QuickBooks "
        "Online. Your audience is lawyers, not accountants — use plain "
        "language and skip accounting jargon.\n\n"
        "Hard rules:\n"
        "- Do not give legal or accounting advice. For firm-specific "
        "decisions, tell the user to check with their accountant or "
        "bookkeeper.\n"
        "- Stay on the topic of PCLaw, QuickBooks Online, and the "
        "PCLaw Migrate workflow. If asked about anything else, say so "
        f"and point them at {_support_email()}.\n"
        "- Keep answers short. Two or three sentences is plenty unless "
        "the user explicitly asks for steps.\n"
        "- Never claim to access the user's data, ledger, or QuickBooks "
        "company. You can only describe how the product works.\n\n"
        "Reference knowledge base:\n"
        f"{faq_block}"
    )


def _call_openai(question: str, cfg: dict) -> Optional[str]:
    """One-shot call to OpenAI Chat Completions. Returns the answer text on
    success, None on any failure (caller falls back).
    """
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": question[:MAX_QUESTION_CHARS]},
        ],
        "temperature": 0.2,
        "max_tokens": 400,
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=cfg["timeout"])
    except requests.RequestException as exc:
        log.warning("support_assistant: AI call failed: %s", type(exc).__name__)
        return None
    if resp.status_code >= 400:
        # Do NOT log resp.text — provider error messages can echo the API key.
        log.warning("support_assistant: AI returned HTTP %s", resp.status_code)
        return None
    try:
        data = resp.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError, TypeError):
        log.warning("support_assistant: AI response shape unexpected")
        return None
    return text or None


def answer(question: str) -> AssistantAnswer:
    """Main entry point. Always returns a non-empty answer.

    Order of resolution:
      1. Empty/too-long question → fallback.
      2. AI mode enabled → ask the model. On any failure → curated retrieval.
      3. Curated retrieval. On hit → return FAQ answer. On miss → fallback.

    Legal/accounting-flavoured questions get a one-line disclaimer prepended
    regardless of source.
    """
    if not question or not question.strip():
        return AssistantAnswer(
            answer=_format(UNKNOWN_ANSWER_TEMPLATE),
            source="fallback",
            confident=False,
        )
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        question = question[:MAX_QUESTION_CHARS]

    disclaimer = LEGAL_OR_ACCOUNTING_DISCLAIMER if _looks_like_legal_or_accounting_advice(question) else ""

    cfg = _ai_config()
    if cfg is not None:
        text = _call_openai(question, cfg)
        if text:
            out = f"{disclaimer}\n\n{text}".strip() if disclaimer else text
            return AssistantAnswer(answer=out, source="ai", confident=True)
        # AI failed — fall through to curated retrieval so the user still
        # gets a useful response.

    entry = _retrieve(question)
    if entry is not None:
        body = _format(entry.answer)
        out = f"{disclaimer}\n\n{body}".strip() if disclaimer else body
        return AssistantAnswer(
            answer=out,
            source="faq",
            confident=True,
            topic=entry.topic,
        )

    fallback = _format(UNKNOWN_ANSWER_TEMPLATE)
    out = f"{disclaimer}\n\n{fallback}".strip() if disclaimer else fallback
    return AssistantAnswer(answer=out, source="fallback", confident=False)


def suggested_topics() -> List[dict]:
    """Topics shown as quick-pick chips in the chat widget. Keeps customers
    moving when they don't know what to ask.
    """
    picks = (
        "overview", "steps", "reports", "quickbooks", "account-matching",
        "trust", "reconciliation", "security", "support",
    )
    by_topic = {e.topic: e for e in FAQ}
    return [
        {"topic": t, "title": by_topic[t].title}
        for t in picks
        if t in by_topic
    ]
