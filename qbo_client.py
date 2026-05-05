"""
QuickBooks Online client for the import flow.

Used by app.py after OAuth completes to:
  - query the company's chart of accounts
  - create JournalEntry records

The QuickBooks Online v3 API uses the same hostname for sandbox and
production companies; the realm_id determines the company. We keep an
`environment` flag for clarity and so logs can show it.
"""

from urllib.parse import quote
import requests


class QBOClient:
    SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
    PRODUCTION_BASE = "https://quickbooks.api.intuit.com"

    def __init__(self, access_token, realm_id, environment="sandbox"):
        self.access_token = access_token
        self.realm_id = realm_id
        self.environment = environment
        self.base_url = (
            self.SANDBOX_BASE if environment == "sandbox" else self.PRODUCTION_BASE
        )

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def query(self, sql):
        encoded_query = quote(sql)
        url = f"{self.base_url}/v3/company/{self.realm_id}/query?query={encoded_query}&minorversion=65"
        response = requests.get(url, headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def get_accounts(self):
        return self.query("SELECT Id, Name, AcctNum, AccountType, Active FROM Account MAXRESULTS 1000")

    def get_company_info(self):
        """Return the connected company's CompanyInfo for the current realmId.

        Uses the dedicated CompanyInfo endpoint rather than a QBO query because
        the realmId itself is the entity Id, which makes this a single GET.
        Useful for confirming the user connected the right sandbox company.
        """
        url = (
            f"{self.base_url}/v3/company/{self.realm_id}/companyinfo/"
            f"{self.realm_id}?minorversion=65"
        )
        response = requests.get(url, headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def get_journal_entry(self, je_id):
        """Fetch a single JournalEntry by Id. Returns the JournalEntry dict
        (with Lines), or None if QBO has no record with that Id.
        """
        url = (
            f"{self.base_url}/v3/company/{self.realm_id}/journalentry/{je_id}"
            f"?minorversion=65"
        )
        response = requests.get(url, headers=self._headers(), timeout=30)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code} fetching JournalEntry {je_id}: {response.text}",
                status_code=response.status_code,
                body=response.text,
            )
        return response.json().get("JournalEntry")

    def create_journal_entry(self, journal_entry_payload):
        url = (
            f"{self.base_url}/v3/company/{self.realm_id}/journalentry?minorversion=65"
        )
        response = requests.post(
            url, headers=self._headers(), json=journal_entry_payload, timeout=30
        )
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code}: {response.text}",
                status_code=response.status_code,
                body=response.text,
            )
        return response.json()

    # --- Customer / Vendor helpers -----------------------------------------

    @staticmethod
    def _escape_qbo_string(value):
        # QBO query language uses single quotes; escape with backslash.
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def find_customer_by_name(self, display_name):
        safe = self._escape_qbo_string(display_name)
        result = self.query(f"SELECT Id, DisplayName FROM Customer WHERE DisplayName = '{safe}'")
        items = result.get("QueryResponse", {}).get("Customer", [])
        return items[0] if items else None

    def find_vendor_by_name(self, display_name):
        safe = self._escape_qbo_string(display_name)
        result = self.query(f"SELECT Id, DisplayName FROM Vendor WHERE DisplayName = '{safe}'")
        items = result.get("QueryResponse", {}).get("Vendor", [])
        return items[0] if items else None

    def create_customer(self, display_name):
        url = f"{self.base_url}/v3/company/{self.realm_id}/customer?minorversion=65"
        response = requests.post(
            url, headers=self._headers(), json={"DisplayName": display_name}, timeout=30
        )
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code} creating Customer: {response.text}",
                status_code=response.status_code,
                body=response.text,
            )
        return response.json().get("Customer", {})

    def create_vendor(self, display_name):
        url = f"{self.base_url}/v3/company/{self.realm_id}/vendor?minorversion=65"
        response = requests.post(
            url, headers=self._headers(), json={"DisplayName": display_name}, timeout=30
        )
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code} creating Vendor: {response.text}",
                status_code=response.status_code,
                body=response.text,
            )
        return response.json().get("Vendor", {})

    def get_or_create_customer(self, display_name):
        existing = self.find_customer_by_name(display_name)
        if existing:
            return existing
        return self.create_customer(display_name)

    def get_or_create_vendor(self, display_name):
        existing = self.find_vendor_by_name(display_name)
        if existing:
            return existing
        return self.create_vendor(display_name)


class QBOError(Exception):
    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
