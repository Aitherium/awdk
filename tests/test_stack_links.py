"""adk.stack_links: every aw* link, measured where it lives, and never reported ok on silence.

Each test fails on the code it pins: a link whose probe could not read it is ``unknown``/``down``,
never ``ok``; an absent program is not a fault; the daemon route answers a page only through the
``node`` pairing and carries no credential.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from adk import browser_grant, device_identity, fleet_enroll, node_commands, stack_links
from adk.agent import AitherAgent
from adk.server import create_app

HEALTH = {"ok": True, "service": "aithershell-harness", "sessions": 2,
          "harnesses_installed": ["claude", "awdk", "terminal"]}
SESSIONS = {"sessions": [
    {"id": "a", "harness": "claude", "state": "idle", "exit_code": None},
    {"id": "b", "harness": "terminal", "state": "idle", "exit_code": None},
]}
PROFILE = {"endpoint": "https://idp.aitherium.com", "token_type": "bearer", "access_token": "x",
           "user": {"username": "david", "display_name": "David"}}


def _get(table):
    def get(url, headers, timeout):
        for suffix, answer in table.items():
            if url.endswith(suffix):
                return answer
        return 0, None
    return get


def _which(found):
    return lambda name: (f"C:/bin/{name}.exe" if name in found else None)


def _by_id(result):
    return {lk["id"]: lk for lk in result["links"]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(device_identity.DEVICE_FILE_ENV, str(tmp_path / "device.json"))
    monkeypatch.setattr(fleet_enroll, "_AITHER_DIR", tmp_path)
    monkeypatch.setattr(fleet_enroll, "_NODE_AUTH_FILE", tmp_path / "node_auth.json")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _signed_in():
    store = MagicMock()
    store.get_active_profile.return_value = dict(PROFILE)
    creds = MagicMock()
    creds.is_expired = False
    return (patch("adk.auth.AuthStore", return_value=store),
            patch("adk.auth.resolve_credentials", return_value=creds))


def _beating(home, node="n1", ok_age=10):
    (home / "node_auth.json").write_text(json.dumps({"node_id": node}))
    (home / "device.json").write_text(json.dumps({"node_id": node, "leader": {
        "facet": "node_beat", "pid": 1, "ts": time.time(), "interval": 60,
        "ok_at": time.time() - ok_age}}))


def test_a_fully_linked_machine_reads_all_ok(home):
    _beating(home)
    (home / ".aither" / "awconnect").mkdir(parents=True)
    a, b = _signed_in()
    with a, b, patch("adk.extension_pair._load", return_value={"x": {}}):
        r = stack_links.measure(
            http_get=_get({"/health": (200, HEALTH), "/sessions": (200, SESSIONS)}),
            run=lambda cmd, t: (0, "in step: a pull would change nothing"),
            which=_which({"claude", "awsettings"}))
    assert [lk["id"] for lk in r["links"]] == list(stack_links.LINK_IDS)
    states = {k: v["state"] for k, v in _by_id(r).items()}
    assert states == {"aither-id": "ok", "device": "ok", "awsh": "ok", "claude": "ok",
                      "awsettings": "ok", "awconnect": "ok", "awnode": "ok"}
    assert r["all_ok"] is True and r["ok"] == r["total"] == 7
    assert _by_id(r)["aither-id"]["detail"] == "Signed in as David"


def test_nothing_answering_is_never_ok(home):
    store = MagicMock()
    store.get_active_profile.return_value = None
    with patch("adk.auth.AuthStore", return_value=store), \
            patch("adk.extension_pair._load", return_value={}):
        r = stack_links.measure(http_get=_get({}), run=lambda c, t: (-1, ""), which=_which(set()))
    by = _by_id(r)
    assert by["aither-id"]["state"] == "attention"
    assert by["device"]["state"] == "attention"
    assert by["awsh"]["state"] == "down" and by["claude"]["state"] == "down"
    assert by["awnode"]["state"] == "down"
    assert by["awsettings"]["state"] == "absent" and by["awconnect"]["state"] == "absent"
    assert r["all_ok"] is False and r["ok"] == 0
    for lk in r["links"]:
        if lk["state"] in ("down", "attention"):
            assert lk["fix"], f"{lk['id']} says it is broken and names no remedy"


def test_the_offline_root_login_is_not_a_sign_in(home):
    store = MagicMock()
    store.get_active_profile.return_value = {"endpoint": "local", "token_type": "local",
                                             "access_token": "x", "user": {"username": "root"}}
    with patch("adk.auth.AuthStore", return_value=store):
        lk = stack_links._aither_id()
    assert lk["state"] == "attention" and "offline" in lk["detail"].lower()


def test_claude_with_no_session_is_ready_and_says_so(home):
    lk = stack_links._claude(_get({"/sessions": (200, {"sessions": []})}), HEALTH,
                             _which({"claude"}))
    assert lk["state"] == "ok" and "none open" in lk["detail"].lower()


def test_an_exited_claude_session_does_not_count(home):
    dead = {"sessions": [{"harness": "claude", "state": "exited", "exit_code": 0}]}
    lk = stack_links._claude(_get({"/sessions": (200, dead)}), HEALTH, _which({"claude"}))
    assert lk["state"] == "ok" and "none open" in lk["detail"].lower()


def test_a_refused_session_list_is_unknown_not_ok(home):
    lk = stack_links._claude(_get({"/sessions": (401, None)}), HEALTH, _which({"claude"}))
    assert lk["state"] == "unknown"


def test_a_stale_beat_is_down_and_an_unconfirmed_one_is_attention(home):
    _beating(home, ok_age=3600)
    assert stack_links._device(time.time())["state"] == "down"
    data = json.loads((home / "device.json").read_text())
    del data["leader"]["ok_at"]
    (home / "device.json").write_text(json.dumps(data))
    assert stack_links._device(time.time())["state"] == "attention"


def test_awsettings_that_differs_is_attention(home):
    said = "local: x\nremote: y\na pull would change 3 keys"
    lk = stack_links._awsettings(lambda c, t: (0, said), _which({"awsettings"}))
    assert lk["state"] == "attention" and lk["command"] == "awsettings pull"


def test_awconnect_installed_but_unpaired_is_attention(home):
    (home / ".aither" / "awconnect").mkdir(parents=True)
    with patch("adk.extension_pair._load", return_value={}):
        assert stack_links._awconnect(time.time())["state"] == "attention"


# --- the daemon serves it, behind the page pairing -----------------------------------------

PAGE = "https://aitherium.com"
NODE = "adk-test-desktop"
KEY = "cd" * 32


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    for k in ("AITHER_OFFLINE", "AITHER_SERVER_API_KEY", "AITHER_CORS_ORIGINS",
              "AITHER_BROWSER_HANDOFF"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AITHER_LOCAL_AUTH", "required")
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv(device_identity.DEVICE_FILE_ENV, str(tmp_path / "device.json"))
    monkeypatch.setattr(fleet_enroll, "_load_node_auth",
                        lambda: {"node_id": NODE, "mode": "rich"})
    node_commands.save_key(NODE, KEY)
    browser_grant._nonces.clear()
    browser_grant._tokens.clear()
    agent = MagicMock(spec=AitherAgent)
    agent.name = "test-agent"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    return TestClient(create_app(agent=agent), client=("127.0.0.1", 50400),
                      raise_server_exceptions=False)


def _token(c):
    nonce = c.get("/local/browser-pair/challenge", headers={"Origin": PAGE}).json()["nonce"]
    g = {"kind": "browser-grant/v1", "node_id": NODE, "tenant_id": "platform", "user_id": "o",
         "origin": PAGE, "nonce": nonce, "scope": "node", "issued_at": int(time.time()),
         "expires_at": int(time.time()) + 120}
    g["sig"] = node_commands.sign(KEY, node_commands.canonical(g, browser_grant.GRANT_FIELDS))
    r = c.post("/local/browser-pair", json={"grant": g}, headers={"Origin": PAGE})
    return r.json()["token"]


def test_an_unpaired_page_cannot_read_the_links(daemon):
    assert daemon.get("/stack/links", headers={"Origin": PAGE}).status_code == 401


def test_a_paired_page_reads_the_links_and_no_credential(daemon):
    h = {"Origin": PAGE, "Authorization": f"Bearer {_token(daemon)}"}
    with patch("adk.stack_links.measure", return_value={"links": [], "ok": 0, "total": 0,
                                                        "all_ok": True}):
        r = daemon.get("/stack/links", headers=h)
    assert r.status_code == 200 and r.json()["all_ok"] is True
    assert "token" not in r.text.lower()


def test_awsettings_is_judged_at_user_level_never_by_the_working_directory(home):
    seen = []
    stack_links._awsettings(lambda c, t: seen.append(c) or (0, "in step"), _which({"awsettings"}))
    assert "--user" in seen[0]


def test_a_failing_settings_service_is_unknown_and_says_it_is_the_service(home):
    dead = "remote: x\nDEAD: GET https://x/y: Server error '503 Service Unavailable'"
    lk = stack_links._awsettings(lambda c, t: (2, dead), _which({"awsettings"}))
    assert lk["state"] == "unknown" and "service" in lk["detail"]


# --- plain words, one clock, the harness URL, concurrency ----------------------------------

import re  # noqa: E402

_JARGON = re.compile(r"awsh|awnode|awsettings|awconnect|awdk|`|adk|harness|daemon", re.I)


def test_no_label_detail_or_fix_needs_a_terminal_or_a_dictionary(home):
    store = MagicMock()
    store.get_active_profile.return_value = None
    with patch("adk.auth.AuthStore", return_value=store), patch(
            "adk.extension_pair._load", return_value={}):
        r = stack_links.measure(http_get=_get({}), run=lambda c, t: (-1, ""), which=_which(set()))
    (home / ".aither" / "awconnect").mkdir(parents=True)
    with patch("adk.extension_pair._load", return_value={}):
        r["links"].append(stack_links._awconnect(time.time()))
    for lk in r["links"]:
        for field in ("label", "detail", "fix"):
            assert not _JARGON.search(lk[field]), f"{lk['id']}.{field} = {lk[field]!r}"


def test_commands_ride_in_their_own_field(home):
    r = stack_links.measure(http_get=_get({}), run=lambda c, t: (-1, ""), which=_which(set()))
    by = _by_id(r)
    assert by["awsh"]["command"] == "adk harness serve"
    assert by["awnode"]["command"] == "awnode up"
    assert all("command" in lk for lk in r["links"])


def test_the_harness_url_honours_the_port_override(home, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_PORT", "9555")
    monkeypatch.delenv("AITHER_HARNESS_URL", raising=False)
    seen = []

    def get(url, headers, timeout):
        seen.append(url)
        return (200, HEALTH) if url.endswith("/health") else (200, SESSIONS)

    link, health = stack_links._awsh(get)
    assert link["state"] == "ok" and health is not None
    stack_links._claude(get, health, _which({"claude"}))
    assert seen and all(u.startswith("http://127.0.0.1:9555/") for u in seen)


def test_probes_run_side_by_side_and_a_slow_one_reads_unknown_not_ok(home):
    import threading

    gate = threading.Event()

    def slow_run(cmd, timeout):
        gate.wait(5)
        return 0, "in step"

    t0 = time.monotonic()
    r = stack_links._measure(None, _get({"/health": (200, HEALTH), "/sessions": (200, SESSIONS)}),
                             slow_run, _which({"claude", "awsettings"}), deadline=0.4)
    gate.set()
    assert time.monotonic() - t0 < 2.0
    by = _by_id(r)
    assert by["awsettings"]["state"] == "unknown"
    assert by["awsh"]["state"] == "ok" and by["claude"]["state"] in ("ok", "attention")


def test_the_live_measurement_is_cached_so_a_polling_page_costs_one(home, monkeypatch):
    calls = []
    monkeypatch.setattr(stack_links, "_measure",
                        lambda *a, **k: calls.append(1) or {"links": [], "ok": 0, "total": 0,
                                                            "all_ok": True})
    stack_links._cache.update(at=0.0, value=None)
    stack_links.measure()
    stack_links.measure()
    assert len(calls) == 1
    stack_links._cache.update(at=0.0, value=None)


def test_awnode_is_the_hub_only_and_a_bonsai_port_does_not_count(home):
    seen = []

    def get(url, headers, timeout):
        seen.append(url)
        return (200, {}) if ":8090/" in url else (0, None)

    assert stack_links._awnode(get)["state"] == "down"
    assert all(f":{stack_links.AWNODE_HUB_PORT}/" in u for u in seen)


def test_a_bad_hub_port_env_does_not_break_the_import(monkeypatch):
    monkeypatch.setenv("AW_HUB_PORT", "not-a-port")
    assert stack_links._port("AW_HUB_PORT", 47933) == 47933
    monkeypatch.setenv("AW_HUB_PORT", "47000")
    assert stack_links._port("AW_HUB_PORT", 47933) == 47000


def test_a_device_json_id_that_differs_from_node_auth_is_still_this_machines_beat(home):
    """Owner's PC 2026-11-16: device.json top-level id != node_auth id, fresh node_beat lease."""
    _beating(home, node="adk-node-auth")
    data = json.loads((home / "device.json").read_text())
    data["node_id"] = "adk-last-facet"
    (home / "device.json").write_text(json.dumps(data))
    assert stack_links._device(time.time())["state"] == "ok"
    data["leader"]["node_id"] = "someone-else-entirely"
    (home / "device.json").write_text(json.dumps(data))
    assert stack_links._device(time.time())["state"] == "down"
