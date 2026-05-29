"""Encoding-tolerant CSV decoding for PCLaw exports.

Real-world PCLaw CSVs come in two flavours:

  * UTF-8 (with or without a BOM) — what modern systems export.
  * Windows-1252 / Latin-1 — what PCLaw on legacy Windows still writes,
    typically when a description field contains an em-dash, curly quote,
    or accented character.

Default-locale UTF-8 readers silently replace bad bytes with U+FFFD ('?'),
which corrupts client/matter descriptions and trips up the bookkeeper
reviewing the import. That's the bug this module exists to fix.

The strategy is small and deterministic:

  1. Strip a UTF-8 BOM if present.
  2. Try UTF-8 strict. If it decodes, return the text.
  3. Try Windows-1252 strict. cp1252 is a strict superset of Latin-1 over
     the bytes PCLaw is likely to emit (em-dash 0x97, smart quotes 0x91-
     0x94, etc.) and round-trips Latin-1 for byte values that overlap.
  4. Try Latin-1 strict. Latin-1 decodes any byte sequence by definition,
     so this is the final fallback.

We do NOT use `errors='replace'` anywhere — that's the silent-corruption
behaviour we are explicitly trying to avoid. If a file is truly garbage
we raise; the caller surfaces a clear error rather than importing a
report full of '?' characters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Tuple

# Order matters: UTF-8 first (the modern correct encoding), then cp1252
# (what legacy Windows writes by default for PCLaw exports), then Latin-1
# (the universal fallback that decodes any byte sequence).
ENCODINGS_TRIED: Tuple[str, ...] = ("utf-8", "cp1252", "latin-1")


def _strip_bom(data: bytes) -> bytes:
    """Strip a leading UTF-8 BOM if present.

    PCLaw exports occasionally include a BOM. Stripping here means each
    attempted decoder sees the same bytes, and the BOM doesn't bias the
    detection toward UTF-8 success on an otherwise cp1252 file (the BOM
    is valid in both, but it's pure noise once consumed).
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:]
    return data


def decode_csv_bytes(data: bytes) -> Tuple[str, str]:
    """Decode raw CSV bytes using the encoding ladder.

    Returns ``(text, encoding_used)``. Raises ``UnicodeDecodeError`` only
    if every encoding in the ladder rejects the bytes — Latin-1 cannot
    actually reach that path because it decodes any 1-byte sequence, but
    we keep the raise for type clarity and future encoding changes.
    """
    stripped = _strip_bom(data)
    last_error: UnicodeDecodeError | None = None
    for enc in ENCODINGS_TRIED:
        try:
            return stripped.decode(enc), enc
        except UnicodeDecodeError as e:
            last_error = e
            continue
    # Latin-1 can decode any bytes, so we should never reach here in
    # practice. The raise is defensive for future maintainers who might
    # remove latin-1 from the ladder.
    raise last_error or UnicodeDecodeError(
        "utf-8", stripped, 0, len(stripped),
        "no encoding in csv_decode.ENCODINGS_TRIED could decode this file",
    )


def open_csv_text(path) -> Tuple[str, str]:
    """Read a file path as decoded CSV text using the encoding ladder.

    Returns ``(text, encoding_used)``. ``encoding_used`` is suitable for
    audit / debug logging so operators can see why a description like
    "Smith — initial retainer" round-trips correctly even when PCLaw
    wrote it in cp1252.
    """
    p = Path(path)
    return decode_csv_bytes(p.read_bytes())
