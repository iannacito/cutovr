import logging
import time
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import base64

from qbo_client import (
    extract_intuit_tid,
    _is_transient_status,
    _retry_after_seconds,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_BACKOFF_CAP_SECONDS,
)


_log = logging.getLogger("qbo_auth")

# Number of retries the token-refresh path takes when Intuit returns a
# transient 5xx or 429. Keep this small: a brief blip should not force
# a full re-OAuth, but a genuine outage should still fail fast.
TOKEN_REFRESH_MAX_RETRIES = 2


class QBOAuthError(Exception):
    """OAuth token exchange / refresh failure that carries the Intuit
    transaction id from the failing response, when one is present.

    The message is the upstream HTTP status string. The raw response body
    is intentionally NOT included here because Intuit's token endpoint can
    echo back fragments that look like client identifiers; callers that
    want the raw body for ops logging should pull it from the underlying
    requests.HTTPError. `intuit_tid` is safe to surface to operators and
    end-user support references — it is an opaque request id, not a token.
    """

    def __init__(self, message, status_code=None, intuit_tid=None):
        super().__init__(message)
        self.status_code = status_code
        self.intuit_tid = intuit_tid


class QBOAuthHandler:
    """
    Handles QuickBooks Online OAuth 2.0 authentication flow.
    Based on Intuit's OAuth 2.0 docs.[web:86][web:90]
    """

    SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
    PRODUCTION_BASE = "https://quickbooks.api.intuit.com"
    AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
    TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

    def __init__(self, client_id, client_secret, redirect_uri, environment="sandbox"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.environment = environment
        self.base_url = self.SANDBOX_BASE if environment == "sandbox" else self.PRODUCTION_BASE
        # The Intuit transaction id from the most recent token endpoint
        # response (success or failure). Operators reference this when
        # contacting Intuit support.
        self.last_intuit_tid = None

    def get_authorization_url(self, state=None):
        """Return the URL to send the user to for QuickBooks consent."""
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "com.intuit.quickbooks.accounting",
            "state": state or "",
        }
        return f"{self.AUTH_URL}?{urlencode(params)}"

    def _auth_header(self):
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return base64.b64encode(raw).decode()

    def _post_token(self, data, *, max_retries: int = TOKEN_REFRESH_MAX_RETRIES):
        """POST to the token endpoint with bounded retry on transient errors.

        Retries 5xx and 429 with exponential backoff (honoring Retry-After).
        This prevents a single Intuit blip during refresh from cascading
        into a forced full re-OAuth flow for the user.

        4xx other than 429 (e.g. 400 invalid_grant on a truly expired
        refresh token) fail fast — those are not transient and retrying
        would just delay surfacing the right "please reconnect" message.

        The raw body is deliberately not included in the error message
        because Intuit's token endpoint can echo fragments that resemble
        client identifiers.
        """
        headers = {
            "Authorization": f"Basic {self._auth_header()}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        attempts = max(1, int(max_retries) + 1)
        last_status = None
        last_tid = None
        for attempt in range(attempts):
            try:
                resp = requests.post(
                    self.TOKEN_URL, headers=headers, data=data, timeout=30,
                )
            except requests.RequestException as e:
                if attempt + 1 < attempts:
                    delay = _retry_after_seconds(
                        None, attempt,
                        DEFAULT_BACKOFF_BASE_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS,
                    )
                    _log.warning(
                        "Intuit token endpoint network error attempt=%s/%s err=%s sleeping=%.1fs",
                        attempt + 1, attempts, e, delay,
                    )
                    time.sleep(delay)
                    continue
                raise QBOAuthError(
                    f"Could not reach Intuit token endpoint: {e}",
                    status_code=None,
                    intuit_tid=None,
                ) from e
            tid = extract_intuit_tid(resp)
            self.last_intuit_tid = tid
            last_tid = tid
            last_status = resp.status_code
            if resp.status_code < 400:
                return resp
            if _is_transient_status(resp.status_code) and attempt + 1 < attempts:
                delay = _retry_after_seconds(
                    resp, attempt,
                    DEFAULT_BACKOFF_BASE_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS,
                )
                _log.warning(
                    "Intuit token endpoint transient status=%s attempt=%s/%s tid=%s sleeping=%.1fs",
                    resp.status_code, attempt + 1, attempts, tid, delay,
                )
                time.sleep(delay)
                continue
            raise QBOAuthError(
                f"Intuit token endpoint returned {resp.status_code}",
                status_code=resp.status_code,
                intuit_tid=tid,
            )
        # Retries exhausted on transient failures.
        raise QBOAuthError(
            f"Intuit token endpoint kept returning {last_status} after {attempts} attempts",
            status_code=last_status,
            intuit_tid=last_tid,
        )

    def get_bearer_token(self, authorization_code):
        """Exchange the authorization code for access + refresh tokens."""
        data = {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": self.redirect_uri,
        }
        resp = self._post_token(data)
        token_data = resp.json()

        expires_in = token_data.get("expires_in", 3600)
        expires_at = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=expires_in)).isoformat()

        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "expires_at": expires_at,
            "token_type": token_data.get("token_type", "bearer"),
            "intuit_tid": self.last_intuit_tid,
        }

    def revoke_token(self, token):
        """Revoke a refresh or access token at Intuit's revoke endpoint.

        Best-effort: success returns True, any failure returns False without
        raising so the local disconnect flow can still wipe the encrypted
        record on disk. The intuit_tid (when present) is captured on the
        handler as `last_intuit_tid` so callers can audit the attempt.

        Per Intuit OAuth 2.0 docs the revoke endpoint accepts the bearer
        client credentials in the Authorization header and the token in a
        JSON body keyed `token`.
        """
        if not token:
            return False
        headers = {
            "Authorization": f"Basic {self._auth_header()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                self.REVOKE_URL,
                headers=headers,
                json={"token": token},
                timeout=15,
            )
        except requests.RequestException:
            return False
        self.last_intuit_tid = extract_intuit_tid(resp)
        return 200 <= resp.status_code < 300

    def refresh_access_token(self, refresh_token):
        """Refresh an expired access token."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        resp = self._post_token(data)
        token_data = resp.json()

        expires_in = token_data.get("expires_in", 3600)
        expires_at = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=expires_in)).isoformat()

        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "expires_at": expires_at,
            "token_type": token_data.get("token_type", "bearer"),
            "intuit_tid": self.last_intuit_tid,
        }
