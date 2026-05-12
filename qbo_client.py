"""
QuickBooks Online client for the import flow.

Used by app.py after OAuth completes to:
  - query the company's chart of accounts
  - create JournalEntry records

The QuickBooks Online v3 API uses the same hostname for sandbox and
production companies; the realm_id determines the company. We keep an
`environment` flag for clarity and so logs can show it.

Every successful or failing QBO HTTP response carries an `intuit_tid`
header (the Intuit transaction id). We capture it on the client as
`last_intuit_tid` after each call and propagate it on QBOError so callers
can surface it to operators / Intuit support without having to plumb the
raw response object through.
"""

from urllib.parse import quote
import requests


# Intuit returns the transaction id under this header. The casing in their
# docs varies; requests' case-insensitive header dict normalises it.
_INTUIT_TID_HEADER = "intuit_tid"


def extract_intuit_tid(response):
    """Pull the intuit_tid from a requests.Response header, or None.

    Safe on any object that exposes a headers mapping (real responses or
    test doubles). Returns the trimmed string, never an empty string.
    """
    if response is None:
        return None
    try:
        headers = response.headers
    except AttributeError:
        return None
    if not headers:
        return None
    value = headers.get(_INTUIT_TID_HEADER) or headers.get("Intuit-TID") or headers.get("Intuit_Tid")
    if not value:
        return None
    value = value.strip()
    return value or None


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
        # Updated after every HTTP call (success or failure) so callers can
        # log / surface it without holding the response.
        self.last_intuit_tid = None

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _record_tid(self, response):
        tid = extract_intuit_tid(response)
        self.last_intuit_tid = tid
        return tid

    def query(self, sql):
        encoded_query = quote(sql)
        url = f"{self.base_url}/v3/company/{self.realm_id}/query?query={encoded_query}&minorversion=65"
        response = requests.get(url, headers=self._headers(), timeout=30)
        # Capture the Intuit transaction id even on non-2xx responses, then
        # preserve raise_for_status semantics callers already depend on.
        self._record_tid(response)
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
        self._record_tid(response)
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
        tid = self._record_tid(response)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code} fetching JournalEntry {je_id}: {response.text}",
                status_code=response.status_code,
                body=response.text,
                intuit_tid=tid,
            )
        return response.json().get("JournalEntry")

    def create_journal_entry(self, journal_entry_payload):
        url = (
            f"{self.base_url}/v3/company/{self.realm_id}/journalentry?minorversion=65"
        )
        response = requests.post(
            url, headers=self._headers(), json=journal_entry_payload, timeout=30
        )
        tid = self._record_tid(response)
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code}: {response.text}",
                status_code=response.status_code,
                body=response.text,
                intuit_tid=tid,
            )
        return response.json()

    def find_account_by_acctnum(self, acct_num):
        """Return the QBO Account dict matching AcctNum exactly, or None.

        Used by the COA-creation flow to defend against races: between
        the dry-run preview and the create POST another user may have
        added the same account number. We re-check inside the create
        loop so we don't end up with parallel duplicates.
        """
        if not acct_num:
            return None
        safe = self._escape_qbo_string(str(acct_num))
        result = self.query(
            f"SELECT Id, Name, AcctNum, AccountType, AccountSubType "
            f"FROM Account WHERE AcctNum = '{safe}'"
        )
        items = result.get("QueryResponse", {}).get("Account", [])
        return items[0] if items else None

    def find_account_by_name(self, name):
        """Return the QBO Account dict matching Name exactly, or None."""
        if not name:
            return None
        safe = self._escape_qbo_string(name)
        result = self.query(
            f"SELECT Id, Name, AcctNum, AccountType, AccountSubType "
            f"FROM Account WHERE Name = '{safe}'"
        )
        items = result.get("QueryResponse", {}).get("Account", [])
        return items[0] if items else None

    def create_account(self, payload):
        """Create a QBO Account. Returns the parsed JSON response.

        ``payload`` should contain at minimum ``Name`` and ``AccountType``.
        Optional fields: ``AccountSubType``, ``AcctNum``, ``Active``,
        ``Description``. Intuit's API enforces AccountType / AccountSubType
        compatibility — callers must pass safe combinations (see
        coa_apply.map_pclaw_account_to_qbo_type).
        """
        url = f"{self.base_url}/v3/company/{self.realm_id}/account?minorversion=65"
        response = requests.post(
            url, headers=self._headers(), json=payload, timeout=30
        )
        tid = self._record_tid(response)
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code} creating Account: {response.text}",
                status_code=response.status_code,
                body=response.text,
                intuit_tid=tid,
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
        tid = self._record_tid(response)
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code} creating Customer: {response.text}",
                status_code=response.status_code,
                body=response.text,
                intuit_tid=tid,
            )
        return response.json().get("Customer", {})

    def create_vendor(self, display_name):
        url = f"{self.base_url}/v3/company/{self.realm_id}/vendor?minorversion=65"
        response = requests.post(
            url, headers=self._headers(), json={"DisplayName": display_name}, timeout=30
        )
        tid = self._record_tid(response)
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code} creating Vendor: {response.text}",
                status_code=response.status_code,
                body=response.text,
                intuit_tid=tid,
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
    """A QBO API error.

    `intuit_tid` is the Intuit transaction id from the failing response's
    headers, when present. It is safe to surface to operators and even to
    end users as a support reference — it identifies the request to Intuit
    support but contains no token, secret, or PII.
    """

    def __init__(self, message, status_code=None, body=None, intuit_tid=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.intuit_tid = intuit_tid
