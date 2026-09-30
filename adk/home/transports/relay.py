"""The relay as a Hearth transport.

The implementation lives in :mod:`adk.home.serve` (it predates the multi-channel
core and keeps its relay-only ``run()`` loop); this module gives it the same
``adk.home.transports.<channel>`` address as every other channel.
"""

from __future__ import annotations

from ..serve import DEFAULT_RELAY_URL, RELAY_CHANNEL, OwnerRelayClient

#: The relay transport under the name the other channels follow.
RelayTransport = OwnerRelayClient

__all__ = ["DEFAULT_RELAY_URL", "RELAY_CHANNEL", "OwnerRelayClient", "RelayTransport"]
