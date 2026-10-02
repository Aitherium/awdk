"""The /embed-migrate shell plugin (layer 6 of the embed-migrate product).

Read-only window onto the Genesis router ``/api/v1/embed-migrate/*``. These tests
pin the three things that would be a real defect: it must register, it must never
grow a verb that starts a run or retires an embedder, and it must never render an
unreachable engine or an unverified collection as a success.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path

import pytest
from adk.shell.plugins import PluginRegistry, SlashCommand

PLUGIN = (Path(__file__).resolve().parents[1] / "adk" / "shell" / "plugins" / "builtins"
          / "embed_migrate.py")


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("_embm_plugin_src", str(PLUGIN))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _calls(monkeypatch, mod, status=200, data=None):
    seen = []

    async def fake_get(ctx, path, params=None):
        seen.append(path)
        return status, ({} if data is None else data)

    monkeypatch.setattr(mod, "_get", fake_get)
    return seen


def test_plugin_is_the_layer6_surface():
    src = PLUGIN.read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert {"EmbedMigratePlugin", "render_plan", "render_status", "render_retire_gate"} <= names
    assert '"/api/v1/embed-migrate"' in src
    for route in ('"/plan"', '"/status"', '"/retire-gate"'):
        assert route in src


def test_plugin_registers_with_its_alias():
    reg = PluginRegistry([])
    reg.load_all()
    cmd = reg.get("embed-migrate")
    assert isinstance(cmd, SlashCommand), "/embed-migrate is not registered"
    assert type(cmd).__name__ == "EmbedMigratePlugin"
    assert reg.get("emig") is cmd, "/emig does not alias /embed-migrate"


def test_surface_is_exactly_the_three_read_only_routes(mod):
    assert mod.ROUTES == {"plan": "/plan", "status": "/status", "retire-gate": "/retire-gate"}
    src = PLUGIN.read_text(encoding="utf-8")
    for verb in ("client.post", "client.put", "client.delete", "client.patch", "client.request"):
        assert verb not in src, f"the plugin must stay GET-only, found {verb}"


@pytest.mark.parametrize("verb", ["run", "retire", "retire-nomic", "--run", "start", "verify"])
def test_a_mutating_verb_is_unknown_and_makes_no_call(monkeypatch, mod, verb):
    seen = _calls(monkeypatch, mod)
    out = asyncio.run(mod.EmbedMigratePlugin().run([verb], {}))
    assert out.startswith("Unknown subcommand")
    assert seen == []


@pytest.mark.parametrize("args,path", [([], "/status"), (["status"], "/status"),
                                       (["plan"], "/plan"), (["retire-gate"], "/retire-gate"),
                                       (["RETIRE_GATE"], "/retire-gate")])
def test_subcommands_hit_their_route(monkeypatch, mod, args, path):
    seen = _calls(monkeypatch, mod, data={"ok": True})
    asyncio.run(mod.EmbedMigratePlugin().run(args, {}))
    assert seen == [path]


def test_plan_renders_actions_and_reasons(mod):
    text = mod.render_plan({
        "ok": True, "source": "nomic", "target": {"name": "qwen3", "dim": 1024},
        "collections": [
            {"store": "main", "source": "memories", "target": "memories__q3", "points": 42,
             "dim": 768, "action": "migrate", "target_points": None},
            {"store": "main", "source": "blank", "target": "blank__q3", "points": 0,
             "dim": 768, "action": "empty", "target_points": 0},
            {"store": "main", "source": "clip", "target": "clip__q3", "points": 9, "dim": 512,
             "action": "skip-other-space", "reason": "512-d is not the nomic space (768-d)"},
        ],
        "stores_unreachable": {"edge": "connection refused"},
    })
    assert "nomic -> qwen3 (1024-d)" in text
    assert "MIGRATE (1):" in text and "main/memories -> memories__q3  42 points" in text
    assert "target not created" in text
    assert "EMPTY (1)" in text and "main/blank" in text
    assert "skip-other-space: 512-d is not the nomic space" in text
    assert "UNREACHABLE" in text and "edge: connection refused" in text


def test_status_renders_progress_and_verdict(mod):
    text = mod.render_status({
        "ok": True, "source": "nomic", "target": "qwen3", "retire_ready_at": None,
        "collections": {
            "main/a": {"target": "a__q3", "source_points": 10, "pending": 0, "embedded_total": 10,
                       "verified_at": "2026-10-01T00:00:00Z", "verify": {"ok": True}},
            "main/b": {"target": "b__q3", "source_points": 10, "pending": 4, "embedded_total": 6,
                       "verify": {"ok": False, "reasons": ["recall 0.61 below floor 0.9"]}},
            "main/c": {"target": "c__q3", "source_points": 5, "pending": 5,
                       "error": "RuntimeError: text source missing"},
        },
    })
    assert "retire-ready at never" in text and "COLLECTIONS (3):" in text
    a, b, c = [ln for ln in text.splitlines() if ln.startswith("  main/")]
    assert "pending 0/10" in a and "PASS at 2026-10-01T00:00:00Z" in a
    assert "pending 4/10" in b and "FAIL (recall 0.61 below floor 0.9)" in b
    assert "UNVERIFIED" in c and "PASS" not in c
    assert "error: RuntimeError: text source missing" in text


def test_a_collection_with_no_verify_record_is_never_a_pass(mod):
    text = mod.render_status({"collections": {"main/a": {"target": "a__q3", "pending": 0,
                                                         "verified_at": None}}})
    assert "UNVERIFIED" in text and "PASS" not in text


def test_retire_gate_ready_and_blocked(mod):
    ready = mod.render_retire_gate({"ok": True, "ready": True, "blockers": [], "source": "nomic"})
    assert ready.startswith("READY  retire nomic")
    blocked = mod.render_retire_gate({"ok": True, "ready": False, "source": "nomic",
                                      "blockers": ["main/b: never verified"]})
    assert blocked.startswith("NOT READY")
    assert "BLOCKERS (1):" in blocked and "main/b: never verified" in blocked


@pytest.mark.parametrize("data", [
    {"ok": True, "blockers": []},                                   # ready missing
    {"ok": True, "ready": "true", "blockers": []},                  # not a literal bool
    {"ok": True, "ready": True, "blockers": ["store x unreachable"]},  # contradicts itself
    {"ok": False, "ready": True, "reason": "engine unreachable"},   # could not judge
    {},
])
def test_retire_gate_fails_closed(mod, data):
    text = mod.render_retire_gate(data)
    assert text.startswith("NOT READY"), text


@pytest.mark.parametrize("render", ["render_plan", "render_status", "render_retire_gate"])
def test_engine_unreachable_is_a_reason_not_an_empty_success(mod, render):
    text = getattr(mod, render)({"ok": False, "reason": "qdrant unreachable", "collections": []})
    assert "Could not judge: qdrant unreachable" in text
    assert "(none)" not in text and "MIGRATE" not in text and "COLLECTIONS" not in text
    assert "Could not judge" in getattr(mod, render)("<html>bad gateway</html>")


def test_http_errors_are_reported(monkeypatch, mod):
    _calls(monkeypatch, mod, status=401, data={"detail": "no bearer"})
    assert "aither login" in asyncio.run(mod.EmbedMigratePlugin().run(["plan"], {}))
    _calls(monkeypatch, mod, status=503, data={"detail": "engine unavailable"})
    assert asyncio.run(mod.EmbedMigratePlugin().run(["status"], {})) == \
        "Error 503: engine unavailable"


def test_an_unreachable_platform_never_raises(monkeypatch, mod):
    monkeypatch.setenv("AITHER_GENESIS_URL", "https://127.0.0.1:9")
    monkeypatch.setattr(mod, "AuthStore", None)
    out = asyncio.run(mod.EmbedMigratePlugin().run(["retire-gate"], {}))
    assert out.startswith("Could not judge: platform unreachable"), out
