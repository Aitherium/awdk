"""PoliteFetcher -- robots.txt, a minimum interval per host, a size cap, one honest UA.

Stdlib only (urllib). robots.txt is fetched WITH our User-Agent and parsed by
``urllib.robotparser``; following the stdlib's own convention, a 401/403 on
robots.txt means "disallow everything" and a 404 means "allow everything". A
robots.txt that cannot be reached at all (DNS, timeout) disallows the host for
this run: when in doubt, do not fetch.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
import urllib.robotparser
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

from adk.intel import USER_AGENT

logger = logging.getLogger("adk.intel.fetch")

#: The biggest feed we read. A feed is metadata; a multi-megabyte one is not.
MAX_FEED_BYTES = 2 * 1024 * 1024
DEFAULT_MIN_INTERVAL_S = 30.0
DEFAULT_TIMEOUT_S = 20.0

#: (status, body) -- status 0 = could not reach.
Opener = Callable[[str, float], Tuple[int, bytes]]


def _urllib_open(url: str, timeout: float) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/rss+xml, "
                                                         "application/atom+xml, "
                                                         "application/xml, text/xml, */*;q=0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - http(s) only
            return int(resp.status), resp.read(MAX_FEED_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.info("fetch %s failed: %s", url, exc)
        return 0, b""


class FetchRefusedError(Exception):
    """robots.txt disallows the URL, the scheme is not http(s), or the body is too big."""


class PoliteFetcher:
    """Fetch URLs the way a well-behaved crawler does. Injectable clock/sleep/opener
    so the rules are testable without a network."""

    def __init__(self, min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
                 timeout_s: float = DEFAULT_TIMEOUT_S, opener: Optional[Opener] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.timeout_s = timeout_s
        self._open = opener or _urllib_open
        self._clock = clock
        self._sleep = sleep
        self._last: Dict[str, float] = {}
        self._robots: Dict[str, urllib.robotparser.RobotFileParser] = {}

    @staticmethod
    def _origin(url: str) -> Tuple[str, str]:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise FetchRefusedError(f"not an http(s) URL: {url[:80]}")
        return parts.scheme.lower(), parts.netloc.lower()

    def _wait_turn(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            gap = self.min_interval_s - (self._clock() - last)
            if gap > 0:
                self._sleep(gap)
        self._last[host] = self._clock()

    def _robots_for(self, scheme: str, host: str) -> urllib.robotparser.RobotFileParser:
        rp = self._robots.get(host)
        if rp is not None:
            return rp
        rp = urllib.robotparser.RobotFileParser()
        robots_url = f"{scheme}://{host}/robots.txt"
        rp.set_url(robots_url)
        self._wait_turn(host)
        status, body = self._open(robots_url, self.timeout_s)
        if status in (401, 403) or status == 0:
            rp.disallow_all = True
        elif 400 <= status < 500:
            rp.allow_all = True
        elif status >= 500:
            rp.disallow_all = True
        else:
            rp.parse(body.decode("utf-8", errors="replace").splitlines())
        self._robots[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        """True when robots.txt lets USER_AGENT fetch *url*."""
        scheme, host = self._origin(url)
        return self._robots_for(scheme, host).can_fetch(USER_AGENT, url)

    def get(self, url: str) -> bytes:
        """The body of *url*, honouring robots.txt and the per-host interval.

        Raises:
            FetchRefusedError: robots.txt disallows it, it is not http(s), or it is too big.
            OSError: the server answered non-2xx or could not be reached.
        """
        scheme, host = self._origin(url)
        if not self._robots_for(scheme, host).can_fetch(USER_AGENT, url):
            raise FetchRefusedError(f"robots.txt disallows {url[:120]}")
        self._wait_turn(host)
        status, body = self._open(url, self.timeout_s)
        if not 200 <= status < 300:
            raise OSError(f"HTTP {status or 'unreachable'} for {url[:120]}")
        if len(body) > MAX_FEED_BYTES:
            raise FetchRefusedError(f"feed larger than {MAX_FEED_BYTES} bytes: {url[:120]}")
        return body
