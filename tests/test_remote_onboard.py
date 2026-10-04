"""adk onboard-remote: the box is added through the SAME pairing path a person uses,
the code never rides on an argv, a failed step stops the run, and ``ok`` means the
box is ONLINE in the owner's device list -- not merely that pair printed success.
Hermetic: ssh and Identity are stubbed."""
from __future__ import annotations

import json
import shutil
import subprocess
import types

import pytest

from adk import remote_onboard as ro

CODE = "K7Q2ZP"


def _box_output(node_id: str = "adk-aa7e4a2a-6d704cf1", beat: str = "detached") -> str:
    return "\n".join([
        "@@os Linux x86_64", "@@python 3.11.2", "@@adk 3.8.57",
        f"@@pair   ✓ Paired as node {node_id} (tenant platform)",
        f"@@node_id {node_id}", f"@@beat {beat}",
    ])


class _Fake:
    """Records every ssh/scp argv + stdin, and every Identity call."""

    def __init__(self, box_output: str, online: bool = True, mint=(200, {"code": CODE})):
        self.box_output, self.online, self.mint = box_output, online, mint
        self.runs: list = []
        self.http: list = []

    def run(self, argv, *, stdin="", timeout=60):
        self.runs.append((list(argv), stdin))
        if argv[-1].startswith("uname"):
            return 0, "Linux\n"
        if argv[0] == "scp":
            return 0, ""
        return 0, self.box_output

    def http_call(self, method, url, bearer, body=None, timeout=30, attempts=3):
        self.http.append((method, url))
        if url.endswith("/pairing/init"):
            return self.mint
        return 200, {"node_id": "adk-aa7e4a2a-6d704cf1",
                     "status": "online" if self.online else "offline"}


@pytest.fixture
def fake(monkeypatch):
    def make(**kw):
        f = _Fake(kw.pop("box_output", _box_output()), **kw)
        monkeypatch.setattr(ro, "_run", f.run)
        monkeypatch.setattr(ro, "_http", f.http_call)
        monkeypatch.setattr(ro.time, "sleep", lambda s: None)
        return f
    return make


def _go(**kw):
    args = dict(port=2222, key_path="/k", bearer="owner-token", identity="https://idp.test",
                online_timeout_s=0)
    args.update(kw)
    return ro.onboard_remote("10.0.0.9", "root", **args)


def test_happy_path_is_online_and_the_code_never_rides_on_an_argv(fake):
    f = fake()
    res = _go()
    assert res["ok"] is True and res["node_id"] == "adk-aa7e4a2a-6d704cf1"
    assert [s["step"] for s in res["steps"]] == [
        "probe", "mint", "install", "pair", "beat", "verify"]
    # minted as the caller, through the product route
    assert ("POST", "https://idp.test/v1/nodes/pairing/init") in f.http
    for argv, stdin in f.runs:
        assert CODE not in " ".join(argv)            # never on the command line
    assert any(CODE in stdin for _, stdin in f.runs)  # only inside the stdin script
    assert CODE not in json.dumps(res)               # and never in the report


def test_paired_but_never_online_is_not_onboarded(fake):
    fake(online=False)
    res = _go()
    assert res["ok"] is False
    assert res["steps"][-1]["step"] == "verify" and res["steps"][-1]["ok"] is False


def test_a_failed_install_stops_before_pair(fake):
    f = fake(box_output="@@os Linux x86_64\n@@fail install could not create a python venv")
    res = _go()
    assert res["ok"] is False
    assert res["steps"][-1] == {"step": "install", "ok": False,
                                "detail": "could not create a python venv"}
    assert not any(u.endswith("/v1/nodes/adk-aa7e4a2a-6d704cf1") for _, u in f.http)


def test_a_mint_refusal_is_said_on_the_owners_screen(fake):
    fake(mint=(402, {"detail": {"error": "subscription_required"}}))
    res = _go()
    assert res["ok"] is False
    assert res["steps"][-1]["step"] == "mint" and "402" in res["steps"][-1]["detail"]


def test_an_unreachable_box_fails_at_probe(monkeypatch):
    refused = "ssh: connect to host 10.0.0.9 port 2222: Connection refused"
    monkeypatch.setattr(ro, "_run", lambda argv, **kw: (255, refused))
    res = _go()
    assert res["ok"] is False and res["steps"][-1]["step"] == "probe"
    assert "Connection refused" in res["steps"][-1]["detail"]


def test_plan_touches_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("plan mode must not ssh or call Identity")
    monkeypatch.setattr(ro, "_run", boom)
    monkeypatch.setattr(ro, "_http", boom)
    res = _go(plan=True)
    assert res["ok"] is True and res["plan"] is True
    assert [s["step"] for s in res["steps"]] == [
        "mint", "probe", "install", "pair", "beat", "verify"]


def test_a_given_code_is_used_and_nothing_is_minted(fake):
    f = fake()
    res = _go(code="ab12cd")
    assert res["ok"] is True
    assert not any(u.endswith("/pairing/init") for _, u in f.http)
    assert any("AB12CD" in stdin for _, stdin in f.runs)


def test_bad_args_are_refused_before_any_ssh(monkeypatch):
    monkeypatch.setattr(ro, "_run", lambda *a, **k: pytest.fail("ssh ran"))
    assert _go(node_class="toaster")["steps"][0]["step"] == "args"
    assert _go(code="not a code!")["steps"][0]["step"] == "args"


def test_a_local_wheel_is_uploaded_under_its_real_filename(fake, tmp_path):
    whl = tmp_path / "awdk-9.9.9-py3-none-any.whl"
    whl.write_bytes(b"PK")
    f = fake()
    res = _go(pip_spec=str(whl))
    assert res["ok"] is True
    scp = [argv for argv, _ in f.runs if argv[0] == "scp"]
    assert scp and scp[0][-1] == "root@10.0.0.9:/tmp/awdk-9.9.9-py3-none-any.whl"
    assert scp[0][1:3] == ["-P", "2222"]
    script = [stdin for argv, stdin in f.runs if argv[-1] == "-s"][0]
    assert "WHEEL=/tmp/awdk-9.9.9-py3-none-any.whl" in script


def test_parse_protocol_folds_tags():
    p = ro.parse_protocol("noise\n@@warn x\n" + _box_output(beat="systemd-user"))
    assert p["paired"] is True and p["node_id"] == "adk-aa7e4a2a-6d704cf1"
    assert p["beat"] == "systemd-user" and p["warn"] == ["x"]
    assert ro.parse_protocol("@@pair   ✗ Pairing failed: HTTP 400")["paired"] is False


def test_http_retries_silence_but_never_an_answer(monkeypatch):
    calls = []

    class Client:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers, json):
            calls.append(url)
            if len(calls) == 1:
                raise TimeoutError("read timed out")
            return types.SimpleNamespace(status_code=401, json=lambda: {"detail": "no"})

    monkeypatch.setitem(__import__("sys").modules, "httpx", types.SimpleNamespace(Client=Client))
    monkeypatch.setattr(ro.time, "sleep", lambda s: None)
    assert ro._http("POST", "https://idp.test/x", "t") == (401, {"detail": "no"})
    assert len(calls) == 2  # one silence retried, the answer (even a 401) is final


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not on PATH")
def test_linux_script_is_valid_bash():
    script = ro.linux_script(CODE, node_class="desktop",
                             inference_url="http://127.0.0.1:8114", pip_spec="awdk",
                             wheel_name="awdk-1-py3-none-any.whl")
    r = subprocess.run(["bash", "-n"], input=script.encode(), capture_output=True)
    assert r.returncode == 0, r.stderr
    assert "adk\" pair - --node-class desktop --inference-url http://127.0.0.1:8114" in script


def test_adk_pair_dash_reads_the_code_from_stdin(monkeypatch, capsys):
    import io

    from adk import node_pairing

    seen = {}

    async def fake_pair(code, portal_url, node_class="laptop", confirm_path="", inference_url=""):
        seen.update(code=code, inference_url=inference_url, node_class=node_class)
        return {"paired": False, "error": "stub"}

    monkeypatch.setattr(node_pairing, "pair_with_code", fake_pair)
    monkeypatch.setattr("sys.stdin", io.StringIO("ab12cd\n"))
    args = types.SimpleNamespace(code="-", portal="https://idp.test", node_class="spark",
                                 inference_url="http://127.0.0.1:8114", no_autostart=True)
    assert node_pairing.cmd_pair(args) == 1
    assert seen == {"code": "ab12cd", "inference_url": "http://127.0.0.1:8114",
                    "node_class": "spark"}


def test_node_inference_url_env_names_a_server_off_the_ladder(monkeypatch):
    from adk import enrollment

    probed = []
    monkeypatch.setattr(enrollment, "_openai_models",
                        lambda base: (probed.append(base) or True, ["some-model"]))
    monkeypatch.setattr(enrollment, "_fingerprint", lambda base: "llama-server")
    monkeypatch.setenv("AITHER_NODE_INFERENCE_URL", "http://127.0.0.1:8114/v1")
    got = enrollment.probe_inference()
    assert (got.inference_url, got.inference_kind, got.ready) == (
        "http://127.0.0.1:8114", "llama-server", True)
    assert probed == ["http://127.0.0.1:8114"]
    # an explicit ladder (tests, callers) is never overridden by the env
    monkeypatch.setattr(enrollment, "_probe_candidate", lambda c: None)
    assert enrollment.probe_inference(candidates=[]).ready is False
