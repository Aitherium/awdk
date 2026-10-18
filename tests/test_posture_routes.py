"""``/posture`` on the harness daemon -- the phone's door to the host posture, proved.

* the closed language: an unknown word, a malformed ``until`` and an ``until`` on status
  are refused before any process starts;
* the REAL daemon refuses an unauthenticated caller (401) and a wrong token (403);
* a scoped agent token reads but cannot set; the owner's signed assertion sets once and
  binds the word; a local owner needs no assertion;
* ``run_posture`` drives a real (fake) ``fleet_verbs.py`` and returns its JSON, reports
  ``pending`` for a slow flip WITHOUT killing it, and names a checkout that has no verb.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="daemon route tests need fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from adk.harnesses import posture_routes  # noqa: E402

TOKEN = "test-token-posture-do-not-reuse"
SEED = bytes(range(32))


# ---------------------------------------------------------------- the closed language


@pytest.mark.parametrize("word", ["", "party", "status; rm -rf /", "GAME ON", "--execute",
                                  "shop --until", "../shop"])
def test_unknown_words_are_refused(word):
    with pytest.raises(ValueError):
        posture_routes.build_argv(word, Path("t.py"))


@pytest.mark.parametrize("until", ["tomorrow", "21:30; calc", "2d", "--json", "x" * 40])
def test_malformed_until_is_refused(until):
    with pytest.raises(ValueError):
        posture_routes.build_argv("game", Path("t.py"), until)


def test_until_is_refused_on_status():
    with pytest.raises(ValueError):
        posture_routes.build_argv("status", Path("t.py"), "21:30")


def test_argv_shape():
    tool = Path("repo") / "fleet_verbs.py"
    assert posture_routes.build_argv("status", tool, python="py") == [
        "py", str(tool), "posture", "status", "--json"]
    assert posture_routes.build_argv("Game", tool, "23:00", python="py") == [
        "py", str(tool), "posture", "game", "--execute", "--until", "23:00", "--json"]
    for good in ("2h", "90m", "2026-10-17T09:00", "2026-10-17 09:00:00-07:00"):
        assert posture_routes.build_argv("after", tool, good, python="py")[-2] == good


# ---------------------------------------------------------------- the real daemon's auth


@pytest.fixture()
def daemon(tmp_path, monkeypatch):
    from adk.harnesses.daemon import create_app
    from adk.harnesses.manager import SessionManager

    calls: list = []

    def fake_run(word, until="", **_):
        calls.append((word, until))
        return {"ok": True, "rc": 0, "posture": {"desired": word if word != "status"
                                                 else "shop"}}

    monkeypatch.setattr(posture_routes, "run_posture", fake_run)
    manager = SessionManager(root=tmp_path)
    with TestClient(create_app(manager=manager, token=TOKEN)) as client:
        yield client, calls
    manager.stop_all()


def test_real_daemon_refuses_an_unauthenticated_caller(daemon):
    client, calls = daemon
    assert client.get("/posture").status_code == 401
    assert client.post("/posture/set", json={"payload": {"text": "{}"}}).status_code == 401
    bad = {"Authorization": "Bearer not-the-token"}
    assert client.get("/posture", headers=bad).status_code == 403
    assert client.post("/posture/set", headers=bad,
                       json={"payload": {"text": "{}"}}).status_code == 403
    assert calls == []


def test_real_daemon_root_bearer_is_the_local_owner(daemon):
    client, calls = daemon
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/posture", headers=auth).json()["posture"]["desired"] == "shop"
    text = json.dumps({"action": "set", "word": "game"})
    res = client.post("/posture/set", headers=auth, json={"payload": {"text": text}})
    assert res.status_code == 200 and res.json()["posture"]["desired"] == "game"
    text = json.dumps({"action": "set", "word": "party"})
    res = client.post("/posture/set", headers=auth, json={"payload": {"text": text}})
    assert res.status_code == 400
    assert calls == [("status", ""), ("game", "")]


# ---------------------------------------------------------------- scoped agent vs owner


def _app(plan: str, verifier=None):
    class P:
        pass

    p = P()
    p.plan = plan
    app = FastAPI()
    posture_routes.mount(app, lambda: p, verifier)
    return TestClient(app)


def _event(text: str, seed: bytes | None = None, to: str = "posture") -> dict:
    ev = {"id": "e-" + str(time.time_ns()), "actor": {"kind": "human", "id": "owner:me"},
          "to": [to], "payload": {"text": text}}
    if seed is not None:
        from adk.harnesses import owner_steer

        ev["payload"]["owner_assertion"] = owner_steer.sign_owner_assertion(
            seed, event_id=ev["id"], target=to, actor_id="owner:me", text=text)
    return ev


def test_agent_token_reads_but_cannot_set(monkeypatch):
    calls: list = []
    monkeypatch.setattr(posture_routes, "run_posture",
                        lambda w, u="", **_: calls.append((w, u)) or {"ok": True})
    c = _app("agent")
    assert c.get("/posture").status_code == 200
    res = c.post("/posture/set", json=_event(json.dumps({"action": "set", "word": "game"})))
    assert res.status_code == 403
    assert calls == [("status", "")]


def test_owner_assertion_sets_once_and_binds_the_word(monkeypatch):
    pytest.importorskip("cryptography")
    from adk.harnesses import owner_steer

    calls: list = []
    monkeypatch.setattr(posture_routes, "run_posture",
                        lambda w, u="", **_: calls.append((w, u)) or {"ok": True})
    verifier = owner_steer.OwnerAssertionVerifier(owner_steer.public_key_from_seed(SEED))
    c = _app("agent", verifier)
    text = json.dumps({"action": "set", "word": "game", "until": "23:30"})
    # signed for the KV relay, not for the posture
    assert c.post("/posture/set", json=_event(text, SEED, to="kvholder")).status_code == 403
    # a key the daemon does not pin
    assert c.post("/posture/set", json=_event(text, bytes(32))).status_code == 403
    ev = _event(text, SEED)
    assert c.post("/posture/set", json=ev).status_code == 200
    assert c.post("/posture/set", json=ev).status_code == 403          # replay
    bad = _event(json.dumps({"action": "set", "word": "party"}), SEED)
    assert c.post("/posture/set", json=bad).status_code == 400          # signed, still closed
    wrong = _event(json.dumps({"action": "join", "word": "game"}), SEED)
    assert c.post("/posture/set", json=wrong).status_code == 403        # action bound
    assert calls == [("game", "23:30")]


# ---------------------------------------------------------------- the CLI, really run


def _fake_tool(root: Path, body: str) -> Path:
    tool = root / "AitherOS" / "dev" / "tools" / "fleet_verbs.py"
    tool.parent.mkdir(parents=True)
    tool.write_text(textwrap.dedent(body), encoding="utf-8")
    return tool


def test_run_posture_returns_the_cli_json(tmp_path):
    state = tmp_path / "posture.json"
    tool = _fake_tool(tmp_path, f"""
        import json, sys
        args = sys.argv[1:]
        doc = {{"desired": args[1], "argv": args}}
        if args[1] != "status":
            open({str(state)!r}, "w").write(json.dumps(doc))
        print("note: applying")
        print(json.dumps(doc))
    """)
    res = posture_routes.run_posture("game", "2h", tool=tool)
    assert res["ok"] is True and res["rc"] == 0
    assert res["posture"]["argv"] == ["posture", "game", "--execute", "--until", "2h", "--json"]
    assert json.loads(state.read_text())["desired"] == "game"


def test_slow_flip_is_pending_and_not_killed(tmp_path):
    done = tmp_path / "done"
    tool = _fake_tool(tmp_path, f"""
        import time
        time.sleep(1.0)
        open({str(done)!r}, "w").write("ok")
        print("{{}}")
    """)
    res = posture_routes.run_posture("after", tool=tool, wait_s=0.05)
    assert res["pending"] is True and res["ok"] is True
    deadline = time.time() + 20
    while not done.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert done.exists(), "the flip was killed instead of left to finish"


def test_checkout_without_the_verb_is_named(tmp_path):
    tool = _fake_tool(tmp_path, """
        import sys
        sys.stderr.write("fleet_verbs.py: error: argument noun: invalid choice: 'posture'\\n")
        sys.exit(2)
    """)
    res = posture_routes.run_posture("status", tool=tool)
    assert res["ok"] is False and res["rc"] == 2
    assert "no `posture` verb" in res["error"]


def test_no_checkout_could_not_judge(monkeypatch):
    monkeypatch.setattr(posture_routes, "find_tool", lambda: None)
    res = posture_routes.run_posture("status")
    assert res["ok"] is False and res["rc"] == 2 and "AITHEROS_ROOT" in res["error"]


def test_python_is_this_interpreter():
    assert posture_routes.build_argv("status", Path("t.py"))[0] == sys.executable
