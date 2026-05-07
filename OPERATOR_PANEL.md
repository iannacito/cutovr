# Operator / Admin Panel

A read-only, cross-firm operations view at `/operator` for app operators
(us, the people running the service) — *not* for the firms' own admins.

## Why a dedicated gate (and not just "role = admin")

Every firm's signup creates a user with `role = 'admin'` for that firm.
That column expresses "this user is an admin **of their firm**", not
"this user is a global app operator". Using `role` to gate cross-firm
data would expose every customer's data to every other customer's first
signup. The panel is therefore gated by an explicit allowlist of
operator email addresses configured at the deployment level.

## Two environment variables

| Variable | Purpose | Default |
|---|---|---|
| `OPERATOR_EMAILS` | Comma-separated allowlist of emails permitted to access the panel. Case- and whitespace-insensitive. | unset (panel fully disabled) |
| `SHOW_OPERATOR_TOOLS` | Optional kill switch. Set to `0` to force-disable the panel even when `OPERATOR_EMAILS` is populated. | enabled when `OPERATOR_EMAILS` is set |

If `OPERATOR_EMAILS` is empty/unset, the panel is completely hidden:

- The `Operator` link is removed from the top nav.
- `GET /operator` and `GET /operator/firm/<id>` return 404 for every
  user, including the very first signup. This avoids the trap where a
  misconfigured deploy silently exposes the panel to any logged-in user.

A logged-in user whose email is not in the allowlist also gets 404
(rather than 403) so we don't reveal that the panel exists.

## Render setup

In your Render service's **Environment** tab, add:

```
OPERATOR_EMAILS = you@yourcompany.com, ops@yourcompany.com
```

Optional kill switch (omit normally, set during incident response):

```
SHOW_OPERATOR_TOOLS = 0
```

Redeploy. The change takes effect on the next process start because the
allowlist is read each request via `os.environ.get`, so a redeploy is
sufficient — no code change needed to add or remove an operator.

## What the panel shows

`GET /operator`

- **Deploy header:** `APP_ENV`, `QBO_ENVIRONMENT`, whether real QBO writes
  are enabled (`QBO_REAL_IMPORT`), and the size of the operator allowlist.
  No actual secrets, tokens, or credentials are rendered.
- **High-level metrics:** total firms, total users, total jobs, total
  imports attempted, successful imports, failed/blocked imports,
  reversed imports, QBO connection rows, distinct QBO realms, login
  failures in the last 7 days, OAuth/token-refresh failures in the last
  7 days.
- **Firms table:** one row per firm with firm name, created date, the
  oldest user's email (the conventional firm admin), user count, job
  count, QBO connectivity (yes/no + realm count), last import timestamp
  and status, total successful/failed imports for the firm, and a
  truncated last-error note. Each row links to the per-firm detail
  view.
- **Recent imports:** last 25 import attempts across all firms, with
  status, transaction count, and a 200-char truncated note.
- **Recent errors / auth failures:** last 25 audit-log entries for the
  set of failure-shaped actions (`login_failed`,
  `oauth_callback_firm_mismatch`, `oauth_failed`,
  `qbo_token_refresh_failed`, `import_failed`, `import_blocked`,
  `import_reversal_blocked`, `delete_job_failed`).

`GET /operator/firm/<firm_id>`

- Firm metadata.
- Users (id, email, role, created — never the password hash).
- QuickBooks connections: realm id, company name, country, expiry,
  connect/update timestamps, last `company_info_error` hint. **Never**
  shows the encrypted access/refresh token blobs.
- Jobs: id, company, source filename, status, QBO-connected flag,
  timestamps. Encrypted file paths and JSON blobs are not rendered.
- Imports for this firm: identical shape to the cross-firm recent
  imports list, with a Reversal column.
- Recent audit log scoped to this firm.

## What the panel does NOT do (v1)

- **No write actions.** Operators cannot trigger imports, reversals,
  disconnects, or deletions through this UI. Those mutations remain
  behind the firm-scoped routes and require a logged-in firm member.
- **No secret rendering.** The aggregation queries explicitly avoid
  selecting `access_token_enc` / `refresh_token_enc`, and the templates
  do not surface them either. The `tests/smoke_operator_panel.py` T5
  test asserts that injected sentinel values for `SECRET_KEY`,
  `ENCRYPTION_KEY`, and `QBO_CLIENT_SECRET` never appear in any
  rendered operator page.
- **No OAuth code/state echoing.** Audit-log details are truncated to
  200 characters and we already strip OAuth `code` from logs at the
  source (see `app.py` token exchange paths and `qbo_error_hint`).

## Files

- `operator_panel.py` — gating helpers (`is_operator_user`,
  `operator_panel_enabled`) and aggregation queries
  (`collect_metrics`, `list_firms_overview`, `recent_imports`,
  `recent_errors`, `firm_detail`).
- `app.py` — `operator_required` decorator, `/operator` and
  `/operator/firm/<int:firm_id>` routes, and the `is_operator` template
  context flag used by `_base.html` to show/hide the nav link.
- `templates/operator-dashboard.html`, `templates/operator-firm.html`.
- `tests/smoke_operator_panel.py` — access-control + secret-leakage
  smoke tests.

## Smoke test

```
python3 tests/smoke_operator_panel.py
```

Covers: panel disabled when `OPERATOR_EMAILS` is unset, denial for
logged-in non-operator users, allowed access for an allowlisted user,
nav-link visibility flipping with the gate, the
`SHOW_OPERATOR_TOOLS=0` kill switch, and assertion that no rendered
operator page leaks `SECRET_KEY`, `ENCRYPTION_KEY`, or
`QBO_CLIENT_SECRET` values that were intentionally injected as
sentinels.
