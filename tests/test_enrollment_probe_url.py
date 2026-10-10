"""A probe URL separate from the advertised inference URL.

A Windows host serving the model from WSL2 behind NAT advertises its LAN URL (a netsh
portproxy), which the box itself cannot reach; it probes 127.0.0.1 instead and still
advertises the LAN URL.
"""
from __future__ import annotations

import json

import pytest

from adk import enrollment

LAN = "http://192.168.1.122:8091"
LOCAL = "http://127.0.0.1:8091"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.delenv("AITHER_NODE_INFERENCE_URL", raising=False)
    monkeypatch.delenv("AITHER_NODE_INFERENCE_PROBE_URL", raising=False)
    return tmp_path


@pytest.fixture
def server(monkeypatch):
    """Stub server: answers only at ``reachable``; records every probed base."""
    state = {"reachable": {LOCAL}, "models": [], "fingerprints": []}

    def models(base):
        state["models"].append(base)
        return (True, ["m"]) if base in state["reachable"] else (False, [])

    def fingerprint(base):
        state["fingerprints"].append(base)
        return "llama-server"

    monkeypatch.setattr(enrollment, "_openai_models", models)
    monkeypatch.setattr(enrollment, "_fingerprint", fingerprint)
    return state


def test_unset_probes_and_advertises_the_same_url(home, server):
    server["reachable"] = {LAN}
    assert enrollment.probe_inference(LAN) == (["m"], LAN, "llama-server", True)
    assert server["models"] == [LAN] and server["fingerprints"] == [LAN]
    assert enrollment.inference_probe_url(LAN) == ""


def test_unset_unreachable_lan_url_is_not_ready(home, server):
    probe = enrollment.probe_inference(LAN)
    assert probe == ([], LAN, "none", False)
    assert server["models"] == [LAN] and server["fingerprints"] == []


def test_env_probe_url_probes_local_and_advertises_lan(home, server, monkeypatch):
    monkeypatch.setenv("AITHER_NODE_INFERENCE_URL", LAN)
    monkeypatch.setenv("AITHER_NODE_INFERENCE_PROBE_URL", LOCAL + "/v1")
    probe = enrollment.probe_inference()
    assert probe == (["m"], LAN, "llama-server", True)
    assert server["models"] == [LOCAL] and server["fingerprints"] == [LOCAL]


def test_file_probe_url_probes_local_and_advertises_lan(home, server):
    assert enrollment.save_advertised_inference_url(LAN, probe_url=LOCAL) == LAN
    stored = json.loads((home / "node-inference.json").read_text())
    assert stored["url"] == LAN and stored["probe_url"] == LOCAL
    probe = enrollment.probe_inference()
    assert probe.inference_url == LAN and probe.ready is True
    assert server["models"] == [LOCAL] and server["fingerprints"] == [LOCAL]
    # build_registration reports the ADVERTISED url
    reg = enrollment.build_registration("n1")
    assert reg["inference_url"] == LAN and reg["inference_ready"] is True


def test_probe_fails_is_not_ready_and_keeps_the_advertised_url(home, server, monkeypatch):
    server["reachable"] = set()
    monkeypatch.setenv("AITHER_NODE_INFERENCE_URL", LAN)
    monkeypatch.setenv("AITHER_NODE_INFERENCE_PROBE_URL", LOCAL)
    assert enrollment.probe_inference() == ([], LAN, "none", False)
    assert server["models"] == [LOCAL]


def test_file_probe_url_only_pairs_with_its_own_url(home, server, monkeypatch):
    enrollment.save_advertised_inference_url(LAN, probe_url=LOCAL)
    other = "http://10.0.0.5:8114"
    monkeypatch.setenv("AITHER_NODE_INFERENCE_URL", other)
    probe = enrollment.probe_inference()
    assert probe.inference_url == other and probe.ready is False
    assert server["models"] == [other]


@pytest.mark.parametrize("bad", ["ftp://127.0.0.1:8091", "http://127.0.0.1",
                                 "http://u:dummy@127.0.0.1:8091", "http://127.0.0.1:8091/x"])
def test_probe_url_uses_the_url_validator(home, server, monkeypatch, bad):
    with pytest.raises(ValueError):
        enrollment.save_advertised_inference_url(LAN, probe_url=bad)
    assert not (home / "node-inference.json").exists()
    # an invalid env value is ignored: the advertised url is probed as before
    monkeypatch.setenv("AITHER_NODE_INFERENCE_PROBE_URL", bad)
    assert enrollment.inference_probe_url(LAN) == ""
    assert enrollment.probe_inference(LAN) == ([], LAN, "none", False)
    assert server["models"] == [LAN]


def test_a_tampered_file_probe_url_is_ignored(home, server):
    (home / "node-inference.json").write_text(
        json.dumps({"url": LAN, "probe_url": "file:///etc/passwd"}), encoding="utf-8")
    assert enrollment.inference_probe_url(LAN) == ""
    assert enrollment.probe_inference().inference_url == LAN
    assert server["models"] == [LAN]


def test_saving_without_probe_url_clears_it(home):
    enrollment.save_advertised_inference_url(LAN, probe_url=LOCAL)
    enrollment.save_advertised_inference_url(LAN)
    assert "probe_url" not in json.loads((home / "node-inference.json").read_text())
    assert enrollment.inference_probe_url(LAN) == ""
