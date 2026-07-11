# cutovr — Codebase Context

**This is the ACTIVE codebase.** Flask app live at cutovr.com. Push to `main` auto-deploys to Render (~2 min). Shared repo — iannacito pushes hotfixes and SEO PRs directly to origin/main; always `git fetch origin && git log HEAD..origin/main --oneline` before editing.

**The brain layer** (rules, session state, patches, planning) lives at
`C:\Users\atala\Desktop\SoloBuilder\Projects\legal-migrator-pro\`:
`CLAUDE.md` (rules + routing table) · `SESSION.md` (current state, pending prompts, Port Checklist) · `COMMANDS.md` · `docs/Errors-and-Lessons.md` + `docs/fixes-and-discoveries.md` (living bug record) · `version/patches/` (Claude Code prompts) · `CODEMAPPING.md` (LEGACY migrator↔cutovr map — port items only) · `migrator/` (FROZEN archive — never copy from it).

*Accurate as of commit `00226a4` (2026-07-07). Line numbers are approximate — grep before editing.*

---

## Layout (flat — no backend/ or frontend/ split)

```
cutovr/
├── app.py               ← ALL Flask routes (~11,000+ lines) — grep, don't scroll
├── <module>.py          ← service modules at repo root (see map below)
├── templates/           ← Jinja2 (kebab-case .html)
├── static/              ← style.css, JS, favicon
├── tests/               ← smoke_*.py suite
├── requirements.txt     ← travels with every app.py change
├── render.yaml          ← Render deploy config + env block
├── .env                 ← single env file at root (needs real Fernet ENCRYPTION_KEY;
│                           a placeholder silently breaks all uploads)
└── *.md                 ← feature notes (DEPLOYMENT, OPERATOR_PANEL, STRIPE_SETUP,
                            NEXT_STEPS, SECURITY_*, etc.)
```

Flask init uses `parent / "templates"` and `parent / "static"` — flat paths. (The frozen migrator archive used `parent.parent / "frontend" / …`; that path crashes here.)

---

## Module Map (feature → file)

| Feature | File(s) |
|---------|---------|
| Routes, auth, stepper context, nexus | `app.py` |
| QBO REST client (batch 25, backoff, minorversion) | `qbo_client.py` |
| QBO OAuth 2.0 | `qbo_auth.py` |
| QBO error → user hint | `qbo_error_hint.py` |
| PC Law CSV parsing / column synonyms | `pclaw_parser.py` |
| GL pipeline, JE building, DocNumbers, CER pairing | `pclaw_pipeline.py` |
| Report type detection + parsers (GL, COA, TB, Trust, V/C) | `report_types.py` |
| GL transaction grouping + auto-balance | `gl_grouping.py` |
| GL row quality / droppable rows | `gl_row_quality.py` |
| COA create plan + apply (camelCase `_TYPE_TABLE`) | `coa_apply.py`, `coa_hierarchy.py` |
| Opening balance JEs | `opening_balance.py` |
| Pre-COA readiness gate for nexus (`is_ready_to_import`) | `precoa.py` |
| Pre-GL vendor/customer auto-push (Step 5 card) | `initialpost.py` (has module-level `_log`) |
| Preflight validation / import gate | `preflight.py`, `migration_quality.py` |
| TB reconciliation / TB↔COA validation | `tb_reconciliation.py`, `tb_coa_validation.py` |
| Trust reconciliation (posting intentionally disabled) | `trust_reconciliation.py` |
| AR/AP single-account strategy | `ar_ap_strategy.py` |
| 6-stage customer stepper (stages, CTAs, nav URLs) | `customer_workflow.py` |
| Cutover setup (Step 1) | `cutover_workflow.py` |
| Unmapped-account guidance | `unmapped_account_guidance.py` |
| SQLite schema + helpers (firms, users, jobs, qbo_connections, audit_logs) | `app_db.py` |
| Import history + dedup | `import_history.py` |
| Long-running job state | `job_checkpoints.py` (never Flask session) |
| Fernet encryption (files + tokens) | `encryption.py` |
| CSV decode / safety | `csv_decode.py`, `csv_safety.py` |
| Bulk upload (nexus multi-file) | `bulk_upload.py` |
| PDF final report | `final_report.py` (`ReconcileSummary` dataclass — no dict assignment) |
| Auth rate limiting | `rate_limit.py` |
| Operator panel | `operator_panel.py` + `templates/operator-*.html` |
| Stripe / email / AI support / demo / branding | `stripe_checkout.py`, `email_sender.py`, `support_assistant.py`, `demo_mode.py`, `branding.py` |

---

## Route Map (key routes in app.py, ~line as of 00226a4)

**Customer 6-step flow** (stages defined in `customer_workflow.py`):

| Step | Route | Notes |
|------|-------|-------|
| 1 Setup | `/cutover` | `cutover_setup` |
| 2 Upload | `/dashboard#intake`, `/upload/bulk` | bulk = nexus multi-file path |
| 3 Match | `/jobs/<job_id>/account-mapping` (~8886) | + add-account / create-missing / undo / refresh sub-routes |
| 4 Review | `/jobs/<job_id>/preview-import` | dry-run preview |
| 5 Send | `/jobs/<job_id>/send-to-qbo` (~2097, job-scoped) | flat `/send-to-qbo` = `send_to_qbo_entry` (~2062) redirects here |
| 6 Reconcile | `/reconcile-balances` (flat, firm-level) | + `/jobs/<job_id>/revert-import`, `reverse-import` |

**Migration Nexus:** `/migration-nexus` → `migration_nexus()` (~2719) — batch tracker, all report types. Sub-routes: `remove/<job_id>`, `bulk-remove`, `bulk-revert`. TB rows route to `post_ob` (OB mapping flow not yet ported — see SESSION.md Port Checklist).

**Flat entry redirect routes** (`match_accounts_entry` ~1865, `import_job_entry` ~1893, `send_to_qbo_entry` ~2062): pick a job by heuristic — "first GL job with a QBO connection", else most recent. ⚠️ Known pitfall: with multiple GL batches these can land on a previous/completed job instead of the one in context. Job-scoped links must carry `job_id`; stepper threading via `import_job_id` (see below).

**⚠️ Naming trap:** here the function is `migration_nexus()`. The frozen migrator archive kept `migration_hub()` for the same feature — any template ported from migrator must be grepped for `url_for('migration_hub')` (BuildError → 500 here).

---

## Stepper Architecture

- `_workflow_stepper_context(firm_id, force_current_stage, review_blocker, review_job_id, on_match_page, import_job_id)` (~11169) — builds the 6-stage rail; spread into `render_template()`.
- Stage CTAs/nav built in `customer_workflow.py`: `_stage_cta`, `_stage_back_link`, `_stage_nav_url`, `build_customer_stages`. `import_job_id` makes Step 5 links job-scoped; stages without a threaded job id fall back to the flat entry routes (heuristic job selection — see pitfall above).
- The checklist is FIRM-scoped (`_build_firm_checklist`) — a completed batch advances the firm state. Per-job pages should pass `force_current_stage` derived from the job's own checkpoint to avoid another job's state leaking in.
- `_review_blocker_kind()` (~11140) → `ready | unmatched | blocked_txns | beginning_balance | row_quality | preview_error | unbalanced` — drives Step 4 badge + CTA.

## QBO Connections

- Stored PER JOB in `qbo_connections` (encrypted tokens). Firm-level inheritance (copy newest firm connection onto a job that has none) currently runs in exactly two places: `account_mapping()` (~8888) and `_init_push_entities()` (~6067). Routes without inheritance will bounce users to `connect_qbo` per job — known gap for Step 5 with fresh uploads.
- `db.list_account_mappings(firm_id, realm_id)` — 2 args only, NO `source_type` kwarg.
- `_log = logging.getLogger("app")` exists at module level in app.py (since `b6a7399`).

---

## Hard Rules (full set: legal-migrator-pro/CLAUDE.md)

- Verify with `QBO_REAL_IMPORT=0`, `QBO_ENVIRONMENT=sandbox` before any push. Push = deploy.
- QBO: batch ≤25 ops, `?minorversion=75+`, camelCase AccountType enums, `round(x, 2)` for balance checks, DocNumber `^\d{1,21}$`.
- `templates/migration-hub.html` is iannacito's — never overwrite. `review-nexus.html` (migrator-only dev tool) must never be committed here.
- Update `legal-migrator-pro/docs/fixes-and-discoveries.md` + `SESSION.md` after every fix session.
