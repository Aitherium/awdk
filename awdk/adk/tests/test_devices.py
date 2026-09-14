"""adk devices: one registry, refusals verbatim, and every new verb reachable.

The last part is the one that matters most: ``cmd_up`` exists in cli.py and was
never registered in the parser, so the phone door invoked a verb that did not
exist. Every ``cmd_*`` this change adds is proven reachable through BOTH the
parser and ``main()``'s dispatch here.
"""

from __future__ import annotations

import argparse
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from adk import devices, fleet_enroll


@pytest.fixture
def aither_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_enroll, "_AITHER_DIR", tmp_path)
    monkeypatch.setattr(fleet_enroll, "_NODE_AUTH_FILE", tmp_path / "node_auth.json")
    monkeypatch.setattr(fleet_enroll, "_AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(fleet_enroll, "_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("AITHER_NODE_TOKEN", raising=False)
    monkeypatch.setenv("AITHER_ENROLL_BASE", "https://identity.test/")
    return tmp_path


@pytest.fixture
def signed_in(aither_dir):
    (aither_dir / "auth.json").write_text(json.dumps({
        "version": 1, "active_profile": "cloud",
        "profiles": {"cloud": {"access_token": "tok-1", "user": {"tenant_slug": "acme"}}},
    }), encoding="utf-8")
    return aither_dir


@pytest.fixture
def http(monkeypatch):
    """Record every request; answer from a queue of ``(status, body)``."""
    calls = []
    answers = []

    def _send(method, url, headers, timeout):
        calls.append({"method": method, "url": url, "headers": headers})
        status, body = answers.pop(0)
        return status, body if isinstance(body, str) else json.dumps(body)

    monkeypatch.setattr(devices, "_send", _send)
    return SimpleNamespace(calls=calls, answers=answers)


def _args(sub, node_id=None, as_json=False):
    return SimpleNamespace(devices_command=sub, node_id=node_id, json=as_json)


NODES = {
    "endpoints": [
        {"node_id": "adk-aaaa-1111", "hostname": "pixel", "node_class": "phone",
         "status": "online", "last_seen": "2026-09-13T10:00:00Z",
         "inference_kind": "llama-server", "inference_ready": True,
         "available_models": ["tiny-model-a"],
         "public_url": "https://gateway.aitherium.com/nodes/adk-aaaa-1111"},
        {"node_id": "adk-bbbb-2222", "hostname": "laptop", "node_class": "laptop",
         "status": "offline", "last_seen": "2026-09-12T10:00:00Z",
         "inference_kind": "none", "inference_ready": False, "available_models": []},
    ],
    "total": 2, "online": 1, "inference_ready": 1,
}


# ---------------------------------------------------------------------------
# bearer + base resolution
# ---------------------------------------------------------------------------


def test_no_token_refuses_without_a_request(aither_dir, http, capsys):
    rc = devices.cmd_devices(_args("list"))
    assert rc == 1
    assert http.calls == []
    assert "adk login" in capsys.readouterr().out


def test_env_token_wins(aither_dir, monkeypatch):
    monkeypatch.setenv("AITHER_NODE_TOKEN", "env-tok")
    assert devices.resolve_bearer() == "env-tok"


def test_login_profile_token_is_used_and_base_is_identity(signed_in, http):
    http.answers.append((200, NODES))
    assert devices.cmd_devices(_args("list")) == 0
    call = http.calls[0]
    assert call["url"] == "https://identity.test/v1/nodes"
    assert call["headers"]["Authorization"] == "Bearer tok-1"


def test_root_placeholder_is_not_a_bearer(aither_dir):
    (aither_dir / "auth.json").write_text(json.dumps({
        "version": 1, "active_profile": "local",
        "profiles": {"local": {"access_token": "aither_root_local", "is_local_root": True}},
    }), encoding="utf-8")
    assert devices.resolve_bearer() == ""


# ---------------------------------------------------------------------------
# list / status / rm
# ---------------------------------------------------------------------------


def test_list_prints_every_device_and_marks_this_one(signed_in, http, capsys):
    (signed_in / "node_auth.json").write_text(
        json.dumps({"node_id": "adk-aaaa-1111"}), encoding="utf-8")
    http.answers.append((200, NODES))
    assert devices.cmd_devices(_args("list")) == 0
    out = capsys.readouterr().out
    assert "adk-aaaa-1111 *" in out
    assert "adk-bbbb-2222" in out
    assert "llama-server ready tiny-model-a" in out
    assert "https://gateway.aitherium.com/nodes/adk-aaaa-1111" in out
    assert "2 device(s)" in out


def test_list_json_prints_the_raw_body(signed_in, http, capsys):
    http.answers.append((200, NODES))
    assert devices.cmd_devices(_args("list", as_json=True)) == 0
    assert json.loads(capsys.readouterr().out) == NODES


def test_list_empty(signed_in, http, capsys):
    http.answers.append((200, {"endpoints": [], "total": 0, "online": 0, "inference_ready": 0}))
    assert devices.cmd_devices(_args("list")) == 0
    assert "No devices enrolled" in capsys.readouterr().out


def test_status_defaults_to_this_device(signed_in, http, capsys):
    (signed_in / "node_auth.json").write_text(
        json.dumps({"node_id": "adk-aaaa-1111"}), encoding="utf-8")
    http.answers.append((200, NODES["endpoints"][0]))
    assert devices.cmd_devices(_args("status")) == 0
    assert http.calls[0]["url"] == "https://identity.test/v1/nodes/adk-aaaa-1111"
    out = capsys.readouterr().out
    assert "(this device)" in out and "phone" in out


def test_status_without_id_and_not_enrolled_says_so(signed_in, http, capsys):
    assert devices.cmd_devices(_args("status")) == 1
    assert http.calls == []
    assert "not enrolled" in capsys.readouterr().out


def test_status_explicit_id(signed_in, http):
    http.answers.append((200, NODES["endpoints"][1]))
    assert devices.cmd_devices(_args("status", node_id="adk-bbbb-2222")) == 0
    assert http.calls[0]["url"].endswith("/v1/nodes/adk-bbbb-2222")


def test_rm_deletes_and_clears_local_enrollment_when_it_is_this_device(signed_in, http, capsys):
    (signed_in / "node_auth.json").write_text(
        json.dumps({"node_id": "adk-aaaa-1111"}), encoding="utf-8")
    http.answers.append((200, {"status": "deleted"}))
    assert devices.cmd_devices(_args("rm", node_id="adk-aaaa-1111")) == 0
    assert http.calls[0]["method"] == "DELETE"
    assert not (signed_in / "node_auth.json").exists()
    assert "Removed adk-aaaa-1111" in capsys.readouterr().out


def test_rm_other_device_keeps_local_enrollment(signed_in, http):
    (signed_in / "node_auth.json").write_text(
        json.dumps({"node_id": "adk-aaaa-1111"}), encoding="utf-8")
    http.answers.append((204, ""))
    assert devices.cmd_devices(_args("rm", node_id="adk-bbbb-2222")) == 0
    assert (signed_in / "node_auth.json").exists()


def test_rm_404_is_printed_and_nonzero(signed_in, http, capsys):
    http.answers.append((404, {"detail": "node not found"}))
    assert devices.cmd_devices(_args("rm", node_id="adk-gone")) == 1
    assert "node not found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# refusals are the product speaking: verbatim, non-zero
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,body", [
    (402, {"detail": "subscription_required",
           "upgrade_url": "https://aitherium.com/pricing", "feature": "byoc_node_enrollment"}),
    (403, {"detail": "device_quota_exceeded", "quota": 3, "used": 3}),
])
def test_refusal_body_is_printed_verbatim(signed_in, http, capsys, status, body):
    raw = json.dumps(body)
    http.answers.append((status, raw))
    assert devices.cmd_devices(_args("list")) == 1
    out = capsys.readouterr().out
    assert f"HTTP {status}" in out
    assert raw in out  # the exact bytes, not a paraphrase


def test_transport_failure_names_the_url(signed_in, monkeypatch, capsys):
    def _boom(method, url, headers, timeout):
        raise ConnectionError("no route")

    monkeypatch.setattr(devices, "_send", _boom)
    assert devices.cmd_devices(_args("list")) == 1
    out = capsys.readouterr().out
    assert "https://identity.test/v1/nodes" in out and "no route" in out


def test_unknown_subcommand_is_usage(signed_in, capsys):
    assert devices.cmd_devices(_args(None)) == 2
    assert "usage" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# every cmd_* added here is reachable: parser AND dispatch
# ---------------------------------------------------------------------------


def _parser():
    from adk.cli import _register_commands

    p = argparse.ArgumentParser(prog="adk")
    _register_commands(p.add_subparsers(dest="command"))
    return p


@pytest.mark.parametrize("argv,expect", [
    (["devices", "list"], {"command": "devices", "devices_command": "list", "json": False}),
    (["devices", "list", "--json"], {"devices_command": "list", "json": True}),
    (["devices", "status"], {"devices_command": "status", "node_id": None}),
    (["devices", "status", "adk-x"], {"devices_command": "status", "node_id": "adk-x"}),
    (["devices", "rm", "adk-x"], {"devices_command": "rm", "node_id": "adk-x"}),
    (["enroll"], {"command": "enroll", "inference_url": "auto", "node_class": "laptop"}),
    (["enroll", "--inference-url", "http://127.0.0.1:8080", "--node-class", "phone"],
     {"inference_url": "http://127.0.0.1:8080", "node_class": "phone"}),
    (["quickstart"], {"command": "quickstart", "cloud": False, "inference_url": "auto"}),
    (["quickstart", "--cloud"], {"cloud": True}),
    (["quickstart", "--node-class", "sovereign"], {"node_class": "sovereign"}),
])
def test_parser_accepts_the_new_verbs_and_flags(argv, expect):
    ns = vars(_parser().parse_args(argv))
    for k, v in expect.items():
        assert ns[k] == v, (k, ns)


def test_parser_rejects_an_unknown_node_class():
    with pytest.raises(SystemExit):
        _parser().parse_args(["enroll", "--node-class", "toaster"])


def test_devices_rm_requires_an_id():
    with pytest.raises(SystemExit):
        _parser().parse_args(["devices", "rm"])


def _run_main(argv):
    """Run adk.cli.main() with argv; return the SystemExit code."""
    import adk.cli as cli

    with patch.object(sys, "argv", ["adk", *argv]), \
            patch.object(cli, "_check_for_updates", lambda: None):
        try:
            cli.main()
        except SystemExit as e:
            return e.code
    return None


def test_main_dispatches_devices_to_cmd_devices():
    seen = {}

    def fake(args):
        seen["args"] = args
        return 7

    with patch("adk.devices.cmd_devices", fake):
        assert _run_main(["devices", "status", "adk-z"]) == 7
    assert seen["args"].devices_command == "status"
    assert seen["args"].node_id == "adk-z"


def test_main_dispatches_enroll_with_the_new_flags():
    import adk.cli as cli

    seen = {}

    def fake(args):
        seen["args"] = args
        return 0

    with patch.object(cli, "cmd_enroll", fake):
        assert _run_main(["enroll", "--inference-url", "http://127.0.0.1:1", "--node-class",
                          "phone"]) == 0
    assert seen["args"].inference_url == "http://127.0.0.1:1"
    assert seen["args"].node_class == "phone"


def test_main_dispatches_quickstart():
    import adk.cli as cli

    with patch.object(cli, "cmd_quickstart", lambda a: 3):
        assert _run_main(["quickstart"]) == 3


def test_every_cmd_function_this_change_added_is_dispatched():
    """The cmd_up shape: a handler nobody can reach. Assert the reverse for ours."""
    import inspect

    import adk.cli as cli

    src = inspect.getsource(cli.main)
    verbs = set(vars(_parser())["_subparsers"]._group_actions[0].choices)
    for verb, needle in (
        ("devices", "cmd_devices(args)"),
        ("enroll", "cmd_enroll(args)"),
        ("quickstart", "cmd_quickstart(args)"),
    ):
        assert verb in verbs, f"{verb} not registered in the parser"
        assert f'args.command == "{verb}"' in src, f"{verb} not dispatched in main()"
        assert needle in src


# ---------------------------------------------------------------------------
# quickstart (no flag) = login -> enroll --inference-url auto -> devices status
# ---------------------------------------------------------------------------


def test_quickstart_default_chains_login_enroll_status(signed_in, capsys):
    import adk.cli as cli

    order = []

    def fake_enroll(args):
        order.append(("enroll", args.inference_url, args.node_class))
        return 0

    def fake_status(args):
        order.append(("devices", args.devices_command, args.node_id))
        return 0

    def fake_login(args):
        order.append(("login",))
        return 0

    ns = SimpleNamespace(cloud=False, api_key=None, inference_url="auto", node_class="phone")
    with patch.object(cli, "cmd_enroll", fake_enroll), \
            patch.object(cli, "cmd_login", fake_login), \
            patch("adk.devices.cmd_devices", fake_status):
        assert cli.cmd_quickstart(ns) == 0
    # signed in already -> login skipped, enroll then status
    assert order == [("enroll", "auto", "phone"), ("devices", "status", None)]
    assert "Already signed in" in capsys.readouterr().out


def test_quickstart_default_runs_login_when_signed_out(aither_dir):
    import adk.cli as cli

    order = []

    with patch.object(cli, "cmd_login", lambda a: order.append("login") or 0), \
            patch.object(cli, "cmd_enroll", lambda a: order.append("enroll") or 0), \
            patch("adk.devices.cmd_devices", lambda a: order.append("status") or 0):
        ns = SimpleNamespace(cloud=False, api_key=None, inference_url="auto", node_class="laptop")
        assert cli.cmd_quickstart(ns) == 0
    assert order == ["login", "enroll", "status"]


def test_quickstart_stops_on_a_refused_enroll(signed_in):
    import adk.cli as cli

    with patch.object(cli, "cmd_enroll", lambda a: 1), \
            patch("adk.devices.cmd_devices", lambda a: pytest.fail("status ran after refusal")):
        ns = SimpleNamespace(cloud=False, api_key=None, inference_url="auto", node_class="laptop")
        assert cli.cmd_quickstart(ns) == 1
