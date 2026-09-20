"""Web fetch ladder: httpx first, Scrapling (optional extra) when a page fights back.

Scrapling (BSD-3, PyPI ``scrapling``) is an optional dependency behind ``awdk[scrape]``,
never vendored. Every engine is lazy-imported so a plain ``pip install awdk`` still
fetches ordinary pages; anti-bot pages degrade to a clear "install the extra" error.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlparse

__all__ = ["FetchResult", "fetch", "is_safe_url"]

_USER_AGENT = "AitherADK/1.0"
_MAX_REDIRECTS = 5
_SCRAPE_HINT = 'install "awdk[scrape]" for stealth fetch'

# Status codes that read as "a WAF said no" rather than "the page is gone".
_BLOCK_STATUSES = frozenset({403, 429, 503})

# Markers of an interstitial that only a real browser gets past. Lower-cased,
# matched against the lower-cased body.
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "cf-challenge",
    "enable javascript and cookies",
    "checking your browser",
    "attention required! | cloudflare",
    "verify you are human",
    "_cf_chl_opt",
)


@dataclass
class FetchResult:
    """Outcome of one ``fetch`` call.

    ``engine`` names the rung that produced ``text``: ``blocked`` (SSRF gate),
    ``httpx``, ``scrapling-curl``, ``scrapling-stealth``. ``error`` is empty on
    success; ``status`` is 0 when no request was made.
    """

    url: str
    status: int
    text: str
    engine: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status < 300


def is_safe_url(url: str) -> bool:
    """Block SSRF: reject private IPs, localhost, metadata endpoints."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        # Block non-HTTP schemes
        if parsed.scheme not in ("http", "https"):
            return False
        # Block localhost and common internal hostnames
        if hostname in ("localhost", "0.0.0.0", "127.0.0.1", "[::]", "[::1]"):
            return False
        if hostname.startswith("169.254.") or hostname.startswith("fe80:"):
            return False  # Link-local / cloud metadata
        if hostname.endswith(".internal") or hostname.endswith(".local"):
            return False
        # Block private IP ranges
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            return True  # hostname, not IP — OK
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except Exception:
        return False


def _looks_like_challenge(html: str) -> bool:
    """True when the body is a bot-check interstitial, not the page asked for."""
    if not html:
        return False
    # Only the head of the document matters; challenge pages are small and
    # a real article that merely QUOTES "just a moment" should not trip this.
    head = html[:4096].lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def _strip_tags(html: str) -> str:
    """Reduce HTML to whitespace-collapsed visible text (same cleanup as ``web_fetch``)."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _decode_body(body: bytes | str) -> str:
    if isinstance(body, str):
        return body
    return body.decode("utf-8", errors="replace")


def _response_text(resp: object) -> str:
    """Best text from a Scrapling ``Response``: markdown when markdownify is present, else tags."""
    try:
        md = resp.markdown(main_content_only=True)  # type: ignore[attr-defined]
    except Exception:
        md = ""  # markdownify absent or the page has no <body> — tag-strip below
    if isinstance(md, str) and md.strip():
        return re.sub(r"\n{3,}", "\n\n", md).strip()
    body = getattr(resp, "body", b"")
    return _strip_tags(_decode_body(body))


async def _fetch_httpx(url: str, timeout: float) -> FetchResult:
    """Rung 1: plain httpx GET. Redirect hops are re-checked against the SSRF gate."""
    import httpx

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, max_redirects=_MAX_REDIRECTS,
    ) as client:
        resp = await client.get(url, headers={"User-Agent": _USER_AGENT})

    for hop in (*resp.history, resp):
        if not is_safe_url(str(hop.url)):
            return FetchResult(
                url=url, status=0, text="", engine="blocked",
                error=f"URL blocked: redirect to private/internal address ({hop.url})",
            )
    return FetchResult(url=str(resp.url), status=resp.status_code, text=resp.text, engine="httpx")


async def _fetch_scrapling_curl(url: str, timeout: float) -> FetchResult:
    """Rung 2: curl_cffi with a Chrome TLS fingerprint and browser-shaped headers."""
    from scrapling.fetchers import Fetcher

    resp = await asyncio.to_thread(
        Fetcher.get, url, impersonate="chrome", stealthy_headers=True, timeout=timeout,
    )
    return FetchResult(
        url=url, status=int(getattr(resp, "status", 0)),
        text=_response_text(resp), engine="scrapling-curl",
    )


async def _fetch_scrapling_stealth(url: str, timeout: float) -> FetchResult:
    """Rung 3: headless stealth browser that solves Cloudflare interstitials."""
    from scrapling.fetchers import StealthyFetcher

    resp = await asyncio.to_thread(
        StealthyFetcher.fetch, url,
        headless=True, solve_cloudflare=True, timeout=int(timeout * 1000),
    )
    return FetchResult(
        url=url, status=int(getattr(resp, "status", 0)),
        text=_response_text(resp), engine="scrapling-stealth",
    )


def _is_blocked(result: FetchResult) -> bool:
    return result.status in _BLOCK_STATUSES or _looks_like_challenge(result.text)


def _blocked_error(result: FetchResult, hint: str = "") -> str:
    if result.status in _BLOCK_STATUSES:
        reason = f"HTTP {result.status}"
    elif _looks_like_challenge(result.text):
        reason = "bot challenge page"
    else:
        reason = "stealth fetch requested"
    return f"blocked ({reason}; {hint})" if hint else f"blocked ({reason})"


async def fetch(
    url: str, *, max_chars: int = 20000, stealth: bool = False, timeout: float = 15.0,
) -> FetchResult:
    """Fetch ``url`` and return visible text, climbing the ladder only when needed.

    Args:
        url: Absolute http(s) URL. Private/internal targets are refused before any request.
        max_chars: Cap on the returned text.
        stealth: Skip straight to Scrapling, and permit the headless-browser rung.
        timeout: Per-request timeout in seconds (Scrapling's browser rung takes ms; converted).

    Returns:
        A ``FetchResult``; ``error`` is non-empty when no rung produced a page.
    """
    if not is_safe_url(url):
        return FetchResult(
            url=url, status=0, text="", engine="blocked",
            error="URL blocked: private/internal addresses not allowed",
        )

    # Rung 1 — httpx. Skipped entirely when the caller already knows the site fights back.
    result: FetchResult | None = None
    if not stealth:
        try:
            result = await _fetch_httpx(url, timeout)
        except ImportError:
            return FetchResult(url=url, status=0, text="", engine="httpx",
                               error="httpx required for web fetch")
        except Exception as e:
            result = FetchResult(url=url, status=0, text="", engine="httpx", error=str(e))
        if result.engine == "blocked":
            return result
        if result.ok and not _looks_like_challenge(result.text):
            result.text = _strip_tags(result.text)[:max_chars]
            return result
        if not result.error and not _is_blocked(result):
            # A plain 404/500 is an answer, not a wall — stealth would not change it.
            result.text = _strip_tags(result.text)[:max_chars]
            result.error = f"HTTP {result.status}"
            return result

    # Rung 2 — Scrapling over curl_cffi.
    fallback = result or FetchResult(url=url, status=0, text="", engine="httpx")
    try:
        curl = await _fetch_scrapling_curl(url, timeout)
    except ImportError:
        fallback.text = _strip_tags(fallback.text)[:max_chars]
        fallback.error = _blocked_error(fallback, _SCRAPE_HINT) if not fallback.error \
            else f"{fallback.error}; {_SCRAPE_HINT}"
        return fallback
    except Exception as e:
        curl = FetchResult(url=url, status=0, text="", engine="scrapling-curl", error=str(e))
    if curl.ok and not _looks_like_challenge(curl.text):
        curl.text = curl.text[:max_chars]
        return curl

    if not stealth:
        curl.text = curl.text[:max_chars]
        curl.error = curl.error or _blocked_error(curl, "pass stealth=True for a browser fetch")
        return curl

    # Rung 3 — headless stealth browser, only when the caller opted in.
    try:
        browser = await _fetch_scrapling_stealth(url, timeout)
    except ImportError:
        curl.text = curl.text[:max_chars]
        curl.error = curl.error or _blocked_error(curl, _SCRAPE_HINT)
        return curl
    except Exception as e:
        browser = FetchResult(url=url, status=0, text="", engine="scrapling-stealth", error=str(e))
    browser.text = browser.text[:max_chars]
    if not browser.error and not browser.ok:
        browser.error = _blocked_error(browser)
    return browser
