"""adk.shell.update_check: the `aither` shell's once-a-day PyPI check.

It used to check the retired `aithershell` package and print
`pip install --upgrade aithershell`; these pin it to awdk on PyPI.
"""

import json
import time

import pytest

from adk.shell import update_check as uc


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    for k in ("AITHER_NO_UPDATE_CHECK", "ADK_NO_UPDATE_CHECK", "AITHER_OFFLINE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(uc, "UPDATE_CACHE", tmp_path / "update-check-awdk.json")
    monkeypatch.setattr(uc, "_installed_version", lambda: "3.8.0")
    monkeypatch.setattr(uc.sys, "prefix", str(tmp_path / "venv"))


def _pypi(monkeypatch, latest="3.9.1"):
    seen = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"info": {"version": latest}}).encode()

    def _urlopen(url, timeout=None):
        seen.append(url)
        return _Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return seen


def test_checks_awdk_on_pypi_and_prints_the_awdk_upgrade(monkeypatch):
    seen = _pypi(monkeypatch)
    msg = uc.check_for_update()
    assert seen == ["https://pypi.org/pypi/awdk/json"]
    assert msg and "3.8.0 -> 3.9.1" in msg
    assert "pip install --upgrade awdk" in msg
    assert "aithershell" not in msg


def test_installed_version_reads_the_awdk_dist(monkeypatch):
    import importlib.metadata as md
    asked = []
    monkeypatch.undo()  # drop the fixture's _installed_version stub
    monkeypatch.setattr(md, "version", lambda name: asked.append(name) or "1.0.0")
    assert uc._installed_version() == "1.0.0"
    assert asked == ["awdk"]


def test_no_notice_when_installed_is_newer_or_equal(monkeypatch):
    _pypi(monkeypatch, latest="3.8.0")
    assert uc.check_for_update() is None
    uc.UPDATE_CACHE.unlink()
    _pypi(monkeypatch, latest="3.7.9")
    assert uc.check_for_update() is None


def test_once_a_day_cache_and_upgrade_silences_it(monkeypatch):
    seen = _pypi(monkeypatch)
    assert uc.check_for_update()
    assert uc.check_for_update()
    assert len(seen) == 1  # second start served from the cache
    monkeypatch.setattr(uc, "_installed_version", lambda: "3.9.1")
    assert uc.check_for_update() is None  # compared against what is installed NOW


def test_stale_cache_refetches(monkeypatch):
    uc.UPDATE_CACHE.write_text(json.dumps({
        "package": "awdk", "latest_version": "3.8.5",
        "checked_at": time.time() - uc.CHECK_INTERVAL - 5}))
    seen = _pypi(monkeypatch)
    assert "3.9.1" in uc.check_for_update()
    assert len(seen) == 1


def test_old_aithershell_cache_is_ignored(monkeypatch):
    uc.UPDATE_CACHE.write_text(json.dumps({
        "latest_version": "99.0.0", "update_available": True, "checked_at": time.time()}))
    seen = _pypi(monkeypatch, latest="3.8.0")
    assert uc.check_for_update() is None
    assert len(seen) == 1


@pytest.mark.parametrize("var", ["AITHER_NO_UPDATE_CHECK", "ADK_NO_UPDATE_CHECK", "AITHER_OFFLINE"])
def test_opt_out_never_dials(monkeypatch, var):
    seen = _pypi(monkeypatch)
    monkeypatch.setenv(var, "1")
    assert uc.check_for_update() is None
    assert seen == []


def test_network_failure_is_silent(monkeypatch):
    import urllib.request

    def _boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert uc.check_for_update() is None
    uc.print_update_notice()  # never raises


def test_failed_lookup_is_not_retried_on_every_start(monkeypatch):
    # An offline box without AITHER_OFFLINE used to wait STARTUP_WAIT on every
    # start: a failed lookup left no cache, so the next start asked again.
    import urllib.request
    calls = []

    def _boom(*a, **k):
        calls.append(1)
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert uc.check_for_update() is None
    t0 = time.monotonic()
    assert uc.check_for_update() is None
    assert uc.check_for_update() is None
    assert time.monotonic() - t0 < 0.2
    assert len(calls) == 1


def test_hung_lookup_is_not_retried_and_keeps_the_old_version(monkeypatch):
    import urllib.request
    uc.UPDATE_CACHE.write_text(json.dumps({
        "package": "awdk", "latest_version": "3.8.5",
        "checked_at": time.time() - uc.CHECK_INTERVAL - 5}))
    calls = []

    def _hang(*a, **k):
        calls.append(1)
        time.sleep(1)
        raise OSError("late")
    monkeypatch.setattr(urllib.request, "urlopen", _hang)
    assert uc.check_for_update(wait=0.1) is None
    t0 = time.monotonic()
    msg = uc.check_for_update(wait=0.5)
    assert time.monotonic() - t0 < 0.2
    assert len(calls) == 1
    assert msg and "3.8.0 -> 3.8.5" in msg  # the stamp kept the last known release


def test_attempt_stamp_expires_after_the_interval(monkeypatch):
    uc.UPDATE_CACHE.write_text(json.dumps({
        "package": "awdk", "latest_version": "",
        "attempted_at": time.time() - uc.CHECK_INTERVAL - 5}))
    seen = _pypi(monkeypatch)
    assert "3.9.1" in uc.check_for_update()
    assert len(seen) == 1


def test_slow_pypi_does_not_block_startup(monkeypatch):
    import urllib.request

    def _slow(*a, **k):
        time.sleep(2)
        raise OSError("late")
    monkeypatch.setattr(urllib.request, "urlopen", _slow)
    t0 = time.monotonic()
    assert uc.check_for_update(wait=0.2) is None
    assert time.monotonic() - t0 < 1.0


@pytest.mark.parametrize("prefix,cmd", [
    ("/home/u/.local/share/pipx/venvs/awdk", "pipx upgrade awdk"),
    (r"C:\Users\u\pipx\venvs\awdk", "pipx upgrade awdk"),
    ("/home/u/.local/share/uv/tools/awdk", "uv tool upgrade awdk"),
    ("/usr", "pip install --upgrade awdk"),
])
def test_upgrade_command_per_install_method(prefix, cmd):
    assert uc.upgrade_command(prefix) == cmd
