"""Fetch a catalogue model: resumable, size-checked against HEAD, sha256 when known.

WHY A PLAIN GET IS NOT A DOWNLOAD. Measured 2026-10-02 on weights.aitherium.com: HEAD
states the true Content-Length and ranges work, but a GET can END EARLY with HTTP 200, no
Content-Length and a clean end-of-body. The client sees success on a fragment; llama.cpp
then reports a "corrupt model", which reads as a bad quant rather than a short file. So:

1. The size is asked for FIRST (HEAD). With no stated size and none in the catalogue the
   URL is refused: bytes that cannot be checked are not downloaded.
2. Every request is a Range from the bytes already on disk, repeated until the file is
   whole. An attempt that adds nothing is a stall; a few stalls in a row give up.
3. The whole file is hashed when the catalogue carries a sha256, and only a file that
   passed is renamed into place. In-progress bytes live in ``<file>.part`` -- the same
   name the Bonsai installer uses, so either can finish what the other started.

The same rule set as the tenant kit's ``fetch_models.py`` and the installer's
``_download()``; stdlib only, because this runs before anything else is installed.
"""

from __future__ import annotations

import hashlib
import http.client
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional, Tuple

CHUNK = 4 * 1024 * 1024
# Cloudflare answers the default Python-urllib agent with 403 (error 1010).
UA = "awdk-models/1 (+https://aitherium.com)"
#: Consecutive attempts that add no bytes before giving up on a URL.
MAX_STALLS = 4
#: A hard stop. Generous: the mirror ends a multi-GB stream every couple of GB, so a
#: complete fetch legitimately needs many resumes. MAX_STALLS is the real stop condition.
MAX_ATTEMPTS = 400
TIMEOUT = 60.0

Say = Callable[[str], None]


class DownloadError(RuntimeError):
    """The file could not be fetched whole and verified."""


def _human(n: int) -> str:
    return f"{n / 1024 ** 3:.2f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


def head(url: str, timeout: float = TIMEOUT) -> Optional[int]:
    """The size the server states for ``url``, or None when it will not say."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length and int(length) > 0 else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _attempt(url: str, part: Path, expected: int, timeout: float,
             tick: Callable[[int], None]) -> Tuple[int, bool]:
    """One ranged request appended to ``part``. -> (bytes on disk, range honoured)."""
    have = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": UA}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    ranged = True
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        if have and status != 206:
            # The server ignored the Range and is sending byte 0 again. Appending would
            # glue a second header onto the fragment; restart, and report it so a server
            # that can never resume fails instead of looping.
            have, ranged = 0, False
        with part.open("ab" if have else "wb") as fh:
            while have < expected:
                block = resp.read(min(CHUNK, expected - have))
                if not block:
                    break
                fh.write(block)
                have += len(block)
                tick(have)
            if have == expected and resp.read(1):
                raise DownloadError(f"{url} sent more than the {expected} bytes it stated")
    return part.stat().st_size, ranged


def _fetch_one(url: str, part: Path, expected: int, say: Say, timeout: float) -> None:
    """Bring ``part`` to exactly ``expected`` bytes from ``url``, resuming as needed."""
    if part.exists() and part.stat().st_size > expected:
        # Larger than the file can be: stale, and resuming onto it yields garbage.
        part.unlink()
    stalls = attempts = 0
    seen = [-1]

    def tick(have: int) -> None:
        tenth = have * 10 // expected
        if tenth > seen[0]:
            seen[0] = tenth
            say(f"  {part.name[:-5]}  {tenth * 10:3d}%  {_human(have)} of {_human(expected)}")

    while True:
        before = part.stat().st_size if part.exists() else 0
        if before == expected:
            return
        try:
            got, ranged = _attempt(url, part, expected, timeout, tick)
        except urllib.error.HTTPError as exc:
            raise DownloadError(f"{url} answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            got, ranged = (part.stat().st_size if part.exists() else 0), True
            say(f"  connection dropped ({type(exc).__name__}); resuming")
        if got == expected:
            return
        attempts += 1
        stalls = 0 if got > before else stalls + 1
        if stalls >= MAX_STALLS or attempts >= MAX_ATTEMPTS:
            if not ranged:
                # Nothing on disk is resumable against this server; do not leave a
                # fragment for the next source to append to.
                part.unlink(missing_ok=True)
            raise DownloadError(
                f"{part.name}: stopped at {_human(got)} of {_human(expected)} after "
                f"{attempts} attempt(s), {stalls} with no progress"
                + ("" if not ranged else "; re-run to resume from here"))
        say(f"  stream ended early at {_human(got)} of {_human(expected)}; resuming")


def fetch(urls: List[str], dest: Path, *, size_bytes: Optional[int] = None,
          sha256: str = "", say: Say = print, timeout: float = TIMEOUT) -> Path:
    """Download one file from the first URL that yields it whole and verified.

    Args:
        urls: Sources for the SAME bytes, tried in order.
        dest: Final path. Written only by renaming a verified ``<dest>.part``.
        size_bytes: The catalogue's measured size, when it has one.
        sha256: The catalogue's digest, when it has one ("" = none recorded).
        say: Progress sink, one line per call.
        timeout: Socket timeout per request, seconds.

    Returns:
        ``dest``.

    Raises:
        DownloadError: No URL produced a file of the stated size and digest.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    errors: List[str] = []
    for url in urls:
        stated = head(url, timeout)
        if stated and size_bytes and stated != size_bytes:
            errors.append(f"{url}: states {stated} bytes, the catalogue says {size_bytes}")
            continue
        expected = stated or size_bytes
        if not expected:
            errors.append(f"{url}: would not state a size (HEAD) and the catalogue has "
                          "none; refusing bytes that cannot be checked")
            continue
        if dest.exists() and dest.stat().st_size == expected:
            if _digest_ok(dest, sha256, say):
                say(f"  {dest.name} is already here and verified ({_human(expected)})")
                return dest
            say(f"  {dest.name} on disk fails its sha256; fetching it again")
            dest.unlink()
        have = part.stat().st_size if part.exists() else 0
        free = shutil.disk_usage(dest.parent).free
        if expected - min(have, expected) > free:
            raise DownloadError(f"{dest.name} needs {_human(expected - have)} more and "
                                f"{dest.parent} has {_human(free)} free")
        say(f"  {dest.name}: {_human(expected)} from {url}"
            + (f" (resuming at {_human(have)})" if 0 < have < expected else ""))
        try:
            _fetch_one(url, part, expected, say, timeout)
        except DownloadError as exc:
            errors.append(str(exc))
            continue
        if not _digest_ok(part, sha256, say):
            part.unlink(missing_ok=True)
            errors.append(f"{url}: sha256 mismatch (expected {sha256[:16]}...); "
                          "the download was discarded")
            continue
        part.replace(dest)
        return dest
    raise DownloadError("; ".join(errors) or "no source URL")


def _digest_ok(path: Path, sha256: str, say: Say) -> bool:
    """True when ``path`` matches ``sha256``, or when there is no digest to check."""
    if not sha256:
        return True
    say(f"  verifying sha256 of {path.name} ...")
    return sha256_file(path) == sha256.lower()


def fetch_companions(model: dict, models_dir: Path, say: Say = print,
                     timeout: float = TIMEOUT) -> List[Path]:
    """Fetch the files that must sit beside a model (a voice's ``.onnx.json``).

    The catalogue pins each companion's size AND sha256 (the generator refuses one
    without), so every companion is verified exactly like the model itself.

    Raises:
        DownloadError: A companion could not be fetched whole and verified.
    """
    out = []
    for comp in model.get("companions") or []:
        out.append(fetch(list(comp["urls"]), Path(models_dir) / comp["file"],
                         size_bytes=comp.get("size_bytes"), sha256=str(comp["sha256"]),
                         say=say, timeout=timeout))
    return out


def fetch_model(model: dict, models_dir: Path, say: Say = print,
                timeout: float = TIMEOUT) -> Tuple[Path, bool]:
    """Fetch a catalogue entry into ``models_dir``. -> (path, sha256_verified).

    A single-file model is one ``fetch``. A model the mirror stores as release slices
    (``join``) is fetched slice by slice, each size-checked, then concatenated and the
    whole file checked against the catalogue's size and digest.

    Raises:
        DownloadError: Any slice or the joined file failed its check.
    """
    dest = Path(models_dir) / model["file"]
    digest = str(model.get("sha256") or "")
    size = model.get("size_bytes")
    if not model.get("join"):
        fetch(list(model["urls"]), dest, size_bytes=size, sha256=digest, say=say,
              timeout=timeout)
        fetch_companions(model, models_dir, say=say, timeout=timeout)
        return dest, bool(digest)
    if dest.exists() and (not size or dest.stat().st_size == size) \
            and (digest or size) and _digest_ok(dest, digest, say):
        say(f"  {dest.name} is already here")
        return dest, bool(digest)
    slices = []
    for i, url in enumerate(model["urls"]):
        slices.append(fetch([url], dest.with_name(f"{dest.name}.slice{i}"), say=say,
                            timeout=timeout))
    part = dest.with_name(dest.name + ".part")
    with part.open("wb") as out:
        for s in slices:
            with s.open("rb") as fh:
                shutil.copyfileobj(fh, out, CHUNK)
    joined = part.stat().st_size
    if size and joined != size:
        part.unlink()
        raise DownloadError(f"{dest.name}: joined {joined} bytes, the catalogue says {size}")
    if not _digest_ok(part, digest, say):
        part.unlink()
        raise DownloadError(f"{dest.name}: sha256 mismatch after joining its slices")
    part.replace(dest)
    for s in slices:
        s.unlink(missing_ok=True)
    return dest, bool(digest)
