"""RSS 2.0 / Atom 1.0 feed source (stdlib ``xml.etree``).

Refuses any document carrying a DTD or entity declaration before parsing (the
billion-laughs / external-entity class), reads at most ``MAX_ITEMS_PER_FEED`` items,
and keeps only the link, a plain-text title/summary, the date, categories and an
optional GeoRSS point. Nothing else from the feed survives.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import List, Optional, Tuple

from adk.intel.fetch import PoliteFetcher
from adk.intel.schema import (
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    Observation,
    clean_tags,
    http_url,
    iso_utc,
    plain_text,
)

logger = logging.getLogger("adk.intel.rss")

MAX_ITEMS_PER_FEED = 100
_ATOM = "{http://www.w3.org/2005/Atom}"
_GEORSS = "{http://www.georss.org/georss}"
_GEO = "{http://www.w3.org/2003/01/geo/wgs84_pos#}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


class FeedRefusedError(ValueError):
    """The document is not a feed we will parse."""


def _text(el: Optional[ET.Element]) -> str:
    return (el.text or "").strip() if el is not None else ""


def _date(value: str) -> Optional[datetime]:
    v = (value or "").strip()
    if not v:
        return None
    try:
        return parsedate_to_datetime(v)          # RFC 822 (RSS)
    except (TypeError, ValueError, IndexError):
        logger.debug("not an RFC 822 date, trying ISO: %r", v[:40])
    if v.endswith(("Z", "z")):
        v = v[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(v)          # RFC 3339 (Atom)
    except ValueError:
        return None


def _point(item: ET.Element) -> Tuple[Optional[float], Optional[float]]:
    raw = _text(item.find(f"{_GEORSS}point"))
    try:
        if raw:
            lat_s, lng_s = raw.replace(",", " ").split()[:2]
            lat, lng = float(lat_s), float(lng_s)
        else:
            lat_el, lng_el = item.find(f"{_GEO}lat"), item.find(f"{_GEO}long")
            if lat_el is None or lng_el is None:
                return None, None
            lat, lng = float(_text(lat_el)), float(_text(lng_el))
    except ValueError:
        return None, None
    if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
        return lat, lng
    return None, None


def _observation(link: Optional[str], title: str, summary: str, date: Optional[datetime],
                 cats: List[str], item: ET.Element) -> Optional[Observation]:
    url = http_url(link)
    title_t = plain_text(title, MAX_TITLE_CHARS)
    if not url or not title_t:
        return None
    lat, lng = _point(item)
    return Observation(source_url=url, source_kind="rss", title=title_t,
                       summary=plain_text(summary, MAX_SUMMARY_CHARS),
                       published_at=iso_utc(date), lat=lat, lng=lng, tags=clean_tags(cats))


def parse_feed(data: bytes) -> List[Observation]:
    """Observations from an RSS 2.0 or Atom 1.0 document.

    Raises:
        FeedRefusedError: a DTD/entity declaration, unparseable XML, or not a feed.
    """
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        raise FeedRefusedError("feed declares a DTD or entities; refusing to parse")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedRefusedError(f"not well-formed XML: {exc}") from exc
    out: List[Observation] = []
    if root.tag == f"{_ATOM}feed":
        for entry in root.findall(f"{_ATOM}entry")[:MAX_ITEMS_PER_FEED]:
            link = None
            for ln in entry.findall(f"{_ATOM}link"):
                if ln.get("rel", "alternate") == "alternate" and ln.get("href"):
                    link = ln.get("href")
                    break
            summary = _text(entry.find(f"{_ATOM}summary")) or _text(entry.find(f"{_ATOM}content"))
            date = _date(_text(entry.find(f"{_ATOM}published"))
                         or _text(entry.find(f"{_ATOM}updated")))
            cats = [c.get("term") or "" for c in entry.findall(f"{_ATOM}category")]
            obs = _observation(link, _text(entry.find(f"{_ATOM}title")), summary, date, cats,
                               entry)
            if obs:
                out.append(obs)
        return out
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise FeedRefusedError(f"not an RSS 2.0 or Atom feed (root <{root.tag}>)")
    for item in channel.findall("item")[:MAX_ITEMS_PER_FEED]:
        summary = _text(item.find("description")) or _text(item.find(f"{_CONTENT}encoded"))
        date = _date(_text(item.find("pubDate")) or _text(item.find(f"{_DC}date")))
        cats = [_text(c) for c in item.findall("category")]
        obs = _observation(_text(item.find("link")) or _text(item.find("guid")),
                           _text(item.find("title")), summary, date, cats, item)
        if obs:
            out.append(obs)
    return out


class FeedSource:
    """One feed URL the volunteer listed."""

    def __init__(self, url: str):
        self.url = url.strip()
        self.name = f"rss:{self.url}"

    def collect(self, fetcher: PoliteFetcher) -> List[Observation]:
        return parse_feed(fetcher.get(self.url))
