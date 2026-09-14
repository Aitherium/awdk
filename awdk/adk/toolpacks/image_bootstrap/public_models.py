"""Resolve a ComfyUI model profile to PUBLIC download URLs, with no monorepo.

WHY THIS EXISTS. `image_bootstrap` resolved its model list by importing
`lib.compute.comfyui_models` -- a MONOREPO module that is not shipped in awdk. Off
the fleet that import fails, `_resolve_model_downloads` returns an empty array, and
the tool says so honestly:

    "the container will start with NO models"

So `python -m adk.toolpacks.image_bootstrap setup` -- the one command that exists to
make image generation self-service -- installed ComfyUI with **zero models** for
every stranger who ran it. The automation was real; its model plane reached nobody.
That is the gate-1i UNSHIPPED IMPORT class: not a disclosure, a BROKEN tool that
reads as authoritative.

CLIENT, NOT LIFT (aw-family.md). The fleet resolver is 603 lines and needs boto3,
the AitherSecrets vault, MinIO presigning and Strata. A stranger needs NONE of it --
they need public URLs. So this ships the PUBLIC half only: read the profile, build
HuggingFace and CivitAI URLs, and be explicit about anything with no public source.
Lifting the whole thing would have produced a package that ModuleNotFoundErrors on
someone else's machine, which is worse than an absent one.

WHAT IS DELIBERATELY NOT HIDDEN. An entry with no public source is REPORTED, never
dropped. Measured 2026-08-24 on the `studio` profile: 25 catalogue entries, sources
10 hf / 7 strata / 5 minio / 3 civitai -- and most of the private ones carry a
`public_ref` naming their upstream HF origin, so they resolve publicly anyway.
`detail_tweaker_xl` does not, and a resolver that silently omitted it would produce
a ComfyUI install missing a LoRA nobody could name. The caller gets both lists.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    import yaml
except ImportError:                                   # pragma: no cover
    yaml = None                                       # type: ignore[assignment]

_HF = "https://huggingface.co"
_CIVITAI = "https://civitai.com/api/download/models"

#: Shipped beside this module. The fleet keeps the authority at
#: `AitherOS/config/comfyui-model-profiles.yaml`; this is a copy that travels with
#: the wheel, kept in step by `check_model_profile_parity.py`. A copy is required,
#: not preferred: a stranger's install has no AitherOS checkout to read.
PROFILE_FILE = Path(__file__).with_name("comfyui-model-profiles.yaml")


class ProfileUnavailableError(Exception):
    """The profile data could not be read at all -- never treated as 'no models'."""


def _load(path: Path | None = None) -> Dict[str, Any]:
    p = path or PROFILE_FILE
    if yaml is None:
        raise ProfileUnavailableError(
            "PyYAML is not installed, so the model profile cannot be read. "
            "An empty model list and an unreadable one look identical to the "
            "container, so this refuses rather than returning nothing.")
    if not p.is_file():
        raise ProfileUnavailableError(f"model profile not shipped at {p}")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not doc.get("catalog") or not doc.get("profiles"):
        raise ProfileUnavailableError(f"{p} has no catalog/profiles")
    return doc


def list_profiles(path: Path | None = None) -> List[str]:
    return sorted((_load(path).get("profiles") or {}).keys())


def _hf_url(ref: str) -> str:
    """`repo::file` -> a resolve URL; a bare repo -> the repo page.

    The `::` split is the fleet's own convention and is reproduced exactly; a file
    with no `::` is a whole-repo reference, which the ComfyUI entrypoint handles.
    """
    if "::" in ref:
        repo, _, fname = ref.partition("::")
        return f"{_HF}/{repo.strip('/')}/resolve/main/{fname.lstrip('/')}"
    return f"{_HF}/{ref.strip('/')}"


def _civitai_url(ref: str) -> str:
    """CivitAI version id -> a download URL.

    A token is appended when CIVITAI_TOKEN is set. Without one CivitAI still serves
    many models and 401s on the gated ones -- which the entrypoint reports per file,
    so a missing token degrades one download rather than the whole install.

    🚨 THE RETURNED URL IS CREDENTIAL-BEARING. Never print or log it: use `redact()`.
    Learned the hard way 2026-08-24 -- a debug print of the resolved list put a live
    CIVITAI_TOKEN into a session transcript. The caller already treats these as
    secrets (they go to a 0600 env file and are popped before any dict is returned);
    the hole was DISPLAY, which no amount of care at the storage end closes.
    """
    token = os.environ.get("CIVITAI_TOKEN", "").strip()
    base = f"{_CIVITAI}/{str(ref).strip()}"
    return f"{base}?token={token}" if token else base


def redact(url: str) -> str:
    """A URL safe to show a human. Use this for EVERY display of a resolved URL.

    Redacts the whole query string rather than a named parameter: `token` is what we
    add today, and a presigned S3 URL carries its credential across several
    parameters (X-Amz-Credential, X-Amz-Signature). A rule keyed on one name would
    keep passing while leaking the next kind.
    """
    head, sep, _ = str(url).partition("?")
    return head + ("?<redacted>" if sep else "")


def public_url(entry: Dict[str, Any]) -> str | None:
    """The public URL for one catalogue entry, or None if it has no public source.

    ORDER MATTERS. `public_ref` wins even when `source` is private, because it exists
    precisely to name the upstream origin of something we also mirror -- e.g.
    juggernaut_xl_v9 is `source: strata` (fast, internal) AND carries an HF
    `public_ref`, so a stranger gets it from HuggingFace while the fleet gets it from
    Strata. Reading `source` first would call that model unavailable.
    """
    pub = entry.get("public_ref")
    if pub:
        # A BARE NUMBER is a CivitAI version id, not an HF repo. Same convention the
        # catalogue already uses for `source: civitai` (ref is the id), so an entry
        # can record a CivitAI upstream without a second field. Without this,
        # detail_tweaker_xl -- whose own note says "upstream civitai 135867" -- had
        # nowhere to put that, and resolved to nothing.
        text = str(pub).strip()
        return _civitai_url(text) if text.isdigit() else _hf_url(text)
    src = str(entry.get("source") or "").lower()
    ref = str(entry.get("ref") or "")
    if not ref:
        return None
    if src == "hf":
        return _hf_url(ref)
    if src == "civitai":
        return _civitai_url(ref)
    # strata:// and minio:// are fleet-internal with no recorded public origin.
    return None


def to_downloads(
    profile: str = "studio",
    *,
    include_optional: bool = True,
    path: Path | None = None,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """(downloads, unavailable) for `profile`, using PUBLIC sources only.

    `downloads` is the `AITHER_MODEL_DOWNLOADS` array the ComfyUI entrypoint
    consumes: `[{"url": ..., "dest": ...}]`.

    `unavailable` names every entry with no public source, with its reason. It is a
    RETURN VALUE rather than a log line because the caller has to be able to tell a
    complete install from a partial one -- a silently short list is how "installed
    ComfyUI" becomes "installed ComfyUI that generates nothing", and the whole reason
    this module exists.
    """
    doc = _load(path)
    catalog = doc["catalog"]
    prof = (doc["profiles"] or {}).get(profile)
    if prof is None:
        raise ProfileUnavailableError(
            f"unknown profile {profile!r}; have {sorted(doc['profiles'])}")
    names = prof.get("models") if isinstance(prof, dict) else prof

    downloads: List[Dict[str, str]] = []
    unavailable: List[Dict[str, str]] = []
    for name in names or []:
        entry = catalog.get(name)
        if not entry:
            unavailable.append({"model": name, "reason": "not in the catalogue"})
            continue
        if not include_optional and not entry.get("required", False):
            continue
        url = public_url(entry)
        if not url:
            unavailable.append({
                "model": name,
                "reason": f"source '{entry.get('source')}' is fleet-internal and the "
                          f"entry records no public_ref",
                "file": str(entry.get("name") or ""),
            })
            continue
        downloads.append({"url": url, "dest": str(entry.get("dest") or "checkpoints"),
                          "name": str(entry.get("name") or "")})
    return downloads, unavailable


def self_test() -> int:
    """Prove it resolves, and that it does NOT quietly drop what it cannot reach."""
    fails: List[str] = []
    try:
        dl, un = to_downloads("studio")
    except ProfileUnavailableError as e:
        print(f"SELF-TEST FAIL: profile not shipped/readable: {e}")
        return 1

    if not dl:
        fails.append("resolved ZERO downloads — an empty list is the exact failure "
                     "this module was written to end")
    if any("token=" in redact(d["url"]) for d in dl):
        fails.append("redact() left a token visible")
    if any(not d["url"].startswith("https://") for d in dl):
        fails.append("a resolved URL is not https")
    for bad in ("aitheros-", "aither://", ":9000"):
        hit = [d["url"] for d in dl if bad in d["url"]]
        if hit:
            fails.append(f"a fleet-internal URL leaked into the public list: {hit[0]}")

    # public_ref must beat a private `source` -- juggernaut is strata + public_ref.
    j = public_url({"source": "strata", "ref": "aither://x", "public_ref": "R/M::f.safetensors"})
    if not (j or "").startswith("https://huggingface.co/"):
        fails.append("public_ref did not override a private source")
    # ...and a private source with NO public_ref must return None, not a guess.
    if public_url({"source": "strata", "ref": "aither://x"}) is not None:
        fails.append("a fleet-internal entry with no public_ref was given a URL anyway")
    if public_url({"source": "hf", "ref": "repo::f.bin"}) != f"{_HF}/repo/resolve/main/f.bin":
        fails.append("hf ref did not build the expected resolve URL")

    for f in fails:
        print(f"SELF-TEST FAIL: {f}")
    if fails:
        return 1
    print(f"self-test OK — {len(dl)} public download(s) resolved, "
          f"{len(un)} entry(ies) reported as having no public source "
          f"({', '.join(u['model'] for u in un) or 'none'})")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
