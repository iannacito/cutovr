"""Smoke tests for csv_decode.open_csv_text and integration with the
PCLaw parsers.

Run from project root:

    python3 tests/smoke_csv_decode.py

Covers:
  T1  UTF-8 with BOM decodes correctly.
  T2  Windows-1252 (cp1252) bytes containing an em-dash 0x97 decode
      to a real U+2014 EM DASH, not U+FFFD.
  T3  Latin-1 fallback decodes accented characters from a non-UTF-8 file.
  T4  pclaw_pipeline.load_general_ledger_csv reads cp1252-encoded files
      without producing replacement characters in the description column.
  T5  pclaw_parser.parse_pclaw_csv reads cp1252-encoded files cleanly.
  T6  report_types._open_csv (via parse_chart_of_accounts) reads
      cp1252-encoded reports cleanly.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from csv_decode import open_csv_text, decode_csv_bytes  # noqa: E402


def _write_bytes(data: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(data)
    return path


def t1_utf8_bom():
    bom = b"\xef\xbb\xbf"
    text, enc = decode_csv_bytes(bom + "name,amount\nSmith,100\n".encode("utf-8"))
    assert enc == "utf-8", enc
    assert text.startswith("name"), "BOM should be stripped"
    print("T1 OK: utf-8 with BOM decodes and strips BOM")


def t2_cp1252_em_dash():
    # 0x97 is an em-dash in cp1252 but is undefined as a continuation
    # byte in utf-8. Build the raw bytes directly so we can construct
    # exactly the byte sequence PCLaw legacy exports produce.
    raw = b"description\nSmith \x97 initial retainer\n"
    text, enc = decode_csv_bytes(raw)
    assert enc == "cp1252", f"expected cp1252 ladder hit, got {enc}"
    # The em-dash should round-trip as U+2014, NOT as U+FFFD.
    assert "—" in text, "cp1252 em-dash should decode to U+2014"
    assert "�" not in text, "decoder must not emit replacement characters"
    print("T2 OK: cp1252 0x97 decodes to a real em-dash")


def t3_latin1_fallback():
    # 0xE9 = é in latin-1 (also cp1252). Build raw bytes that are valid
    # latin-1 / cp1252 but invalid as utf-8 multi-byte starts.
    raw = b"description\nCaf\xe9 trust account\n"
    text, enc = decode_csv_bytes(raw)
    assert enc in ("cp1252", "latin-1"), f"unexpected ladder hit: {enc}"
    assert "Café" in text
    assert "�" not in text
    print("T3 OK: latin-1 fallback decodes accented chars")


def t4_pipeline_pclaw_gl_cp1252():
    from pclaw_pipeline import load_general_ledger_csv

    raw = (
        b"transaction_id,date,account_number,account_name,debit,credit,description\n"
        b"JE-1,2026-01-15,1000,Operating Bank,1000.00,0.00,Smith \x97 initial retainer\n"
        b"JE-1,2026-01-15,2000,Trust Liability,0.00,1000.00,Smith \x97 initial retainer\n"
    )
    path = _write_bytes(raw)
    try:
        rows = load_general_ledger_csv(path)
    finally:
        os.unlink(path)
    assert len(rows) == 2
    descs = [r.get("description") for r in rows]
    assert all("—" in d for d in descs), f"description em-dashes not decoded: {descs}"
    assert all("�" not in d for d in descs)
    print("T4 OK: pclaw_pipeline reads cp1252-encoded GL cleanly")


def t5_parser_pclaw_csv_cp1252():
    from pclaw_parser import parse_pclaw_csv

    raw = (
        b"Date,Account,Description,Debit,Credit\n"
        b"2026-01-15,Operating Bank,Caf\xe9 client retainer,1000.00,0.00\n"
    )
    path = _write_bytes(raw)
    try:
        rows = parse_pclaw_csv(path)
    finally:
        os.unlink(path)
    assert len(rows) == 1
    memo = rows[0].get("memo", "")
    assert "Café" in memo, f"latin-1/cp1252 accented char corrupted: {memo!r}"
    assert "�" not in memo
    print("T5 OK: pclaw_parser.parse_pclaw_csv handles cp1252")


def t6_report_types_open_csv_cp1252():
    from report_types import _open_csv

    raw = (
        b"Account Number,Account Name,Type\n"
        b"1000,Op\xe9rations Bank,Bank\n"
    )
    path = _write_bytes(raw)
    try:
        rows, fields = _open_csv(path)
    finally:
        os.unlink(path)
    assert len(rows) == 1
    name = rows[0].get("Account Name", "")
    assert "é" in name, f"report_types lost accented char: {name!r}"
    assert "�" not in name
    print("T6 OK: report_types._open_csv handles cp1252")


if __name__ == "__main__":
    t1_utf8_bom()
    t2_cp1252_em_dash()
    t3_latin1_fallback()
    t4_pipeline_pclaw_gl_cp1252()
    t5_parser_pclaw_csv_cp1252()
    t6_report_types_open_csv_cp1252()
    print("\nALL CSV-DECODE SMOKE TESTS PASSED")
