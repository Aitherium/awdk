"""Where ``adk.cognition.classroom`` came from, pinned by hash.

The h25 synthetic-game generator lives in the ARC-AGI-3 prototype repository
``aither-kaggle-agent`` (package ``agent/synth``, commit :data:`SYNTH_SHA`). The
cognition slice needs its transition function (``rules.py`` -- the same bytes every
generated game embeds), its BFS solver and its seeded level generator, and CI cannot
reach that repository, so the three files are vendored under ``_vendor/``:

* ``rules.py`` -- verbatim plus a one-line provenance header;
* ``solve.py`` -- the import line only (``from . import rules``);
* ``generate.py`` -- the import lines, and ``suite_seed`` formatted with ``%s`` instead
  of ``%d`` so a string seed (``"cog-slice-1"``) works; ``"%d" % n == "%s" % n``
  for every int, so integer-seeded suites are unchanged.

Both hashes are sha256 over the bytes with CRLF normalised to LF.
``tests/test_cognition_slice.py`` re-hashes the vendored files, and the upstream ones
when ``$ADK_SYNTH_DIR`` points at a checkout. Sync = re-vendor and update this table.
"""

from __future__ import annotations

from typing import Dict

__all__ = ["SYNTH_REPO", "SYNTH_SHA", "SYNTH_PATH", "PINNED"]

SYNTH_REPO = "aither-kaggle-agent"
SYNTH_SHA = "a5cd9b6532eb039dd5b7c27633293dfd55672a9c"
SYNTH_PATH = "agent/synth"

PINNED: Dict[str, Dict[str, str]] = {
    "rules.py": {
        "upstream_sha256": "ef17c4ae9ccf4767032280e163b114ec8a2b5b8fee2f2088c6423c4b0ed81b7f",
        "vendored_sha256": "68337b816714cf9e71eaadda39869f02e67381793971189f84e5975c180d05ae",
    },
    "solve.py": {
        "upstream_sha256": "542b4ecaf47d56fb2d7f601b2bcb53b3d255912fe96150316ae1c342cd15de26",
        "vendored_sha256": "fd0d6f0f670cdf89031d1b175cb48123ef04c08f297b93477640f16f14ba611b",
    },
    "generate.py": {
        "upstream_sha256": "64bf81c1136bb4251028356bc3101442de7eb5fc19f946afb2c2cf0b60dba390",
        "vendored_sha256": "d2f092dabea7f88a2be9abd9d2997a2e0000d3a1bb7ccf9418e77aa17f79ee09",
    },
}
