# Support Assistant — internal notes

This is the customer-facing migration assistant — the floating chat widget
in the bottom-right of every page, plus the `/support` page surface that
introduces it. The audience is lawyers (not accountants), so the copy is
deliberately plain.

## How it works

There are two answer paths, decided at request time by environment:

1. **AI mode** (when `SUPPORT_AI_API_KEY` or `OPENAI_API_KEY` is set):
   the assistant forwards the customer question to OpenAI Chat Completions
   with a system prompt anchored on the curated FAQ in
   `support_assistant.py`. The key is sent only as an `Authorization: Bearer`
   header to the provider — never returned to the browser, never logged.
2. **Fallback / FAQ mode** (no key configured, or AI call failed):
   the assistant scores the question against curated keywords and returns
   the matching FAQ entry verbatim. If nothing scores high enough, it
   returns a short fallback that points the customer at `SUPPORT_EMAIL`.

AI failures (timeout, non-2xx, unexpected shape) silently downgrade to the
fallback so a misconfigured provider never breaks the feature for end users.

## Environment variables

All optional. The feature works with none of them set.

| Variable | Purpose | Default |
| --- | --- | --- |
| `SUPPORT_AI_API_KEY` | Preferred key. If set, enables AI mode. | unset |
| `OPENAI_API_KEY` | Fallback key (recognised if `SUPPORT_AI_API_KEY` isn't set). | unset |
| `SUPPORT_AI_PROVIDER` | Currently only `openai` is wired up. Set to `none` to force fallback even with a key present. | `openai` |
| `SUPPORT_AI_MODEL` | OpenAI model name. | `gpt-4o-mini` |
| `SUPPORT_AI_TIMEOUT` | HTTP timeout, seconds. | `12` |

Set these in the same place you set the other production env vars (Render
dashboard for the hosted deploy). Never commit them.

## Endpoints

- `GET /support` — public help page. Lists curated FAQ topics and
  indicates whether AI mode is active on this deployment.
- `POST /api/support/ask` — JSON endpoint backing the chat widget.
  - Body: `{"question": "..."}` (max 600 chars).
  - Response: `{"answer": "...", "source": "faq|ai|fallback",
    "confident": true|false, "topic": "<faq-topic-or-null>",
    "support_email": "..."}`.
  - CSRF: enforced via the global before-request hook. The widget sends
    the token in the `X-CSRF-Token` header.
  - Rate-limited per-IP: 30 questions / 5 minutes. Returns `429` with a
    `retry_after` field when exceeded.

## UI placement

The widget is mounted in `templates/_base.html`, so it appears on every
template that extends the base — landing, login, signup, support, dashboard,
and all migration-workflow steps. On mobile the launcher collapses to a
circular icon.

The `/support` page also surfaces a brief explainer card pointing customers
at the assistant, plus the existing curated quick-answer content.

## Guardrails

Hard-coded in `support_assistant.py`:

- The system prompt (AI mode) instructs the model to never give legal or
  accounting advice and to stay on the topic of PCLaw, QuickBooks Online,
  and the migration workflow.
- A heuristic (`_looks_like_legal_or_accounting_advice`) prepends a
  short disclaimer to answers when the customer's question reads like a
  request for a firm-specific accounting or legal call.
- When the assistant can't answer, the fallback always includes the
  `SUPPORT_EMAIL` so the customer has a real human escalation path.
- The endpoint never echoes the value of any environment variable. Even
  if the model is asked to "print env" or "reveal SUPPORT_AI_API_KEY",
  the system prompt + retrieval anchoring keep us off-topic and the
  fallback never references env state. See
  `tests/smoke_support_assistant.py::t4` for the regression test.

## Tests

`tests/smoke_support_assistant.py` covers:

- T1 curated FAQ answers common questions confidently.
- T2 unknown / empty questions return the fallback with the support email.
- T3 legal/accounting-flavoured questions get the disclaimer.
- T4 the endpoint returns JSON and never leaks the AI key (also asserts
  the key travels only inside the `Authorization` header to the provider).
- T5 CSRF protection: POST without token is rejected.
- T6 rate-limit triggers after rapid repeated requests.
- T7 the widget is mounted on every public page.
- T8 the curated fallback is used when no AI key is configured.
- T9 `ai_mode_enabled()` reflects env state without exposing the key.

Run with: `python3 tests/smoke_support_assistant.py`.

## What this does NOT do

- It does not read or speak about the customer's actual ledger, job, or
  QuickBooks company. It only answers product questions.
- It does not persist the conversation. Each question is independent;
  there's no session memory and no per-user history.
- It does not call any provider other than OpenAI. Adding a new provider
  means wiring a new `_call_<provider>` branch in `support_assistant.py`
  and updating the `_ai_config` allowlist — please don't add an unsupported
  provider as a silent passthrough.
