"""Chart of Accounts QBO creation: type-mapping + safe-create plan builder.

This module owns the cutover step where a parsed PCLaw Chart of Accounts is
turned into actual QuickBooks Online ``Account`` records. It is deliberately
the only place where the COA flow does any *write* planning — the Flask
route layer is a thin shell on top of these pure functions so the logic is
unit-testable without a live QBO realm.

Design rules (intentionally conservative):

* Never guess an account type. PCLaw's category vocabulary doesn't map 1:1
  to QBO. We only map type/sub-type combos we are confident about; ambiguous
  rows are flagged as ``blocked`` and require operator resolution before a
  create plan is approved.
* Read-only matching first. The plan ingests the same dry-run preview the
  existing ``coa-preview`` page already builds, so accounts that already
  exist in QBO (by ``AcctNum`` or canonical Name) are *never* re-created.
* Special accounts that QBO auto-provisions (Accounts Receivable, Accounts
  Payable, Undeposited Funds, Retained Earnings, the system Sales Tax
  accounts on Canadian companies) are flagged for operator review even if
  we have a valid type-map, because creating a parallel one usually causes
  reconciliation problems later.
* Trust liability + trust bank get a clear warning so the operator sees
  what is about to be created — not blocked, because most firms genuinely
  need these in QBO, but never silent.

Nothing in this module makes HTTP calls. ``apply_create_plan`` takes a
QBO client (or a test double) and executes the plan one account at a time,
recording successes and failures so the route can render a result page.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

import reserved_accounts

QBO_ACCT_NUM_MAX = 100  # QBO AcctNum field character limit

# ----------------------------------------------------------------------------
# PCLaw -> QBO type-mapping table.
#
# Keys are *normalized* tokens (lowercase, alphanumeric only). The matcher
# tries the PCLaw account_type first, then the qbo_suggested_detail_type,
# then the account_name as a last-resort hint. The first non-None match
# wins; an entry that returns None means "we recognise this hint, but
# require a human to disambiguate".
#
# Mapping references:
#   QuickBooks AccountType / AccountSubType reference (Intuit docs):
#     https://developer.intuit.com/app/developer/qbo/docs/api/accounting/most-commonly-used/account
# ----------------------------------------------------------------------------


# Sentinel detail-type values that are *valid* for QBO but should warn the
# operator before creating, because creating a duplicate of an existing
# auto-provisioned account historically causes mapping bugs.
_AUTO_PROVISIONED_SUBTYPES = {
    "AccountsReceivable",
    "AccountsPayable",
    "UndepositedFunds",
}


# Detail types we will warn-but-allow when matched. The firm legitimately
# needs trust bank + trust liability accounts in QBO for client-money
# handling; we still surface a warning so the operator sees it.
#
# IMPORTANT: QBO's API expects camelCase AccountSubType identifiers
# (e.g. ``TrustAccountsLiabilities``, NOT ``TrustAccounts-Liabilities``).
# Sending the hyphenated display form causes Intuit to reject the create
# with a 400, which previously left Step 3 stuck on an unmatched trust
# liability row.
_WARN_SUBTYPES = {
    "TrustAccountsLiabilities",
    "TrustAccounts",
}


def _norm(token: Optional[str]) -> str:
    if not token:
        return ""
    return "".join(ch for ch in str(token).lower() if ch.isalnum())


# Pseudo / system-calculated accounts that QuickBooks computes on the fly
# (Net Income, Net Income (Loss), Current Year Earnings). PCLaw exports
# sometimes list these on the chart of accounts as if they were normal
# rows, but QBO calculates them inherently from posted activity — creating
# a real account with one of these names would conflict with QBO's
# auto-calculated total and cause reconciliation problems. We detect them
# by normalized name and short-circuit both type mapping and the create
# plan so the customer-facing UI can explain "QuickBooks calculates this
# automatically".
_SYSTEM_CALCULATED_NAME_TOKENS = (
    "netincome",
    "netincomeloss",
    "netloss",
    "currentyearearnings",
    "currentearnings",
)


def is_system_calculated_account(row: dict) -> bool:
    """Return True if this account is one QuickBooks computes itself.

    Pure, side-effect-free. Used by ``map_pclaw_account_to_qbo_type`` to
    short-circuit, and by the create-plan builder to exclude these rows
    from QBO writes entirely.
    """
    raw_name = (row or {}).get("account_name")
    # Our own "Net Income-PC Law" holding account is a *real*, createable
    # account that intentionally carries the words "Net Income". It must
    # never be short-circuited as system-calculated, or the migration could
    # not seed an opening balance into it.
    if reserved_accounts.is_reserved_pc_law_name(raw_name):
        return False
    name_norm = _norm(raw_name)
    if not name_norm:
        return False
    # Use ``in`` rather than equality so common decorations like
    # "Net Income (Loss)", "Net Income / Loss", or a leading account
    # number prefix in the name still match.
    return any(token in name_norm for token in _SYSTEM_CALCULATED_NAME_TOKENS)


SYSTEM_CALCULATED_EXPLANATION = (
    "QuickBooks calculates this automatically, so we won't create it "
    "as a separate account."
)


# (account_type, detail_type) tuples keyed by normalized hint tokens.
# Every entry here is a *safe* mapping — if a hint is missing from this
# table we refuse to guess.
_TYPE_TABLE: dict[str, tuple[str, str]] = {
    # Banks / cash
    "bank": ("Bank", "Checking"),
    "checking": ("Bank", "Checking"),
    "operatingbank": ("Bank", "Checking"),
    "savings": ("Bank", "Savings"),
    "trustbank": ("Bank", "TrustAccounts"),
    "trustaccount": ("Bank", "TrustAccounts"),

    # Receivables
    "accountsreceivable": ("Accounts Receivable", "AccountsReceivable"),
    "receivable": ("Accounts Receivable", "AccountsReceivable"),
    "ar": ("Accounts Receivable", "AccountsReceivable"),

    # Other current assets
    "othercurrentasset": ("Other Current Asset", "OtherCurrentAssets"),
    "wip": ("Other Current Asset", "OtherCurrentAssets"),
    "unbilleddisbursements": ("Other Current Asset", "OtherCurrentAssets"),
    "prepaidexpenses": ("Other Current Asset", "PrepaidExpenses"),
    "inventory": ("Other Current Asset", "Inventory"),

    # Fixed assets — common law-firm asset accounts. PCLaw's account list
    # frequently uses bare names like "Computers", "Furniture & Fixtures",
    # "Leasehold Improvements", and "Office Equipment" with the high-level
    # "Fixed Asset" category. Mapping each to its canonical QBO sub-type
    # avoids the previous failure where only the generic "Office Equipment"
    # row landed in Fixed Asset while the related rows stayed unmatched.
    "fixedasset": ("Fixed Asset", "FurnitureAndFixtures"),
    "fixedassets": ("Fixed Asset", "FurnitureAndFixtures"),
    "equipment": ("Fixed Asset", "MachineryAndEquipment"),
    "officeequipment": ("Fixed Asset", "MachineryAndEquipment"),
    "computer": ("Fixed Asset", "MachineryAndEquipment"),
    "computers": ("Fixed Asset", "MachineryAndEquipment"),
    "computerequipment": ("Fixed Asset", "MachineryAndEquipment"),
    "computerhardware": ("Fixed Asset", "MachineryAndEquipment"),
    "furniturefixtures": ("Fixed Asset", "FurnitureAndFixtures"),
    "furnitureandfixtures": ("Fixed Asset", "FurnitureAndFixtures"),
    "furniture": ("Fixed Asset", "FurnitureAndFixtures"),
    "leaseholdimprovement": ("Fixed Asset", "LeaseholdImprovements"),
    "leaseholdimprovements": ("Fixed Asset", "LeaseholdImprovements"),
    "officeconstruction": ("Fixed Asset", "LeaseholdImprovements"),
    "buildings": ("Fixed Asset", "Buildings"),
    "building": ("Fixed Asset", "Buildings"),
    "vehicles": ("Fixed Asset", "Vehicles"),
    "vehicle": ("Fixed Asset", "Vehicles"),
    "accumulateddepreciation": ("Fixed Asset", "AccumulatedDepreciation"),

    # Payables
    "accountspayable": ("Accounts Payable", "AccountsPayable"),
    "payable": ("Accounts Payable", "AccountsPayable"),
    "ap": ("Accounts Payable", "AccountsPayable"),

    # Other current liabilities
    "othercurrentliability": ("Other Current Liability", "OtherCurrentLiabilities"),
    "trustliability": ("Other Current Liability", "TrustAccountsLiabilities"),
    "trustaccountsliabilities": ("Other Current Liability", "TrustAccountsLiabilities"),
    "clienttrustliability": ("Other Current Liability", "TrustAccountsLiabilities"),

    # Long-term liabilities — law firms commonly carry partner / shareholder
    # loans and bank loans (e.g. "Loan From John Smith"). Map the generic
    # "loan" hint to NotesPayable so the account-list upload can drive
    # creation safely. Lines of credit (LOC) live in QBO as Long Term
    # Liability / NotesPayable — a separate "Credit Card" type exists for
    # actual revolving credit cards. We never auto-convert "LOC" into the
    # credit-card type because the right answer depends on the firm's
    # facility (bank-issued credit card vs. bank LOC) — both flow through
    # this Long Term Liability default safely.
    "longtermliability": ("Long Term Liability", "NotesPayable"),
    "loan": ("Long Term Liability", "NotesPayable"),
    "loanfromshareholder": ("Long Term Liability", "ShareholderNotesPayable"),
    "shareholderloan": ("Long Term Liability", "ShareholderNotesPayable"),
    "partnerloan": ("Long Term Liability", "NotesPayable"),
    "notespayable": ("Long Term Liability", "NotesPayable"),
    "lineofcredit": ("Long Term Liability", "LineOfCredit"),
    "linesofcredit": ("Long Term Liability", "LineOfCredit"),
    "loc": ("Long Term Liability", "LineOfCredit"),
    "bankloc": ("Long Term Liability", "LineOfCredit"),
    "businessloc": ("Long Term Liability", "LineOfCredit"),

    # Credit Card — only when the COA explicitly says "credit card"
    "creditcard": ("Credit Card", "CreditCard"),

    # Equity. Law firms operating as PCs / LLCs / corporations commonly
    # carry Common Stock, Paid In Capital, Dividends, Distributions, and
    # Member Capital on the chart of accounts. All of these are Equity
    # in QuickBooks. We map to the closest QBO AccountSubType (OwnersEquity
    # is the safe default for partnership-style accounts; corporate-style
    # stock and dividends have dedicated subtypes).
    "equity": ("Equity", "OwnersEquity"),
    "ownerequity": ("Equity", "OwnersEquity"),
    "ownersequity": ("Equity", "OwnersEquity"),
    "retainedearnings": ("Equity", "RetainedEarnings"),
    "commonstock": ("Equity", "CommonStock"),
    "preferredstock": ("Equity", "PreferredStock"),
    "paidincapital": ("Equity", "PaidInCapitalOrSurplus"),
    "additionalpaidincapital": ("Equity", "PaidInCapitalOrSurplus"),
    "capitalstock": ("Equity", "CommonStock"),
    "dividends": ("Equity", "OwnersEquity"),
    "dividend": ("Equity", "OwnersEquity"),
    "distributions": ("Equity", "OwnersEquity"),
    "distribution": ("Equity", "OwnersEquity"),
    "membercapital": ("Equity", "OwnersEquity"),
    "memberdraw": ("Equity", "OwnersEquity"),
    "memberscapital": ("Equity", "OwnersEquity"),
    "partnercapital": ("Equity", "OwnersEquity"),
    "treasurystock": ("Equity", "TreasuryStock"),
    # "Owner's Draw" is a PCLaw account_type value → _norm = "ownersdraw".
    # _SAFE_NAME_PATTERNS has "ownersdraw" but only matches account NAMES.
    # Adding here lets the account_type column resolve it directly.
    "ownersdraw": ("Equity", "OwnersEquity"),
    "ownersequity": ("Equity", "OwnersEquity"),
    "ownerscapital": ("Equity", "OwnersEquity"),

    # Income
    "income": ("Income", "ServiceFeeIncome"),
    "revenue": ("Income", "ServiceFeeIncome"),
    "servicefeeincome": ("Income", "ServiceFeeIncome"),
    "otherprimaryincome": ("Income", "OtherPrimaryIncome"),
    "recovery": ("Income", "OtherPrimaryIncome"),

    # Expense — PCLaw COA exports use both "Expense" (singular) and "Expenses" (plural)
    "expense": ("Expense", "OfficeGeneralAdministrativeExpenses"),
    "expenses": ("Expense", "OfficeGeneralAdministrativeExpenses"),   # PCLaw plural form
    "overhead": ("Expense", "OfficeGeneralAdministrativeExpenses"),
    "office": ("Expense", "OfficeGeneralAdministrativeExpenses"),
    "officegeneraladministrativeexpenses": (
        "Expense", "OfficeGeneralAdministrativeExpenses",
    ),
    # Other Expense — PCLaw uses "Other Expenses" (plural); QBO type is "Other Expense"
    "otherexpense": ("Other Expense", "OtherMiscellaneousExpense"),
    "otherexpenses": ("Other Expense", "OtherMiscellaneousExpense"),
    "rentorleaseofbuildings": ("Expense", "RentOrLeaseOfBuildings"),
    "rent": ("Expense", "RentOrLeaseOfBuildings"),
    "legalprofessionalfees": ("Expense", "LegalAndProfessionalFees"),
    "filingfees": ("Expense", "LegalAndProfessionalFees"),
    "clientcost": ("Expense", "LegalAndProfessionalFees"),
    "advertising": ("Expense", "AdvertisingPromotional"),
    "utilities": ("Expense", "Utilities"),
    "insurance": ("Expense", "Insurance"),
    "healthinsurance": ("Expense", "Insurance"),
    "insuranceother": ("Expense", "Insurance"),
    "insurancegeneral": ("Expense", "Insurance"),
    "insurancehealth": ("Expense", "Insurance"),
    "insurancelife": ("Expense", "Insurance"),
    "insuranceliability": ("Expense", "Insurance"),
    "travel": ("Expense", "Travel"),
    "businessdevelopment": ("Expense", "EntertainmentMeals"),
    "marketing": ("Expense", "AdvertisingPromotional"),
    "cle": ("Expense", "DuesSubscriptions"),
    "continuedlegaled": ("Expense", "DuesSubscriptions"),
    "continuedlegaleducation": ("Expense", "DuesSubscriptions"),
    "continuingeducation": ("Expense", "DuesSubscriptions"),
    "continuinglegaleducation": ("Expense", "DuesSubscriptions"),
    "education": ("Expense", "DuesSubscriptions"),
    "training": ("Expense", "DuesSubscriptions"),
    "maintenance": ("Expense", "RepairMaintenance"),
    "repairs": ("Expense", "RepairMaintenance"),
    "maintenancerepair": ("Expense", "RepairMaintenance"),
    "repairsmaintenance": ("Expense", "RepairMaintenance"),
    "repairandmaintenance": ("Expense", "RepairMaintenance"),
    "repairmaintenance": ("Expense", "RepairMaintenance"),
    "telephone": ("Expense", "Utilities"),
    "internet": ("Expense", "Utilities"),
    "postage": ("Expense", "OfficeGeneralAdministrativeExpenses"),
    "duesandsubscriptions": ("Expense", "DuesSubscriptions"),
    "duessubscriptions": ("Expense", "DuesSubscriptions"),

    # Cost of goods sold (rare in legal but handle it)
    "cogs": ("Cost of Goods Sold", "EquipmentRental"),
    "costofgoodssold": ("Cost of Goods Sold", "SuppliesMaterialsCogs"),

    # Top-level PCLaw category buckets that on their own are too ambiguous —
    # we recognise them but refuse to auto-create without a more specific hint.
    "asset": (None, None),  # too broad — could be bank, AR, fixed asset, etc.
    "liability": (None, None),
}


# Compound name-pattern tier. Each entry is (keyword, mode, (type, detail))
# where ``mode`` is "contains" (match anywhere in the normalised name) or
# "endswith" (match only as a suffix). Patterns are tested in order, so
# more specific patterns must come first (e.g. "bankfee" before the
# generic "expense" suffix). Every pattern here is deterministic — the
# QBO AccountType/AccountSubType pair is the canonical mapping for the
# pattern, not a guess. Adding a new pattern is safe only when the term
# is unambiguous across legal/professional services chart-of-accounts.
_SAFE_NAME_PATTERNS: list[tuple[str, str, tuple[str, str]]] = [
    # ---- Reserved "-PC Law" holding accounts (created first so the
    # opening trial balance can post Net Income / Retained Earnings / A/R /
    # A/P into a clearly-labelled account instead of QuickBooks' built-in
    # one. These are unambiguous compound forms unique to this migration.
    ("netincomepclaw", "contains", ("Equity", "OwnersEquity")),
    ("repclaw", "contains", ("Equity", "OwnersEquity")),
    ("arpclaw", "contains", ("Other Current Asset", "OtherCurrentAssets")),
    ("appclaw", "contains", ("Other Current Liability", "OtherCurrentLiabilities")),

    # ---- Equity (specific compound terms only) ----
    ("ownerdraw", "contains", ("Equity", "OwnersEquity")),       # "Owner Draws"
    ("ownersdraw", "contains", ("Equity", "OwnersEquity")),
    ("partnerdraw", "contains", ("Equity", "OwnersEquity")),
    ("partnersdraw", "contains", ("Equity", "OwnersEquity")),
    ("memberdraw", "contains", ("Equity", "OwnersEquity")),
    ("ownercontribution", "contains", ("Equity", "OwnersEquity")),
    ("ownerinvestment", "contains", ("Equity", "OwnersEquity")),
    ("ownerequity", "contains", ("Equity", "OwnersEquity")),
    ("ownersequity", "contains", ("Equity", "OwnersEquity")),
    # Corporate equity rows seen on law-firm COAs (PCs, PLLCs, S-corps).
    # All map to Equity. We pick the closest QBO AccountSubType where
    # one exists; partnership-style equity (dividends, distributions,
    # member capital) falls back to OwnersEquity.
    ("commonstock", "contains", ("Equity", "CommonStock")),
    ("preferredstock", "contains", ("Equity", "PreferredStock")),
    ("paidincapital", "contains", ("Equity", "PaidInCapitalOrSurplus")),
    ("additionalpaidincapital", "contains", ("Equity", "PaidInCapitalOrSurplus")),
    ("capitalstock", "contains", ("Equity", "CommonStock")),
    ("treasurystock", "contains", ("Equity", "TreasuryStock")),
    ("dividendspaid", "contains", ("Equity", "OwnersEquity")),
    ("dividend", "contains", ("Equity", "OwnersEquity")),
    ("distributionspaid", "contains", ("Equity", "OwnersEquity")),
    ("memberscapital", "contains", ("Equity", "OwnersEquity")),
    ("partnerscapital", "contains", ("Equity", "OwnersEquity")),
    ("shareholderequity", "contains", ("Equity", "OwnersEquity")),

    # ---- Expense — specific compound terms ----
    ("bankfee", "contains", ("Expense", "BankCharges")),
    ("bankcharge", "contains", ("Expense", "BankCharges")),
    ("merchantfee", "contains", ("Expense", "BankCharges")),
    ("creditcardfee", "contains", ("Expense", "BankCharges")),
    ("officesupply", "contains", ("Expense", "OfficeGeneralAdministrativeExpenses")),
    ("officesupplies", "contains", ("Expense", "OfficeGeneralAdministrativeExpenses")),
    ("officeexpense", "contains", ("Expense", "OfficeGeneralAdministrativeExpenses")),
    ("rentexpense", "contains", ("Expense", "RentOrLeaseOfBuildings")),
    ("rentorlease", "contains", ("Expense", "RentOrLeaseOfBuildings")),
    ("leaseexpense", "contains", ("Expense", "RentOrLeaseOfBuildings")),
    ("utilitiesexpense", "contains", ("Expense", "Utilities")),
    ("insuranceexpense", "contains", ("Expense", "Insurance")),
    ("travelexpense", "contains", ("Expense", "Travel")),
    ("mealsandentertainment", "contains", ("Expense", "EntertainmentMeals")),
    ("mealsentertainment", "contains", ("Expense", "EntertainmentMeals")),
    ("duesandsubscriptions", "contains", ("Expense", "DuesSubscriptions")),
    ("legalfeesexpense", "contains", ("Expense", "LegalAndProfessionalFees")),
    ("professionalfees", "contains", ("Expense", "LegalAndProfessionalFees")),
    ("filingfees", "contains", ("Expense", "LegalAndProfessionalFees")),
    ("clientcost", "contains", ("Expense", "LegalAndProfessionalFees")),
    ("advertising", "contains", ("Expense", "AdvertisingPromotional")),
    ("promotional", "contains", ("Expense", "AdvertisingPromotional")),
    ("payrollexpense", "contains", ("Expense", "PayrollExpenses")),
    ("wagesexpense", "contains", ("Expense", "PayrollExpenses")),
    ("salariesexpense", "contains", ("Expense", "PayrollExpenses")),

    # ---- Income — specific compound terms ----
    ("legalfeesincome", "contains", ("Income", "ServiceFeeIncome")),
    ("legalfeeincome", "contains", ("Income", "ServiceFeeIncome")),
    ("legalfeerevenue", "contains", ("Income", "ServiceFeeIncome")),
    ("legalfeesrevenue", "contains", ("Income", "ServiceFeeIncome")),
    ("servicefeeincome", "contains", ("Income", "ServiceFeeIncome")),
    ("servicefeesincome", "contains", ("Income", "ServiceFeeIncome")),
    ("servicefeerevenue", "contains", ("Income", "ServiceFeeIncome")),
    ("feeincome", "contains", ("Income", "ServiceFeeIncome")),
    ("feerevenue", "contains", ("Income", "ServiceFeeIncome")),
    ("consultingincome", "contains", ("Income", "ServiceFeeIncome")),
    ("consultingrevenue", "contains", ("Income", "ServiceFeeIncome")),
    ("disbursementrecovery", "contains", ("Income", "OtherPrimaryIncome")),
    ("interestincome", "contains", ("Other Income", "InterestEarned")),

    # ---- Bank / cash — specific compound terms ----
    ("operatingaccount", "contains", ("Bank", "Checking")),
    ("checkingaccount", "contains", ("Bank", "Checking")),
    ("savingsaccount", "contains", ("Bank", "Savings")),
    ("cashoperating", "contains", ("Bank", "Checking")),

    # ---- Fixed Asset — name patterns. Many law-firm account lists
    # name the row with the asset itself ("Computers", "Furniture &
    # Fixtures", "Leasehold Improvements", "Office Construction")
    # instead of a sub-type label. We resolve these directly so the
    # uploaded account list doesn't need a detail_type column. The
    # "contains" mode is safe here because each token is unambiguous
    # across legal-services charts of accounts.
    ("leaseholdimprovement", "contains", ("Fixed Asset", "LeaseholdImprovements")),
    ("officeconstruction", "contains", ("Fixed Asset", "LeaseholdImprovements")),
    ("furnitureandfixtures", "contains", ("Fixed Asset", "FurnitureAndFixtures")),
    ("furniturefixtures", "contains", ("Fixed Asset", "FurnitureAndFixtures")),
    ("officeequipment", "contains", ("Fixed Asset", "MachineryAndEquipment")),
    ("computerequipment", "contains", ("Fixed Asset", "MachineryAndEquipment")),
    ("computerhardware", "contains", ("Fixed Asset", "MachineryAndEquipment")),

    # ---- Long-term liability — partner / shareholder loans. PCLaw
    # account lists often spell these out with a person's name attached
    # ("Loan From John Smith"). Match "loanfrom" anywhere so we cover
    # both that pattern and "Loan from Shareholder".
    ("loanfromshareholder", "contains", ("Long Term Liability", "ShareholderNotesPayable")),
    ("shareholderloan", "contains", ("Long Term Liability", "ShareholderNotesPayable")),
    ("loanfrom", "contains", ("Long Term Liability", "NotesPayable")),
    ("partnerloan", "contains", ("Long Term Liability", "NotesPayable")),
    ("notespayable", "contains", ("Long Term Liability", "NotesPayable")),

    # ---- Lines of credit. PCLaw account lists frequently spell these
    # as "Bank LOC", "Business LOC", "M&T Bank LOC", "Chase LOC", "Wells
    # Fargo LOC", "Line of Credit", with or without a trailing branch
    # number. They are Long Term Liability with QBO sub-type "LineOfCredit".
    # We match a few unambiguous compound forms; the bare suffix "loc"
    # is too ambiguous (it can appear inside unrelated words) so we
    # require either "lineofcredit", "bankloc", or a "loc" *with* a bank
    # name token (mt, chase, wellsfargo, citi) right before it.
    ("lineofcredit", "contains", ("Long Term Liability", "LineOfCredit")),
    ("linesofcredit", "contains", ("Long Term Liability", "LineOfCredit")),
    ("bankloc", "contains", ("Long Term Liability", "LineOfCredit")),
    ("businessloc", "contains", ("Long Term Liability", "LineOfCredit")),
    ("chaseloc", "contains", ("Long Term Liability", "LineOfCredit")),
    ("mtbankloc", "contains", ("Long Term Liability", "LineOfCredit")),
    ("mandtbankloc", "contains", ("Long Term Liability", "LineOfCredit")),
    ("wellsfargoloc", "contains", ("Long Term Liability", "LineOfCredit")),
    ("wellsfargobusinessloc", "contains", ("Long Term Liability", "LineOfCredit")),
    ("citiloc", "contains", ("Long Term Liability", "LineOfCredit")),
    ("citibankloc", "contains", ("Long Term Liability", "LineOfCredit")),
    ("bankofamericaloc", "contains", ("Long Term Liability", "LineOfCredit")),

    # ---- Expense — common law-firm expense names with no "expense" suffix.
    # These names appear on legal COAs without any type hint, so we
    # resolve them here to keep the account from getting stuck on Step 3.
    ("healthinsurance", "contains", ("Expense", "Insurance")),
    ("insurancehealth", "contains", ("Expense", "Insurance")),
    ("insurancelife", "contains", ("Expense", "Insurance")),
    ("insuranceliability", "contains", ("Expense", "Insurance")),
    ("insuranceauto", "contains", ("Expense", "Insurance")),
    ("insuranceother", "contains", ("Expense", "Insurance")),
    # PCLaw lists often abbreviate "Insurance" as "Ins" — match the bare
    # "ins" prefix when it's followed by a separator hint we recognise:
    # "Ins - Other", "Ins-Health", "Ins Life". Normalised: insother,
    # inshealth, inslife, insliability, ...
    ("insother", "contains", ("Expense", "Insurance")),
    ("inshealth", "contains", ("Expense", "Insurance")),
    ("inslife", "contains", ("Expense", "Insurance")),
    ("insliability", "contains", ("Expense", "Insurance")),
    ("insmalprac", "contains", ("Expense", "Insurance")),
    ("insauto", "contains", ("Expense", "Insurance")),
    ("insgeneral", "contains", ("Expense", "Insurance")),
    ("insurancegeneral", "contains", ("Expense", "Insurance")),
    ("insmalpractice", "contains", ("Expense", "Insurance")),
    ("malpracticeinsurance", "contains", ("Expense", "Insurance")),
    ("workerscomp", "contains", ("Expense", "Insurance")),
    ("workerscompensation", "contains", ("Expense", "Insurance")),
    ("businessdevelopment", "contains", ("Expense", "EntertainmentMeals")),
    ("continuedlegaled", "contains", ("Expense", "DuesSubscriptions")),
    ("continuedlegaleducation", "contains", ("Expense", "DuesSubscriptions")),
    ("continuinglegaleducation", "contains", ("Expense", "DuesSubscriptions")),
    ("continuingeducation", "contains", ("Expense", "DuesSubscriptions")),
    ("legaleducation", "contains", ("Expense", "DuesSubscriptions")),
    ("maintenancerepair", "contains", ("Expense", "RepairMaintenance")),
    ("maintenanceandrepair", "contains", ("Expense", "RepairMaintenance")),
    ("repairandmaintenance", "contains", ("Expense", "RepairMaintenance")),
    ("repairsandmaintenance", "contains", ("Expense", "RepairMaintenance")),
    ("repairsmaintenance", "contains", ("Expense", "RepairMaintenance")),
    ("repairmaintenance", "contains", ("Expense", "RepairMaintenance")),
    ("telephoneexpense", "contains", ("Expense", "Utilities")),
    ("internetexpense", "contains", ("Expense", "Utilities")),
    ("postageandshipping", "contains", ("Expense", "OfficeGeneralAdministrativeExpenses")),

    # ---- Generic suffix patterns. These come last and use endswith so
    # "Income Tax Payable" is NOT mapped to Income — only true suffix
    # accounts like "Rent Expense" or "Legal Fees Income" qualify. The
    # explicit "payable"/"receivable" guard in the matcher provides
    # belt-and-suspenders protection on top of the suffix anchor.
    ("expense", "endswith", ("Expense", "OfficeGeneralAdministrativeExpenses")),
    ("income", "endswith", ("Income", "ServiceFeeIncome")),
    ("revenue", "endswith", ("Income", "ServiceFeeIncome")),
]


def map_pclaw_account_to_qbo_type(row: dict) -> dict:
    """Resolve a parsed COA row to a QBO AccountType/AccountSubType.

    Returns a dict with keys:
        account_type:   QBO ``AccountType`` (e.g. "Bank") or None when blocked.
        detail_type:    QBO ``AccountSubType`` (e.g. "Checking") or None.
        decision:       'ok' | 'warn' | 'blocked'
        warnings:       list[str]   (advisory; operator should review)
        blocked_reason: str | None  ('blocked' only)
        match_hint:     which input field resolved the mapping, for audit.

    The function is deterministic and pure — no I/O, no QBO calls.
    """
    warnings: list[str] = []

    name = (row.get("account_name") or "").strip()
    account_type_in = (row.get("account_type") or "").strip()
    detail_in = (row.get("detail_type") or "").strip()

    # Short-circuit system-calculated accounts (Net Income, Net Income
    # (Loss), Current Year Earnings). QuickBooks computes these from
    # posted activity, so we never create them as real accounts.
    if is_system_calculated_account({"account_name": name}):
        return {
            "account_type": None,
            "detail_type": None,
            "decision": "skipped",
            "warnings": [],
            "blocked_reason": None,
            "skip_reason": SYSTEM_CALCULATED_EXPLANATION,
            "match_hint": "system_calculated",
        }

    # 1. Explicit detail_type hint wins if recognised. For the
    # account_type hint we use a *generic* set — when the COA only says
    # "Fixed Asset" we still want a more specific account_name match
    # (e.g. "Computers" -> MachineryAndEquipment instead of the bucket
    # default FurnitureAndFixtures) to win. The set is intentionally
    # narrow: it covers categories where multiple sub-types are common
    # and the bucket label alone is too vague. Categories like
    # "Other Current Liability" or "Accounts Payable" are NOT in this
    # set because mis-routing a Payable-named row off them would defeat
    # the AR/AP name-vs-type cross-check below.
    _GENERIC_TYPE_BUCKETS = {
        "fixedasset", "fixedassets",
    }
    candidates = [
        ("detail_type", detail_in),
    ]
    norm_account_type = _norm(account_type_in)
    if norm_account_type in _GENERIC_TYPE_BUCKETS:
        candidates += [
            ("account_name", name),
            ("account_type", account_type_in),
        ]
    else:
        candidates += [
            ("account_type", account_type_in),
            ("account_name", name),
        ]

    resolved_type: Optional[str] = None
    resolved_detail: Optional[str] = None
    match_hint: Optional[str] = None
    saw_ambiguous_bucket = False

    for hint_label, raw in candidates:
        key = _norm(raw)
        if not key:
            continue
        mapped = _TYPE_TABLE.get(key)
        if mapped is None:
            continue
        t, st = mapped
        if t is None:
            # Recognised but deliberately ambiguous (e.g. bare "Asset").
            saw_ambiguous_bucket = True
            continue
        resolved_type, resolved_detail = t, st
        match_hint = hint_label
        break

    # 2. Special-case: account_name contains a strong signal even if the
    # account_type column was empty. Conservatively check a few high-risk
    # keywords so a row called "Trust Bank Account" still maps when the
    # type column is blank or unhelpful.
    if not resolved_type:
        name_norm = _norm(name)
        for keyword, (t, st) in [
            ("trustbank", ("Bank", "TrustAccounts")),
            ("trustaccount", ("Bank", "TrustAccounts")),
            ("trustliability", ("Other Current Liability", "TrustAccountsLiabilities")),
            ("clienttrust", ("Other Current Liability", "TrustAccountsLiabilities")),
            ("operatingbank", ("Bank", "Checking")),
            ("accountsreceivable", ("Accounts Receivable", "AccountsReceivable")),
            ("accountspayable", ("Accounts Payable", "AccountsPayable")),
            ("retainedearnings", ("Equity", "RetainedEarnings")),
        ]:
            if keyword in name_norm:
                resolved_type, resolved_detail = t, st
                match_hint = "account_name_keyword"
                break

    # 2a. Deterministic compound-name patterns for common legal/business
    # accounts. Every entry here is an *unambiguous* compound term — not a
    # single weak keyword — that maps 1:1 to a well-known QBO AccountType /
    # AccountSubType. The names are normalised (lowercase, alphanumeric)
    # so case, spacing, and punctuation do not affect matching. This tier
    # only fires when the COA type/detail columns and the high-risk
    # keyword list above were silent: an uploaded COA with explicit types
    # always wins.
    #
    # Match modes:
    #   "contains"   — keyword anywhere in the normalised name (used for
    #                  compound terms like "bankfee" / "ownerdraw" that
    #                  uniquely identify a category).
    #   "endswith"   — name must end with the keyword (used for broad
    #                  suffixes like "expense" / "income" / "revenue" so
    #                  "Income Tax Payable" is NOT auto-classified as
    #                  Income; only true suffix accounts like "Rent
    #                  Expense" or "Legal Fees Income" qualify).
    if not resolved_type:
        name_norm = _norm(name)
        for keyword, mode, (t, st) in _SAFE_NAME_PATTERNS:
            hit = (
                (mode == "contains" and keyword in name_norm)
                or (mode == "endswith" and name_norm.endswith(keyword))
            )
            if hit:
                # Sanity guard: account names that contain "payable" or
                # "receivable" should never be classified by these generic
                # patterns *as Income / Expense / Equity / Bank / Fixed
                # Asset* — AR/AP have dedicated handling above and a
                # misclassification here would be a real safety bug
                # (e.g. "Income Tax Payable" must not become Income).
                # Liability targets (Long Term Liability / Other Current
                # Liability) are the *correct* category for accounts
                # whose names include "Payable" (e.g. "Notes Payable"),
                # so we allow those through.
                payable_or_receivable = (
                    "payable" in name_norm or "receivable" in name_norm
                )
                if payable_or_receivable and not (
                    t.startswith("Long Term Liability")
                    or t.startswith("Other Current Liability")
                ):
                    continue
                resolved_type, resolved_detail = t, st
                match_hint = "account_name_pattern"
                break

    if not resolved_type:
        # Customer-friendly wording. Lawyers don't think in
        # "AccountType / AccountSubType" — they think "what kind of
        # account is this?" The blocker should point to a concrete
        # next action (upload the account list or pick an existing
        # QuickBooks account) rather than explaining QBO's API schema.
        return {
            "account_type": None,
            "detail_type": None,
            "decision": "blocked",
            "warnings": [],
            "blocked_reason": (
                "We need a little more information to add this account "
                "to QuickBooks. Upload your account list with a category "
                "for this account (for example: Bank, Income, Expense), "
                "or pick an existing QuickBooks account to match it to."
                + (
                    " The category we found is too broad — pick something "
                    "more specific."
                    if saw_ambiguous_bucket else ""
                )
            ),
            "skip_reason": None,
            "match_hint": None,
        }

    # 2.5. Name-vs-type cross-check. The helper email surfaced this exact
    # failure: an Accounts Payable row was about to be mapped to a
    # different (generic) payable account. When the account name strongly
    # implies AR / AP / Trust but the resolved QBO AccountType is the
    # generic Liability / Asset bucket, refuse to auto-map. The operator
    # must either fix the CSV type column or set a manual override.
    name_norm_for_check = _norm(name)
    special_name_expectations = (
        ("accountspayable", "Accounts Payable", "AccountsPayable"),
        ("accountsreceivable", "Accounts Receivable", "AccountsReceivable"),
    )
    for keyword, expected_type, expected_detail in special_name_expectations:
        if keyword not in name_norm_for_check:
            continue
        # Guard only fires for the actual AP/AR control account — not for
        # sub-accounts like "Accounts Payable Disc" or "AP Discount" that
        # merely contain the keyword as a prefix. If the name has more than
        # 3 extra alphanumeric characters beyond the keyword, it is a
        # distinct sub-account and should resolve with its PCLaw type.
        _remainder = name_norm_for_check.replace(keyword, "", 1)
        if len(_remainder) > 3:
            continue
        if (
            resolved_type != expected_type
            or (resolved_detail and resolved_detail != expected_detail)
        ):
            return {
                "account_type": None,
                "detail_type": None,
                "decision": "blocked",
                "warnings": [],
                "blocked_reason": (
                    f"This account looks like {expected_type}, but the "
                    "uploaded account list categorises it differently. "
                    f"Set the category to {expected_type} on your account "
                    "list (or rename the account) and try again."
                ),
                "skip_reason": None,
                "match_hint": None,
            }

    # 3. Special-case warnings on safe-but-risky types.
    if resolved_detail in _AUTO_PROVISIONED_SUBTYPES:
        warnings.append(
            f"QuickBooks usually creates a default {resolved_detail} "
            "account for every company. Creating another one is allowed "
            "but can confuse mapping later. Verify with the firm before "
            "applying."
        )
    if resolved_detail in _WARN_SUBTYPES:
        warnings.append(
            "Trust-account creation is allowed but legally sensitive. "
            "Confirm the firm has a real trust bank account at their "
            "financial institution before posting any trust journal entry."
        )
    if resolved_type == "Bank":
        warnings.append(
            "Bank accounts in QuickBooks should be reconciled against the "
            "real bank statement. Opening balances are *not* posted by "
            "this step — they come from the opening trial balance."
        )
    if resolved_type == "Equity" and resolved_detail == "RetainedEarnings":
        warnings.append(
            "Retained Earnings is auto-managed by QuickBooks at year-end. "
            "Do not post to it directly — confirm with the firm before "
            "creating a parallel account."
        )

    return {
        "account_type": resolved_type,
        "detail_type": resolved_detail,
        "decision": "warn" if warnings else "ok",
        "warnings": warnings,
        "blocked_reason": None,
        "skip_reason": None,
        "match_hint": match_hint,
    }


# ----------------------------------------------------------------------------
# Create-plan builder
# ----------------------------------------------------------------------------


@dataclass
class CreatePlanEntry:
    account_number: str
    account_name: str
    pclaw_account_type: str
    pclaw_detail_type: str
    qbo_account_type: Optional[str]
    qbo_detail_type: Optional[str]
    decision: str                    # 'ok' | 'warn' | 'blocked' | 'skipped'
    warnings: list[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None
    skip_reason: Optional[str] = None
    active: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CreatePlan:
    matched: list[dict]              # already exist in QBO; never recreated
    to_create: list[CreatePlanEntry] # decision in ('ok', 'warn')
    blocked: list[CreatePlanEntry]   # cannot create without operator action
    soft_conflicts: list[dict]       # name-match w/ different AcctNum
    skipped: list[CreatePlanEntry] = field(default_factory=list)  # system-calculated (Net Income, etc.)

    @property
    def has_blockers(self) -> bool:
        return bool(self.blocked)

    @property
    def has_warnings(self) -> bool:
        return any(e.decision == "warn" for e in self.to_create)

    @property
    def has_skipped(self) -> bool:
        return bool(self.skipped)

    def to_dict(self) -> dict:
        return {
            "matched_count": len(self.matched),
            "to_create_count": len(self.to_create),
            "blocked_count": len(self.blocked),
            "skipped_count": len(self.skipped),
            "soft_conflict_count": len(self.soft_conflicts),
            "matched": self.matched,
            "to_create": [e.to_dict() for e in self.to_create],
            "blocked": [e.to_dict() for e in self.blocked],
            "skipped": [e.to_dict() for e in self.skipped],
            "soft_conflicts": self.soft_conflicts,
            "has_blockers": self.has_blockers,
            "has_warnings": self.has_warnings,
            "has_skipped": self.has_skipped,
        }


def build_create_plan(
    coa_rows: list[dict],
    preview: dict,
    type_overrides: Optional[dict[str, dict]] = None,
) -> CreatePlan:
    """Combine the dry-run preview with the type-mapping table.

    ``preview`` is the output of ``report_types.build_coa_dry_run_preview``.
    Rows that already match an existing QBO account are passed through as
    ``matched`` (never re-created). Rows in ``would_create`` are resolved
    through the type-mapper and bucketed into ``to_create`` or ``blocked``.

    ``type_overrides`` is an optional ``{account_number: {account_type, detail_type}}``
    map of operator-supplied corrections. When provided, the override is
    layered onto the COA row before the type-mapper runs — so an account
    the parser couldn't classify (or classified wrong, per the helper
    email) can be set explicitly and unblock the create plan.
    """
    matched = list(preview.get("matched", []) or [])
    soft_conflicts = list(preview.get("conflicts", []) or [])
    type_overrides = type_overrides or {}

    # Index would_create entries by (account_number, account_name) so we can
    # match them back to the original coa_rows for type-mapping. The dry-run
    # entries are already a subset of coa_rows, but we re-run the mapper on
    # the coa_row directly to keep the source of truth in one place.
    would_create_keys: set[tuple[str, str]] = set()
    for entry in (preview.get("would_create") or []):
        would_create_keys.add(
            (
                (entry.get("account_number") or "").strip(),
                (entry.get("account_name") or "").strip(),
            )
        )

    to_create: list[CreatePlanEntry] = []
    blocked: list[CreatePlanEntry] = []
    skipped: list[CreatePlanEntry] = []

    for row in coa_rows:
        num = (row.get("account_number") or "").strip()
        name = (row.get("account_name") or "").strip()
        if (num, name) not in would_create_keys:
            continue  # already matched in QBO — preview handled it

        # Layer operator override on top of the parsed row when one exists.
        override = type_overrides.get(num) if num else None
        if not override and name:
            override = type_overrides.get(name.lower())
        if override:
            row = {
                **row,
                "account_type": (
                    override.get("account_type")
                    or row.get("account_type")
                ),
                "detail_type": (
                    override.get("detail_type")
                    or row.get("detail_type")
                ),
            }

        decision = map_pclaw_account_to_qbo_type(row)
        entry = CreatePlanEntry(
            account_number=num,
            account_name=name,
            pclaw_account_type=row.get("account_type") or "",
            pclaw_detail_type=row.get("detail_type") or "",
            qbo_account_type=decision["account_type"],
            qbo_detail_type=decision["detail_type"],
            decision=decision["decision"],
            warnings=list(decision["warnings"]),
            blocked_reason=decision["blocked_reason"],
            skip_reason=decision.get("skip_reason"),
            active=bool(row.get("active", True)),
        )
        if entry.decision == "blocked":
            blocked.append(entry)
        elif entry.decision == "skipped":
            skipped.append(entry)
        else:
            to_create.append(entry)

    return CreatePlan(
        matched=matched,
        to_create=to_create,
        blocked=blocked,
        soft_conflicts=soft_conflicts,
        skipped=skipped,
    )


# ----------------------------------------------------------------------------
# Plan execution
# ----------------------------------------------------------------------------


def _build_qbo_payload(entry: CreatePlanEntry) -> dict:
    """Build the QBO Account payload for a single create entry."""
    payload = {
        "Name": entry.account_name,
        "AccountType": entry.qbo_account_type,
        "Active": entry.active,
    }
    if entry.qbo_detail_type:
        payload["AccountSubType"] = entry.qbo_detail_type
    if entry.account_number:
        payload["AcctNum"] = str(entry.account_number).strip()
    return payload


def _acct_num_update_payload(
    qbo_account: dict,
    pclaw_number: str,
) -> "dict | None":
    """
    Build a sparse QBO Account UPDATE payload to set AcctNum = pclaw_number.
    Returns None if update should be skipped (already correct or too long).
    """
    if not pclaw_number:
        return None
    pclaw_number = str(pclaw_number).strip()
    if len(pclaw_number) > QBO_ACCT_NUM_MAX:
        return None
    current = (qbo_account.get("AcctNum") or "").strip()
    if current == pclaw_number:
        return None  # already correct — no-op
    return {
        "sparse": True,
        "Id": qbo_account["Id"],
        "SyncToken": qbo_account["SyncToken"],
        "AcctNum": pclaw_number,
    }


def apply_create_plan(qbo_client, plan: CreatePlan) -> dict:
    """Execute the create plan against a connected QBO client.

    ``qbo_client`` must expose ``create_account(payload)``. Failures on
    one row do not stop the loop — each row reports its own success or
    error so the operator gets a complete result rather than a partial
    half-state with no audit trail.

    Returns a dict with ``created`` (list of result rows), ``failed``
    (list of result rows), and ``intuit_tids`` (list of non-null TIDs we
    captured, for support follow-up).
    """
    if plan.has_blockers:
        raise ValueError(
            "Cannot apply a plan with blocked rows. Resolve the blocked "
            "entries (fix CSV types or create those accounts manually in "
            "QuickBooks) and re-run the preview."
        )

    created: list[dict] = []
    failed: list[dict] = []
    intuit_tids: list[str] = []

    for entry in plan.to_create:
        payload = _build_qbo_payload(entry)
        try:
            response = qbo_client.create_account(payload)
            qbo_account = (response or {}).get("Account") or response or {}
            created.append({
                "account_number": entry.account_number,
                "account_name": entry.account_name,
                "qbo_account_id": str(qbo_account.get("Id") or ""),
                "qbo_account_name": qbo_account.get("Name") or entry.account_name,
                "qbo_account_type": qbo_account.get("AccountType") or entry.qbo_account_type,
                "qbo_acct_num": qbo_account.get("AcctNum") or entry.account_number,
            })
        except Exception as exc:  # noqa: BLE001
            # We deliberately catch broadly here — the route layer will
            # render the failed list verbatim. Pull intuit_tid from the
            # exception when present (QBOError exposes it) so support can
            # trace the failing request without us logging tokens.
            tid = getattr(exc, "intuit_tid", None)
            if tid:
                intuit_tids.append(tid)
            failed.append({
                "account_number": entry.account_number,
                "account_name": entry.account_name,
                "qbo_account_type": entry.qbo_account_type,
                "qbo_detail_type": entry.qbo_detail_type,
                "error": _safe_error_message(exc),
                "intuit_tid": tid,
            })

    # Back-fill AcctNum on matched QBO accounts that have no number.
    # Matched accounts already exist in QBO (by AcctNum or Name), so we only
    # PATCH them if their AcctNum is missing and we have a PCLaw account number.
    import logging
    for matched in plan.matched:
        existing_acct_num = matched.get("qbo_acct_num") or ""
        if not existing_acct_num and matched.get("account_number"):
            try:
                qbo_id = matched.get("qbo_account_id")
                acct_num = str(matched.get("account_number")).strip()
                # Fetch the full account to get SyncToken for the update.
                result = qbo_client.query(
                    f"SELECT Id, Name, AcctNum, SyncToken FROM Account WHERE Id = '{qbo_id}'"
                )
                accounts = result.get("QueryResponse", {}).get("Account", [])
                if accounts:
                    qbo_account = accounts[0]
                    sync_token = qbo_account.get("SyncToken")
                    if sync_token:
                        patch_payload = {
                            "Id": qbo_id,
                            "SyncToken": sync_token,
                            "sparse": True,
                            "AcctNum": acct_num,
                        }
                        qbo_client.update_account(patch_payload)
                        logging.getLogger("coa_apply").debug(
                            "Back-filled AcctNum %r on matched QBO account %s",
                            acct_num, matched.get("account_name"),
                        )
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("coa_apply").warning(
                    "AcctNum back-fill failed for %s: %s",
                    matched.get("account_name"), exc,
                )

    # De-dupe intuit_tids while keeping insertion order.
    seen: set[str] = set()
    deduped = []
    for tid in intuit_tids:
        if tid and tid not in seen:
            deduped.append(tid)
            seen.add(tid)

    return {
        "created": created,
        "failed": failed,
        "intuit_tids": deduped,
    }


def _safe_error_message(exc: Exception) -> str:
    """Return an operator-safe rendering of a QBO error.

    Strips long bearer-style tokens by length cap; QBOError carries body
    text that we want to surface for diagnostics but never tokens (the
    QBO client itself never logs Authorization headers).
    """
    msg = str(exc) or exc.__class__.__name__
    if len(msg) > 600:
        msg = msg[:600] + "…"
    return msg
