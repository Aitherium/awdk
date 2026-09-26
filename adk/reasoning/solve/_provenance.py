"""Where ``adk.reasoning.solve._vendor`` came from, pinned by hash.

The reasoning core was prototyped as "h30" (branch ``h30-repl-agent``, package
``agent/repl/core/``) in the ARC-AGI-3 prototype repository. It is vendored here
verbatim at one commit. For each file this records:

* ``h30_sha256`` -- the h30 original at :data:`H30_SHA` (``git show <sha>:<path>``);
* ``vendored_sha256`` -- the vendored copy: the original plus a one-line provenance
  header and, in ``SEAMED`` files only, the ``SEAM(adk)`` hunks (a line marked
  ``# SEAM(adk) drop`` is added; one marked ``# SEAM(adk) pass`` replaces a bare ``pass``).

Both are sha256 over the bytes with CRLF normalised to LF, so a Windows checkout
with ``core.autocrlf`` hashes the same as Linux CI.

``tests/test_reasoning_solve_pinned.py`` re-hashes the vendored files (always) and
the h30 originals (when a checkout is reachable through ``$ADK_H30_DIR``), so a
hand edit here and upstream drift in h30 are both visible. Sync = re-vendor at a
new sha and update this table in the same commit.
"""

from __future__ import annotations

from typing import Dict

__all__ = ["H30_SHA", "H30_PATH", "PINNED_SOURCE", "SEAMED"]

#: The h30 commit the vendored core was copied from.
H30_SHA = "f27271775d6786b1df5dd40234005436af8081c8"

#: Path of the core package inside the h30 repository.
H30_PATH = "agent/repl/core"

#: Files whose vendored copy differs from the original by more than the header.
SEAMED = ("evidence.py", "loop.py", "sandbox.py")

#: ``{file: {"h30_sha256": ..., "vendored_sha256": ...}}`` plus the sha.
PINNED_SOURCE: Dict[str, object] = {
    "repo": "h30-repl-agent",
    "sha": H30_SHA,
    "path": H30_PATH,
    "files": {
        "interfaces.py": {
            "h30_sha256": "5adb703a266135dbcae802d9361d484b825ae5031cb7c3576eb22b8bc94b0c3c",
            "vendored_sha256": "df25df7321c9f94f1ac5121949a90764f76ccd237467bb90ea56490abbccc821",
        },
        "intent.py": {
            "h30_sha256": "3cd44901e14af79a86c4dc983ebb98f6d9974e3674cc0ec4600ad4426e123c4d",
            "vendored_sha256": "5f13845f2b74900611767c26b4a8bb3e2d0fceefea669d355c3ffd7b21624006",
        },
        "sase.py": {
            "h30_sha256": "efa3b2badc0f1f4661023b035e453eb0d293c92b79aa6a07d44ee25dc0f07d6b",
            "vendored_sha256": "c6795c072d9bbf08acdcdde45d245b125190c6513222947da669f50e14ca265c",
        },
        "prism.py": {
            "h30_sha256": "9f9ab41e6a34e39eb4a33e1965f06410a3ad9a4410710854e7dee78d4d977157",
            "vendored_sha256": "3f0a54dd4607edc622cd8712dc8b45de04cc344ebdc16915277fa2e65568a34f",
        },
        "learning.py": {
            "h30_sha256": "de9d8eb9899e5f90b6b40292077c5aa1dee2ac712022fcebe50bb07baac25634",
            "vendored_sha256": "1b2c656eb28330d5ef4a5d9a252155f175eae28b99b5ab49e0d5337da666a071",
        },
        "memory.py": {
            "h30_sha256": "845c2104c14bbf1992cc497885fe796787d5a29fcfb0302078fa65b3c9c9be42",
            "vendored_sha256": "78f9846acd9734f53e89840907f487b6b8a8d49ca20ef49447a978b8081be167",
        },
        "sandbox.py": {
            "h30_sha256": "7af8c70b65dc350982b8a37cb39163afc8c5a82d9a6d118a7a06a72e6fca7d69",
            "vendored_sha256": "fa12af82110170f44d0eccc74d01392663dc53036724ea8752ce1543ca73c0cf",
        },
        "loop.py": {
            "h30_sha256": "e88f86be71f4e7c43422b6fe401f48d9e0044af8b44b41b662c676b4eeedd3e7",
            "vendored_sha256": "84ca751e553067c8562e044d188a7d4db35fb5e1556f1ca23e42c5283f3c0437",
        },
        "evidence.py": {
            "h30_sha256": "4de89b9f069f9faca6ccd2ba9956403c1f07b9392fdf1017c9de8786ff1dce7a",
            "vendored_sha256": "4f5be6f947d0f552398b8102b520a4c24f971f95b1d1421cba9a78ade84b33db",
        },
    },
}
