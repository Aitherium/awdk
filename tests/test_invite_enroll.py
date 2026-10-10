"""``adk enroll --invite``: peek -> (human check) -> accept -> device, against a fake API.

Pins: the steps run in order with the secret only in request bodies; a second PC of an
existing member skips accept; "waiting for approval" exits 3 without enrolling; a refusal
stops before the device step; a bad code never reaches the network; the device reply is
saved; the parser carries --invite and cmd_enroll stops on a non-zero invite step.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from adk import invite_enroll as ie


class _Resp:
    def __init__(self, status: int, body: Optional[Dict[str, Any]] = None) -> None:
        self.status_code = status
        self._body = body or {}

    def json(self) -> Dict[str, Any]:
        return self._body


class _Client:
    def __init__(self, replies: Dict[str, _Resp]) -> None:
        self.replies = replies
        self.calls: List[tuple] = []

    def post(self, path: str, json: Dict[str, Any]) -> _Resp:  # noqa: A002 -- httpx's name
        self.calls.append((path, json))
        return self.replies[path]


PEEK = {"invite_id": "inv-0123456789abcdef", "target": "mesh", "network": "Acme",
        "role": "operator", "requires_human": False, "already_member": False,
        "status": "open"}
DEVICE = {"device_id": "dev-1", "device_token": "tok-secret", "workspace_id": "ws1",
          "tenant_id": "tnt_acme", "device_role": "operator", "network": "Acme",
          "invite_id": "inv-0123456789abcdef", "heartbeat_s": 300}


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(ie, "_bearer", lambda: "bearer-x")


def _run(client, tmp_path, code="abcde-23456", **kw):
    kw.setdefault("assume_yes", True)
    lines: List[str] = []
    rc = ie.run_invite_enroll(code, client=client, out=lines.append,
                              state_file=tmp_path / "invite_devices.json", **kw)
    return rc, "\n".join(lines)


def test_normalize_secret_accepts_codes_and_tokens_and_refuses_junk():
    assert ie.normalize_secret("abcde 23456") == ("ABCDE-23456", "3456")
    assert ie.normalize_secret("ABCDE-23456")[0] == "ABCDE-23456"
    tok = "awb1." + "a" * 40
    assert ie.normalize_secret(tok) == (tok, "aaaa")
    for bad in ("", "ABCDE-2345", "ABCDE-2345O", "abcde-23456; rm -rf /", "awb1.<x>"):
        with pytest.raises(ValueError):
            ie.normalize_secret(bad)


def test_full_flow_peeks_accepts_and_enrolls_this_device(signed_in, tmp_path):
    c = _Client({"/invites/peek": _Resp(200, PEEK),
                 "/invites/accept": _Resp(200, {"status": "joined"}),
                 "/invites/device": _Resp(201, DEVICE)})
    rc, text = _run(c, tmp_path)
    assert rc == ie.EXIT_OK, text
    assert [p for p, _ in c.calls] == ["/invites/peek", "/invites/accept", "/invites/device"]
    assert c.calls[0][1] == {"invite": "ABCDE-23456"}
    assert c.calls[1][1] == {"invite": "ABCDE-23456", "attestation": ""}
    device_body = c.calls[2][1]
    assert device_body["invite_id"] == PEEK["invite_id"] and "invite" not in device_body
    assert "Acme" in text and "operator" in text
    assert "ABCDE-23456" not in text  # only the last four characters are ever printed
    saved = json.loads((tmp_path / "invite_devices.json").read_text(encoding="utf-8"))
    assert saved[PEEK["invite_id"]]["device_id"] == "dev-1"


def test_a_second_pc_of_a_member_skips_accept(signed_in, tmp_path):
    c = _Client({"/invites/peek": _Resp(200, dict(PEEK, already_member=True)),
                 "/invites/device": _Resp(201, DEVICE)})
    rc, _ = _run(c, tmp_path)
    assert rc == ie.EXIT_OK
    assert [p for p, _ in c.calls] == ["/invites/peek", "/invites/device"]


def test_already_member_on_accept_still_enrolls_the_device(signed_in, tmp_path):
    c = _Client({"/invites/peek": _Resp(200, PEEK),
                 "/invites/accept": _Resp(409, {"detail": "already_member"}),
                 "/invites/device": _Resp(201, DEVICE)})
    assert _run(c, tmp_path)[0] == ie.EXIT_OK


def test_waiting_for_approval_exits_3_without_a_device(signed_in, tmp_path):
    c = _Client({"/invites/peek": _Resp(200, PEEK),
                 "/invites/accept": _Resp(200, {"status": "pending_approval",
                                                "request_id": "req-1"})})
    rc, text = _run(c, tmp_path)
    assert rc == ie.EXIT_PENDING and "approve" in text
    assert "/invites/device" not in [p for p, _ in c.calls]


def test_a_refusal_stops_before_the_device_step(signed_in, tmp_path):
    c = _Client({"/invites/peek": _Resp(410, {"detail": "invite_expired"})})
    rc, text = _run(c, tmp_path)
    assert rc == ie.EXIT_FAIL and "expired" in text
    assert [p for p, _ in c.calls] == ["/invites/peek"]


def test_human_check_answers_go_to_the_judge_and_the_attestation_to_accept(
        signed_in, tmp_path, monkeypatch):
    monkeypatch.setattr(ie.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    c = _Client({"/invites/peek": _Resp(200, dict(PEEK, requires_human=True)),
                 "/invites/human/challenge": _Resp(200, {
                     "set_id": "set-12345678",
                     "challenges": [{"id": "c1", "prompt": "Describe your morning."}]}),
                 "/invites/human/answer": _Resp(200, {"passed": True, "attestation": "att"}),
                 "/invites/accept": _Resp(200, {"status": "joined"}),
                 "/invites/device": _Resp(201, DEVICE)})
    rc, _ = _run(c, tmp_path, ask=lambda prompt: "coffee, then the bus")
    assert rc == ie.EXIT_OK
    answer = dict(c.calls)["/invites/human/answer"]
    assert answer["answers"] == {"c1": "coffee, then the bus"}
    assert dict(c.calls)["/invites/accept"]["attestation"] == "att"


def test_bad_code_or_no_sign_in_never_reaches_the_network(tmp_path, monkeypatch):
    c = _Client({})
    monkeypatch.setattr(ie, "_bearer", lambda: "bearer-x")
    assert _run(c, tmp_path, code="nope")[0] == ie.EXIT_USAGE
    monkeypatch.setattr(ie, "_bearer", lambda: "")
    assert _run(c, tmp_path)[0] == ie.EXIT_FAIL
    assert c.calls == []


def test_cli_parser_and_enroll_stop_on_a_failed_invite(monkeypatch):
    from adk import cli

    enroll = [c for c in cli.build_command_manifest() if c.get("name") == "enroll"]
    flags = {f for a in enroll[0]["args"] for f in a.get("flags", [])}
    assert {"--invite", "--label"} <= flags
    monkeypatch.setattr("adk.fleet_enroll._load_auth_config",
                        lambda: {"tenant_slug": "acme"})
    seen = {}

    def _fake(invite, label="", assume_yes=False):
        seen["invite"], seen["label"], seen["yes"] = invite, label, assume_yes
        return ie.EXIT_PENDING

    monkeypatch.setattr(ie, "run_invite_enroll", _fake)
    rc = cli.cmd_enroll(SimpleNamespace(invite="ABCDE-23456", label="Desk", yes=True))
    assert rc == ie.EXIT_PENDING
    assert seen == {"invite": "ABCDE-23456", "label": "Desk", "yes": True}
    assert "--yes" in flags


def test_confirmation_names_the_codes_org_and_a_no_enrolls_nothing(signed_in, tmp_path):
    # A link opened on one org's page can carry another org's code: the prompt
    # names the org the SERVER says the code belongs to, and "no" stops before accept.
    c = _Client({"/invites/peek": _Resp(200, PEEK)})
    asked: List[str] = []
    rc, text = _run(c, tmp_path, assume_yes=False, interactive=True,
                    ask=lambda q: asked.append(q) or "n")
    assert rc != 0 and "Not enrolled." in text
    assert asked and "Acme" in asked[0] and "operator" in asked[0]
    assert [p for p, _ in c.calls] == ["/invites/peek"]


def test_unattended_without_yes_refuses_before_accept(signed_in, tmp_path):
    c = _Client({"/invites/peek": _Resp(200, PEEK)})
    rc, text = _run(c, tmp_path, assume_yes=False, interactive=False)
    assert rc == ie.EXIT_USAGE and "--yes" in text
    assert [p for p, _ in c.calls] == ["/invites/peek"]
