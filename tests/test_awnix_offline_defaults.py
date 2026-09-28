"""Offline defaults an air-gapped image depends on.

Two facts, each of which fails silently when wrong:

- the self-host port scan must include the image's own local model ports, or an
  offline agent finds no model and has nowhere else to go;
- the harness daemon must bind loopback when the box is offline and nobody named a
  host, or it adds a LAN listener to a machine that is supposed to have none.
"""

from __future__ import annotations

from adk import local_inference
from adk.harnesses import daemon


def test_selfhost_ports_include_the_image_model_ports():
    assert 8199 in local_inference.SELFHOST_PORTS
    assert 8089 in local_inference.SELFHOST_PORTS


def test_selfhost_ports_keep_the_existing_ladder_first():
    # The new ports are appended: a desktop with a server on :8080 still finds it first.
    assert local_inference.SELFHOST_PORTS[0] == 8080
    assert len(set(local_inference.SELFHOST_PORTS)) == len(local_inference.SELFHOST_PORTS)


def test_offline_bind_default_is_loopback():
    assert daemon._default_bind_host({"AITHER_OFFLINE": "1"}) == "127.0.0.1"
    assert daemon._default_bind_host({"AITHER_OFFLINE": "true"}) == "127.0.0.1"


def test_online_bind_default_is_unchanged():
    assert daemon._default_bind_host({}) == "0.0.0.0"  # noqa: S104
    assert daemon._default_bind_host({"AITHER_OFFLINE": "0"}) == "0.0.0.0"  # noqa: S104


def test_explicit_host_overrides_offline():
    env = {"AITHER_OFFLINE": "1", "AITHER_HARNESS_BIND_HOST": "10.0.0.5"}
    assert daemon._default_bind_host(env) == "10.0.0.5"
    env = {"AITHER_OFFLINE": "1", "AITHER_HARNESS_HOST": "192.168.1.9"}
    assert daemon._default_bind_host(env) == "192.168.1.9"


def test_is_offline_reads_the_same_truthy_set():
    assert daemon._is_offline({"AITHER_OFFLINE": "on"}) is True
    assert daemon._is_offline({"AITHER_OFFLINE": ""}) is False
    assert daemon._is_offline({}) is False


def test_module_entry_point_does_not_hardcode_a_bind_all_host():
    # `python -m adk.harnesses.daemon` must defer to DEFAULT_BIND_HOST, or it bypasses
    # the offline loopback default above.
    import inspect

    src = inspect.getsource(daemon)
    main_block = src[src.index('if __name__ == "__main__":'):]
    assert 'add_argument("--host", default="")' in main_block
    assert '"0.0.0.0")' not in main_block


def test_offline_cors_default_is_loopback_only(monkeypatch):
    monkeypatch.delenv("AITHER_HARNESS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("AITHER_OFFLINE", "1")
    offline = daemon.allowed_origins()
    assert offline and all(daemon._is_loopback_origin(o) for o in offline), offline
    assert not any("aitherium.com" in o for o in offline)
    monkeypatch.setenv("AITHER_OFFLINE", "0")
    assert "https://api.aitherium.com" in daemon.allowed_origins()


def test_offline_cors_explicit_list_still_wins(monkeypatch):
    monkeypatch.setenv("AITHER_OFFLINE", "1")
    monkeypatch.setenv("AITHER_HARNESS_ALLOWED_ORIGINS", "https://portal.lan.example")
    assert daemon.allowed_origins() == ["https://portal.lan.example"]


def test_offline_skips_the_pypi_update_check(monkeypatch, tmp_path):
    """`adk harness serve` looked up pypi.org once a day, offline included."""
    import urllib.request

    from adk import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    dialed = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: dialed.append(a) or 1 / 0)
    monkeypatch.setenv("AITHER_OFFLINE", "1")
    cli._check_for_updates()
    assert dialed == []
    monkeypatch.setenv("AITHER_OFFLINE", "0")
    cli._check_for_updates()
    assert len(dialed) == 1  # online, the check still runs (and fails quietly here)
