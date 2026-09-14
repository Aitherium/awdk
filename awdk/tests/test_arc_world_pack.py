"""B1 (BYO-cognition): the ArcGatewayAdapter is a real EnvironmentAdapter —
env_enroll accepts it, the learn-safely loop drives a real ARC-shaped game, the
local mini-WM learns it, and every transition is submitted to the gateway.

The heavy gateway/ARC client is the vendored arc-brainpack; the adapter file-loads
it. Tests here run NETWORK-FREE: a deterministic FakeArcHttp stands in for the
ARC-AGI-3 API, and the gateway submit path is monkeypatched to a recorder.
"""
from __future__ import annotations

import json
import os

os.environ["AITHER_OFFLINE"] = "1"

import pytest  # noqa: E402

from tests._optional_world_model import requires_world_model_engine




@pytest.fixture(autouse=True)
def _force_offline(monkeypatch):
    """Re-assert offline mode AFTER conftest's env isolation strips it.

    conftest._isolate_env now delenv's AITHER_OFFLINE so no test INHERITS offline
    mode from another module (the leak that made TestSwarmCode take swarm_code's
    sovereign A2A path and fail the public-payload gate). These packs genuinely
    need it, and they read it at CALL time — not just at import — so they declare
    it here per-test instead of relying on a module-level side effect that also
    lands on everyone else. Module-level autouse fixtures run after conftest's,
    which is the ordering conftest's own docstring relies on.
    """
    monkeypatch.setenv("AITHER_OFFLINE", "1")

def _load_brainpack(path: str = "tools.py"):
    """File-load the vendored arc-brainpack module (the dir name is hyphenated,
    so it is NOT importable as a dotted module — the ADK loader file-loads it, and
    the adapter does too). Mirrors that spec_from_file_location path."""
    import importlib.util
    from pathlib import Path
    p = (Path(__file__).resolve().parents[1] / "adk" / "toolpacks"
         / "arc-brainpack" / path)
    mod_name = f"_arc_brainpack_test_{path.rsplit('.', 1)[0]}"
    spec = importlib.util.spec_from_file_location(
        mod_name, p,
        submodule_search_locations=[str(p.parent)] if path == "__init__.py" else None)
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class FakeArcHttp:
    """Deterministic 64x64 ARC-format grid world (marker + paint), no network.

    Emulates the subset of the ARC-AGI-3 API the adapter drives: /api/cmd/RESET,
    /api/cmd/ACTION<n>, and a frame with state/frame/guid/available_actions/
    full_reset. WIN when the marker reaches column 63.
    """

    def __init__(self):
        self.calls: list = []
        self.reset()

    def reset(self) -> None:
        self.r = 32
        self.c = 32
        self.paint: dict = {}

    def _grid(self) -> list:
        g = [[0] * 64 for _ in range(64)]
        for (r, c), color in self.paint.items():
            if 0 <= r < 64 and 0 <= c < 64:
                g[r][c] = color
        g[self.r][self.c] = 1
        return g

    def post(self, url: str, body: dict) -> dict:
        self.calls.append((url, body))
        action = url.rsplit("/", 1)[-1]
        if action == "RESET":
            self.reset()
        elif action.startswith("ACTION"):
            aid = int(action[len("ACTION"):])
            if aid == 1:
                self.c = (self.c + 1) % 64
            elif aid == 2:
                self.r = (self.r + 1) % 64
            elif aid == 3:
                self.paint[(self.r, self.c)] = 2
            elif aid == 6:
                # DETERMINISTIC click: paint the cell ahead of the marker. The
                # real ARC API takes arbitrary (x, y), but a random target would
                # make ACTION6 unpredictable to the tabular WM and the
                # learnability proof would flake. The fake must be learnable.
                self.paint[(self.r, (self.c + 1) % 64)] = 3
            # other actions: no-op
        state = "WIN" if self.c == 63 else "NOT_FINISHED"
        return {"state": state, "frame": [self._grid()], "guid": "g-fake",
                "available_actions": [1, 2, 3, 6], "full_reset": False}

    def close(self) -> None:
        pass


@pytest.fixture()
def enrolled(monkeypatch, tmp_path):
    """A recorder for the gateway submit path + a clean proof registry + an
    in-process WM engine reset (the engine is a module global shared across the
    pytest process — reload around the enroll like test_env_enroll does)."""
    import importlib
    import adk.packs.arc_world.adapter as arc_adapter

    # Set the proof path BEFORE the reloads: PROOF_PATH is bound at module import
    # time, so the module must be re-imported after the env lands (same as
    # test_env_enroll.py — the in-process WM engine is also a module global that
    # the chaos/learn tests must not share).
    monkeypatch.setenv("ADK_SANDBOX_PROOF_PATH", str(tmp_path / "proofs.json"))
    import adk.packs.world_model.env_enroll as ee
    import adk.packs.world_model.tools as wm_tools

    importlib.reload(wm_tools)
    importlib.reload(ee)

    sent: list = []

    def fake_submit(base, tok, grid, action_str, next_grid, game):
        sent.append({"base": base, "tok": tok, "action": action_str, "game": game,
                     "grid_rows": len(grid) if grid else None})
        return True, True  # (submitted, accepted)

    monkeypatch.setattr(arc_adapter._BP, "_submit_observe", fake_submit)

    fake = FakeArcHttp()
    yield fake, sent, arc_adapter
    importlib.reload(wm_tools)


def test_adapter_importable_and_contracts(enrolled):
    """adk.packs.arc_world imports, exposes ArcGatewayAdapter, and the adapter
    passes env_enroll's EnvironmentAdapter contract check."""
    from adk.packs.arc_world import ArcGatewayAdapter
    from adk.packs.world_model.env_enroll import _validate

    fake, _sent, _aa = enrolled
    a = ArcGatewayAdapter("ls20", _http=fake, token="wm_test",
                          gateway_url="http://fake-gateway")
    missing = _validate(a)
    assert missing == [], f"adapter fails EnvironmentAdapter contract: {missing}"
    assert a.domain == "arc:ls20"
    assert callable(a.observe) and callable(a.actions) and callable(a.step)


@requires_world_model_engine
def test_enroll_learns_and_submits(enrolled):
    """env_enroll over the ArcGatewayAdapter: the deterministic world proves, a
    sandbox proof persists, and every transition reached the gateway submit path."""
    fake, sent, arc_adapter = enrolled
    ee_path = os.environ["ADK_SANDBOX_PROOF_PATH"]

    from adk.packs.world_model.env_enroll import env_enroll
    out = env_enroll(
        "adk.packs.arc_world.adapter:ArcGatewayAdapter",
        adapter_kwargs={"game_id": "ls20", "_http": fake,
                        "token": "wm_test", "gateway_url": "http://fake-gateway"},
        episodes=6, budget=30,
        name="arc:ls20")
    assert out["ok"], out
    assert out["transitions"] >= 20, out
    assert out["proven"] is True, (
        f"deterministic ARC world must prove: trailing mean "
        f"{out['trailing_mean_surprise']} > 0.3")
    assert out["proof_persisted"] and os.path.exists(ee_path)

    # the contribution half really fired — per-step gateway submits, tagged by game
    assert len(sent) >= out["transitions"], (
        f"expected ~{out['transitions']} gateway submits, got {len(sent)}")
    assert all(s["game"] == "ls20" for s in sent)
    assert all(s["tok"] == "wm_test" for s in sent)
    assert any(s["action"].startswith("ACTION") for s in sent)
    # grid payloads are the ARC 64x64 shape the gateway filter requires
    assert all(s["grid_rows"] == 64 for s in sent)


@requires_world_model_engine
def test_enroll_without_token_learns_locally(enrolled, monkeypatch):
    """No contributor token -> the game is still played and learned locally; only
    the gateway half is skipped. This is the zero-setup path (first-timer, no
    arc_register yet)."""
    fake, sent, arc_adapter = enrolled
    # Force "no token" even if a prior arc_register persisted one on this box:
    # the adapter's token resolution falls back to the persisted file by design.
    monkeypatch.setattr(arc_adapter._BP, "_token", lambda: "")
    from adk.packs.world_model.env_enroll import env_enroll
    out = env_enroll(
        "adk.packs.arc_world.adapter:ArcGatewayAdapter",
        adapter_kwargs={"game_id": "ls20", "_http": fake, "submit": True},
        episodes=6, budget=30,
        name="arc:ls20-tok")
    assert out["ok"], out
    assert out["transitions"] >= 16, out
    assert out["proven"] is True, out
    assert sent == [], "without a token nothing may be submitted to the gateway"


def test_no_api_key_is_loud():
    """Constructing the real adapter without ARC_API_KEY (and without an injected
    session) raises a readable error — never a silent 'no-op' world."""
    from adk.packs.arc_world.adapter import ArcGatewayAdapter
    import os as _os
    old = _os.environ.pop("ARC_API_KEY", None)
    try:
        with pytest.raises(ValueError, match="ARC_API_KEY"):
            ArcGatewayAdapter("ls20")
    finally:
        if old is not None:
            _os.environ["ARC_API_KEY"] = old


def test_arc_enroll_needs_api_key(monkeypatch):
    """arc_enroll bails early, readably, when ARC_API_KEY is unset — no network,
    no wasted ARC quota."""
    monkeypatch.delenv("ARC_API_KEY", raising=False)
    bp = _load_brainpack()
    raw = bp.arc_enroll("ls20")
    d = json.loads(raw)
    assert d["ok"] is False
    assert "ARC_API_KEY" in d["error"]


def test_pack_registers_arc_enroll():
    """The discoverable arc-brainpack registers six arc_* tools including the new
    arc_enroll (the count the loader's required-pack contract checks)."""
    bp = _load_brainpack("__init__.py")
    register = bp.register

    class MockRegistry:
        def __init__(self):
            self.tools = []

        def register(self, fn):
            self.tools.append(fn)

    reg = MockRegistry()
    count = register(reg)
    names = {getattr(f, "__name__", "") for f in reg.tools}
    assert count == 6, (count, names)
    for want in ("arc_register", "arc_contribute", "arc_enroll",
                 "arc_status", "arc_leaderboard", "arc_solo"):
        assert want in names, (want, names)
