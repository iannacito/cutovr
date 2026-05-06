# Go-Live Readiness

Cutover ships with a built-in readiness checklist that confirms the deploy is
configured for an Intuit QuickBooks Online production review. The checklist
runs entirely server-side from environment variables &mdash; **no secret
values are ever exposed**, only booleans and operator-facing prompts.

## Surfaces

| Surface | Auth | Purpose |
| --- | --- | --- |
| `GET /healthz` | Public | Lightweight JSON probe with non-secret booleans (`secret_key_set`, `encryption_key_set`, &hellip;), the per-check map, and `ready_for_production`. Safe to point Render's health probe at. |
| `GET /admin/readiness` | Logged in | Full HTML checklist with per-check labels, severity, and operator hints (e.g. "Set QBO_REDIRECT_URI to the live HTTPS callback URL"). Reachable from the dashboard's *Workspace* card. |
| `readiness.collect_checks()` | n/a | Pure function used by both surfaces; new checks should be added here. |

Neither surface ever renders the value of a SECRET, only whether it is
present and well-formed.

## Required environment variables on Render

Configure these in **Render &rarr; your service &rarr; Environment**. After
saving, click *Manual Deploy &rarr; Clear build cache & deploy* so the new
values take effect. The readiness page reflects the running container's env,
so refresh it after each deploy to confirm.

### Core hardening (required for production submission)

| Var | Required | Notes |
| --- | --- | --- |
| `APP_ENV` | yes | Set to `production`. Enables HTTPS-only cookies and the production env validator. |
| `SECRET_KEY` | yes | Flask session secret. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. Must be at least 32 chars. |
| `ENCRYPTION_KEY` | yes | Fernet key used to encrypt uploaded ledgers and stored QBO tokens. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `APP_DB` | yes | Path to the SQLite app database on the Render persistent disk (e.g. `/var/data/app.sqlite3`). |
| `IMPORT_HISTORY_DB` | yes | Path to the import-history SQLite DB on the Render disk (e.g. `/var/data/import_history.sqlite3`). |

### QuickBooks Online OAuth (required)

| Var | Required | Notes |
| --- | --- | --- |
| `QBO_CLIENT_ID` | yes | From the **production** keys tab of your Intuit app. Sandbox keys will not pass production review. |
| `QBO_CLIENT_SECRET` | yes | Production client secret. |
| `QBO_REDIRECT_URI` | yes | Must match exactly what is registered in Intuit **AND** be `https://<your-domain>/oauth/callback`. The readiness page rejects `http://` and `localhost` values when `APP_ENV=production`. |
| `QBO_ENVIRONMENT` | yes | `production` to hit the live Intuit API; `sandbox` for pre-launch testing. |
| `QBO_REAL_IMPORT` | recommended | Set to `1` to actually post journal entries instead of running in demo mode. Until this is on, the **Import to QuickBooks** button only simulates. |

### Operator contacts (required for Intuit review)

| Var | Required | Notes |
| --- | --- | --- |
| `SUPPORT_EMAIL` | yes | Customer-facing support inbox. Must be monitored. Surfaced in the public `/support` page and footer. |
| `SECURITY_EMAIL` | yes | Vulnerability-reporting inbox. Surfaced in `/support`. |
| `PRIVACY_CONTACT_EMAIL` | recommended | Privacy / data-subject inbox. Defaults to `SUPPORT_EMAIL` if unset. |

### Branding & domain

| Var | Required | Notes |
| --- | --- | --- |
| `APP_NAME`, `COMPANY_NAME` | optional | Default to "Cutover". Override to white-label. |
| `PUBLIC_APP_URL` | recommended | The canonical public URL (e.g. `https://www.pclawmigrate.com`). The readiness page uses this to confirm the custom domain is wired up. |

## Mapping to the Intuit production review checklist

Intuit's production submission asks the integrator to confirm a small set of
things. Each row below maps directly to a readiness check the page surfaces:

| Intuit requirement | Readiness check | Source of truth |
| --- | --- | --- |
| OAuth secret stored securely | `secret_key`, `encryption_key`, `qbo_client_secret` | `SECRET_KEY`, `ENCRYPTION_KEY`, `QBO_CLIENT_SECRET` envs |
| HTTPS redirect URI registered | `qbo_redirect_uri` | `QBO_REDIRECT_URI` (must be `https://`) |
| Production endpoints, not sandbox | `qbo_real_import`, `app_env_production` | `QBO_REAL_IMPORT`, `APP_ENV=production` |
| Visible support contact | `support_email` | `SUPPORT_EMAIL` (monitored mailbox) |
| Visible security contact | `security_email` | `SECURITY_EMAIL` (monitored mailbox) |
| Public privacy / terms pages | n/a (already wired) | `/privacy`, `/terms` routes |
| Custom domain (no auto-generated host) | `custom_domain` | `PUBLIC_APP_URL` and current request host |

When **all required checks are green** on `/admin/readiness` AND the
`/privacy`, `/terms`, `/support` pages render correct copy on the live
custom domain, the deploy is ready to submit for Intuit production review.

## Programmatic / monitoring usage

The `/healthz` body includes:

```json
{
  "status": "ok",
  "ready_for_production": true,
  "required_passed": 9,
  "required_total": 9,
  "checks": {
    "app_env_production": true,
    "secret_key": true,
    "encryption_key": true,
    "qbo_client_id": true,
    "qbo_client_secret": true,
    "qbo_redirect_uri": true,
    "support_email": true,
    "security_email": true,
    "health_endpoint": true,
    "qbo_real_import": true,
    "privacy_contact_email": true,
    "custom_domain": true
  }
}
```

The `checks` map is stable: keys are the same as on `/admin/readiness`, and
the values are pure booleans &mdash; safe to consume from an external
monitor (Render health probe, Pingdom, etc.) without leaking config detail.
