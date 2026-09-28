"""Agent Home games: generic + Saga clients against fake servers, learning loop,
world-model bridge, CLI join, and the license gate on learning."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from adk.games import GameError, open_game, split_token
from adk.games.env_adapter import GameEnvAdapter
from adk.games.fake_server import FakeSaga, TreasureLineGame, serve_in_thread
from adk.games.generic import GenericGameClient
from adk.games.learning import (
    GameLearner,
    TransitionModel,
    game_memory,
    match_action,
)
from adk.games.saga import SagaRoomClient, parse_saga_url
from adk.home import cli as home_cli
from adk.home import config as hc
from adk.licensing import LicenseError


def _mock(server) -> httpx.MockTransport:
    return httpx.MockTransport(server.handle_httpx)


@pytest.fixture
def unlicensed(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_LICENSE_FILE", str(tmp_path / "none.json"))
    monkeypatch.setenv(hc.HOME_ENV, str(tmp_path / "home"))
    for v in ("AITHER_LICENSE_KEY", "AITHER_LICENSE_ENFORCE", "AITHER_TENANT_SLUG"):
        monkeypatch.delenv(v, raising=False)


@pytest.fixture
def licensed(monkeypatch, tmp_path):
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = crypto.Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    payload = json.dumps({"tier": "community", "packs": ["agent-home"],
                          "issued_at": time.time()}).encode()
    lic = tmp_path / "license.json"
    lic.write_text(json.dumps({"payload": base64.b64encode(payload).decode(),
                               "signature": key.sign(payload).hex()}))
    monkeypatch.setenv("AITHER_LICENSE_PUBLIC_KEY", pub)
    monkeypatch.setenv("AITHER_LICENSE_FILE", str(lic))
    monkeypatch.setenv(hc.HOME_ENV, str(tmp_path / "home"))
    for v in ("AITHER_LICENSE_KEY", "AITHER_LICENSE_ENFORCE", "AITHER_TENANT_SLUG"):
        monkeypatch.delenv(v, raising=False)


def _no_wm(*_a, **_k):
    return {"ok": False, "reason": "disabled in test"}


# ── URL handling ─────────────────────────────────────────────────────────────

def test_split_token_strips_query_and_fragment_tokens():
    clean, tok = split_token("https://g.example/room?world=a&token=s3cret")
    assert tok == "s3cret" and "s3cret" not in clean and "world=a" in clean
    clean, tok = split_token("https://g.example/room#token=abc")
    assert tok == "abc" and "abc" not in clean


def test_parse_saga_url_forms():
    assert parse_saga_url("saga+http://h:8793?world=elysium") == {
        "base": "http://h:8793/api/local/saga", "world": "elysium"}
    assert parse_saga_url("https://saga.example.com/play/cyber-feudalism")["world"] \
        == "cyber-feudalism"
    assert parse_saga_url("http://h/api/local/saga")["world"] == ""
    with pytest.raises(GameError):
        parse_saga_url("saga+http://h?world=BAD_ID!")


def test_open_game_picks_client_by_url():
    t = _mock(TreasureLineGame())
    assert isinstance(open_game("http://h/game", transport=t), GenericGameClient)
    s = _mock(FakeSaga())
    assert isinstance(open_game("saga+http://h?world=elysium", transport=s),
                      SagaRoomClient)
    assert isinstance(open_game("http://h/api/local/saga", transport=s),
                      SagaRoomClient)


# ── generic protocol ─────────────────────────────────────────────────────────

def test_generic_join_observe_act_chat_leave():
    game = TreasureLineGame(length=3)
    c = open_game("http://h/game", name="pip", transport=_mock(game))
    obs = c.join()
    assert obs.signature == "pos=0" and "right" in obs.actions
    assert c.domain == "game:aither-game:treasure-line"
    step = c.act("right")
    assert step.observation.signature == "pos=1" and step.reward < 0
    assert c.chat("hello")["ok"] is True
    assert game.chat_log[-1] == {"from": "pip", "text": "hello"}
    c.act("right")
    win = c.act("dig")
    assert win.done and win.reward > 0.9
    c.close()
    assert game.sessions == {}


def test_generic_token_is_sent_and_required():
    game = TreasureLineGame(require_token="tok123")
    with pytest.raises(GameError, match="401"):
        open_game("http://h/game", transport=_mock(game)).join()
    ok = open_game("http://h/game?token=tok123", transport=_mock(game))
    assert ok.join().signature == "pos=0"


def test_act_before_join_is_refused():
    c = open_game("http://h/game", transport=_mock(TreasureLineGame()))
    with pytest.raises(GameError, match="join"):
        c.act("right")


# ── Saga ─────────────────────────────────────────────────────────────────────

def test_saga_join_seeds_and_plays_with_shaped_reward():
    saga = FakeSaga()
    c = open_game("saga+http://h?world=elysium&token=t0k", transport=_mock(saga))
    obs = c.join()
    assert saga.seeded["elysium"] is True
    assert c.domain == "game:saga:elysium"
    assert "look around" in obs.actions
    step = c.act("go to Market")
    assert step.info["new_nodes"]                     # discovered Market (+ Mira)
    assert step.reward > 0.1
    assert step.observation.state["location"] == "Market"
    assert "talk to Mira" in step.observation.actions
    assert saga.auth_seen and saga.auth_seen[-1] == "Bearer t0k"
    bad = c.act("summon a dragon")                    # contradicts the world
    assert bad.info["new_continuity_issues"] == 1 and bad.reward < 0


def test_saga_unknown_world_refused():
    c = open_game("saga+http://h?world=atlantis", transport=_mock(FakeSaga()))
    with pytest.raises(GameError, match="atlantis"):
        c.join()


def test_saga_without_server_model_uses_agent_prose():
    saga = FakeSaga(has_model=False)
    no_prose = open_game("saga+http://h?world=elysium", transport=_mock(saga))
    no_prose.join()
    with pytest.raises(GameError, match="503"):
        no_prose.act("look around")
    c = open_game("saga+http://h?world=elysium", transport=_mock(saga),
                  prose_fn=lambda ctx, msg: f"The agent narrates: {msg}.")
    c.join()
    step = c.act("look around")
    assert step.info["prose_source"] == "client"
    assert "agent narrates" in step.observation.text


def test_saga_chat_is_a_spoken_turn():
    saga = FakeSaga()
    c = open_game("saga+http://h?world=elysium", transport=_mock(saga))
    c.join()
    c.chat("hello Mira")
    assert saga.turns["elysium"][-1]["message"] == 'I say: "hello Mira"'


# ── learning ─────────────────────────────────────────────────────────────────

def test_transition_model_surprise_falls_with_experience():
    m = TransitionModel("d")
    assert m.surprise("a", "go", "b") == 1.0
    m.update("a", "go", 0.0, "b", False)
    assert m.surprise("a", "go", "b") == 0.0
    assert m.surprise("a", "go", "c") == 1.0


def test_unpersisted_play_is_free(unlicensed):
    c = open_game("http://h/game", transport=_mock(TreasureLineGame()))
    rep = GameLearner(c, persist=False, seed=1, wm_observe_fn=_no_wm).play_session(10)
    assert rep.steps > 0 and rep.persisted is False


def test_persisted_learning_requires_license(unlicensed):
    c = open_game("http://h/game", transport=_mock(TreasureLineGame()))
    with pytest.raises(LicenseError, match="agent-home"):
        GameLearner(c, persist=True)


def test_agent_improves_across_sessions_and_remembers(licensed, tmp_path):
    game = TreasureLineGame(length=5)
    state = tmp_path / "games"
    mem = game_memory("pip", db_path=tmp_path / "mem.db")
    reports = []
    for i in range(8):  # a NEW client + learner each session: only disk carries over
        c = open_game("http://h/game", transport=_mock(game))
        learner = GameLearner(c, persist=True, state_dir=state, seed=i,
                              epsilon=0.05, memory=mem, wm_observe_fn=_no_wm)
        reports.append(learner.play_session(budget=40))
        c.close()
    first, last = reports[0], reports[-1]
    assert last.done and last.total_reward > first.total_reward
    assert last.steps <= 6                   # walks straight to the treasure
    assert last.mean_surprise < first.mean_surprise
    prog = TransitionModel.load(c.domain, state).progress()
    assert prog["sessions"] == 8 and prog["improving"] is True
    import asyncio

    got = asyncio.run(mem.recall(f"{c.domain}:session:8"))
    assert got and "reward" in got and "Lessons" in got


def test_world_model_observer_receives_signatures(licensed, tmp_path):
    seen = []

    def wm(obs, action, next_obs, reward=0.0, done=False, domain=""):
        seen.append((obs, action, next_obs, domain))
        return {"ok": True}

    c = open_game("http://h/game", transport=_mock(TreasureLineGame()))
    rep = GameLearner(c, persist=False, seed=0, wm_observe_fn=wm).play_session(5)
    assert rep.wm_recorded == len(seen) == rep.steps
    assert all(s[0].startswith("pos=") and s[3] == c.domain for s in seen)


def test_policy_picks_and_bad_policy_falls_back(unlicensed):
    c = open_game("http://h/game", transport=_mock(TreasureLineGame(length=3)))
    rep = GameLearner(c, persist=False, policy=lambda o, a, h: "right",
                      wm_observe_fn=_no_wm).play_session(3)
    assert [t.action for t in rep.trace] == ["right", "right", "right"]
    assert all(t.why == "policy" for t in rep.trace)

    def boom(o, a, h):
        raise RuntimeError("model down")

    c2 = open_game("http://h/game", transport=_mock(TreasureLineGame()))
    rep2 = GameLearner(c2, persist=False, policy=boom, seed=0,
                       wm_observe_fn=_no_wm).play_session(3)
    assert rep2.steps == 3 and all(t.why != "policy" for t in rep2.trace)


def test_match_action():
    acts = ["go left", "go right", "dig"]
    assert match_action("2", acts) == "go right"
    assert match_action("I think: dig.", acts) == "dig"
    assert match_action("fly", acts) is None


# ── world-model bridge ───────────────────────────────────────────────────────

def test_env_adapter_satisfies_explore_contract():
    from adk.packs.world_model.env_enroll import _validate
    from adk.packs.world_model.safe_explore import explore

    ad = GameEnvAdapter("http://h/game", transport=_mock(TreasureLineGame()))
    assert _validate(ad) == []
    res = explore(ad, budget=6, epsilon=1.0, wm_observe_fn=_no_wm)
    assert res["degraded"] is False and res["steps"] >= 1


def test_enroll_game_is_gated(unlicensed):
    from adk.games.learning import enroll_game

    with pytest.raises(LicenseError):
        enroll_game("http://h/game")


# ── real socket + CLI ────────────────────────────────────────────────────────

def test_cli_join_real_http_saga(unlicensed, capsys):
    httpd, base = serve_in_thread(FakeSaga())
    try:
        rc = home_cli.main(["join", f"saga+{base}?world=elysium",
                            "--act", "go to Market", "--json"])
    finally:
        httpd.shutdown()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["domain"] == "game:saga:elysium"
    assert out["act"]["observation"]["state"]["location"] == "Market"


def test_cli_join_learn_needs_license(unlicensed):
    assert home_cli.main(["join", "http://127.0.0.1:9/game", "--steps", "3",
                          "--learn"]) == home_cli.EXIT_LICENSE
    assert home_cli.main(["join", "http://127.0.0.1:9/game",
                          "--agents", "2"]) == home_cli.EXIT_LICENSE


def test_cli_join_learn_real_http(licensed, capsys):
    httpd, base = serve_in_thread(TreasureLineGame(length=4))
    try:
        rc = home_cli.main(["join", f"{base}/game", "--steps", "30",
                            "--sessions", "4", "--learn", "--seed", "3", "--json"])
    finally:
        httpd.shutdown()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    sessions = out["sessions"]
    assert len(sessions) == 4 and sessions[-1]["persisted"] is True
    assert sessions[-1]["progress"]["sessions"] == 4


def test_cli_join_unreachable_game_exits_fail(unlicensed):
    assert home_cli.main(["join", "http://127.0.0.1:9/game"]) == home_cli.EXIT_FAIL


def test_enroll_game_licensed_runs_world_model_loop(licensed, tmp_path, monkeypatch,
                                                    capsys):
    """Licensed enrollment drives env_enroll -> safe_explore over a REAL socket and
    records every transition with the world model's observer."""
    from adk.packs.world_model import env_enroll as ee
    from adk.packs.world_model import tools as wm_tools

    seen = []

    def fake_observe(obs, action, next_obs, reward=0.0, done=False, domain=""):
        seen.append((obs, action, next_obs, domain))
        return {"ok": True}

    def fake_surprise(items, domain=""):
        return {"ok": True, "surprises": {it["id"]: 0.1 for it in items}}

    monkeypatch.setattr(wm_tools, "wm_observe", fake_observe)
    monkeypatch.setattr(wm_tools, "wm_surprise", fake_surprise)
    monkeypatch.setattr(ee, "PROOF_PATH", tmp_path / "proofs.json")

    httpd, base = serve_in_thread(TreasureLineGame(length=3))
    try:
        rc = home_cli.main(["enroll", f"{base}/game", "--episodes", "2",
                            "--budget", "5", "--json"])
    finally:
        httpd.shutdown()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["transitions"] == len(seen) > 0
    assert out["domain"].startswith("game:")
    assert all(d == out["domain"] for *_x, d in seen)


def test_cli_enroll_unlicensed_exits_license(unlicensed):
    assert home_cli.main(["enroll", "http://127.0.0.1:9/game"]) == \
        home_cli.EXIT_LICENSE
