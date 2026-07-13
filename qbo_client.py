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
import logging
import time
import requests


_log = logging.getLogger("qbo_client")


# Intuit returns the transaction id under this header. The casing in their
# docs varies; requests' case-insensitive header dict normalises it.
_INTUIT_TID_HEADER = "intuit_tid"

# Retry posture for transient QBO failures (5xx and 429). Bounded so a
# user-initiated import does not hang for minutes during an Intuit outage,
# but tolerant enough to absorb a single transient blip mid-batch.
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_CAP_SECONDS = 8.0


def _is_transient_status(status_code) -> bool:
    """Return True if a status code is worth retrying.

    429 (rate limited) and 5xx (server side) are retryable. 4xx other than
    429 indicate the request itself is wrong — retrying is just noise.
    """
    if status_code is None:
        return False
    if status_code == 429:
        return True
    return 500 <= status_code < 600


def _retry_after_seconds(response, attempt: int, base: float, cap: float) -> float:
    """Compute the sleep before the next retry.

    Honors a numeric Retry-After header when present. Otherwise falls back
    to an exponential backoff: base * 2**attempt, capped at ``cap``.
    """
    if response is not None:
        try:
            ra = response.headers.get("Retry-After") if response.headers else None
        except AttributeError:
            ra = None
        if ra:
            try:
                return max(0.0, min(float(ra), cap))
            except (TypeError, ValueError):
                pass
    return min(cap, base * (2 ** attempt))


def _sleep(seconds: float) -> None:
    """Indirection so tests can monkeypatch out the sleep."""
    if seconds > 0:
        time.sleep(seconds)


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
        url = f"{self.base_url}/v3/company/{self.realm_id}/query?query={encoded_query}&minorversion=75"
        try:
            response = requests.get(url, headers=self._headers(), timeout=30)
        except requests.RequestException as e:
            # Network-level failure (DNS, TLS, connection reset, timeout).
            # No HTTP response so no intuit_tid is available.
            raise QBOError(
                f"Could not reach QuickBooks while running a query: {e}",
                status_code=None,
                body=None,
                intuit_tid=None,
            ) from e
        # Capture the Intuit transaction id even on non-2xx responses so the
        # caller can include it in audit / user-facing diagnostics.
        tid = self._record_tid(response)
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code} on query: {response.text}",
                status_code=response.status_code,
                body=response.text,
                intuit_tid=tid,
            )
        return response.json()

    def get_accounts(self):
        return self.query("SELECT Id, Name, AcctNum, AccountType, AccountSubType, Active FROM Account MAXRESULTS 1000")

    def get_company_info(self):
        """Return the connected company's CompanyInfo for the current realmId.

        Uses the dedicated CompanyInfo endpoint rather than a QBO query because
        the realmId itself is the entity Id, which makes this a single GET.
        Useful for confirming the user connected the right sandbox company.
        """
        url = (
            f"{self.base_url}/v3/company/{self.realm_id}/companyinfo/"
            f"{self.realm_id}?minorversion=75"
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
            f"?minorversion=75"
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

    def create_journal_entry(self, journal_entry_payload, *,
                             max_retries: int = DEFAULT_MAX_RETRIES):
        """Create one JournalEntry, retrying transient failures.

        Retries on 5xx and 429 with bounded exponential backoff (honoring
        Retry-After when Intuit sends one). 4xx other than 429 fail
        immediately because retrying a bad request is just noise.

        Network-level failures (DNS / TLS / read timeout) are also retried
        the same way, since they typically reflect a transient blip rather
        than a permanent error.

        Each attempt creates a new JournalEntry on the QBO side IF the
        previous attempt actually reached Intuit and succeeded but the
        response was lost. We document this limitation rather than try to
        fully fix it here — true idempotency would require a stable
        idempotency key that QBO's v3 API does not yet support. Callers
        guard against this at the batch level: every PCLaw transaction_id
        is recorded in ImportHistory and re-runs of the same file are
        blocked by file SHA, and the duplicate transaction_id guard in the
        send path rejects already-imported transaction ids.
        """
        url = (
            f"{self.base_url}/v3/company/{self.realm_id}/journalentry?minorversion=75"
        )
        attempts = max(1, int(max_retries) + 1)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                response = requests.post(
                    url, headers=self._headers(),
                    json=journal_entry_payload, timeout=30,
                )
            except requests.RequestException as e:
                # Network-level transient — retry if budget remains.
                last_exc = e
                if attempt + 1 < attempts:
                    delay = _retry_after_seconds(
                        None, attempt,
                        DEFAULT_BACKOFF_BASE_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS,
                    )
                    _log.warning(
                        "create_journal_entry network error attempt=%s/%s err=%s sleeping=%.1fs",
                        attempt + 1, attempts, e, delay,
                    )
                    _sleep(delay)
                    continue
                raise QBOError(
                    f"Could not reach QuickBooks while creating a journal entry: {e}",
                    status_code=None,
                    body=None,
                    intuit_tid=None,
                ) from e

            tid = self._record_tid(response)
            if response.status_code < 400:
                return response.json()

            if _is_transient_status(response.status_code) and attempt + 1 < attempts:
                delay = _retry_after_seconds(
                    response, attempt,
                    DEFAULT_BACKOFF_BASE_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS,
                )
                _log.warning(
                    "create_journal_entry transient status=%s attempt=%s/%s tid=%s sleeping=%.1fs",
                    response.status_code, attempt + 1, attempts, tid, delay,
                )
                _sleep(delay)
                continue

            raise QBOError(
                f"QBO returned {response.status_code}: {response.text}",
                status_code=response.status_code,
                body=response.text,
                intuit_tid=tid,
            )

        # All retries exhausted on transient failures.
        raise QBOError(
            f"QuickBooks remained unavailable after {attempts} attempts: {last_exc}",
            status_code=None,
            body=None,
            intuit_tid=self.last_intuit_tid,
        )

    def create_journal_entries_batch(self, payloads: list[dict]) -> list[dict]:
        """Create up to 25 JournalEntries in one QBO /batch request. Returns a
        list of BatchItemResponse entries, same order as `payloads`. Each is
        either {"JournalEntry": {...}} (success) or {"Fault": {...}} (that one
        item failed) — QBO's batch endpoint only raises for a malformed
        request or an outage, never for an individual item's business-rule
        rejection, so the caller must inspect every item.
        """
        if not payloads:
            return []
        if len(payloads) > 25:
            raise ValueError("create_journal_entries_batch: caller must chunk to <=25 items")

        batch_items = [
            {"bId": str(i + 1), "operation": "create", "JournalEntry": p}
            for i, p in enumerate(payloads)
        ]
        url = f"{self.base_url}/v3/company/{self.realm_id}/batch?minorversion=75"
        attempts = max(1, int(DEFAULT_MAX_RETRIES) + 1)
        last_exc = None

        # Diagnostic logging: capture full batch payload before posting (DIAG6000:)
        if batch_items:
            sample_item = batch_items[0]
            sample_je = sample_item.get("JournalEntry", {})
            sample_lines = sample_je.get("Line", [])
            # Log the full sample JE with descriptions truncated for readability
            sample_je_display = dict(sample_je)
            for line in sample_je_display.get("Line", []):
                if "Description" in line and len(line["Description"]) > 80:
                    line = dict(line)
                    line["Description"] = line["Description"][:80] + "..."
            _log.warning(
                "DIAG6000: batch request — posting %d items, sample JE: %s",
                len(batch_items),
                json.dumps(sample_je_display, default=str),
            )

        for attempt in range(attempts):
            try:
                response = requests.post(
                    url, headers=self._headers(),
                    json={"BatchItemRequest": batch_items}, timeout=60,
                )
            except requests.RequestException as e:
                last_exc = e
                if attempt + 1 < attempts:
                    delay = _retry_after_seconds(
                        None, attempt, DEFAULT_BACKOFF_BASE_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS,
                    )
                    _log.warning(
                        "create_journal_entries_batch network error attempt=%s/%s err=%s sleeping=%.1fs",
                        attempt + 1, attempts, e, delay,
                    )
                    _sleep(delay)
                    continue
                raise QBOError(
                    f"Could not reach QuickBooks batch endpoint: {e}",
                    status_code=None, body=None, intuit_tid=None,
                ) from e

            tid = self._record_tid(response)
            if response.status_code < 400:
                resp_items = response.json().get("BatchItemResponse", [])
                by_bid = {item.get("bId"): item for item in resp_items}

                # Log any faults for diagnosis (DIAG6000:)
                fault_count = sum(1 for item in resp_items if "Fault" in item)
                if fault_count > 0:
                    sample_fault = next((item.get("Fault") for item in resp_items if "Fault" in item), {})
                    _log.warning(
                        "DIAG6000: batch faults — %d of %d items returned Fault. "
                        "Sample fault: %s, intuit_tid: %s",
                        fault_count, len(resp_items),
                        json.dumps(sample_fault, default=str),
                        tid,
                    )

                return [
                    by_bid.get(str(i + 1)) or {"Fault": {"Error": [{"Message": "Missing batch response item", "code": "MISSING"}]}}
                    for i in range(len(payloads))
                ]

            if _is_transient_status(response.status_code) and attempt + 1 < attempts:
                delay = _retry_after_seconds(
                    response, attempt, DEFAULT_BACKOFF_BASE_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS,
                )
                _log.warning(
                    "create_journal_entries_batch transient status=%s attempt=%s/%s tid=%s sleeping=%.1fs",
                    response.status_code, attempt + 1, attempts, tid, delay,
                )
                _sleep(delay)
                continue

            raise QBOError(
                f"QBO returned {response.status_code} on batch journal-entry create: {response.text}",
                status_code=response.status_code, body=response.text, intuit_tid=tid,
            )

        raise QBOError(
            f"Could not reach QuickBooks batch endpoint after {attempts} attempts: {last_exc}",
            status_code=None, body=None, intuit_tid=None,
        )

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

    def find_journal_entry_by_doc_number(self, doc_number):
        """Return the QBO JournalEntry dict matching DocNumber exactly, or None.

        This is the idempotency probe for journal-entry writes. QBO's v3
        API does not accept a client idempotency key, so to make a retry
        after a *lost response* safe we stamp each entry with a stable,
        deterministic DocNumber and check here whether one already exists
        before posting. If it does, the previous attempt actually reached
        Intuit and succeeded — we reuse that entry instead of creating a
        duplicate.
        """
        if not doc_number:
            return None
        safe = self._escape_qbo_string(str(doc_number))
        result = self.query(
            f"SELECT Id, DocNumber, TxnDate FROM JournalEntry "
            f"WHERE DocNumber = '{safe}'"
        )
        items = result.get("QueryResponse", {}).get("JournalEntry", [])
        return items[0] if items else None

    def find_journal_entries_by_doc_numbers(self, doc_numbers: list[str]) -> dict[str, dict]:
        """Batch idempotency probe. Returns {DocNumber: JournalEntry} for every
        DocNumber that already exists in QBO; missing ones are simply absent.

        Replaces one find_journal_entry_by_doc_number() call per JE with a
        single query per chunk (<=25 doc numbers — same chunk size as the
        create batch, kept in lockstep for simplicity).
        """
        doc_numbers = [str(d) for d in doc_numbers if d]
        if not doc_numbers:
            return {}
        safe_list = ", ".join(f"'{self._escape_qbo_string(d)}'" for d in doc_numbers)
        result = self.query(
            f"SELECT Id, DocNumber, TxnDate FROM JournalEntry "
            f"WHERE DocNumber IN ({safe_list})"
        )
        items = result.get("QueryResponse", {}).get("JournalEntry", [])
        return {je.get("DocNumber"): je for je in items if je.get("DocNumber")}

    def delete_journal_entry(self, je_id: str, sync_token: str) -> dict:
        """Delete (void) a Journal Entry from QuickBooks.

        QBO requires the current SyncToken to delete. Raises QBOAuthExpired on 401,
        QBOError on any other non-200 response.
        """
        url = f"{self.base_url}/v3/company/{self.realm_id}/journalentry?operation=delete&minorversion=75"
        # QBO delete endpoint requires fields at top level — NOT wrapped
        # in the entity name. Wrapper causes error 2010 on ?operation=delete.
        payload = {
            "Id": je_id,
            "SyncToken": sync_token,
        }
        response = requests.post(
            url, headers=self._headers(), json=payload, timeout=30
        )
        tid = self._record_tid(response)
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code} deleting JournalEntry {je_id}: {response.text}",
                status_code=response.status_code,
                body=response.text,
                intuit_tid=tid,
            )
        return response.json()

    def create_account(self, payload):
        """Create a QBO Account. Returns the parsed JSON response.

        ``payload`` should contain at minimum ``Name`` and ``AccountType``.
        Optional fields: ``AccountSubType``, ``AcctNum``, ``Active``,
        ``Description``. Intuit's API enforces AccountType / AccountSubType
        compatibility — callers must pass safe combinations (see
        coa_apply.map_pclaw_account_to_qbo_type).
        """
        url = f"{self.base_url}/v3/company/{self.realm_id}/account?minorversion=75"
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

    def update_account(self, payload):
        """Update a QBO Account (sparse update). Returns the parsed JSON response.

        ``payload`` should contain ``Id``, ``SyncToken``, ``sparse: True``,
        and the fields to update (e.g., ``AcctNum``).
        """
        url = f"{self.base_url}/v3/company/{self.realm_id}/account?minorversion=75"
        response = requests.post(
            url, headers=self._headers(), json=payload, timeout=30
        )
        tid = self._record_tid(response)
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code} updating Account: {response.text}",
                status_code=response.status_code,
                body=response.text,
                intuit_tid=tid,
            )
        return response.json()

    def update_accounts(self, payloads: "list[dict]") -> "list[dict]":
        """
        Sparse-update QBO Account objects to set AcctNum.
        Caller must chunk to <=25 items before calling.
        """
        batch_items = [
            {"bId": str(i + 1), "operation": "update", "Account": p}
            for i, p in enumerate(payloads)
        ]
        url = f"{self.base_url}/v3/company/{self.realm_id}/batch?minorversion=75"
        resp = self._post(url, {"BatchItemRequest": batch_items})
        return resp.get("BatchItemResponse", [])

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
        if not str(display_name or "").strip():
            raise QBOError(
                "Refusing to create a Customer with a blank DisplayName. "
                "Resolve the customer name before syncing.",
                status_code=None,
            )
        url = f"{self.base_url}/v3/company/{self.realm_id}/customer?minorversion=75"
        response = requests.post(
            url, headers=self._headers(), json={"DisplayName": display_name}, timeout=30
        )
        tid = self._record_tid(response)
        if response.status_code == 400:
            # 6240 = Duplicate Name Exists. The find-before-create check can miss
            # the entity due to case/whitespace differences or prior partial runs.
            # Fall back to a second lookup; if found, return it as if we created it.
            try:
                errors = (response.json().get("Fault") or {}).get("Error") or []
                if any(str(e.get("code")) == "6240" for e in errors):
                    _log.warning(
                        "create_customer 6240 for %r — entity already exists in QBO; "
                        "falling back to find_customer_by_name",
                        display_name,
                    )
                    existing = self.find_customer_by_name(display_name)
                    if existing:
                        return existing
            except Exception:  # noqa: BLE001
                pass  # fall through to generic raise below
        if response.status_code >= 400:
            raise QBOError(
                f"QBO returned {response.status_code} creating Customer: {response.text}",
                status_code=response.status_code,
                body=response.text,
                intuit_tid=tid,
            )
        return response.json().get("Customer", {})

    def create_vendor(self, display_name):
        if not str(display_name or "").strip():
            raise QBOError(
                "Refusing to create a Vendor with a blank DisplayName. "
                "Resolve the vendor name before syncing.",
                status_code=None,
            )
        url = f"{self.base_url}/v3/company/{self.realm_id}/vendor?minorversion=75"
        response = requests.post(
            url, headers=self._headers(), json={"DisplayName": display_name}, timeout=30
        )
        tid = self._record_tid(response)
        if response.status_code == 400:
            # 6240 = Duplicate Name Exists — fall back to find.
            try:
                errors = (response.json().get("Fault") or {}).get("Error") or []
                if any(str(e.get("code")) == "6240" for e in errors):
                    _log.warning(
                        "create_vendor 6240 for %r — entity already exists in QBO; "
                        "falling back to find_vendor_by_name",
                        display_name,
                    )
                    existing = self.find_vendor_by_name(display_name)
                    if existing:
                        return existing
            except Exception:  # noqa: BLE001
                pass  # fall through to generic raise below
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

    def get_vendor_by_id(self, entity_id: str):
        """Read a single Vendor by QBO Id — works for inactive vendors too.

        The query API skips inactive entities; the REST read endpoint does not.
        Used as fallback when 6240 fires and exact-name lookup returns nothing.
        """
        url = (
            f"{self.base_url}/v3/company/{self.realm_id}"
            f"/vendor/{entity_id}?minorversion=75"
        )
        response = requests.get(url, headers=self._headers(), timeout=30)
        if response.status_code == 200:
            return response.json().get("Vendor")
        return None

    def get_customer_by_id(self, entity_id: str):
        """Read a single Customer by QBO Id — works for inactive customers too."""
        url = (
            f"{self.base_url}/v3/company/{self.realm_id}"
            f"/customer/{entity_id}?minorversion=75"
        )
        response = requests.get(url, headers=self._headers(), timeout=30)
        if response.status_code == 200:
            return response.json().get("Customer")
        return None

    def get_all_customers(self) -> list[dict]:
        """Fetch all active customers from QBO for entity matching in Step 3.

        Returns a list of dicts with at minimum Id and DisplayName.
        QBO paginates at 1000 — fetch up to 500 which covers typical firm size.
        """
        result = self.query(
            "SELECT Id, DisplayName FROM Customer WHERE Active = true MAXRESULTS 500"
        )
        return result.get("QueryResponse", {}).get("Customer", [])

    def get_all_vendors(self) -> list[dict]:
        """Fetch all active vendors from QBO for entity matching in Step 3."""
        result = self.query(
            "SELECT Id, DisplayName FROM Vendor WHERE Active = true MAXRESULTS 500"
        )
        return result.get("QueryResponse", {}).get("Vendor", [])


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
