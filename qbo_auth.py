import requests
from datetime import datetime, timedelta
from urllib.parse import urlencode
import base64

from qbo_client import extract_intuit_tid


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

    def _post_token(self, data):
        """POST to the token endpoint and capture the intuit_tid from the
        response (success or failure). On non-2xx, raises QBOAuthError with
        the captured tid; the raw body is NOT included to avoid leaking
        anything Intuit echoes back.
        """
        headers = {
            "Authorization": f"Basic {self._auth_header()}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        resp = requests.post(self.TOKEN_URL, headers=headers, data=data, timeout=30)
        tid = extract_intuit_tid(resp)
        self.last_intuit_tid = tid
        if resp.status_code >= 400:
            # Build a non-leaky message. We deliberately omit resp.text — it
            # can contain snippets that resemble client identifiers and is
            # not useful to surface; the status + intuit_tid is what Intuit
            # support needs.
            raise QBOAuthError(
                f"Intuit token endpoint returned {resp.status_code}",
                status_code=resp.status_code,
                intuit_tid=tid,
            )
        return resp

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
        expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "expires_at": expires_at,
            "token_type": token_data.get("token_type", "bearer"),
            "intuit_tid": self.last_intuit_tid,
        }

    def refresh_access_token(self, refresh_token):
        """Refresh an expired access token."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        resp = self._post_token(data)
        token_data = resp.json()

        expires_in = token_data.get("expires_in", 3600)
        expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "expires_at": expires_at,
            "token_type": token_data.get("token_type", "bearer"),
            "intuit_tid": self.last_intuit_tid,
        }
