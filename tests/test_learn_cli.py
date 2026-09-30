"""`adk learn` (child's terminal) and `adk bonsai setup --device phone`.

Two halves:
  * the phone install stays LIGHT: importing adk.cli and running `adk learn --help`,
    `adk login --help` and `adk bonsai setup --help` in a fresh interpreter, with
    torch/CUDA/numpy made unimportable, succeeds and loads none of them;
  * the quest loop talks the learner contract (/api/tutor/me/*) and speaks kindly.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest
from adk import bonsai_phone, learn_cli

ROOT = Path(__file__).resolve().parents[1]

HEAVY = (
    "torch",
    "torchvision",
    "sentence_transformers",
    "transformers",
    "numpy",
    "cupy",
    "pycuda",
    "nvidia",
    "llama_cpp",
    "onnxruntime",
    "tensorflow",
    "jax",
)

_PROBE = textwrap.dedent(
    """
    import importlib.abc, json, sys
    HEAVY = {heavy!r}
    attempts = []

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in HEAVY:
                attempts.append(name)
                raise ImportError("blocked for the phone-install test: " + name)
            return None

    sys.meta_path.insert(0, Block())
    sys.argv = ["adk"] + {argv!r}
    code = None
    import adk.cli
    try:
        adk.cli.main()
    except SystemExit as exc:
        code = exc.code
    loaded = sorted(m for m in sys.modules if m.split(".")[0] in HEAVY)
    print("PROBE=" + json.dumps({{"code": code, "loaded": loaded, "attempts": attempts}}))
    """
)


def _probe(argv):
    code = _PROBE.format(heavy=HEAVY, argv=argv)
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        timeout=240,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT), "AITHER_NO_UPDATE_CHECK": "1"},
    )
    line = [ln for ln in res.stdout.splitlines() if ln.startswith("PROBE=")]
    assert line, f"probe printed nothing\nstdout={res.stdout[-2000:]}\nstderr={res.stderr[-2000:]}"
    return json.loads(line[-1][len("PROBE=") :]), res.stdout


@pytest.mark.parametrize(
    "argv", [["learn", "--help"], ["login", "--help"], ["bonsai", "setup", "--help"]]
)
def test_phone_commands_need_no_gpu_stack(argv):
    out, stdout = _probe(argv)
    assert out["code"] in (0, None), stdout[-2000:]
    assert out["loaded"] == [], f"heavy modules loaded: {out['loaded']}"
    assert out["attempts"] == [], f"heavy imports attempted: {out['attempts']}"
    assert "usage: adk" in stdout


# --------------------------------------------------------------------------- learn


ITEM_NUM = {
    "item_id": "i1",
    "skill_id": "math.number_bonds_10",
    "prompt_text": "7 and ? make 10",
    "visual": {"kind": "ten_frame", "filled": 7},
    "input": "number",
}
ITEM_TAP = {
    "item_id": "i2",
    "skill_id": "reading.cvc_picture_choice",
    "prompt_text": "Which picture starts with c?",
    "input": "tap",
    "choices": [
        {"value": "cat", "label": None, "picture": "cat", "emoji": "\U0001f431"},
        {"value": "dog", "label": None, "picture": "dog", "emoji": "\U0001f436"},
    ],
}


class FakeTutor:
    """The learner routes, scripted per test."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body, dict(request.headers)))
        path = request.url.path
        if path == "/api/tutor/me":
            return httpx.Response(
                200, json={"alias": "Sunny", "grade": 1, "today_minutes_left": 12}
            )
        if path == "/api/tutor/me/quest/start":
            return httpx.Response(200, json={"quest_id": "q_1", "items_total": 2, "item": ITEM_NUM})
        if path.endswith("/end"):
            return httpx.Response(200, json={"saved": True, "say": "Saved! See you soon."})
        if path.endswith("/answer"):
            return httpx.Response(200, json=self.answers.pop(0))
        return httpx.Response(404, json={"detail": "Not found"})


def _run(fake, action, typed, monkeypatch):
    monkeypatch.setenv("AITHER_LEARN_TOKEN", "child-bearer")
    monkeypatch.setenv("AITHER_LEARN_URL", "https://learn.test")
    lines = []
    inputs = iter(typed)
    args = argparse.Namespace(learn_action=action, open=False, url="")
    code = learn_cli.cmd_learn(
        args, transport=httpx.MockTransport(fake), ask=lambda _p: next(inputs), out=lines.append
    )
    return code, "\n".join(lines)


def test_info_prints_link_and_minutes(monkeypatch):
    fake = FakeTutor([])
    code, text = _run(fake, None, [], monkeypatch)
    assert code == 0
    assert "https://learn.test/learn" in text
    assert "Hi Sunny!" in text and "adk learn play" in text
    method, path, _b, headers = fake.calls[0]
    assert (method, path) == ("GET", "/api/tutor/me")
    assert headers["authorization"] == "Bearer child-bearer"


def test_quest_loop_miss_shows_steps_then_redo_then_done(monkeypatch):
    fake = FakeTutor(
        [
            {
                "feedback": "lets_look",
                "say": "Let's look together.",
                "hint_steps": ["Count the empty spots."],
                "next_item": None,
                "break": False,
                "done": False,
                "progress": {},
            },
            {
                "feedback": "yay",
                "say": "Yay!",
                "hint_steps": [],
                "next_item": ITEM_TAP,
                "break": False,
                "done": False,
                "progress": {},
            },
            {
                "feedback": "yay",
                "say": "Great work today!",
                "hint_steps": [],
                "next_item": None,
                "break": False,
                "done": True,
                "progress": {},
            },
        ]
    )
    code, text = _run(fake, "play", ["", "4", "3", "9", "1"], monkeypatch)
    assert code == 0
    answers = [c for c in fake.calls if c[1].endswith("/answer")]
    assert [a[2]["answer"] for a in answers] == ["4", "3", "cat"]
    assert [a[2]["redo"] for a in answers] == [False, True, False]
    assert answers[2][2]["item_id"] == "i2"
    assert all(a[3]["content-type"] == "application/json" for a in answers)
    assert "[o o o o o] [o o . . .]" in text  # the ten-frame
    assert "Count the empty spots." in text
    assert "\U0001f431" in text and "cat" not in text.split("Which picture")[1].split("\n")[1]
    assert "Great work today!" in text
    lowered = text.lower()
    for bad in ("wrong", "incorrect", "error", "fail", "streak", "score"):
        assert bad not in lowered


def test_quit_saves_the_quest(monkeypatch):
    fake = FakeTutor([])
    code, text = _run(fake, "play", ["q"], monkeypatch)
    assert code == 0
    assert fake.calls[-1][1] == "/api/tutor/me/quest/q_1/end"
    assert "Saved! See you soon." in text


def test_rest_time_is_kind(monkeypatch):
    def rest(request):
        return httpx.Response(
            429, json={"reason": "rest_time", "say": "Rest time! See you tomorrow."}
        )

    code, text = _run(rest, "play", [], monkeypatch)
    assert code == 0
    assert "Rest time! See you tomorrow." in text


def test_not_signed_in_points_at_adk_login(monkeypatch):
    monkeypatch.setenv("AITHER_LEARN_TOKEN", "")
    monkeypatch.setattr(learn_cli, "learn_token", lambda: "")
    lines = []
    code = learn_cli.cmd_learn(
        argparse.Namespace(learn_action=None, open=False, url="https://x.test"), out=lines.append
    )
    assert code == 0
    assert any("adk login" in ln for ln in lines)


def test_read_answer_maps_numbers_and_text():
    assert learn_cli.read_answer("2", ["cat", "dog"]) == "dog"
    assert learn_cli.read_answer("DOG", ["cat", "dog"]) == "dog"
    assert learn_cli.read_answer("5", ["cat", "dog"]) is None
    assert learn_cli.read_answer("  ", []) is None
    assert learn_cli.read_answer("12345678901234567890", []) == "1234567890123456"


def test_default_origin_is_veil_not_the_api_less_apex(monkeypatch):
    monkeypatch.delenv("AITHER_LEARN_URL", raising=False)
    monkeypatch.setattr("adk.config.load_saved_config", lambda *a, **k: {})
    assert learn_cli.learn_origin() == "https://app.aitherium.com"


# --------------------------------------------------------------------------- bonsai


def test_bonsai_phone_server_cmd_is_loopback_and_small(tmp_path):
    cmd = bonsai_phone.server_cmd(Path("/b/llama-server"), Path("/m/x.gguf"), 8080, 3)
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert cmd[cmd.index("-c") + 1] == "2048"
    assert cmd[cmd.index("-t") + 1] == "3"
    assert "-ngl" not in cmd
    assert bonsai_phone.phone_threads(8) == 4
    assert bonsai_phone.phone_threads(2) == 2
    assert bonsai_phone.phone_threads(1) == 1
    launcher = bonsai_phone.write_launcher(cmd, home=tmp_path)
    assert launcher.read_text(encoding="utf-8").startswith("#!/bin/sh")


def test_bonsai_models_are_bonsai1_q1_only():
    for spec in bonsai_phone.MODELS.values():
        assert spec["file"].startswith("Bonsai-") and spec["file"].endswith("-Q1_0.gguf")


def test_bonsai_dry_run_changes_nothing(monkeypatch, capsys, tmp_path):
    from adk import llamacpp_setup as lc

    info = lc.AccelInfo(kind="cuda", os_family="linux", arch="arm64", ram_gb=8.0)
    monkeypatch.setattr(lc, "detect_accel", lambda: info)
    monkeypatch.setattr(lc, "LLAMACPP_DIR", tmp_path / "llamacpp")
    monkeypatch.setattr(lc, "MODELS_DIR", tmp_path / "models")
    args = argparse.Namespace(
        bonsai_action="setup",
        device="phone",
        model="1.7b",
        port=8080,
        server="",
        use=False,
        no_start=False,
        dry_run=True,
    )
    assert bonsai_phone.cmd_bonsai(args) == 0
    out = capsys.readouterr().out
    assert info.kind == "cpu"  # phone forces the CPU build
    assert "Bonsai-1.7B-Q1_0.gguf" in out and "[DRY]" in out
    assert not (tmp_path / "models").exists()


def test_llamacpp_picks_the_plain_ubuntu_arm64_build():
    from adk import llamacpp_setup as lc

    names = [
        "llama-b1-bin-linux-arm64-snapdragon.tar.gz",
        "llama-b1-bin-ubuntu-arm64.tar.gz",
        "llama-b1-bin-ubuntu-vulkan-arm64.tar.gz",
        "llama-b1-bin-ubuntu-x64.tar.gz",
        "llama-b1-bin-ubuntu-cuda-13.4-arm64.tar.gz",
    ]
    assets = [{"name": n, "browser_download_url": "u/" + n} for n in names]
    accel = lc.AccelInfo(kind="cpu", os_family="linux", arch="arm64")
    assert lc._pick_release_asset(assets, accel) == "u/llama-b1-bin-ubuntu-arm64.tar.gz"


ITEM_AUDIO = {
    "item_id": "i3",
    "skill_id": "read.phx.letter_sounds",
    "prompt_text": "Which letter makes this sound?",
    "tts_text": "Which letter says mmm?",
    "visual": {"kind": "letter_choice"},
    "input": "tap",
    "choices": [{"value": "m", "label": "m"}, {"value": "s", "label": "s"}],
}


def test_quest_start_asks_for_the_text_safe_strand(monkeypatch):
    fake = FakeTutor([{"feedback": "yay", "say": "Yay!", "done": True}])
    _run(fake, "play", ["3"], monkeypatch)
    start = [c for c in fake.calls if c[1] == "/api/tutor/me/quest/start"][0]
    assert start[2] == {"strand": learn_cli.TERMINAL_STRAND} == {"strand": "math"}


def test_audio_only_item_ends_kindly_and_is_never_answered(monkeypatch):
    fake = FakeTutor([])
    fake_start = FakeTutor.__call__

    def call(self, request):
        if request.url.path == "/api/tutor/me/quest/start":
            self.calls.append((request.method, request.url.path, None, {}))
            return httpx.Response(200, json={"quest_id": "q_2", "item": ITEM_AUDIO})
        return fake_start(self, request)

    monkeypatch.setattr(FakeTutor, "__call__", call)
    code, text = _run(fake, "play", [], monkeypatch)
    assert code == 0
    assert learn_cli.LISTEN_LINE in text
    assert "mmm" not in text  # the spoken question (the answer) is never printed
    assert not any(p.endswith("/answer") for _m, p, _b, _h in fake.calls)
    assert any(p.endswith("/q_2/end") for _m, p, _b, _h in fake.calls)


def test_text_answerable_rules():
    assert learn_cli.text_answerable(ITEM_NUM)
    assert learn_cli.text_answerable(ITEM_TAP)
    assert not learn_cli.text_answerable(ITEM_AUDIO)
    same = dict(ITEM_AUDIO, tts_text=ITEM_AUDIO["prompt_text"])
    assert learn_cli.text_answerable(same)


def _gguf(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + b"\0" * (size - 4))


def test_partial_model_is_not_complete(tmp_path):
    spec = dict(bonsai_phone.MODELS["1.7b"], size_bytes=64)
    model = tmp_path / spec["file"]
    _gguf(model, 32)  # magic present, download cut off
    assert bonsai_phone.is_gguf(model)
    assert not bonsai_phone.model_complete(model, spec)
    _gguf(model, 64)
    assert bonsai_phone.model_complete(model, spec)


def test_every_phone_model_pins_its_size():
    for spec in bonsai_phone.MODELS.values():
        assert int(spec["size_bytes"]) > 100 * 1024 * 1024
        assert spec["sha256"] == "" or len(spec["sha256"]) == 64


def test_fetch_model_writes_part_then_renames(monkeypatch, tmp_path):
    from adk import llamacpp_setup as lc

    spec = dict(bonsai_phone.MODELS["4b"], size_bytes=64, sha256="")
    model = tmp_path / spec["file"]
    seen = []

    def fake_download(url, dest, label="", expected_sha256=""):
        seen.append((dest.name, expected_sha256))
        assert not model.exists()  # never writes the final path
        _gguf(dest, 64)
        return True

    monkeypatch.setattr(lc, "_download", fake_download)
    assert bonsai_phone.fetch_model("u", model, spec)
    assert seen == [(spec["file"] + ".part", "")]
    assert bonsai_phone.model_complete(model, spec)
    assert not (tmp_path / (spec["file"] + ".part")).exists()


def test_fetch_model_short_or_interrupted_leaves_no_model(monkeypatch, tmp_path):
    from adk import llamacpp_setup as lc

    spec = dict(bonsai_phone.MODELS["1.7b"], size_bytes=64)
    model = tmp_path / spec["file"]

    def short(url, dest, label="", expected_sha256=""):
        assert expected_sha256 == bonsai_phone.MODELS["1.7b"]["sha256"]
        _gguf(dest, 32)
        return True

    monkeypatch.setattr(lc, "_download", short)
    assert not bonsai_phone.fetch_model("u", model, spec)
    assert not model.exists() and not (tmp_path / (spec["file"] + ".part")).exists()

    def interrupted(url, dest, label="", expected_sha256=""):
        _gguf(dest, 16)
        raise KeyboardInterrupt

    monkeypatch.setattr(lc, "_download", interrupted)
    with pytest.raises(KeyboardInterrupt):
        bonsai_phone.fetch_model("u", model, spec)
    assert not model.exists() and not (tmp_path / (spec["file"] + ".part")).exists()
