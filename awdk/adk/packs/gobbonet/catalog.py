"""What you COULD have — the half a models folder cannot tell you.

`models.py` answers GobboNet's picker from GGUFs already on disk. That is the
whole of it: `available()` globs a directory. So a fresh install shows an empty
dropdown, and the honest reading of an empty dropdown is not "no models" — it is
"this machine has never been told what exists."

This module is the other half: a curated list of models that RUN, each with the
byte count the mirror actually serves, plus a resumable downloader and an escape
hatch for people who would rather go pick something themselves.

WHY A MIRROR AND NOT HUGGINGFACE DIRECTLY. HF is the right default for anyone
who can reach it, and `from_hf()` exists precisely so they can. But a gated repo
answers an anonymous client with 401, a corporate proxy blocks the host outright,
and neither failure looks like a failure — the download simply never starts.
The mirror is a flat, filename-addressed bucket over public release assets: one
URL shape, no auth, no repo/branch/resolve path to get wrong, and it stays
reachable when HF is not.

SIZES ARE MEASURED, NEVER ESTIMATED, AND THAT IS LOAD-BEARING. `size_bytes` is
what the mirror returned for that exact filename, and `download()` refuses any
transfer that does not land on it. A rounded or copied-from-elsewhere number is
worse than none: the client uses it to detect truncation, so a wrong one turns a
short read into a file that passes every check and then fails to load, hours
later, as "corrupt model". If you add an entry, ask the URL for its size and
paste what it says.

Pure stdlib. No pip dependency, no API key, nothing to install first.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

#: Flat, filename-addressed weight mirror. Range + CORS, no auth.
MIRROR_BASE = "https://weights.aitherium.com/"

#: HuggingFace direct-file URL shape, for `from_hf()`.
HF_RESOLVE = "https://huggingface.co/{repo}/resolve/{revision}/{filename}"

#: Sent on every request. Cloudflare bot-challenges a bare urllib agent and
#: answers 403 to everything, which would read as "the file is gone" on a host
#: that is serving it perfectly.
UA = "Mozilla/5.0 (compatible; adk-gobbonet)"

#: Where adk keeps GGUFs — the same directory `models.py` lists, so anything
#: downloaded here appears in the picker with no further wiring.
try:  # pragma: no cover - import shape differs when run as a script
    from adk.llamacpp_setup import MODELS_DIR
except Exception:  # noqa: BLE001 - adk internals absent in a bare checkout
    MODELS_DIR = Path.home() / ".aither" / "models"


@dataclass
class CatalogEntry:
    """One downloadable model.

    `size_bytes` is the mirror's own answer for `filename` (see the module
    docstring — it is the truncation detector, not a display value).
    `min_ram_gb` is what it takes to LOAD, roughly the file plus context
    overhead; a machine below it will thrash or be killed rather than run
    slowly.
    """

    filename: str
    label: str
    params_b: float
    size_bytes: int
    min_ram_gb: float
    family: str = "custom"
    note: str = ""
    #: Absent for mirror entries; set by `from_hf()` for a user's own pick.
    url: str = ""

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / (1024 ** 3), 2)

    def resolve_url(self) -> str:
        return self.url or (MIRROR_BASE + self.filename)

    def as_json(self) -> dict:
        out = asdict(self)
        out["size_gb"] = self.size_gb
        out["url"] = self.resolve_url()
        return out


#: The curated list. Every `size_bytes` below was read from the mirror with a
#: ranged request on 2026-08-20 — do not hand-edit one to make something pass.
#:
#: Spans 0.25 GB to 16.8 GB on purpose: the point of a curated list is that
#: SOME entry fits the machine reading it. A list whose smallest entry needs a
#: discrete GPU is a list that tells most visitors they are not welcome.
CATALOG: list[CatalogEntry] = [
    CatalogEntry(
        filename="Bonsai-1.7B-Q1_0.gguf", label="Bonsai 1.7B", params_b=1.7,
        size_bytes=248302272, min_ram_gb=2.0, family="bonsai",
        note="Runs on anything, including integrated graphics.",
    ),
    CatalogEntry(
        filename="Bonsai-4B-Q1_0.gguf", label="Bonsai 4B", params_b=4.0,
        size_bytes=572270624, min_ram_gb=3.0, family="bonsai",
        note="The default. Good answers, still small enough for a laptop.",
    ),
    CatalogEntry(
        filename="Bonsai-8B-Q1_0.gguf", label="Bonsai 8B", params_b=8.0,
        size_bytes=1158654496, min_ram_gb=5.0, family="bonsai",
    ),
    CatalogEntry(
        filename="Bonsai-27B-Q1_0.gguf", label="Bonsai 27B", params_b=27.0,
        size_bytes=3803452480, min_ram_gb=10.0, family="bonsai",
        note="Served stitched from parts; a plain ranged GET still works.",
    ),
    CatalogEntry(
        filename="aither-orchestrator-Q4_K_M.gguf", label="Orchestrator 8B (Q4_K_M)",
        params_b=8.0, size_bytes=5027783808, min_ram_gb=10.0, family="llama",
        note="Higher-fidelity quant. Tool-calling and routing.",
    ),
    CatalogEntry(
        filename="gemma4-12b-Q4_K_M.gguf", label="Gemma 4 12B (Q4_K_M)",
        params_b=12.0, size_bytes=7662533088, min_ram_gb=14.0, family="gemma",
    ),
    CatalogEntry(
        filename="qwen36-27b-Q4_K_M.gguf", label="Qwen 3.6 27B (Q4_K_M)",
        params_b=27.0, size_bytes=16817244384, min_ram_gb=28.0, family="qwen",
        note="Needs a workstation. Listed so large boxes are not sent to a 4B.",
    ),
]


def entries() -> list[CatalogEntry]:
    """The curated list, smallest first."""
    return sorted(CATALOG, key=lambda e: e.size_bytes)


def find(filename: str) -> Optional[CatalogEntry]:
    for e in CATALOG:
        if e.filename == filename:
            return e
    return None


def remote_size(url: str, timeout: int = 20) -> int:
    """Byte length of a remote file, from Content-Range on a 1-byte GET.

    A bare HEAD is unreliable across these hosts — a redirect to a CDN can drop
    Content-Length, and some object stores answer HEAD differently from GET.
    Asking for one byte and reading the total out of Content-Range works on
    every host in use here and costs one byte.
    """
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        cr = r.headers.get("Content-Range") or ""
    total = cr.rsplit("/", 1)[-1] if "/" in cr else ""
    if not total.isdigit():
        raise RuntimeError(
            f"host did not report a size for {url} (Content-Range: {cr!r})")
    return int(total)


def from_hf(repo: str, filename: str, revision: str = "main",
            size_bytes: int = 0, label: str = "") -> CatalogEntry:
    """An entry for any HuggingFace GGUF — the browse-it-yourself lane.

    The curated list is a starting point, not a fence. This resolves a repo +
    filename to a direct URL and, when `size_bytes` is not supplied, ASKS the
    host for it rather than guessing, so the same truncation check protects a
    user's own pick.

    A gated or private repo answers 401 here and raises, which is the point:
    the alternative is a download that quietly never starts.
    """
    url = HF_RESOLVE.format(repo=repo.strip("/"), revision=revision, filename=filename)
    if not size_bytes:
        size_bytes = remote_size(url)
    return CatalogEntry(
        filename=filename,
        label=label or f"{filename} ({repo})",
        params_b=0.0,
        size_bytes=size_bytes,
        # Unknown for an arbitrary file. Reported as 0 rather than invented —
        # `fits()` treats 0 as "cannot judge" instead of "fits everywhere".
        min_ram_gb=0.0,
        url=url,
    )


def fits(entry: CatalogEntry, ram_gb: float, vram_gb: float = 0.0) -> bool:
    """Whether this box can load it.

    The pool is the LARGER of VRAM and system RAM, because llama.cpp runs
    CPU-only perfectly well — sizing off VRAM alone would tell a 64 GB
    workstation with no discrete GPU that nothing fits.

    An entry with `min_ram_gb == 0` (an arbitrary HF pick) is NOT assumed to
    fit. There is no information, and answering True on no information is how a
    picker recommends something that gets the process killed.
    """
    if entry.min_ram_gb <= 0:
        return False
    return max(ram_gb, vram_gb) >= entry.min_ram_gb


def recommended(ram_gb: float = 0.0, vram_gb: float = 0.0) -> Optional[CatalogEntry]:
    """The largest curated entry this box can load, or None.

    None is a real answer and is returned rather than falling back to the
    smallest: a probe that could not read memory reports 0.0, and 0.0 must not
    silently become "recommend the tiny one" — that is indistinguishable from a
    genuine recommendation and hides a broken probe.
    """
    if max(ram_gb, vram_gb) <= 0:
        return None
    ok = [e for e in entries() if fits(e, ram_gb, vram_gb)]
    return ok[-1] if ok else None


def detect_and_recommend() -> tuple[Optional[CatalogEntry], dict]:
    """`recommended()` against this machine's own probe.

    Returns (entry, probe) so a caller can SHOW what was measured. A bare
    recommendation with no numbers behind it cannot be argued with, and the
    memory probe has been wrong before.
    """
    try:
        from adk.llamacpp_setup import detect_accel
    except Exception:  # noqa: BLE001 - bare checkout
        return None, {"error": "accelerator probe unavailable"}
    a = detect_accel()
    probe = {"kind": a.kind, "name": a.name,
             "ram_gb": round(a.ram_gb, 1), "vram_gb": round(a.vram_gb, 1)}
    return recommended(a.ram_gb, a.vram_gb), probe


def refresh_from(url: str, timeout: int = 20) -> list[CatalogEntry]:
    """Replace the in-process catalog from a remote JSON list.

    The shipped list is versioned with the package, so a stale adk shows stale
    models. This is the seam for a list that updates without a release — and it
    REPLACES rather than merges, so an entry withdrawn upstream really goes away
    instead of lingering forever because nothing removes it.

    A malformed document raises and the previous catalog is left INTACT: a
    partial apply would leave the picker in a state no version ever shipped.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        doc = json.loads(r.read().decode("utf-8"))
    raw = doc.get("models") if isinstance(doc, dict) else doc
    if not isinstance(raw, list) or not raw:
        raise ValueError("catalog document has no models")
    parsed: list[CatalogEntry] = []
    for item in raw:
        missing = [k for k in ("filename", "label", "size_bytes") if not item.get(k)]
        if missing:
            raise ValueError(f"catalog entry missing {missing}: {item!r}")
        parsed.append(CatalogEntry(
            filename=item["filename"], label=item["label"],
            params_b=float(item.get("params_b") or 0),
            size_bytes=int(item["size_bytes"]),
            min_ram_gb=float(item.get("min_ram_gb") or 0),
            family=item.get("family") or "custom",
            note=item.get("note") or "", url=item.get("url") or "",
        ))
    CATALOG[:] = parsed
    return parsed


class SizeMismatchError(RuntimeError):
    """The transfer did not land on the declared size — refuse it."""


def download(entry: CatalogEntry, dest_dir: Optional[Path] = None,
             progress: Optional[Callable[[int, int], None]] = None,
             chunk: int = 1 << 20, timeout: int = 60) -> Path:
    """Fetch a model, resumably, and refuse anything that is not the right size.

    RESUMABLE IS NOT A LUXURY HERE. These are 0.25-16.8 GB over whatever
    connection the user has; a transfer that restarts from zero on every blip
    never finishes on a home line, and the failure looks like the mirror being
    slow rather than like progress being discarded. Partial bytes land in
    `<name>.part` and the next call asks for the remainder with a Range header.

    THE SIZE CHECK IS THE WHOLE POINT OF THE CATALOG CARRYING BYTES. A truncated
    GGUF is not a clean failure — it loads far enough to look real and then dies
    in the loader, hours after the download, naming the model rather than the
    transfer. So a finished file that is not exactly `size_bytes` is deleted and
    raises, and the caller finds out now instead of then.
    """
    dest_dir = Path(dest_dir or MODELS_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / entry.filename
    part = dest_dir / (entry.filename + ".part")

    if final.exists() and final.stat().st_size == entry.size_bytes:
        if progress:
            progress(entry.size_bytes, entry.size_bytes)
        return final
    if final.exists():
        # Present but the wrong length: a previous truncated or drifted copy.
        # Removed rather than resumed — resuming onto it would append to
        # garbage and produce a file of exactly the right size that is wrong.
        final.unlink()

    have = part.stat().st_size if part.exists() else 0
    if have > entry.size_bytes:
        part.unlink()
        have = 0

    url = entry.resolve_url()
    headers = {"User-Agent": UA}
    if have:
        headers["Range"] = f"bytes={have}-"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if have and r.status != 206:
                # The host ignored Range and is about to send the whole file.
                # Restart cleanly rather than appending a second full copy onto
                # the partial one, which yields an oversized file that no size
                # check can repair and no loader can read.
                have = 0
                part.unlink(missing_ok=True)
            mode = "ab" if have else "wb"
            with open(part, mode) as f:
                while True:
                    buf = r.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    have += len(buf)
                    if progress:
                        progress(have, entry.size_bytes)
    except urllib.error.HTTPError as e:
        hint = " The repo may be gated — try the mirror." if e.code in (401, 403) else ""
        raise RuntimeError(
            f"{entry.filename}: host answered HTTP {e.code}.{hint}") from e

    got = part.stat().st_size
    if got != entry.size_bytes:
        part.unlink(missing_ok=True)
        raise SizeMismatchError(
            f"{entry.filename}: got {got} bytes, expected {entry.size_bytes}. "
            "Nothing was installed.")
    os.replace(part, final)
    return final
