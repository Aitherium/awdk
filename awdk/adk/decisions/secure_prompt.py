"""Secure value capture for credential cards.

``kind="credential"`` cards never carry the value: the card names WHICH
secret is wanted (``secret_name``) and why (``credential_description``);
the owner enters the value through THIS module — a masked prompt — and it
goes straight to the vault, then the card closes with the
``CREDENTIAL_ANSWER`` marker.

The value's journey: owner terminal (masked) -> gateway ``/secrets``
(Bearer = session identity) -> vault. It never touches the card store, the
daemon's wire format, the popup render, or a session transcript. This module
never prints the value.

Why the masked prompt must be the ONLY door (DC008): before this module
existed, ``store.answer()`` accepted free text on an optionless credential
card — persisting the value in the card's durable JSON, which the daemon
serves over HTTP, the popup renders, and the steering mailbox copies into
session transcripts. The marker-only enforcement in ``store.answer()``
closes that; the prompt here is the designed entry point.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
from pathlib import Path

from adk.decisions.store import DecisionCard, DecisionError, DecisionStore

logger = logging.getLogger(__name__)


def _session_bearer() -> str:
    """The caller's Identity bearer — the same credential the MCP plane uses."""
    try:
        p = Path.home() / ".aither" / "session-bearer"
        if p.exists():
            return p.read_text().strip()
    except OSError as exc:
        # Falling back to the env credential is intentional — an unreadable
        # bearer file must not be a silent no-op, but must not hard-fail a
        # desktop where AITHER_API_KEY is the configured credential either.
        logger.debug("session-bearer unreadable (%s); falling back to env", exc)
    return os.environ.get("AITHER_API_KEY", "")


def _push_to_vault(key: str, value: str) -> bool:
    """Write the value to the platform vault via the gateway /secrets plane.

    ``secrets_url=""`` forces the gateway branch on purpose: the vault's
    direct endpoint is service-to-service (X-API-Key), while the gateway
    speaks the session bearer — the plane this CLI is already authenticated
    to. A wrong endpoint would 401 and read as a vault outage.
    """
    from adk.sync.secrets import SecretsSync

    api_key = _session_bearer()
    if not api_key:
        return False
    return asyncio.run(SecretsSync(api_key=api_key, secrets_url="").push(key, value))


def capture_credential(card_id: str, store: DecisionStore) -> int:
    """Prompt (masked) for a credential card's value, vault it, close the card.

    Returns an exit code:
      0  value vaulted and card closed with the CREDENTIAL_ANSWER marker
      1  vault write failed — the card is left OPEN so the ask is not lost
      2  empty input refused — the card is left OPEN
    Raises DecisionError for a non-credential card or an unknown id.
    """
    card = store.get(card_id)
    if card is None:
        raise DecisionError(f"no such card: {card_id}")
    if (card.kind or "").strip().lower() != "credential":
        raise DecisionError(f"card {card_id} is not a credential ask")

    label = card.secret_name or card.id
    value = getpass.getpass(f"secret for {label} (masked): ")
    if not value:
        return 2  # empty — leave the card open, never close on nothing
    try:
        if not _push_to_vault(label, value):
            return 1
    finally:
        del value  # never keep it longer than the push
    store.answer(card_id, DecisionCard.CREDENTIAL_ANSWER, via="cli-masked")
    return 0
