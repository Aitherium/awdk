"""quarantine_fetch: naming, type sniffing, and refusal of non-http URLs."""

import asyncio
import json

import pytest

from adk.quarantine import _safe_name, _sniff, quarantine_fetch


def test_safe_name_strips_paths_and_traversal():
    assert _safe_name("https://h/pub/tool.zip") == "tool.zip"
    traversal = _safe_name("https://h/a/..%2F..%2Fevil.exe")
    assert "/" not in traversal and "\\" not in traversal
    assert traversal.endswith("evil.exe")
    assert _safe_name("https://h/x", 'attachment; filename="a b.zip"') == "a b.zip"
    assert _safe_name("https://h/") == "download.bin"


def test_sniff_reads_magic_bytes():
    assert _sniff(b"PK\x03\x04rest") == "zip"
    assert _sniff(b"MZ\x90\x00") == "windows-executable"
    assert _sniff(b"#! /bin/sh\n") == "script"
    assert _sniff(b"nothing known") == "unknown"


def test_non_http_url_is_refused():
    out = json.loads(asyncio.run(quarantine_fetch("file:///C:/Windows/System32/cmd.exe")))
    assert "error" in out and "http" in out["error"]
