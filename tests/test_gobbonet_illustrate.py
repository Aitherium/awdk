"""campaign_illustrate: the picture keeps the scope, the face, and Media Forge's words.

The fake Media Forge below is a real HTTP server on loopback, so every test
drives the actual wire path (httpx, `<base>/op/<name>`, JSON in and out) and the
fake records exactly what was SENT. That record is the point: a scoping bug is
invisible in the tool's return value and obvious in the prompt that left the
machine.

Each test asserts a positive AND its boundary — a tool that refuses everything
passes every "refused" assertion while being inert.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("awm", reason="illustration reads campaign memory")

from adk.packs.gobbonet.campaign_memory import WORLD, CampaignMemory  # noqa: E402
from adk.packs.gobbonet.cards import card_to_memory, memory_to_card  # noqa: E402
from adk.packs.gobbonet.illustrate import (  # noqa: E402
    StudioLane,
    get_reference,
    illustrate,
    register_illustrate_tools,
)


class FakeForge:
    """Media Forge's curated surface: POST /op/{name} -> {ok, head_ids, images}."""

    def __init__(self):
        self.calls: list = []          # (path, body)
        self.refuse: dict = {}         # op name -> refusal text
        self._next = 100
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401 - silence
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                fake.calls.append((self.path, body))
                if not self.path.startswith("/op/"):
                    self.send_response(404)
                    self.end_headers()
                    return
                op = self.path[len("/op/"):]
                if op in fake.refuse:
                    out = {"ok": False, "error": fake.refuse[op]}
                else:
                    count = int(body.get("count") or 1)
                    ids = list(range(fake._next, fake._next + count))
                    fake._next += count
                    out = {"ok": True, "head_ids": ids,
                           "images": [f"/media/{op}-{i}.png" for i in ids]}
                raw = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def ops(self) -> list:
        return [p[len("/op/"):] for p, _ in self.calls]

    def sent_prompts(self) -> str:
        return "\n".join(str(b.get("prompt") or "") for _, b in self.calls)


@pytest.fixture()
def forge():
    f = FakeForge()
    yield f
    f.server.shutdown()
    f.server.server_close()


@pytest.fixture()
def mem(tmp_path):
    m = CampaignMemory("illus", db_path=tmp_path / "i.db")
    card_to_memory({
        "persona_id": "vex-7", "name": "Vex",
        "description": "Pale, silver-eyed, a crow-feather cloak.",
        "personality": "Sardonic.",
        "startingLore": "The city gates close at dusk.",
    }, m)
    m.note("The landlord is a vampire who sleeps under the old mill.", known_by="Vex")
    m.note("Mira keeps a lantern at the market stall.", known_by="Mira")
    return m


def lane(forge) -> StudioLane:
    return StudioLane(forge.base, key="")


class TestScoping:
    def test_a_character_cannot_be_drawn_with_a_fact_they_do_not_know(self, mem, forge):
        """Mira does not know the landlord's secret: refused, and NOTHING is sent."""
        out = illustrate(mem, lane(forge), character="Mira",
                         scene="the vampire landlord under the old mill")
        assert out["ok"] is False and out["error_code"] == "not_known", out
        assert forge.calls == [], "a refused scene still reached Media Forge"

    def test_the_holder_of_the_fact_can_be_drawn_with_it(self, mem, forge):
        out = illustrate(mem, lane(forge), character="Vex",
                         scene="the vampire landlord under the old mill")
        assert out["ok"], out
        assert "vampire" in forge.sent_prompts()

    def test_world_facts_reach_a_character_but_a_siblings_secret_never_does(self, mem, forge):
        out = illustrate(mem, lane(forge), character="Mira", scene="dusk at the city gates")
        assert out["ok"], out
        sent = forge.sent_prompts()
        assert "gates close at dusk" in sent          # world state: everyone's
        assert "vampire" not in sent and "mill" not in sent   # Vex's: never Mira's

    def test_the_scene_text_is_a_query_not_prompt_text(self, mem, forge):
        """Smuggling a secret in the scene string must not put it in the prompt."""
        out = illustrate(mem, lane(forge), character="Mira",
                         scene="dusk gates, and the landlord is secretly a vampire")
        assert out["ok"], out
        assert "vampire" not in forge.sent_prompts()

    def test_the_illustration_note_lands_at_the_viewpoints_scope(self, mem, forge):
        out = illustrate(mem, lane(forge), character="Vex", scene="vampire landlord")
        assert out["ok"] and out["note"]["ok"], out
        assert out["note"]["scope"] == "illus:vex:*"
        assert "Illustration of vex" in mem.brief(["vex"])
        assert "Illustration of vex" not in mem.brief(["mira"])

    def test_unknown_character_is_refused_before_any_request(self, mem, forge):
        out = illustrate(mem, lane(forge), character="Nobody", scene="gates")
        assert out["error_code"] == "unknown_character" and forge.calls == []


class TestReferenceReuse:
    def test_first_call_makes_the_reference_later_calls_reuse_it(self, mem, forge):
        first = illustrate(mem, lane(forge), character="vex-7")   # by persona id
        assert first["ok"] and first["reference"]["created"] is True, first
        ref_id = first["reference"]["media_id"]
        assert forge.ops() == ["txt2img"]
        assert get_reference(mem, "Vex")["media_id"] == ref_id

        forge.calls.clear()
        second = illustrate(mem, lane(forge), character="Vex", scene="dusk gates", count=2)
        assert second["ok"], second
        assert forge.ops() == ["compose"], "the character was re-rolled instead of reused"
        assert forge.calls[0][1]["face_ref"] == ref_id
        assert second["reference"] == dict(first["reference"], created=False)
        assert len(second["media_ids"]) == 2 and len(second["urls"]) == 2
        assert second["urls"][0].startswith(forge.base + "/media/")

    def test_the_reference_travels_on_the_card(self, mem, forge, tmp_path):
        made = illustrate(mem, lane(forge), character="Vex")
        card = memory_to_card("Vex", mem)["card"]
        assert card["reference"]["media_id"] == made["reference"]["media_id"]
        assert "card-ref" not in card.get("personality", ""), "the reference leaked into prose"

        other = CampaignMemory("elsewhere", db_path=tmp_path / "o.db")
        assert card_to_memory(card, other)["ok"]
        assert get_reference(other, "Vex")["media_id"] == made["reference"]["media_id"]

    def test_a_chosen_reference_replaces_the_stored_one(self, mem, forge):
        illustrate(mem, lane(forge), character="Vex")
        forge.calls.clear()
        out = illustrate(mem, lane(forge), character="Vex", scene="gates",
                         reference_media_id="777")
        assert out["ok"] and forge.calls[0][1]["face_ref"] == 777
        assert get_reference(mem, "Vex")["media_id"] == 777

    def test_references_are_per_character(self, mem, forge):
        illustrate(mem, lane(forge), character="Vex")
        assert get_reference(mem, "Mira") is None
        assert get_reference(mem, WORLD) is None


class TestMediaForgeAnswers:
    def test_a_curated_refusal_surfaces_in_its_own_words(self, mem, forge):
        forge.refuse["txt2img"] = "prompt blocked by AitherSafety at tier 'public'"
        out = illustrate(mem, lane(forge), character="Vex")
        assert out["ok"] is False and out["error_code"] == "refused", out
        # Media Forge's own words, attributed to the op that said them
        assert out["error"] == ("Media Forge refused txt2img: "
                                "prompt blocked by AitherSafety at tier 'public'"), out
        assert out["calls"] == ["txt2img"], "a refused portrait was followed by another op"
        # nothing was recorded as if it had happened
        assert get_reference(mem, "Vex") is None
        assert "Illustration of" not in mem.brief(["vex"])

    def test_a_refused_compose_keeps_the_new_reference_and_says_so(self, mem, forge):
        forge.refuse["compose"] = "op 'compose' is restricted at this safety level"
        out = illustrate(mem, lane(forge), character="Vex", scene="gates")
        assert out["error_code"] == "refused" and "restricted" in out["error"]
        assert out["reference"]["created"] is True
        assert "Illustration of" not in mem.brief(["vex"])

    def test_unreachable_media_forge_is_a_clean_error(self, mem):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                   # nothing listens here now
        out = illustrate(mem, StudioLane(f"http://127.0.0.1:{port}", timeout=3),
                         character="Vex")
        assert out["ok"] is False and out["error_code"] == "unreachable", out
        assert "did not answer" in out["error"]
        assert get_reference(mem, "Vex") is None

    def test_no_consent_means_no_request(self, mem, monkeypatch, tmp_path):
        monkeypatch.setenv("AITHER_HOME", str(tmp_path / "home"))
        out = illustrate(mem, StudioLane(), character="Vex")
        assert out["error_code"] == "not_connected" and "consent" in out["fix"]

    def test_the_consent_record_is_the_lane(self, mem, forge, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        (home / "studio-remote.json").write_text(json.dumps({"url": forge.base}))
        monkeypatch.setenv("AITHER_HOME", str(home))
        assert illustrate(mem, StudioLane(), character="Vex")["ok"]
        assert forge.calls and forge.calls[0][0] == "/op/txt2img"

    def test_the_owner_api_surface_is_refused(self, mem, forge):
        out = illustrate(mem, StudioLane(forge.base + "/api"), character="Vex")
        assert out["error_code"] == "owner_surface" and forge.calls == []
        # ...while a host proxy that merely contains "api" in its path is fine
        assert StudioLane("https://host/api/studio").unavailable() is None

    def test_every_request_is_a_curated_op_path(self, mem, forge):
        illustrate(mem, lane(forge), character="Vex")
        illustrate(mem, lane(forge), character="Vex", scene="gates")
        assert forge.calls and all(p.startswith("/op/") for p, _ in forge.calls)


class TestTool:
    def test_registers_and_runs(self, mem, forge):
        class Reg:
            tools: dict = {}

            def register(self, fn, name=None, description=None, **_):
                self.tools[name] = fn

        class Agent:
            tools = Reg()

        agent = Agent()
        assert register_illustrate_tools(agent, mem, lane_factory=lambda: lane(forge)) == 1
        out = agent.tools.tools["campaign_illustrate"](character="Vex", scene="gates")
        assert out["ok"] and out["media_ids"], out
