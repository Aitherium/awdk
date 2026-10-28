"""quarantine_fetch -- download a file into quarantine and virus-scan it, never run it.

An agent asked to "download X and check it is safe" had web_search and web_fetch
(page TEXT) and nothing that lands a binary on disk or scans it, so it answered
"the tools are not available". This is that tool:

- streams the URL into ``~/.aither/quarantine/<stamp>/`` (``AITHER_QUARANTINE_DIR``),
  with a size cap, never into the working tree and never executed or unpacked;
- on Windows stamps the Mark-of-the-Web (``Zone.Identifier``, zone 3), so anything
  that later opens it treats it as internet content;
- scans with Microsoft Defender (``MpCmdRun -Scan -ScanType 3 -DisableRemediation``)
  or ClamAV (``clamscan``) when either is present, and says ``unscanned`` plainly
  when neither is -- an absent scanner is never reported as clean.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

_MAX_MB_DEFAULT = 500
_SCAN_TIMEOUT_S = 600
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _quarantine_root() -> Path:
    env = os.environ.get("AITHER_QUARANTINE_DIR", "").strip()
    return Path(env) if env else Path.home() / ".aither" / "quarantine"


def _safe_name(url: str, content_disposition: str = "") -> str:
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition or "", re.I)
    raw = unquote(m.group(1)) if m else unquote(Path(urlparse(url).path).name)
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", raw).strip(" .")[:120]
    return name or "download.bin"


def _sniff(head: bytes) -> str:
    sigs = [(b"MZ", "windows-executable"), (b"\x7fELF", "elf-executable"),
            (b"PK\x03\x04", "zip"), (b"%PDF", "pdf"), (b"\x1f\x8b", "gzip"),
            (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!", "rar"), (b"\x89PNG", "png"),
            (b"\xff\xd8\xff", "jpeg"), (b"#!", "script")]
    for sig, kind in sigs:
        if head.startswith(sig):
            return kind
    return "unknown"


def _defender_exe() -> str | None:
    if sys.platform != "win32":
        return None
    platform = sorted(glob.glob(
        r"C:\ProgramData\Microsoft\Windows Defender\Platform\*\MpCmdRun.exe"))
    if platform:
        return platform[-1]
    legacy = Path(os.environ.get("ProgramFiles", r"C:\Program Files"),
                  "Windows Defender", "MpCmdRun.exe")
    return str(legacy) if legacy.exists() else None


async def _run(argv: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        creationflags=_NO_WINDOW)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), _SCAN_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "scan timed out"
    return proc.returncode if proc.returncode is not None else -1, out.decode(
        "utf-8", "replace")


async def scan_file(path: Path) -> dict:
    """Scan one file. ``verdict`` is clean | threat | error | unscanned."""
    exe = _defender_exe()
    if exe:
        rc, out = await _run([exe, "-Scan", "-ScanType", "3", "-File", str(path),
                              "-DisableRemediation"])
        verdict = {0: "clean", 2: "threat"}.get(rc, "error")
        return {"scanner": "defender", "verdict": verdict, "exit_code": rc,
                "detail": out.strip()[-1500:]}
    clam = shutil.which("clamscan")
    if clam:
        rc, out = await _run([clam, "--no-summary", str(path)])
        verdict = {0: "clean", 1: "threat"}.get(rc, "error")
        return {"scanner": "clamav", "verdict": verdict, "exit_code": rc,
                "detail": out.strip()[-1500:]}
    return {"scanner": None, "verdict": "unscanned",
            "detail": "no antivirus found (Defender MpCmdRun or clamscan); NOT a clean result"}


def _mark_of_the_web(path: Path, url: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        with open(f"{path}:Zone.Identifier", "w", encoding="utf-8") as fh:
            fh.write(f"[ZoneTransfer]\nZoneId=3\nHostUrl={url}\n")
        return True
    except OSError:
        return False


async def quarantine_fetch(url: str, max_mb: int = _MAX_MB_DEFAULT) -> str:
    """Download a URL into quarantine and virus-scan it (never runs or unpacks it).

    Returns JSON: path, bytes, sha256, detected type and the scan verdict
    (clean / threat / error / unscanned).
    """
    import httpx

    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return json.dumps({"error": "url must be http(s)://host/..."})
    cap = max(1, int(max_mb)) * 1024 * 1024
    folder = _quarantine_root() / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256()
    size = 0
    head = b""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return json.dumps({"error": f"HTTP {resp.status_code}", "url": url})
                dest = folder / _safe_name(str(resp.url),
                                           resp.headers.get("content-disposition", ""))
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > cap:
                            fh.close()
                            dest.unlink(missing_ok=True)
                            return json.dumps({"error": f"larger than {max_mb} MB cap",
                                               "url": url})
                        if len(head) < 16:
                            head += chunk[:16 - len(head)]
                        sha.update(chunk)
                        fh.write(chunk)
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"download failed: {type(exc).__name__}: {exc}",
                           "url": url})
    motw = _mark_of_the_web(dest, url)
    if not dest.exists():
        # Real-time protection removed it on write: that IS a detection.
        scan = {"scanner": "realtime", "verdict": "threat",
                "detail": "file removed by real-time antivirus as soon as it was written"}
    else:
        scan = await scan_file(dest)
    return json.dumps({
        "path": str(dest), "bytes": size, "sha256": sha.hexdigest(),
        "type": _sniff(head), "mark_of_the_web": motw, "scan": scan,
        "note": "quarantined: not executed or extracted",
    })
