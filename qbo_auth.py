import requests
from datetime import datetime, timedelta
from urllib.parse import urlencode
import base64

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

    def get_bearer_token(self, authorization_code):
        """Exchange the authorization code for access + refresh tokens."""
        headers = {
            "Authorization": f"Basic {self._auth_header()}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": self.redirect_uri,
        }
        resp = requests.post(self.TOKEN_URL, headers=headers, data=data)
        resp.raise_for_status()
        token_data = resp.json()

        expires_in = token_data.get("expires_in", 3600)
        expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "expires_at": expires_at,
            "token_type": token_data.get("token_type", "bearer"),
        }

    def refresh_access_token(self, refresh_token):
        """Refresh an expired access token."""
        headers = {
            "Authorization": f"Basic {self._auth_header()}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        resp = requests.post(self.TOKEN_URL, headers=headers, data=data)
        resp.raise_for_status()
        token_data = resp.json()

        expires_in = token_data.get("expires_in", 3600)
        expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "expires_at": expires_at,
            "token_type": token_data.get("token_type", "bearer"),
        }