"""find_search must drive awfind's real API (FindClient), not a non-existent Finder.

Regression: builtin_tools.find_search imported ``awfind.Finder`` -- a name the
package never exported -- so the ImportError branch answered "awfind not
available" on every box, including ones with awfind installed and configured.
"""

from __future__ import annotations

import json

import pytest

awfind = pytest.importorskip("awfind")

from adk import builtin_tools  # noqa: E402


class _FakeAnswer:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


def test_find_search_uses_findclient_not_finder(monkeypatch):
    seen = {}

    class _FakeClient:
        def __init__(self, base_url, token=None, *, verify=True, **_kw):
            seen["url"] = base_url
            seen["token"] = token
            seen["verify"] = verify

        def quick(self, query, **kwargs):
            seen["query"] = query
            seen["limit"] = kwargs.get("limit")
            return _FakeAnswer([
                awfind.Result({"title": "T1", "url": "https://a", "snippet": "s1"}),
                awfind.Result({"title": "T2", "url": "https://b", "snippet": "s2"}),
            ])

    monkeypatch.setattr(awfind, "FindClient", _FakeClient)
    monkeypatch.setenv("ADK_SEARCH_URL", "https://search.test:8114")
    monkeypatch.setenv("ADK_SEARCH_TOKEN", "caller-bearer")
    monkeypatch.delenv("ADK_SEARCH_CA_BUNDLE", raising=False)

    out = json.loads(builtin_tools.find_search("podman quadlets", limit=1))

    assert out.get("error") != "awfind not available", out
    assert "error" not in out, out
    assert out["count"] == 1
    assert out["results"][0] == {"title": "T1", "url": "https://a", "snippet": "s1", "rank": 1}
    assert seen["url"] == "https://search.test:8114"
    assert seen["token"] == "caller-bearer"
    assert seen["verify"] is not False
    assert seen["query"] == "podman quadlets"


def test_find_search_unconfigured_names_the_missing_url(monkeypatch, tmp_path):
    for name in ("ADK_SEARCH_URL", "AITHER_SEARCH_URL", "AWFIND_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWFIND_CONFIG", str(tmp_path / "none.json"))
    monkeypatch.setattr(awfind, "resolve_url",
                        lambda *a, **k: (_ for _ in ()).throw(awfind.UnresolvedError("no service URL")))

    out = json.loads(builtin_tools.find_search("x"))

    assert out.get("error") != "awfind not available", out
    assert "no service URL" in out["error"]
