"""One terminal QR printer for every awdk surface that hands a phone a link.

``adk pair --join`` (node_join), ``adk devices add``, ``adk rc`` and the tunnel phone
link (``adk up``) all print a URL a phone should open. They used to carry their own
copies of the same ``qrcode`` incantation; one of them checked the console encoding and
one did not, so the same link crashed on one verb and rendered on another.

The QR is a convenience and never the essential: the caller always prints the link (or
code) as text first. When the optional ``qrcode`` package is missing, or the console's
encoding cannot represent the block glyphs (a legacy cp1252 Windows console), this
returns nothing and the caller carries on.
"""

from __future__ import annotations

__all__ = ["qr_lines", "print_qr"]

import io
import logging
import sys
from typing import Callable, List, Optional

log = logging.getLogger("adk.term_qr")


def qr_lines(text: str, *, encoding: Optional[str] = None) -> List[str]:
    """The QR for ``text`` as terminal lines, or ``[]`` when it cannot be shown.

    Args:
        text: What the QR encodes (a URL, normally).
        encoding: The output encoding to check the glyphs against
            (default: ``sys.stdout.encoding``, then utf-8).

    Returns:
        One string per QR row; empty when ``qrcode`` is absent or the encoding
        cannot carry the glyphs.
    """
    if not text:
        return []
    try:
        import qrcode  # type: ignore[import-untyped]

        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        block = buf.getvalue()
        block.encode(encoding or getattr(sys.stdout, "encoding", None) or "utf-8")
    except Exception as exc:  # noqa: BLE001 -- the link still works without the picture
        log.debug("no terminal QR (%s)", type(exc).__name__)
        return []
    return block.rstrip("\n").splitlines()


def print_qr(text: str, out: Callable[[str], None] = print, *, indent: str = "") -> bool:
    """Print the QR for ``text`` through ``out``. Returns whether one was printed."""
    lines = qr_lines(text)
    if not lines:
        return False
    out("\n".join(indent + ln for ln in lines))
    return True
