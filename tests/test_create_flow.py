"""Create -> Plan -> Deploy: the six-pillar loop, end to end, with real validators.

The backend and the memory plane are fakes that behave like the routes they stand for;
the file-backed kinds (skill pack, tool pack) run the REAL loaders and validators.
"""

from __future__ import annotations

import asyncio

import pytest

from adk.create_flow import PILLARS, CreateEngine, PlanStore, Principal
from adk.create_flow.backends import Reply
from adk.create_flow.engine import PlanError

OWNER = Principal(user_id="u-owner", tenant_id="t-owner")
OTHER = Principal(user_id="u-other", tenant_id="t-other")


class FakeMemory:
    """Stands in for the memory plane: remembers text, recalls by substring."""

    name = "fake"

    def __init__(self, up: bool = True):
        self.rows: list[str] = []
        self.up = up

    async def remember(self, category, content):
        if not self.up:
            return []
        self.rows.append(content)
        return [self.name]

    async def recall(self, query, category=""):
        return [r for r in self.rows if query.split("=")[0] in r]


class FakePlatform:
    """The workspace + agent routes, as far as the engine touches them."""

    name = "fake-platform"

    def __init__(self):
        self.workspaces: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.refuse_agents = False

    async def call(self, method, path, body=None):
        self.calls.append((method, path))
        if (method, path) == ("GET", "/workspace"):
            return Reply(
                200,
                {"workspaces": [w for w in self.workspaces.values() if w["status"] == "active"]},
            )
        if (method, path) == ("POST", "/workspace"):
            wid = f"ws_{len(self.workspaces) + 1}"
            self.workspaces[wid] = {"workspace_id": wid, "slug": body["slug"], "status": "active"}
            return Reply(200, {"status": "ok", "workspace": self.workspaces[wid]})
        if method == "GET" and path.startswith("/workspace/"):
            ws = self.workspaces.get(path.rsplit("/", 1)[1])
            return Reply(200, {"workspace": ws}) if ws else Reply(404, {"detail": "no"})
        if method == "PATCH" and path.startswith("/workspace/"):
            ws = self.workspaces[path.rsplit("/", 1)[1]]
            ws["status"] = body["status"]
            return Reply(200, {"workspace": ws})
        if (method, path) == ("GET", "/agents/compose"):
            return Reply(200, {"agents": list(self.agents.values())})
        if (method, path) == ("POST", "/agents/compose"):
            if self.refuse_agents:
                return Reply(403, {"detail": "Your plan allows 1 agent(s)"})
            self.agents[body["name"]] = {"name": body["name"], "workspace_id": body["workspace_id"]}
            return Reply(200, {"name": body["name"], "status": "created"})
        if path.startswith("/agents/compose/"):
            name = path.rsplit("/", 1)[1]
            if method == "GET":
                a = self.agents.get(name)
                return Reply(200, {"identity": a}) if a else Reply(404, {"detail": "no"})
            if method == "DELETE":
                self.agents.pop(name)
                return Reply(200, {"removed": name})
        return Reply(404, {"detail": f"unrouted {method} {path}"})


@pytest.fixture
def env(tmp_path):
    mem, plat = FakeMemory(), FakePlatform()
    eng = CreateEngine(
        runtime="awdk",
        store=PlanStore(tmp_path / "plans"),
        backend=plat,
        memory=mem,
        pack_root=tmp_path / "packs",
    )
    return eng, mem, plat, tmp_path


def _trace(plan):
    return {t["pillar"]: t for t in plan["trace"]}


async def test_skill_pack_runs_all_six_pillars_and_undoes(env):
    eng, mem, _plat, tmp = env
    plan = await eng.plan(
        OWNER,
        kind="skill_pack",
        name="Invoice Triage",
        description="Sort incoming invoices into pay, query and ignore.",
        docs=[{"name": "policy.txt", "text": "Pay under 500 without approval."}],
    )
    assert plan["status"] == "planned" and not plan["blocked"]
    assert plan["slug"] == "invoice-triage"
    assert any(f.endswith("SKILL.md") for f in plan["preview"]["files"])
    assert plan["preview"]["cost"]["usd"] == 0
    done = await eng.approve(OWNER, plan["id"], wait=True)
    assert done["status"] == "deployed", done.get("error")
    tr = _trace(done)
    assert [t["pillar"] for t in done["trace"]] == list(PILLARS)
    assert all(tr[p]["status"] in ("done",) for p in PILLARS), {p: tr[p]["status"] for p in PILLARS}
    skill = tmp / "packs" / "invoice-triage" / "skills" / "invoice-triage" / "SKILL.md"
    assert skill.is_file() and "Pay under 500" in skill.read_text(encoding="utf-8")
    assert done["verify"]["ok"]
    undone = await eng.undo(OWNER, plan["id"])
    assert undone["status"] == "undone"
    assert not (tmp / "packs" / "invoice-triage").exists()


async def test_learning_closes_the_loop(env):
    """A run's outcome is written back and the NEXT plan's context + prediction move."""
    eng, mem, plat, _ = env
    first = await eng.plan(OWNER, kind="skill_pack", name="First Pack", description="One.")
    assert first["prediction"]["n"] == 0 and first["context"]["lessons"] == 0
    await eng.approve(OWNER, first["id"], wait=True)
    assert len(mem.rows) == 1 and "create_flow lesson kind=skill_pack" in mem.rows[0]
    second = await eng.plan(OWNER, kind="skill_pack", name="Second Pack", description="Two.")
    assert second["context"]["lessons"] == 1
    assert second["prediction"]["n"] == 1
    assert second["prediction"]["p_success"] > first["prediction"]["p_success"]
    # a failure moves it back down
    plat.refuse_agents = True
    bad = await eng.plan(OWNER, kind="agent", name="Helper", description="Helps.")
    failed = await eng.approve(OWNER, bad["id"], wait=True)
    assert failed["status"] == "failed" and "refused" in failed["error"]
    again = await eng.plan(OWNER, kind="agent", name="Helper Two", description="Helps again.")
    assert again["prediction"]["n"] == 1
    assert again["prediction"]["p_success"] < 0.8


async def test_workspace_then_agent_inside_it_then_archive(env):
    eng, _mem, plat, _ = env
    ws_plan = await eng.plan(OWNER, kind="workspace", name="Acme Ops", description="Ops for Acme.")
    ws = await eng.approve(OWNER, ws_plan["id"], wait=True)
    assert ws["status"] == "deployed" and ws["verify"]["ok"]
    wid = ws["result"]["workspace_id"]
    ag_plan = await eng.plan(
        OWNER, kind="agent", name="Scout", description="Watches the inbox.", workspace_id=wid
    )
    assert not ag_plan["preview"]["risks"]
    ag = await eng.approve(OWNER, ag_plan["id"], wait=True)
    assert ag["status"] == "deployed", ag.get("error")
    assert plat.agents["scout"]["workspace_id"] == wid
    assert any(c["name"] == "in the chosen workspace" and c["ok"] for c in ag["verify"]["checks"])
    await eng.undo(OWNER, ag_plan["id"])
    assert "scout" not in plat.agents
    arch = await eng.undo(OWNER, ws_plan["id"])
    assert arch["undo"]["result"]["archived"] and plat.workspaces[wid]["status"] == "archived"


async def test_other_principal_cannot_see_approve_or_undo(env):
    eng, *_ = env
    plan = await eng.plan(OWNER, kind="skill_pack", name="Mine", description="Private.")
    for call in (lambda: eng.get(OTHER, plan["id"]),):
        with pytest.raises(PlanError) as e:
            call()
        assert e.value.status == 404
    with pytest.raises(PlanError) as e:
        await eng.approve(OTHER, plan["id"])
    assert e.value.status == 404
    with pytest.raises(PlanError) as e:
        await eng.undo(OTHER, plan["id"])
    assert e.value.status == 404
    assert eng.list(OTHER) == []


async def test_name_collision_blocks_approval(env):
    eng, _mem, plat, _ = env
    plat.workspaces["ws_x"] = {"workspace_id": "ws_x", "slug": "acme-ops", "status": "active"}
    plan = await eng.plan(OWNER, kind="workspace", name="Acme Ops", description="dup")
    assert plan["blocked"] and plan["status"] == "blocked"
    with pytest.raises(PlanError) as e:
        await eng.approve(OWNER, plan["id"])
    assert e.value.status == 409


async def test_planned_kinds_preview_but_refuse_to_run(env):
    eng, *_ = env
    plan = await eng.plan(OWNER, kind="workflow", name="Weekly Report", description="Every Monday.")
    assert plan["blocked"] and plan["preview"]["blockers"]
    assert plan["undo"]["available"] is False


async def test_irreversible_options_are_refused_not_ignored(env):
    eng, *_ = env
    with pytest.raises(PlanError) as e:
        await eng.plan(
            OWNER, kind="skill_pack", name="Pub", description="x", options={"publish": True}
        )
    assert e.value.status == 422


async def test_plain_words_infer_the_kind(env):
    eng, *_ = env
    plan = await eng.plan(OWNER, description="Make me a workspace for my bakery")
    assert plan["kind"] == "workspace" and plan["name"].lower().startswith(
        "workspace for my bakery"
    )


async def test_tool_pack_uses_the_real_authoring_verbs(env):
    eng, _m, _p, tmp = env
    plan = await eng.plan(
        OWNER, kind="tool_pack", name="Pdf Helpers", description="Tools for pdfs."
    )
    assert plan["status"] == "planned", plan["preview"].get("blockers")
    done = await eng.approve(OWNER, plan["id"], wait=True)
    assert done["status"] == "deployed", done.get("error")
    assert len(done["result"]["sha256"]) == 64
    await eng.undo(OWNER, plan["id"])
    assert not (tmp / "packs" / "pdf-helpers").exists()


async def test_reserved_pack_prefix_is_blocked_at_plan_time(env):
    eng, *_ = env
    plan = await eng.plan(OWNER, kind="tool_pack", name="Aither Extras", description="x")
    assert plan["blocked"]


async def test_undo_refuses_a_folder_it_did_not_make(env):
    eng, _m, _p, tmp = env
    plan = await eng.plan(OWNER, kind="skill_pack", name="Guarded", description="g")
    await eng.approve(OWNER, plan["id"], wait=True)
    marker = tmp / "packs" / "guarded" / ".create-flow.json"
    marker.write_text('{"plan_id": "someone-else"}', encoding="utf-8")
    with pytest.raises(PlanError):
        await eng.undo(OWNER, plan["id"])
    assert (tmp / "packs" / "guarded" / "pack.yaml").is_file()


async def test_degraded_learning_is_visible_not_silent(tmp_path):
    mem = FakeMemory(up=False)
    eng = CreateEngine(
        runtime="awdk",
        store=PlanStore(tmp_path / "p"),
        backend=None,
        memory=mem,
        pack_root=tmp_path / "packs",
    )
    plan = await eng.plan(OWNER, kind="skill_pack", name="Quiet", description="q")
    done = await eng.approve(OWNER, plan["id"], wait=True)
    assert done["status"] == "deployed"
    assert _trace(done)["learning"]["status"] == "degraded"


async def test_no_backend_fails_clearly_for_platform_kinds(tmp_path):
    eng = CreateEngine(
        runtime="awdk", store=PlanStore(tmp_path / "p"), backend=None, memory=FakeMemory()
    )
    plan = await eng.plan(OWNER, kind="workspace", name="Nowhere", description="n")
    done = await eng.approve(OWNER, plan["id"], wait=True)
    assert done["status"] == "failed" and "sign in" in done["error"]


def test_asyncio_mode_is_wired():
    assert asyncio.iscoroutinefunction(CreateEngine.plan)


async def test_agent_in_a_workspace_that_is_not_yours_is_blocked(env):
    eng, *_ = env
    plan = await eng.plan(
        OWNER, kind="agent", name="Spy", description="x", workspace_id="ws_theirs"
    )
    assert plan["blocked"]
    assert any("not one of yours" in b for b in plan["preview"]["blockers"])


def test_daemon_mounts_the_same_engine_and_runs_a_skill_pack(tmp_path, monkeypatch):
    """Parity: the harness daemon serves /create/* and builds with no platform bearer."""
    import time

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from adk.harnesses import create_routes

    monkeypatch.setenv("AITHER_CREATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AITHER_CREATOR_PACKS_DIR", str(tmp_path / "packs"))
    monkeypatch.setenv("AITHER_SESSION_BEARER", "")
    monkeypatch.setenv("AITHER_SPIRIT_BRIDGE", "false")
    monkeypatch.setattr(create_routes, "_BEARER_FILE", tmp_path / "none")
    app = FastAPI()
    create_routes.mount(app, lambda: None)
    with TestClient(app) as tc:
        plan = tc.post(
            "/create/plan",
            json={"kind": "skill_pack", "name": "Local Skill", "description": "Runs on this box."},
        ).json()
        assert plan.get("runtime") == "awdk" and plan["status"] == "planned", plan
        assert tc.post(f"/create/plans/{plan['id']}/approve").status_code == 202
        for _ in range(60):
            cur = tc.get(f"/create/plans/{plan['id']}").json()
            if cur["status"] != "running":
                break
            time.sleep(0.1)
        assert cur["status"] == "deployed", cur.get("error")
        assert [t["pillar"] for t in cur["trace"]] == list(PILLARS)
        assert cur["trace"][-1]["status"] == "done"  # learning wrote to the local memory plane
        skill = tmp_path / "packs" / "local-skill" / "skills" / "local-skill" / "SKILL.md"
        assert skill.is_file()
        assert tc.post(f"/create/plans/{plan['id']}/undo").json()["status"] == "undone"


async def test_a_poisoned_lesson_cannot_break_planning(env):
    """Memory is writable by every member of a tenant: junk rows are dropped, never trusted."""
    eng, mem, *_ = env
    mem.rows.append(
        "create_flow lesson kind=skill_pack "
        '{"kind":"skill_pack","event":"run","ok":true,"actual_s":"x"}'
    )
    mem.rows.append(
        "create_flow lesson kind=skill_pack "
        '{"kind":"skill_pack","event":"run","ok":true,"actual_s":1e99}'
    )
    mem.rows.append("create_flow lesson kind=skill_pack {not json")
    plan = await eng.plan(OWNER, kind="skill_pack", name="Still Works", description="x")
    assert plan["status"] == "planned"
    assert plan["prediction"]["duration_s"] <= 86400


async def test_two_approves_run_the_build_once(env):
    eng, *_ = env
    plan = await eng.plan(OWNER, kind="skill_pack", name="Once", description="x")
    first = await eng.approve(OWNER, plan["id"])
    with pytest.raises(PlanError) as e:
        await eng.approve(OWNER, plan["id"])
    assert e.value.status == 409
    await eng.wait_idle()
    assert eng.get(OWNER, first["id"])["status"] == "deployed"


async def test_a_plan_is_bound_to_the_tenant_that_made_it(env):
    eng, *_ = env
    plan = await eng.plan(OWNER, kind="skill_pack", name="Tenant Bound", description="x")
    elsewhere = Principal(user_id=OWNER.user_id, tenant_id="t-other")
    for call in (lambda: eng.get(elsewhere, plan["id"]),):
        with pytest.raises(PlanError) as e:
            call()
        assert e.value.status == 404
    with pytest.raises(PlanError):
        await eng.approve(elsewhere, plan["id"])


async def test_the_shared_public_bucket_never_builds(env):
    eng, *_ = env
    for tenant in ("", "public", "unknown"):
        with pytest.raises(PlanError) as e:
            await eng.plan(
                Principal(user_id="u", tenant_id=tenant),
                kind="skill_pack",
                name="x",
                description="x",
            )
        assert e.value.status == 403


async def test_workspace_id_must_look_like_an_id(env):
    eng, *_ = env
    for bad in ("../healthz", "a/b", "x" * 200, "ws id"):
        with pytest.raises(PlanError):
            await eng.plan(OWNER, kind="agent", name="Spy", description="x", workspace_id=bad)


async def test_bad_options_are_a_plan_error_not_a_500(env):
    eng, *_ = env
    with pytest.raises(PlanError):
        await eng.plan(
            OWNER, kind="agent", name="A", description="x", options={"effort_cap": "lots"}
        )
    with pytest.raises(PlanError):
        await eng.plan(
            OWNER, kind="skill_pack", name="B", description="x", options={"skills": "nope"}
        )
    with pytest.raises(PlanError):
        await eng.plan(
            OWNER, kind="skill_pack", name="C", description="x", options={"skills": ["s"] * 11}
        )


async def test_a_stale_running_plan_is_failed_not_stuck_forever(env):
    eng, *_ = env
    plan = await eng.plan(OWNER, kind="skill_pack", name="Ghost", description="x")
    stored = eng.store.load(plan["id"])
    stored["status"], stored["updated_at"] = "running", 1.0  # a restart killed the task long ago
    eng.store.save(stored)
    got = eng.get(OWNER, plan["id"])
    assert got["status"] == "failed" and "interrupted" in got["error"]


async def test_lessons_carry_an_error_category_never_typed_text(env):
    eng, mem, plat, _ = env
    plat.refuse_agents = True
    plan = await eng.plan(OWNER, kind="agent", name="Leaky", description="my password is hunter2")
    await eng.approve(OWNER, plan["id"], wait=True)
    assert mem.rows and "hunter2" not in mem.rows[-1] and "Your plan allows" not in mem.rows[-1]
    assert '"error": "refused"' in mem.rows[-1]


async def test_a_yaml_hostile_description_still_verifies(env):
    eng, _m, _p, tmp = env
    plan = await eng.plan(
        OWNER,
        kind="skill_pack",
        name="Colon Pack",
        description="Rules: never #skip the - review: ever",
    )
    done = await eng.approve(OWNER, plan["id"], wait=True)
    assert done["status"] == "deployed", done.get("error")


async def test_a_failed_tool_pack_leaves_no_orphan_folder(env, monkeypatch):
    eng, _m, _p, tmp = env
    from adk import pack_author

    monkeypatch.setattr(pack_author, "build", lambda d: (_ for _ in ()).throw(ValueError("boom")))
    plan = await eng.plan(OWNER, kind="tool_pack", name="Orphan Check", description="x")
    done = await eng.approve(OWNER, plan["id"], wait=True)
    assert done["status"] == "failed"
    assert not (tmp / "packs" / "orphan-check").exists()


async def test_asgi_backend_never_copies_client_identity_headers():
    from adk.create_flow.backends import AsgiBackend

    seen = {}

    async def app(scope, receive, send):
        seen.update({k.decode(): v.decode() for k, v in scope["headers"]})
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    be = AsgiBackend(
        app,
        {
            "Authorization": "Bearer t",
            "x-tenant-id": "victim",
            "x-caller-type": "platform",
            "X-Internal-Key": "k",
        },
        identity={"x-user-id": "u1", "x-tenant-id": "mine", "x-caller-type": "tenant"},
    )
    assert (await be.call("GET", "/workspace")).ok
    assert seen["authorization"] == "Bearer t"
    assert seen["x-tenant-id"] == "mine" and seen["x-caller-type"] == "tenant"
    assert seen["x-user-id"] == "u1" and "x-internal-key" not in seen
    await be.aclose()


async def test_the_running_cap_holds_at_approve_not_only_at_plan(env):
    eng, *_ = env
    plans = [
        await eng.plan(OWNER, kind="skill_pack", name=f"Cap {i}", description="x") for i in range(5)
    ]
    for p in plans[:3]:  # three builds are "running" (their tasks never ran in this test)
        assert eng.store.claim(p["id"], "planned", "running") is not None
    with pytest.raises(PlanError) as e:
        await eng.approve(OWNER, plans[3]["id"])
    assert e.value.status == 429


async def test_listing_reads_only_the_callers_own_plans(env):
    eng, *_ = env
    await eng.plan(OWNER, kind="skill_pack", name="Mine One", description="x")
    other = Principal(user_id="u-other", tenant_id="t-other")
    await eng.plan(other, kind="skill_pack", name="Theirs", description="x")
    assert [p["name"] for p in eng.list(OWNER)] == ["Mine One"]
    assert eng.store.bucket("u-owner") != eng.store.bucket("u-other")


async def test_a_failed_skill_pack_write_leaves_no_folder(env, monkeypatch):
    eng, _m, _p, tmp = env
    from adk.create_flow import kinds

    real = kinds._write

    def flaky(path, text):
        if path.name == "SKILL.md":
            raise OSError("disk full")
        return real(path, text)

    monkeypatch.setattr(kinds, "_write", flaky)
    plan = await eng.plan(OWNER, kind="skill_pack", name="Half Made", description="x")
    done = await eng.approve(OWNER, plan["id"], wait=True)
    assert done["status"] == "failed"
    assert not (tmp / "packs" / "half-made").exists()
