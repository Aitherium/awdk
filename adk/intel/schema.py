"""The contribution shape the scout produces -- mirrors the server's /intel/contribute schema.

The server re-validates everything; normalizing here keeps a volunteer's batch from
being refused whole because one feed put HTML in a description.
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

SOURCE_KINDS = ("news", "rss", "social", "sensor", "other")
MAX_URL_CHARS = 2048
MAX_TITLE_CHARS = 300
MAX_SUMMARY_CHARS = 2000
MAX_TAGS = 10
MAX_TAG_CHARS = 40

_SPACE = re.compile(r"\s+")
_TAG_OK = re.compile(r"[^a-z0-9 _.:\-]+")
_ANGLE = re.compile(r"[<>]")


@dataclass
class Observation:
    """One normalized observation, ready to POST."""

    source_url: str
    source_kind: str
    title: str
    published_at: str
    summary: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    tags: List[str] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        """The JSON object for ``/intel/contribute`` (coordinates only when both exist)."""
        out = asdict(self)
        if self.lat is None or self.lng is None:
            out.pop("lat")
            out.pop("lng")
        return out


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def plain_text(value: Optional[str], max_chars: int) -> str:
    """Markup stripped, entities decoded, whitespace collapsed, clipped to *max_chars*.

    Any ``<``/``>`` left after stripping (an escaped tag in the feed) is removed so the
    server's no-HTML rule never refuses the item.
    """
    if not value:
        return ""
    parser = _TextOnly()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # noqa: BLE001 - malformed markup: fall back to the raw text
        text = value
    text = _ANGLE.sub(" ", html.unescape(text))
    text = _SPACE.sub(" ", text).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def clean_tags(raw: List[str]) -> List[str]:
    """Lowercase, charset-limited, deduplicated, at most MAX_TAGS."""
    out: List[str] = []
    for t in raw:
        t = _TAG_OK.sub(" ", (t or "").lower())
        t = _SPACE.sub(" ", t).strip(" _.:-")[:MAX_TAG_CHARS].strip()
        if t and t not in out:
            out.append(t)
        if len(out) >= MAX_TAGS:
            break
    return out


def iso_utc(dt: Optional[datetime]) -> str:
    """ISO-8601 UTC with a Z; now when *dt* is None; clamped to now when in the future."""
    now = datetime.now(timezone.utc)
    if dt is None:
        dt = now
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = min(dt.astimezone(timezone.utc), now)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def http_url(value: Optional[str]) -> Optional[str]:
    """The URL when it is an absolute http(s) link within the length cap, else None."""
    v = (value or "").strip()
    if not v or len(v) > MAX_URL_CHARS or any(c.isspace() for c in v):
        return None
    if not v.lower().startswith(("http://", "https://")):
        return None
    return v
