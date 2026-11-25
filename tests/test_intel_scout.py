"""adk intel scout -- normalizer on fixture feeds, robots.txt, host interval, upload.

No network: PoliteFetcher takes an injected opener/clock/sleep, upload an injected
httpx client. Each test fails if the rule it names is removed.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import httpx
import pytest

from adk.commands import intel as cmd
from adk.intel import USER_AGENT
from adk.intel.fetch import FetchRefusedError, PoliteFetcher
from adk.intel.schema import Observation, plain_text
from adk.intel.sources import SOURCES, Source, local_secret
from adk.intel.sources.rss import FeedRefusedError, parse_feed

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:georss="http://www.georss.org/georss">
  <channel>
    <title>Example wire</title>
    <item>
      <title>Bridge &amp; tunnel closed after quake</title>
      <link>https://news.example.com/world/story-1?utm_source=rss</link>
      <description>&lt;p&gt;Officials &lt;b&gt;closed&lt;/b&gt; the bridge.&lt;/p&gt;
        &lt;script&gt;track()&lt;/script&gt;</description>
      <pubDate>Sat, 14 Nov 2026 10:00:00 GMT</pubDate>
      <category>Quake</category><category>Infra!</category>
      <georss:point>37.8 -122.4</georss:point>
    </item>
    <item>
      <title>No link here</title>
    </item>
    <item>
      <title>Bad scheme</title>
      <link>javascript:alert(1)</link>
    </item>
  </channel>
</rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom wire</title>
  <entry>
    <title>Port reopens</title>
    <link rel="alternate" href="https://port.example.org/a/1"/>
    <updated>2026-11-14T08:30:00Z</updated>
    <summary>Ships move again.</summary>
    <category term="shipping"/>
  </entry>
</feed>"""

BOMB = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>
<rss version="2.0"><channel><item><title>&lol2;</title>
<link>https://x.example/</link></item></channel></rss>"""


# ── normalizer ──────────────────────────────────────────────────────────────

def test_rss_items_normalize_to_plain_text_schema():
    items = parse_feed(RSS)
    assert len(items) == 1                      # no-link and javascript: items dropped
    o = items[0]
    assert o.source_kind == "rss"
    assert o.title == "Bridge & tunnel closed after quake"
    assert o.summary == "Officials closed the bridge."
    assert "<" not in o.summary and "track" not in o.summary
    assert o.published_at == "2026-11-14T10:00:00Z"
    assert (o.lat, o.lng) == (37.8, -122.4)
    assert o.tags == ["quake", "infra"]
    assert set(o.to_payload()) == {"source_url", "source_kind", "title", "summary",
                                   "published_at", "lat", "lng", "tags"}


def test_atom_entries_normalize():
    (o,) = parse_feed(ATOM)
    assert o.source_url == "https://port.example.org/a/1"
    assert o.published_at == "2026-11-14T08:30:00Z"
    assert o.tags == ["shipping"]
    assert "lat" not in o.to_payload()          # no point -> no coordinates sent


def test_entity_declarations_are_refused():
    with pytest.raises(FeedRefusedError):
        parse_feed(BOMB)


def test_plain_text_clips_and_strips():
    assert plain_text("<i>a</i>" + "b" * 50, 10).endswith("…")
    assert len(plain_text("x" * 5000, 2000)) == 2000


# ── polite fetching ─────────────────────────────────────────────────────────

class FakeNet:
    def __init__(self, pages: Dict[str, Tuple[int, bytes]]):
        self.pages = pages
        self.calls: List[str] = []

    def __call__(self, url: str, timeout: float) -> Tuple[int, bytes]:
        self.calls.append(url)
        return self.pages.get(url, (404, b""))


def _fetcher(net: FakeNet, interval: float = 0.0):
    clock = {"t": 0.0}
    slept: List[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        clock["t"] += s

    f = PoliteFetcher(min_interval_s=interval, opener=net, clock=lambda: clock["t"],
                      sleep=sleep)
    return f, slept


def test_robots_disallow_is_respected():
    robots = f"User-agent: {USER_AGENT.split('/')[0]}\nDisallow: /private/\n".encode()
    net = FakeNet({"https://a.example/robots.txt": (200, robots),
                   "https://a.example/private/feed.xml": (200, RSS),
                   "https://a.example/public/feed.xml": (200, RSS)})
    f, _ = _fetcher(net)
    with pytest.raises(FetchRefusedError):
        f.get("https://a.example/private/feed.xml")
    assert "https://a.example/private/feed.xml" not in net.calls
    assert f.get("https://a.example/public/feed.xml") == RSS


def test_robots_forbidden_or_unreachable_disallows_and_404_allows():
    net = FakeNet({"https://locked.example/robots.txt": (403, b""),
                   "https://down.example/robots.txt": (0, b""),
                   "https://open.example/feed": (200, ATOM)})
    f, _ = _fetcher(net)
    assert f.allowed("https://locked.example/feed") is False
    assert f.allowed("https://down.example/feed") is False
    assert f.get("https://open.example/feed") == ATOM   # robots 404 -> allowed


def test_min_interval_per_host():
    net = FakeNet({"https://a.example/f1": (200, RSS), "https://a.example/f2": (200, RSS),
                   "https://b.example/f": (200, RSS)})
    f, slept = _fetcher(net, interval=30.0)
    f.get("https://a.example/f1")      # robots then feed on host a
    f.get("https://b.example/f")
    f.get("https://a.example/f2")
    assert slept and all(s <= 30.0 for s in slept)
    assert sum(slept) >= 60.0          # robots->feed gap on a and b, plus a's second feed


def test_non_http_is_refused():
    f, _ = _fetcher(FakeNet({}))
    with pytest.raises(FetchRefusedError):
        f.get("file:///etc/passwd")


# ── sources extension point ─────────────────────────────────────────────────

def test_rss_source_satisfies_the_protocol():
    src = SOURCES["rss"]("https://open.example/feed")
    assert isinstance(src, Source)


def test_local_secret_reads_only_the_local_keyring(monkeypatch):
    import adk.builtin_tools as bt
    monkeypatch.setattr(bt, "_load_secrets", lambda: {"X_BEARER": "local-only"})
    assert local_secret("X_BEARER") == "local-only"
    assert local_secret("MISSING") is None


# ── feeds + upload ──────────────────────────────────────────────────────────

def test_parse_feed_args_file_and_list(tmp_path):
    f = tmp_path / "feeds.txt"
    f.write_text("# mine\nhttps://a.example/rss\n\nftp://no\nhttps://b.example/atom # x\n",
                 encoding="utf-8")
    assert cmd.parse_feed_args([str(f), "https://c.example/f,https://a.example/rss"]) == [
        "https://a.example/rss", "https://b.example/atom", "https://c.example/f"]


def _obs(i: int) -> Observation:
    return Observation(source_url=f"https://x.example/{i}", source_kind="rss", title=f"t{i}",
                       published_at="2026-11-14T00:00:00Z")


def test_upload_batches_with_the_saved_credential(monkeypatch):
    seen: List[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        seen.append({"auth": request.headers.get("authorization"), "n": len(body["items"]),
                     "ua": request.headers.get("user-agent")})
        return httpx.Response(200, json={"accepted": len(body["items"]), "duplicate": 0,
                                         "promoted": []})

    monkeypatch.setattr(cmd, "_headers", lambda: {"Authorization": "Bearer t",
                                                  "User-Agent": USER_AGENT})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    totals = cmd.upload([_obs(i) for i in range(120)], url="https://cp.example/intel/contribute",
                        client=client)
    assert totals["accepted"] == 120
    assert [s["n"] for s in seen] == [50, 50, 20]
    assert all(s["auth"] == "Bearer t" and s["ua"] == USER_AGENT for s in seen)


def test_upload_refuses_without_a_credential(monkeypatch):
    monkeypatch.setattr(cmd, "_headers", lambda: {"User-Agent": USER_AGENT})
    with pytest.raises(PermissionError):
        cmd.upload([_obs(1)], url="https://cp.example/intel/contribute")


def test_contribute_url_uses_the_genesis_data_base(monkeypatch):
    monkeypatch.delenv("AITHER_GENESIS_URL", raising=False)
    monkeypatch.delenv("AITHER_API_URL", raising=False)
    monkeypatch.delenv("AITHER_GATEWAY_URL", raising=False)
    monkeypatch.setenv("AITHER_PORTAL_URL", "https://cp.example")
    assert cmd.contribute_url() == "https://cp.example/api/bridge/genesis/intel/contribute"


def test_schedule_builds_an_awrise_add(monkeypatch, tmp_path):
    feeds = tmp_path / "feeds.txt"
    feeds.write_text("https://a.example/rss\n", encoding="utf-8")
    ran: List[list] = []

    class P:
        returncode, stdout, stderr = 0, "ok", ""

    import adk.wakes as wakes
    monkeypatch.setattr(wakes, "resolve_bin", lambda: "/usr/bin/awrise")
    monkeypatch.setattr(cmd.subprocess, "run", lambda argv, **k: ran.append(argv) or P())

    import types
    args = types.SimpleNamespace(feeds=[str(feeds)], every="15m")
    assert cmd.cmd_schedule(args) == 0
    argv = ran[0]
    assert argv[:4] == ["/usr/bin/awrise", "add", "--name", "intel-scout"]
    assert argv[argv.index("--every") + 1] == "15m"
    run = argv[argv.index("--run") + 1]
    assert run.startswith("adk intel scout --once --feeds") and str(feeds.resolve()) in run
