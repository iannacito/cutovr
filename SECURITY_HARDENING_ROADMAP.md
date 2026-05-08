# Security Hardening Roadmap

PCLaw Migrate handles **law-firm financial data** and writes JournalEntry
records into customer QuickBooks Online companies. The threat model is
therefore closer to a small fintech than a generic SaaS: an attacker who
can post or alter journal entries can hide fraud or manufacture audit
findings.

This roadmap is **opinionated and incremental**. Items are grouped by
status:

* ✅ **Done** — already implemented.
* 🟡 **Quick win** — implementable in <1 day, low risk; prioritized next.
* 🟠 **Hardening** — requires code, ops, or product work; weeks-scale.
* 🔴 **Compliance posture** — multi-quarter, requires governance.

> ⚠️ This document does **not** claim SOC 2 readiness. SOC 2 Type 2
> requires 6+ months of evidence collection across the controls below
> and a third-party audit. Items here are the *technical preconditions*
> for that audit, not the audit itself.

---

## Authentication & access

| Item | Status | Notes |
| --- | --- | --- |
| Email + password login over HTTPS | ✅ | `werkzeug.security` PBKDF2 hashing in `app_db.py`. |
| CSRF on every state-changing route | ✅ | Per-session token, redirects with friendly flash on mismatch. |
| Session cookie `HttpOnly` + `SameSite=Lax` + `Secure` (prod) | ✅ | `app.py` `app.config.update(...)`. |
| Bounded session lifetime (12h default, `SESSION_HOURS` env) | ✅ | Added in this branch. Tunable per deploy. |
| Operator panel gated by `OPERATOR_EMAILS` allowlist | ✅ | Cross-firm view is read-only and env-gated. |
| Per-firm tenancy enforcement on all job/import routes | ✅ | `_job_or_403` returns 404 on cross-firm access (no existence leak). |
| Password reset (email magic link) | 🟡 | Today users can lose access if they forget the password. Add a signed token + expiry; reuse `SUPPORT_EMAIL` SMTP path. |
| MFA / TOTP for admin + operator accounts | 🟠 | Use `pyotp` + a recovery-code list. Roll out to operators first, then enforce for all customers via firm-level setting. |
| Login throttling / lockout | 🟡 | Add a per-IP and per-account counter (e.g. `flask-limiter` or a small in-DB counter); 10 failures / 15 min → 1 hour lockout. |
| Forced password rotation on role change | 🟠 | Lower priority once MFA exists. |
| SSO (OIDC) for enterprise firms | 🔴 | Customer-driven; defer until a paying firm requests it. |

## Network & transport

| Item | Status | Notes |
| --- | --- | --- |
| HTTPS termination at Render | ✅ | Verified by `/readiness` checks. |
| HSTS in production | ✅ | Added in this branch (`max-age=31536000; includeSubDomains`). |
| Strict-Transport-Security preload | 🟡 | After 30 days of stable HSTS, submit to <https://hstspreload.org>. |
| `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy` | ✅ | Added in this branch. |
| `Permissions-Policy` disables sensors | ✅ | Added in this branch. |
| Content-Security-Policy (nonce-based) | 🟠 | Currently no CSP because of inline `style="..."` attributes in some templates. Audit, replace inline styles with classes, then ship a strict CSP. |
| Subresource Integrity for Google Fonts | 🟡 | Replace the fonts CDN link with `integrity="sha384-..."` or self-host the WOFF2 files in `static/fonts/`. |

## Application input handling

| Item | Status | Notes |
| --- | --- | --- |
| `MAX_CONTENT_LENGTH=25 MB` (override via `MAX_UPLOAD_MB`) | ✅ | Added in this branch. 413 handler flashes a friendly message. |
| Upload extension allowlist (`.csv` only) | ✅ | Added in this branch. |
| Per-file SHA-256 fingerprinting + duplicate-protection | ✅ | `import_history` already blocks the same file content into the same realm. |
| Anti-malware scan on uploads (e.g. ClamAV on Render disk) | 🟠 | Today the file is encrypted at rest immediately, but it is parsed by the app first. Add a scan step before parsing. |
| Reject CSV bombs / billion-laughs / formula injection | 🟡 | Strip cells starting with `=`, `+`, `-`, `@` when round-tripping to QBO descriptions. Today our pipeline only writes numeric/date/string fields, but defense-in-depth. |
| Friendly errors instead of stack traces on every user-facing route | 🟡 | The mapping route (this branch) is the model. Audit `app.py` for the remaining `raise`s reachable from a request. |

## Data protection

| Item | Status | Notes |
| --- | --- | --- |
| `ENCRYPTION_KEY` (Fernet / AES-256) for uploads + tokens at rest | ✅ | `encryption.py`. App refuses to start in production without it. |
| Encryption-key rotation procedure | 🟠 | Today rotating the key invalidates all existing encrypted blobs. Implement a `keyring` of {key_id → key} so we can decrypt-with-old, encrypt-with-new, and re-encrypt in the background. |
| QBO refresh tokens encrypted at rest | ✅ | Stored as `*_enc` columns. |
| Encryption at the transport layer (TLS 1.2+) | ✅ | Render-managed. |
| Database-level encryption | 🟠 | SQLite on a Render disk inherits disk encryption from the host; for stronger isolation move to Postgres + `pgcrypto` or AWS RDS with KMS. |
| Backups (encrypted, off-host) | 🟠 | Render does daily disk snapshots; document the customer-data restore RTO/RPO and test it quarterly. |
| Data purge / retention policy | 🟡 | Job purge is implemented (`/jobs/<id>/delete` requires `DELETE` confirmation). Add a scheduled job that purges encrypted uploads older than `RETENTION_DAYS` (default 365) and a per-firm self-service "delete my workspace" flow. |
| Customer right-to-deletion (GDPR / PIPEDA art. 25) | 🟡 | Surface a single `Delete my firm and all data` action on the firm settings page; today the user has to delete jobs one-by-one. |

## Tokens, secrets, and dependencies

| Item | Status | Notes |
| --- | --- | --- |
| Secrets only via env vars (Render) | ✅ | No secrets in repo or logs. `INTUIT_PRODUCTION_REVIEW.md` lists every var. |
| `_validate_production_env()` fails startup on missing secrets | ✅ | App refuses to boot with placeholder values when `APP_ENV=production`. |
| Secret rotation runbook | 🟠 | Document: rotate `SECRET_KEY` (logs everyone out), rotate `ENCRYPTION_KEY` (requires re-encryption, see roadmap above), rotate Intuit `QBO_CLIENT_SECRET` (requires Intuit dashboard + zero-downtime overlap window). |
| Dependency CVE scanning | 🟡 | Add `pip-audit` to CI. The current `requirements.txt` is small enough to review by hand, but a recurring scan is cheap insurance. |
| SBOM (CycloneDX) | 🟠 | Generate per-release; required by some procurement reviews. |

## Logging, monitoring, audit

| Item | Status | Notes |
| --- | --- | --- |
| Per-firm audit log of state-changing actions | ✅ | `db.audit(...)` rows include actor, target, and details. |
| `intuit_tid` capture on every QBO error | ✅ | Surfaced to the user and stored in the audit row for support. |
| No secret leakage into logs | ✅ | Tokens are redacted; access tokens never log. Add a periodic `grep` audit. |
| Centralized log shipping (e.g. Logtail, BetterStack) | 🟡 | Render captures stdout but lacks long-term retention; ship to a log store with 90-day retention before SOC 2. |
| Per-route latency + error metrics | 🟠 | Add Prometheus-format `/metrics` (auth-gated) or send to OpenTelemetry. |
| Alerting on QBO error spikes / login-failure spikes | 🟠 | Trigger PagerDuty / email when the audit log shows >N failures in 5 minutes. |

## Operational / governance

| Item | Status | Notes |
| --- | --- | --- |
| Principle of least privilege for the Render service account | 🟡 | Today the app runs as a single Render service. Confirm it doesn't have AWS or Intuit Developer privileges beyond what's needed. |
| Background-check + 2FA enforcement on operator GitHub + Render accounts | 🟠 | Document in an internal HR / security policy. |
| Quarterly access review (who has prod access, who has Intuit Developer access) | 🟠 | Track in a shared doc; remove dormant accounts. |
| Vendor security review (Render, Intuit, Google Fonts CDN) | 🟠 | Each handles customer data or browsing metadata; record in a vendor inventory. |
| Incident-response runbook | 🟠 | Define detection → triage → customer notification (within 72h to satisfy GDPR / PIPEDA breach reporting). |
| SOC 2 Type 1 readiness assessment | 🔴 | Engage a SOC 2 advisor once the 🟠 items are mostly done. **Do not market SOC 2 readiness until an audit is in progress.** |
| SOC 2 Type 2 audit (12-month observation) | 🔴 | Customer-driven; defer until enterprise firms are paying. |

## Customer-controllable safeguards (already present)

These are already in the product and worth re-stating because they're
the strongest defenses against operator error or a compromised account:

* **Idempotency / duplicate protection** — the same file content into
  the same QBO realm is rejected; documented in `support.html`.
* **Demo mode by default** — `QBO_REAL_IMPORT` must be explicitly set
  to `1` before any real journal entry is posted.
* **Reverse-import button** — every successful real import has a
  one-click reversal that posts offsetting JEs with `REV-` doc numbers.
* **Operator panel is read-only** — even with `OPERATOR_EMAILS` set, an
  operator cannot mutate firm data; they can only view it.
* **CSRF on every form, including logout** — guards against account
  hijack via cross-site forms.
* **Per-job encryption at rest** — even a stolen disk image cannot
  produce plaintext PCLaw or QBO tokens without `ENCRYPTION_KEY`.

## Suggested order of work

1. Ship the quick wins in this branch (✅).
2. Login throttling + password reset + CSP audit (🟡 batch).
3. Anti-malware scan on uploads + key-rotation runbook (🟠 batch).
4. Centralized logging + alerting + dependency CVE scanning (🟠 batch).
5. SOC 2 Type 1 readiness assessment (🔴) — only after ~3 months of
   running the controls above.
