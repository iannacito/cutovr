# Clio Accounting API v1 — internal foundation (back-pocket)

**Status: INTERNAL / dry-run only. Not public. Does not post to Clio or QuickBooks.**

This document is the developer handoff for the Clio Accounting API v1 foundation
that Cutovr is building *ahead* of Clio opening production API access. It stacks
on top of PR #122 (the Clio readiness service lanes) and adds the API-aligned
architecture: a capability registry, a disabled-by-default adapter, canonical
payload builders, per-lane data-flow plans, and an operator-only readiness view.

The goal is to have durable, tested wiring in place so that when Clio publishes
its developer-portal docs/OpenAPI and grants production access, we flip a flag
and fill in one method — instead of building from scratch under time pressure.

## What is implemented now

| Area | File | Notes |
|------|------|-------|
| Capability registry | `clio_accounting_capabilities.py` | Endpoint families + operations with an **assumed** status each (from the roadmap), platform-hardening notes, `MAX_PAGE_SIZE=200`, serializable snapshot marked `internal_only`. |
| Adapter boundary | `clio_accounting.py` (`ClioAccountingAdapter`) | Typed operation methods per write family. Disabled by default → writes return a structured **blocked** `OperationResult` (never silent success). Idempotency key on every operation. Secret-free `config_summary()`. |
| Payload builders | `clio_accounting_payloads.py` | Canonical Cutovr-side payloads for ledger accounts, journal entries, reports, vendor bills, vendor bill payments, expenses, and vendor/client/matter reference placeholders. Each carries `_meta` (idempotency key + schema version). Journal entries enforce a real debits==credits invariant. |
| Two-lane data-flow plans | `clio_accounting_lanes.py` | Ordered migration steps for PC Law→Clio and QBO→Clio, each mapping a source artifact → Clio capability → builder, with per-step "blocked on Clio" status. |
| Operator readiness view | `app.py` route `/operator/clio-accounting` + `templates/operator-clio-accounting.html` | Operator-gated, `noindex`, shows capability matrix, adapter dry-run config, and both lane plans. Linked only from the operator dashboard. |
| Tests | `tests/smoke_clio_api_foundation.py` | T1–T8 (see below). |

### Status vocabulary (capability registry)
`unavailable` < `production_pending` < `staging_expected` < `read_only` <
`write_supported` < `feature_flag_disabled`. The last one is a **Cutovr-side**
gate: Clio supports the write, but our feature flag keeps it OFF by default.

### Feature flags / env (no secrets needed to run tests)
- `CLIO_ACCOUNTING_API_ENABLED` — truthy permits live mode (still allow-listed).
- `CLIO_ACCOUNTING_API_BASE_URL` — API base URL (unset ⇒ dry-run).
- `CLIO_ACCOUNTING_API_TOKEN` — bearer/OAuth access-token placeholder (never logged/displayed).

Even with all three set, `_perform_live()` currently **fails closed** (returns a
`blocked` result) because the real HTTP client is intentionally not built until
docs exist. Nothing can post to Clio today.

## What is intentionally stubbed / disabled

- **No live HTTP calls.** `ClioAccountingAdapter._perform_live` is a placeholder.
- **No public exposure.** No landing cards, intake selector, or sitemap entries.
  The only new route is operator-gated `/operator/clio-accounting`.
- **No change to PC Law → QuickBooks Online.** The default/NULL lane still posts
  to QBO; Clio lanes remain fail-closed against QBO posting (PR #122 gate).
- **Assumed schema.** Every payload field name and capability status is an
  assumption from the roadmap, marked with `TODO(clio-docs)` and
  `assumed_schema: True` / `docs_published: False`.

## Safety gates (verified by tests)

- Direct Clio posting is feature-flag disabled by default; writes return
  structured `blocked` results.
- PC Law → QBO default/NULL lanes behave exactly as before.
- Clio lanes cannot trigger QBO posting routes.
- Public landing / intake / sitemap / robots carry zero Clio Accounting API exposure.

## What we need from Clio when docs go live

1. Official developer-portal docs / OpenAPI schema.
2. Auth model + OAuth scopes.
3. Base URL / environment details.
4. Idempotency header name + key format (placeholder: `Idempotency-Key`).
5. Required fields + validation rules for ledger accounts, journal entries,
   vendor bills, vendor payments, expenses, reports.
6. Rate limits + pagination/filtering details (assumed page max 200).

## How to upgrade when access opens

1. Update statuses/notes/field names in `clio_accounting_capabilities.py` and
   `clio_accounting_payloads.py` from the published OpenAPI (search `TODO(clio-docs)`).
2. Implement `ClioAccountingAdapter._perform_live` with a real HTTP client
   (auth header + idempotency header + response contract validation).
3. Set the env flags in the target environment; the operator view will flip to
   `mode: live` and per-step statuses update automatically.
4. Wire lane steps (`clio_accounting_lanes.py`) into the readiness workflow to
   drive real dry-run → live migrations.

## Tests

```
python3 tests/smoke_clio_api_foundation.py     # T1–T8, this foundation
python3 tests/smoke_clio_readiness.py          # PR #122 readiness lanes (regression)
```
