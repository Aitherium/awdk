"""Crowdsourced OSINT scout -- the volunteer side of shadowbroker.aitherium.com.

A volunteer runs this on THEIR machine against feeds THEY chose. It fetches
politely (robots.txt, a minimum interval per host, an honest User-Agent),
normalizes each item to the contribution schema, and posts only that -- a link,
a short plain-text headline and summary -- to Genesis ``/intel/contribute`` with
the volunteer's own awdk credential. Page bodies never leave the machine.

Layout:
    adk.intel.schema      -- the Observation shape + plain-text normalizers
    adk.intel.fetch       -- PoliteFetcher (robots + per-host interval + size cap)
    adk.intel.sources     -- the Source protocol and the built-in RSS/Atom source
    adk.commands.intel    -- ``adk intel scout`` / ``adk intel schedule``
"""

from __future__ import annotations

USER_AGENT = "AitherIntelScout/0.1 (+https://aitherium.com)"

__all__ = ["USER_AGENT"]
