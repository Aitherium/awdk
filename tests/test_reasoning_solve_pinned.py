"""The vendored h30 core is pinned: a hand edit here, or drift in h30, fails loudly.

* ``test_vendored_files_match_the_manifest`` always runs: every vendored file hashes
  to ``PINNED_SOURCE[...]["vendored_sha256"]`` and carries the provenance header.
* ``test_vendored_equals_original_plus_header`` always runs: removing the header and
  the ``SEAM(adk)`` hunks is the only difference from the original the manifest
  names -- proven by re-hashing the reconstructed original.
* The two h30 tests run when ``$ADK_H30_DIR`` points at an h30 checkout (skip
  otherwise): the originals at the pinned sha still hash as recorded, and the
  checkout's working copy has not moved on (drift = re-vendor).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest
from adk.reasoning.solve._provenance import H30_PATH, H30_SHA, PINNED_SOURCE, SEAMED

VENDOR = Path(__file__).resolve().parents[1] / "adk" / "reasoning" / "solve" / "_vendor"
FILES = dict(PINNED_SOURCE["files"])  # type: ignore[arg-type]


def _h(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _h30_dir() -> Path:
    d = os.environ.get("ADK_H30_DIR", "")
    if not d or not (Path(d) / H30_PATH).is_dir():
        pytest.skip("set ADK_H30_DIR to an h30 checkout to check the originals")
    return Path(d)


def test_manifest_covers_every_vendored_module():
    on_disk = {p.name for p in VENDOR.glob("*.py") if p.name != "__init__.py"}
    assert on_disk == set(FILES), "vendored files and PINNED_SOURCE disagree"
    assert re.fullmatch(r"[0-9a-f]{40}", H30_SHA)
    assert set(SEAMED) <= set(FILES)


@pytest.mark.parametrize("name", sorted(FILES))
def test_vendored_files_match_the_manifest(name):
    data = (VENDOR / name).read_bytes()
    assert _h(data) == FILES[name]["vendored_sha256"], (
        "%s was edited in place; re-vendor from h30 and update _provenance.py" % name
    )
    first = data.replace(b"\r\n", b"\n").split(b"\n", 1)[0].decode("utf-8")
    assert first.startswith("# vendored from h30-repl-agent@%s:%s/%s" % (H30_SHA, H30_PATH, name))


def _unseam(text: str) -> str:
    """Undo the provenance header and the SEAM(adk) hunks (exact inverse of the vendoring)."""
    text = text.split("\n", 1)[1]
    # Line seams: an added line is marked ``# SEAM(adk) drop``; a statement that replaces a
    # bare ``pass`` (a logged swallow) is marked ``# SEAM(adk) pass``.
    text = re.sub(r"^[^\n]*# SEAM\(adk\) drop\n", "", text, flags=re.M)
    text = re.sub(r"^([ \t]*)[^\n]*# SEAM\(adk\) pass$", r"\1pass", text, flags=re.M)
    reps = [
        (
            """    def __init__(self, time_cap_s: float = 20.0, print_cap: int = 1500, *,
                 cancel_check: Optional[Callable[[], None]] = None) -> None:
        self.time_cap_s = float(time_cap_s)
        self.print_cap = int(print_cap)
        # SEAM(adk): called on every traced line of model code; it raises a
        # BaseException to stop the run (budget / cancel).  None = h30 behaviour.
        self.cancel_check = cancel_check
""",
            """    def __init__(self, time_cap_s: float = 20.0, print_cap: int = 1500) -> None:
        self.time_cap_s = float(time_cap_s)
        self.print_cap = int(print_cap)
""",
        ),
        (
            """        if self.cancel_check is not None and self._active and self._pause_depth == 0:
            self.cancel_check()  # SEAM(adk)
""",
            "",
        ),
        (
            (
                '                 hooks: Any = None, episode_id: str = "episode", *,\n'
                '                 sink: Optional[Callable[[Dict[str, Any]], None]] = None, '
                'governor: Any = None) -> None:\n'
                '        # SEAM(adk): ``sink(record)`` receives every log record; '
                '``governor.check()``\n'
                '        # runs at the head of each turn and on every traced line of model code.\n'
                '        # Both None = h30 behaviour.\n'
                '        self.sink = sink\n'
                '        self.governor = governor\n'
            ),
            """                 hooks: Any = None, episode_id: str = "episode") -> None:
""",
        ),
        (
            (
                '        self.sandbox = Sandbox(time_cap_s=cfg.turn_s, print_cap=cfg.print_cap,\n'
                '                               cancel_check=governor.check if governor is not '
                'None else None)  # SEAM(adk)\n'
            ),
            """        self.sandbox = Sandbox(time_cap_s=cfg.turn_s, print_cap=cfg.print_cap)
""",
        ),
        (
            """        if self.governor is not None:
            self.governor.check()  # SEAM(adk)
""",
            "",
        ),
        (
            """        if self.sink is not None:  # SEAM(adk)
            try:
                self.sink(dict(rec))
            except Exception:  # noqa: BLE001 - an event sink never fails an episode
                pass
""",
            "",
        ),
    ]
    for new_old in reps:
        seam, orig = new_old
        text = text.replace(seam, orig)
    return text


@pytest.mark.parametrize("name", sorted(FILES))
def test_vendored_equals_original_plus_header(name):
    text = (VENDOR / name).read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    restored = _unseam(text)
    assert ("SEAM(adk)" in restored) is False
    assert _h(restored.encode("utf-8")) == FILES[name]["h30_sha256"], (
        "%s differs from the h30 original by more than the header and the SEAM(adk) hunks" % name
    )
    assert (name in SEAMED) == (restored != text.split("\n", 1)[1])


@pytest.mark.parametrize("name", sorted(FILES))
def test_h30_originals_at_the_pinned_sha_hash_as_recorded(name):
    d = _h30_dir()
    r = subprocess.run(
        ["git", "-C", str(d), "show", "%s:%s/%s" % (H30_SHA, H30_PATH, name)],
        capture_output=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert _h(r.stdout) == FILES[name]["h30_sha256"]


def test_h30_checkout_has_not_drifted_from_the_pin():
    d = _h30_dir()
    drifted = [
        n for n in sorted(FILES) if _h((d / H30_PATH / n).read_bytes()) != FILES[n]["h30_sha256"]
    ]
    new = sorted({p.name for p in (d / H30_PATH).glob("*.py")} - set(FILES) - {"__init__.py"})
    assert not drifted and not new, (
        "h30 core moved since %s (changed: %s, new: %s): re-vendor at the new sha"
        % (H30_SHA[:10], drifted, new)
    )
