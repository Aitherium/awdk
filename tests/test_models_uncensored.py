"""`adk models pull <id> --uncensored`: refusal paths and the gated happy path.

The network is a seam (``http_get``) and the downloader is a seam (``fetch_model``), so
these tests prove the one property that matters: NOTHING is downloaded unless the
platform returned the entry for this account, and every refusal names the missing step.
"""
from __future__ import annotations

import importlib
import sys
import urllib.error
from pathlib import Path
from typing import Any, Dict, List

import pytest

from adk.models import cli as models_cli
from adk.models import uncensored as unc

ENTRY = {
    "id": "tiny-abliterated", "name": "Tiny", "license": "apache-2.0",
    "gguf_url": "https://huggingface.co/org/tiny-GGUF/resolve/main/tiny.Q4_K_M.gguf",
    "file": "tiny.Q4_K_M.gguf", "size_bytes": 1000, "sha256": "a" * 64,
}


class _Sink:
    def __init__(self) -> None:
        self.out: List[str] = []
        self.err: List[str] = []

    def say(self, m: str) -> None:
        self.out.append(m)

    def errs(self, m: str) -> None:
        self.err.append(m)


def _never_download(*a: Any, **k: Any) -> Any:
    raise AssertionError("downloaded while the gate was closed")


def _answer(body: Dict[str, Any]):
    calls: List[tuple] = []

    def get(url: str, token: str, timeout: float) -> Dict[str, Any]:
        calls.append((url, token))
        return body
    get.calls = calls  # type: ignore[attr-defined]
    return get


def test_import_touches_no_network(monkeypatch):
    import socket

    def refuse(*a, **k):
        raise AssertionError("network at import time")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    import adk.models

    monkeypatch.delitem(sys.modules, "adk.models.uncensored")  # both restored after
    monkeypatch.setattr(adk.models, "uncensored", unc)
    importlib.import_module("adk.models.uncensored")


def test_unauthenticated_says_sign_in_and_downloads_nothing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(unc, "_saved_token", lambda: "")
    s = _Sink()
    get = _answer({"entries": [ENTRY]})
    rc = unc.pull("tiny-abliterated", tmp_path, s.say, s.errs, http_get=get,
                  fetch_model=_never_download)
    assert rc == 1
    assert get.calls == []  # no credential -> no request at all
    assert "not signed in" in s.err[0] and "adk login" in s.err[0]


@pytest.mark.parametrize("gate, needles", [
    ("age_verification_required", ["age verification", unc.SETTINGS_URL]),
    ("opt_in_required", ["switched off", unc.SETTINGS_URL]),
    ("account_restricted", ["cannot see uncensored models"]),
    ("unavailable", ["could not be checked", "nothing was downloaded"]),
])
def test_closed_gate_names_the_step(tmp_path: Path, gate: str, needles: List[str]):
    s = _Sink()
    get = _answer({"entries": [], "gate": gate})
    rc = unc.pull("tiny-abliterated", tmp_path, s.say, s.errs, token="tok",
                  base_url="https://api.example", http_get=get, fetch_model=_never_download)
    assert rc == 1
    assert get.calls == [("https://api.example/safety/config/user/uncensored-models", "tok")]
    for n in needles:
        assert n in s.err[0]


def test_401_reads_as_sign_in(tmp_path: Path):
    def get(url, token, timeout):
        raise urllib.error.HTTPError(url, 401, "no", {}, None)

    s = _Sink()
    rc = unc.pull("x", tmp_path, s.say, s.errs, token="stale", base_url="https://a.example",
                  http_get=get, fetch_model=_never_download)
    assert rc == 1 and "adk login" in s.err[0]


def test_offline_refuses_without_download(tmp_path: Path):
    def get(url, token, timeout):
        raise OSError("network unreachable")

    s = _Sink()
    rc = unc.pull("x", tmp_path, s.say, s.errs, token="t", base_url="https://a.example",
                  http_get=get, fetch_model=_never_download)
    assert rc == 1 and "could not reach your account" in s.err[0]


def test_garbage_answer_refuses(tmp_path: Path):
    s = _Sink()
    rc = unc.pull("x", tmp_path, s.say, s.errs, token="t", base_url="https://a.example",
                  http_get=_answer({"nope": 1}), fetch_model=_never_download)
    assert rc == 1 and "without a catalog" in s.err[0]


def test_unknown_id_lists_what_the_account_has(tmp_path: Path):
    s = _Sink()
    rc = unc.pull("other", tmp_path, s.say, s.errs, token="t", base_url="https://a.example",
                  http_get=_answer({"entries": [ENTRY]}), fetch_model=_never_download)
    assert rc == 1 and "tiny-abliterated" in s.err[0]


def test_open_gate_downloads_with_size_and_sha(tmp_path: Path):
    got: Dict[str, Any] = {}

    def fetch_model(model, dest, say):
        got.update(model=model, dest=dest)
        return dest / model["file"], True

    s = _Sink()
    rc = unc.pull("tiny-abliterated", tmp_path, s.say, s.errs, token="t",
                  base_url="https://a.example", http_get=_answer({"entries": [ENTRY]}),
                  fetch_model=fetch_model)
    assert rc == 0, s.err
    assert got["model"] == {"file": "tiny.Q4_K_M.gguf", "urls": [ENTRY["gguf_url"]],
                            "size_bytes": 1000, "sha256": "a" * 64}
    assert any("sha256 verified" in m for m in s.out)


def test_cli_flag_routes_to_the_gated_path(monkeypatch, tmp_path: Path):
    seen: Dict[str, Any] = {}

    def fake_pull(model_id, dest, say, err):
        seen.update(id=model_id, dest=dest)
        return 1

    monkeypatch.setattr(unc, "pull", fake_pull)

    def no_local_catalogue():
        raise AssertionError("the uncensored path must not read the local catalogue")

    monkeypatch.setattr(models_cli.catalogue, "load", no_local_catalogue)
    rc = models_cli.main(["pull", "tiny-abliterated", "--uncensored", "--dir", str(tmp_path)])
    assert rc == 1 and seen == {"id": "tiny-abliterated", "dest": tmp_path}
