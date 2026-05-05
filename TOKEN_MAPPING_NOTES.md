# Token Refresh + Account Mapping

This sprint closes two of the limitations called out in the persistence
sprint: expired QBO access tokens are now refreshed automatically, and
firms can now configure their PCLaw → QuickBooks account mappings from
a UI instead of relying on number/name auto-match.

## Token refresh — how it works

1. The OAuth callback already stored both the access token and the
   refresh token (Fernet-encrypted) plus an `expires_at` timestamp.
2. Every code path that needs to call QBO (import, verify, mapping page)
   now goes through `_get_qbo_client(job_id, user)` in `app.py`.
3. That helper checks `_qbo_token_is_fresh(...)`: if the access token is
   missing or expires within the next **5 minutes**, it calls
   `qbo_auth.refresh_access_token(...)` using the stored refresh token.
4. Intuit's refresh endpoint rotates the refresh token on every call and
   invalidates the previous one, so we save **both** the new access and
   refresh tokens (encrypted) back to `qbo_connections`.
5. Each successful refresh logs a `qbo_token_refreshed` audit event.
6. If the refresh fails (e.g. the refresh token itself expired — Intuit
   currently sets that to 100 days), the user sees:
   > QuickBooks connection expired. Please reconnect.
   and a `qbo_token_refresh_failed` audit row is recorded. They click
   **Disconnect QuickBooks** then **Connect to QuickBooks** to re-OAuth.

There is no "background refresh" — refreshes happen lazily, immediately
before any QBO call. That keeps the design simple and means a long-idle
session doesn't proactively talk to Intuit.

## Account mapping — how it works

A new table `account_mappings` stores the mapping per firm + QBO realm:

```
firm_id, realm_id, pclaw_account_number, pclaw_account_name,
qbo_account_id, qbo_account_name, qbo_account_type,
created_at, updated_at
UNIQUE (firm_id, realm_id, pclaw_account_number, pclaw_account_name)
```

### From the user's point of view

1. On the job detail page, a new **Map accounts** button is always
   visible once a QBO connection exists.
2. If the import is blocked because some accounts couldn't be matched,
   a red banner appears at the top of the job detail page listing the
   PCLaw accounts that have no QBO match, with a direct link to the
   mapping page. The flash message also tells the user where to click.
3. The mapping page lists each unique PCLaw account from the uploaded
   CSV (one row per `(account_number, account_name)`). For each row
   there's:
   - the PCLaw account info on the left,
   - a `<select>` populated with every QBO account in the connected
     company, sorted by `AccountType` then `Name`,
   - a status badge: **Saved** (already in `account_mappings`),
     **Auto-match** (would be matched by AcctNum or Name today), or
     **Unmapped** (red).
4. The user changes any dropdowns, clicks **Save mappings**, and a flash
   confirms how many rows were saved.
5. Going back to the job and clicking **Import to QuickBooks** uses the
   saved mappings as the first-priority lookup.

### From the import flow's point of view

In `import_to_qbo` the lookup is now:

```
mapping = auto-match by AcctNum  (or by Name if no AcctNum exists)
overlay user-saved mappings on top
```

User-saved entries always win. After this overlay, the existing
`find_unmapped_accounts(...)` still runs and short-circuits the import
if anything is unmapped. The unmapped list is also stashed on the job
(`unmapped_accounts`) so the detail page can render the link banner.

## Files added

- `tests/smoke_token_mapping.py` — 6-case smoke suite (T1–T6 below).
- `templates/account-mapping.html` — the mapping UI.
- `TOKEN_MAPPING_NOTES.md` — this file.

## Files changed

- **`app_db.py`** —
  new `account_mappings` table; new `unmapped_accounts_json` column on
  `jobs` (added via `_migrate(...)`); new helpers
  `list_account_mappings`, `save_account_mapping`,
  `delete_account_mapping`. `save_job_state` and `hydrate_job` learned
  about `unmapped_accounts`.
- **`app.py`** —
  `_qbo_token_is_fresh(...)`, `_refresh_qbo_tokens(...)`,
  `_get_qbo_client(...)`, `QBOAuthExpired`. The `import_to_qbo` and
  `verify_import` routes now go through `_get_qbo_client(...)` and
  surface `QBOAuthExpired` as a friendly flash. Saved mappings are
  overlaid on the auto-match before checking for unmapped accounts.
  Successful import clears `unmapped_accounts`. New
  `/jobs/<id>/account-mapping` route (GET + POST).
- **`templates/job-detail.html`** —
  red banner at top when `job.unmapped_accounts` is set; new
  **Map accounts** button under the QBO connection panel.

## Tests run (all 29 still pass)

```
python3 -m py_compile app.py qbo_auth.py qbo_client.py pclaw_parser.py \
    pclaw_pipeline.py encryption.py import_history.py app_db.py
# OK

python3 tests/smoke_auth.py            # 8/8
python3 tests/smoke_phase2.py          # 8/8
python3 tests/smoke_persistence.py     # 7/7
python3 tests/smoke_token_mapping.py   # 6/6 (new)
```

`smoke_token_mapping.py` covers:

| # | What |
| - | - |
| T1 | Stored access token marked expired in the DB → refresh helper exchanges it, stores rotated tokens (encrypted), import succeeds, audit row `qbo_token_refreshed` written. |
| T2 | Refresh raises → user sees "QuickBooks connection expired. Please reconnect.", `qbo_token_refresh_failed` audit row written. |
| T3 | Mapping page lists every unique PCLaw account, an Auto-match badge appears for the rows with a matching AcctNum, posting the form saves the rows to `account_mappings`. |
| T4 | An override mapping (PCLaw `1000` → QBO `A12` instead of the auto-matched `A11`) is honored on the next import — the JE payload uses `A12`. |
| T5 | When QBO has no matching accounts, the import is blocked, **no JEs are posted**, `job.unmapped_accounts` is populated, and the job detail page surfaces the link to the mapping page. |
| T6 | A second firm gets 404 on the first firm's mapping route. |

## Limitations still open

- The mapping page only renders for the rich PCLaw GL format (the CSV
  with a `transaction_id` column). The legacy flat CSV doesn't have
  enough information to populate the table; the page redirects with an
  info flash.
- One mapping table is shared per firm + realm. Two different jobs in
  the same QBO company use the same mappings (this is the right
  behavior, but worth knowing).
- Refresh behavior assumes Intuit's standard sandbox/production token
  endpoint is reachable. The refresh request itself isn't retried; a
  transient 5xx will look like an expired connection until the user
  retries.
- No "delete a single saved mapping" UI yet. Helper exists in
  `app_db.delete_account_mapping(...)`; route + button are an obvious
  small follow-up.

## Run / install

```bash
cd ~/Desktop/pclaw-qbo-v2
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

# For real QBO writes:
export QBO_CLIENT_ID="<sandbox client id>"
export QBO_CLIENT_SECRET="<sandbox client secret>"
export QBO_REDIRECT_URI="http://localhost:5000/oauth/callback"
export QBO_REAL_IMPORT=1

python3 app.py        # http://localhost:5000

# Offline tests:
python3 tests/smoke_auth.py
python3 tests/smoke_phase2.py
python3 tests/smoke_persistence.py
python3 tests/smoke_token_mapping.py
```

In the browser, the new flow is:

1. Sign up + upload + Connect to QuickBooks (unchanged).
2. Click **Map accounts** on the job page (or click the link in the red
   banner if the import was blocked).
3. Pick the QBO account for each PCLaw row from the dropdown.
4. Click **Save mappings**.
5. Click **Import to QuickBooks**.
6. If your access token had expired, you'll see the import succeed
   anyway — it was refreshed transparently. Check Recent activity on
   the dashboard to see the `qbo_token_refreshed` audit row.
