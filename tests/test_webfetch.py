"""adk.webfetch: the SSRF gate, the httpx rung, and the Scrapling rungs behind a fake module."""

from __future__ import annotations

import sys
import types

import httpx
import pytest
import respx
from adk.webfetch import FetchResult, _looks_like_challenge, fetch, is_safe_url

URL = "https://example.com/page"
CHALLENGE_HTML = (
    "<html><head><title>Just a moment...</title></head>"
    '<body><div id="cf-browser-verification">Enable JavaScript and cookies to continue'
    "</div></body></html>"
)


# ── is_safe_url ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x",
    "http://localhost:8080/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://foo.internal/",
    "http://printer.local/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "file:///etc/passwd",
    "ftp://example.com/",
    "",
])
def test_is_safe_url_blocks(url):
    assert is_safe_url(url) is False


@pytest.mark.parametrize("url", ["https://example.com/", "http://93.184.216.34/", URL])
def test_is_safe_url_allows_public(url):
    assert is_safe_url(url) is True


# ── _looks_like_challenge ────────────────────────────────────────────────────

def test_challenge_detection():
    assert _looks_like_challenge(CHALLENGE_HTML)
    assert _looks_like_challenge("<title>Attention Required! | Cloudflare</title>")
    assert not _looks_like_challenge("<html><body><p>Hello world</p></body></html>")
    assert not _looks_like_challenge("")
    # A quote deep inside a real article is not an interstitial.
    assert not _looks_like_challenge("x" * 5000 + "just a moment")


# ── the httpx rung ───────────────────────────────────────────────────────────

@pytest.fixture
def no_scrapling(monkeypatch):
    """Make ``import scrapling`` raise ImportError regardless of what is installed."""
    monkeypatch.setitem(sys.modules, "scrapling", None)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", None)


@pytest.fixture
def fake_scrapling(monkeypatch):
    """Inject a ``scrapling.fetchers`` module whose fetchers return canned responses.

    Returns the module; tests set ``Fetcher.get`` / ``StealthyFetcher.fetch`` on it.
    """
    pkg = types.ModuleType("scrapling")
    fetchers = types.ModuleType("scrapling.fetchers")
    pkg.fetchers = fetchers  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scrapling", pkg)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fetchers)
    return fetchers


class _Resp:
    """The two attributes of a Scrapling ``Response`` the ladder relies on."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body


def _fetcher(calls: list, resp: _Resp):
    class Fetcher:
        @classmethod
        def get(cls, url, **kw):
            calls.append((url, kw))
            return resp
    return Fetcher


def _stealthy(calls: list, resp: _Resp):
    class StealthyFetcher:
        @classmethod
        def fetch(cls, url, **kw):
            calls.append((url, kw))
            return resp
    return StealthyFetcher


@respx.mock
async def test_httpx_200_is_the_first_rung(no_scrapling):
    respx.get(URL).mock(return_value=httpx.Response(
        200, text="<html><script>x()</script><body><h1>Hi</h1> <p>there</p></body></html>",
    ))
    r = await fetch(URL)
    assert isinstance(r, FetchResult)
    assert r.engine == "httpx"
    assert r.status == 200
    assert r.error == ""
    assert r.text == "Hi there"


@respx.mock
async def test_max_chars_caps_text(no_scrapling):
    respx.get(URL).mock(return_value=httpx.Response(200, text="<p>" + "a" * 100 + "</p>"))
    r = await fetch(URL, max_chars=10)
    assert r.text == "a" * 10


@respx.mock
async def test_404_does_not_climb_the_ladder(fake_scrapling):
    calls: list = []
    fake_scrapling.Fetcher = _fetcher(calls, _Resp(200, b"<p>should not be used</p>"))
    respx.get(URL).mock(return_value=httpx.Response(404, text="<p>gone</p>"))
    r = await fetch(URL)
    assert r.engine == "httpx"
    assert r.status == 404
    assert r.error == "HTTP 404"
    assert calls == []


@respx.mock
async def test_403_without_scrapling_names_the_extra(no_scrapling):
    respx.get(URL).mock(return_value=httpx.Response(403, text="<p>forbidden</p>"))
    r = await fetch(URL)
    assert r.engine == "httpx"
    assert r.status == 403
    assert "awdk[scrape]" in r.error
    assert r.error.startswith("blocked (HTTP 403")
    assert r.text == "forbidden"


@respx.mock
async def test_ssrf_gate_runs_before_any_request(no_scrapling):
    route = respx.get("http://127.0.0.1/x").mock(return_value=httpx.Response(200, text="secret"))
    r = await fetch("http://127.0.0.1/x")
    assert r.engine == "blocked"
    assert r.status == 0
    assert r.text == ""
    assert "blocked" in r.error
    assert route.call_count == 0


@respx.mock
async def test_redirect_to_private_ip_is_blocked(no_scrapling):
    respx.get(URL).mock(return_value=httpx.Response(
        301, headers={"Location": "http://127.0.0.1/x"},
    ))
    inner = respx.get("http://127.0.0.1/x").mock(return_value=httpx.Response(200, text="secret"))
    r = await fetch(URL)
    assert r.engine == "blocked"
    assert r.status == 0
    assert "127.0.0.1" in r.error
    assert "secret" not in r.text
    # httpx follows before we can veto; the veto is what keeps the body out of the result.
    assert inner.call_count == 1


# ── the scrapling rungs ──────────────────────────────────────────────────────

@respx.mock
async def test_403_climbs_to_scrapling_curl(fake_scrapling):
    calls: list = []
    fake_scrapling.Fetcher = _fetcher(calls, _Resp(200, b"<html>CONTROL-9912</html>"))
    respx.get(URL).mock(return_value=httpx.Response(403, text="<p>forbidden</p>"))
    r = await fetch(URL)
    assert r.engine == "scrapling-curl"
    assert r.status == 200
    assert r.error == ""
    assert "CONTROL-9912" in r.text
    assert calls and calls[0][0] == URL
    assert calls[0][1]["impersonate"] == "chrome"
    assert calls[0][1]["stealthy_headers"] is True


@respx.mock
async def test_challenge_page_200_climbs_to_scrapling_curl(fake_scrapling):
    calls: list = []
    fake_scrapling.Fetcher = _fetcher(calls, _Resp(200, b"<html>CONTROL-4471</html>"))
    respx.get(URL).mock(return_value=httpx.Response(200, text=CHALLENGE_HTML))
    r = await fetch(URL)
    assert r.engine == "scrapling-curl"
    assert "CONTROL-4471" in r.text
    assert len(calls) == 1


@respx.mock
async def test_challenge_page_200_without_scrapling_reports_challenge(no_scrapling):
    respx.get(URL).mock(return_value=httpx.Response(200, text=CHALLENGE_HTML))
    r = await fetch(URL)
    assert r.engine == "httpx"
    assert r.error.startswith("blocked (bot challenge page")
    assert "awdk[scrape]" in r.error


@respx.mock
async def test_curl_still_blocked_without_stealth_stops_at_rung_2(fake_scrapling):
    curl_calls: list = []
    browser_calls: list = []
    fake_scrapling.Fetcher = _fetcher(curl_calls, _Resp(403, b"<p>nope</p>"))
    fake_scrapling.StealthyFetcher = _stealthy(browser_calls, _Resp(200, b"<p>browser</p>"))
    respx.get(URL).mock(return_value=httpx.Response(403, text="<p>forbidden</p>"))
    r = await fetch(URL)
    assert r.engine == "scrapling-curl"
    assert r.status == 403
    assert "stealth=True" in r.error
    assert browser_calls == []


@respx.mock
async def test_stealth_true_skips_httpx_and_reaches_the_browser(fake_scrapling):
    curl_calls: list = []
    browser_calls: list = []
    fake_scrapling.Fetcher = _fetcher(curl_calls, _Resp(200, CHALLENGE_HTML.encode()))
    fake_scrapling.StealthyFetcher = _stealthy(
        browser_calls, _Resp(200, b"<html><body>CONTROL-7730</body></html>"),
    )
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="<p>plain</p>"))
    r = await fetch(URL, stealth=True, timeout=2.5)
    assert route.call_count == 0
    assert len(curl_calls) == 1
    assert len(browser_calls) == 1
    assert browser_calls[0][1]["solve_cloudflare"] is True
    assert browser_calls[0][1]["headless"] is True
    assert browser_calls[0][1]["timeout"] == 2500
    assert r.engine == "scrapling-stealth"
    assert r.status == 200
    assert "CONTROL-7730" in r.text


@respx.mock
async def test_stealth_true_without_scrapling_names_the_extra(no_scrapling):
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="<p>plain</p>"))
    r = await fetch(URL, stealth=True)
    assert route.call_count == 0
    assert r.engine == "httpx"
    assert r.status == 0
    assert "awdk[scrape]" in r.error


@respx.mock
async def test_markdown_preferred_when_available(fake_scrapling):
    calls: list = []

    class MdResp(_Resp):
        def markdown(self, main_content_only=False):
            return "# Title\n\n\n\nbody CONTROL-2201\n"

    fake_scrapling.Fetcher = _fetcher(calls, MdResp(200, b"<h1>Title</h1><p>body</p>"))
    respx.get(URL).mock(return_value=httpx.Response(429, text="slow down"))
    r = await fetch(URL)
    assert r.engine == "scrapling-curl"
    assert r.text == "# Title\n\nbody CONTROL-2201"


@respx.mock
async def test_scrapling_exception_is_reported_not_raised(fake_scrapling):
    class Fetcher:
        @classmethod
        def get(cls, url, **kw):
            raise RuntimeError("curl exploded")

    fake_scrapling.Fetcher = Fetcher
    respx.get(URL).mock(return_value=httpx.Response(403, text="no"))
    r = await fetch(URL)
    assert r.engine == "scrapling-curl"
    assert r.status == 0
    assert "curl exploded" in r.error
