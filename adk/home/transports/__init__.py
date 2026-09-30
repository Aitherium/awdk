"""Hearth transports: one module per channel, imported lazily by ``adk home serve``.

    relay     :class:`relay.RelayTransport` (the relay DM client from ``adk.home.serve``)
    chat      ``TelegramTransport``, ``DiscordTransport``, ``SlackTransport``
    email     ``EmailTransport``
    whatsapp  ``WhatsAppTransport``
    local     ``LocalTransport`` (127.0.0.1 only; ``adk home say``, awsh ``/hearth``)

Nothing is imported here on purpose: a channel whose module (or one of its
dependencies) is missing must not stop the others from loading. Every transport
satisfies :class:`adk.home.hearth.Transport`.
"""
