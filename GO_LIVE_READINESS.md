# Go-Live Readiness

Cutover ships a self-checking readiness layer so you can confirm that a
Render deploy is fully configured for real customer use **before**
flipping the switch with Intuit.

## Where to look

| Surface | Path | Visibility | Use case |
| --- | --- | --- | --- |
| JSON probe | `GET /healthz` | Public, no auth | Render health checks, ops dashboards, uptime monitors. Booleans only — never returns secret values. |
| Operator checklist | `GET /readiness` | Login required | Human-readable cards with remediation hints. Same data as `/healthz` plus what to do for each red item. |

Both surfaces are driven by the same source of truth: `readiness.py` →
`collect_checks()`. Adding or renaming a check updates both at once.

## Checks performed

Required (blocks go-live):

- `app_env_production` — `APP_ENV=production`. Enables Secure cookies and
  the strict env validator at startup.
- `secret_key_set` — `SECRET_KEY` is at least 32 chars.
- `encryption_key_set` — `ENCRYPTION_KEY` is a valid Fernet key.
- `qbo_client_id_set` — `QBO_CLIENT_ID` is set and not the placeholder.
- `qbo_client_secret_set` — `QBO_CLIENT_SECRET` is set and not the placeholder.
- `qbo_redirect_uri_https` — `QBO_REDIRECT_URI` is a public `https://` URL
  (not `localhost`, not `http://`).
- `support_email_set` — `SUPPORT_EMAIL` is not the `@your-domain.example`
  default. Intuit's reviewers will email this address.
- `security_email_set` — `SECURITY_EMAIL` is not the placeholder. Required
  on the Intuit security questionnaire.

Recommended (strongly suggested before launch):

- `qbo_real_import_enabled` — `QBO_REAL_IMPORT=1`. Demo mode is fine for
  staging but will not let real customers post journal entries.
- `privacy_contact_email_set` — `PRIVACY_CONTACT_EMAIL` set (or
  `SUPPORT_EMAIL` is good enough — falls back automatically).
- `custom_domain_present` — request host is not `*.onrender.com`, **or**
  `PUBLIC_APP_URL` is set. Intuit prefers a stable customer-facing domain.

Informational (not a blocker):

- `health_endpoint_ok` — confirms this module loaded and `/healthz` is
  reachable. By definition true if you can read the response.

## Render env vars (recap)

Set these in **Settings → Environment** for the `pclaw-qbo` service. Most
of them are also declared in `render.yaml`:

| Var | Required | What it does |
| --- | --- | --- |
| `APP_ENV` | yes | Set to `production`. Triggers Secure cookies + strict validation. |
| `SECRET_KEY` | yes | Flask session signing key. Render can `generateValue: true`. |
| `ENCRYPTION_KEY` | yes | Fernet key used to encrypt uploaded files and stored OAuth tokens. Render can `generateValue: true`. |
| `QBO_CLIENT_ID` | yes | Production Client ID from the Intuit developer portal. |
| `QBO_CLIENT_SECRET` | yes | Production Client Secret. Sync disabled in `render.yaml`; set in dashboard only. |
| `QBO_REDIRECT_URI` | yes | Public HTTPS callback. For pclawmigrate, `https://www.pclawmigrate.com/oauth/callback`. Must match the URL Intuit has on file. |
| `QBO_ENVIRONMENT` | yes | `sandbox` or `production`. |
| `QBO_REAL_IMPORT` | recommended | `1` to enable real journal posting. `0` keeps demo behavior. |
| `SUPPORT_EMAIL` | yes | Real, monitored mailbox. Shown on `/support`. |
| `SECURITY_EMAIL` | yes | Real, monitored mailbox for vulnerability disclosure. Shown on `/support`. |
| `PRIVACY_CONTACT_EMAIL` | recommended | Optional override; falls back to `SUPPORT_EMAIL` on `/privacy`. |
| `PUBLIC_APP_URL` | recommended | Canonical public URL (e.g. `https://www.pclawmigrate.com`). Used for the custom-domain readiness check and for documentation links. |
| `APP_DB`, `IMPORT_HISTORY_DB` | yes | Paths on the persistent Render disk. Defaults in `render.yaml` point at `/var/data/...`. |

Never paste secrets into source files, logs, or PR descriptions. The
readiness page and `/healthz` only ever return booleans for these
values.

## Custom domain (pclawmigrate.com)

Until DNS for `www.pclawmigrate.com` cuts over to Render, the readiness
page will mark `custom_domain_present` as TODO when you visit it through
the `pclaw-qbo-v2.onrender.com` host. Two ways to clear it:

1. Finish the DNS cutover and use the custom domain. The check infers
   custom-domain presence from the request host.
2. Set `PUBLIC_APP_URL=https://www.pclawmigrate.com` in Render even
   before DNS cuts over. The check treats a configured public URL as
   sufficient evidence that the operator has chosen a canonical domain.

## How this supports Intuit go-live readiness

Intuit's production-app review asks for evidence of:

- A public, HTTPS, **stable** redirect URI — covered by
  `qbo_redirect_uri_https` and `custom_domain_present`.
- A working support contact and a way to report security issues —
  covered by `support_email_set` and `security_email_set`. The
  `/support` page renders these addresses.
- A privacy / terms surface — `PRIVACY_CONTACT_EMAIL` (or
  `SUPPORT_EMAIL` fallback) ensures `/privacy` lists a real mailbox.
- Production wiring of secrets, not demo placeholders — covered by
  `secret_key_set`, `encryption_key_set`, `qbo_client_id_set`,
  `qbo_client_secret_set`.
- Ability to actually post to QuickBooks for live customers —
  `qbo_real_import_enabled` flips off demo mode.

When `/readiness` shows "Ready for go-live" with no required failures,
you can submit the Intuit production application with confidence that
the deploy will not bounce on a missing-config issue.

## Testing locally

```
APP_ENV=local CSRF_DISABLE=1 python3 tests/smoke_health.py
APP_ENV=local CSRF_DISABLE=1 python3 tests/smoke_readiness.py
```

The readiness smoke test never sends a real value into the response; it
only asserts presence/absence behavior.
