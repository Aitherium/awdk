"""Source adapters for ``adk intel scout`` -- the extension point.

A Source turns something the VOLUNTEER configured into Observations. The scout
owns the network etiquette (``PoliteFetcher``) and the upload; a source only
fetches through the fetcher it is given and normalizes.

Adding an adapter (X, Reddit, Mastodon, a sensor API...) later:

1. Implement the ``Source`` protocol in ``adk/intel/sources/<name>.py``.
2. Read any API key with ``local_secret("<NAME>")`` -- the volunteer's LOCAL
   encrypted keyring (populated by ``adk secrets`` / ``adk.sync.secrets``). A key
   is used on this machine to call the provider directly; it is never put in an
   Observation, never logged, and never sent to Aitherium.
3. Register it in ``SOURCES``.

Only the RSS/Atom source ships today; no social-network scraping is implemented.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

try:  # Protocol is typing-only; keep 3.10 and old typing_extensions both happy
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]

from adk.intel.fetch import PoliteFetcher
from adk.intel.schema import Observation


@runtime_checkable
class Source(Protocol):
    """Something that yields observations. ``name`` identifies it in logs."""

    name: str

    def collect(self, fetcher: PoliteFetcher) -> List[Observation]:
        """Fetch through *fetcher* (never directly) and return normalized items."""
        ...


def local_secret(name: str) -> Optional[str]:
    """A secret from the volunteer's LOCAL keyring, or None. Never networked here.

    Adapters call this for their own provider key. It reads the encrypted
    ``~/.aither`` store that ``adk.sync.secrets`` maintains; it does not pull from or
    push to any vault.
    """
    try:
        from adk.builtin_tools import _load_secrets  # local encrypted store only
    except ImportError:  # pragma: no cover - minimal installs
        return None
    try:
        value = _load_secrets().get(name)
    except Exception:  # noqa: BLE001 - an unreadable keyring is "no key"
        return None
    return value or None


def _rss_factory(spec: str) -> Source:
    from adk.intel.sources.rss import FeedSource
    return FeedSource(spec)


#: kind -> factory(spec). ``adk intel scout --feeds`` builds "rss" sources.
SOURCES: Dict[str, Callable[[str], Source]] = {"rss": _rss_factory}

__all__ = ["SOURCES", "Source", "local_secret"]
