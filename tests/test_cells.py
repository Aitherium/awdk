"""adk.cells: one contract, same caller code in-process or across nodes, generated
surfaces, and the estate planner."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml
from pydantic import BaseModel, ValidationError

from adk.cells import (
    ANONYMOUS,
    Caller,
    CellCallError,
    Cells,
    ContractError,
    HttpsTransport,
    PlanError,
    Scope,
    ScopeDeniedError,
    UnknownCellError,
    cli_verbs,
    contract,
    diff,
    load,
    mcp_tools,
    op,
    operator,
    plan,
    schedules,
)
from adk.cells.__main__ import main as cells_main
from adk.cells.server import build_app


class Hit(BaseModel):
    key: str
    score: float


@contract("memory", version=2)
class Memory:
    @op(scope=Scope.workspace, cli="aw mem recall")
    async def recall(self, query: str, k: int = 3) -> list[Hit]:
        """Find what was remembered about a query."""

    @op(scope=Scope.workspace, idempotent=True)
    async def remember(self, key: str, value: str) -> bool:
        """Store a value under a key."""

    @op(scope=Scope.operator, schedule="0 */4 * * *", mcp=False)
    async def compact(self) -> int:
        """Drop expired entries."""

    @op(scope=Scope.public)
    async def ping(self) -> str:
        """Liveness."""


class DictMemory(Memory):
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def recall(self, query: str, k: int = 3) -> list[Hit]:
        hits = [Hit(key=key, score=1.0) for key, v in self.store.items() if query in v]
        return hits[:k]

    async def remember(self, key: str, value: str) -> bool:
        self.store[key] = value
        return True

    async def compact(self) -> int:
        return 0

    async def ping(self) -> str:
        return "pong"


WORKSPACE = Caller(subject="agent:demiurge", scopes=frozenset({Scope.workspace}))
TOKENS = {"ws-token": WORKSPACE, "op-token": operator()}


def authenticate(token: str | None) -> Caller:
    return TOKENS.get(token or "", ANONYMOUS)


def remote_cells(impl: DictMemory, token: str) -> Cells:
    app = build_app(Cells().host(impl), authenticate)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    transport = HttpsTransport("https://node-2.cells.test", token=token, client=client)
    return Cells().remote(Memory, transport)


# --- contracts -------------------------------------------------------------------------


def test_contract_generates_one_op_per_method_with_routes():
    from adk.cells import spec_of

    spec = spec_of(Memory)
    assert set(spec.ops) == {"recall", "remember", "compact", "ping"}
    assert spec.ops["recall"].route == "/cells/memory/v2/recall"
    assert spec_of(DictMemory()) is spec


def test_params_model_rejects_unknown_and_mistyped_fields():
    from adk.cells import spec_of

    params = spec_of(Memory).ops["recall"].params
    assert params.model_validate({"query": "x"}).k == 3
    with pytest.raises(ValidationError):
        params.model_validate({"query": "x", "tenant": "someone-else"})
    with pytest.raises(ValidationError):
        params.model_validate({"query": "x", "k": "many"})


def test_bad_declarations_are_refused():
    with pytest.raises(ContractError, match="async"):
        op()(lambda self: None)

    with pytest.raises(ContractError, match="needs a type"):
        @contract("bad")
        class Untyped:
            @op()
            async def go(self, x) -> int: ...

    with pytest.raises(ContractError, match="no ops"):
        @contract("empty")
        class Empty:
            pass


# --- same caller code, local and remote ------------------------------------------------


async def exercise(cells: Cells) -> list[Hit]:
    me = cells.as_caller(WORKSPACE)
    assert await me.memory.remember(key="vhdx", value="the vhdx died of a full disk") is True
    return await me.memory.recall(query="full disk")


async def test_local_and_remote_return_identical_results():
    local = await exercise(Cells().host(DictMemory()))
    remote = await exercise(remote_cells(DictMemory(), "ws-token"))
    assert local == remote == [Hit(key="vhdx", score=1.0)]


async def test_scope_is_enforced_locally_and_remotely():
    with pytest.raises(ScopeDeniedError):
        await Cells().host(DictMemory()).as_caller(WORKSPACE).memory.compact()
    with pytest.raises(ScopeDeniedError):
        await Cells().host(DictMemory()).memory.recall(query="x")  # anonymous
    assert await Cells().host(DictMemory()).memory.ping() == "pong"  # public

    # The server re-checks scope from the TOKEN; a client claiming more cannot bypass it.
    cells = remote_cells(DictMemory(), "ws-token")
    forged = Caller(subject="forged", scopes=frozenset({Scope.operator}))
    with pytest.raises(CellCallError) as err:
        await cells.as_caller(forged).memory.compact()
    assert err.value.status == 403
    assert await remote_cells(DictMemory(), "op-token").as_caller(forged).memory.compact() == 0


async def test_remote_validates_payload_server_side():
    app = build_app(Cells().host(DictMemory()), authenticate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://n"
    ) as client:
        resp = await client.post(
            "/cells/memory/v2/recall",
            json={"query": "x", "extra": 1},
            headers={"Authorization": "Bearer ws-token"},
        )
        assert resp.status_code == 422
        index = (await client.get("/cells/_contracts")).json()
        assert index["memory"]["ops"]["compact"]["schedule"] == "0 */4 * * *"


async def test_unknown_cell_and_op():
    cells = Cells().host(DictMemory())
    with pytest.raises(UnknownCellError):
        cells.relay
    with pytest.raises(AttributeError):
        cells.memory.drop_everything


def test_plain_http_is_refused_and_tls_checks_cannot_be_disabled(tmp_path):
    import inspect

    with pytest.raises(ValueError, match="https only"):
        HttpsTransport("http://node-2:8443")
    # No parameter exists that could turn certificate checks off.
    assert "verify" not in inspect.signature(HttpsTransport).parameters
    # The CA bundle is really loaded, not silently ignored.
    with pytest.raises(OSError):
        HttpsTransport("https://node-2:8443", ca=str(tmp_path / "missing-ca.pem"))


# --- generated surfaces ----------------------------------------------------------------


def test_mcp_tools_are_scoped_to_the_caller():
    from adk.cells import spec_of

    specs = [spec_of(Memory)]
    names = {t["name"] for t in mcp_tools(specs, WORKSPACE)}
    assert names == {"memory_recall", "memory_remember", "memory_ping"}
    assert {t["name"] for t in mcp_tools(specs, ANONYMOUS)} == {"memory_ping"}
    recall = next(t for t in mcp_tools(specs, WORKSPACE) if t["name"] == "memory_recall")
    assert recall["inputSchema"]["required"] == ["query"]
    assert recall["description"].startswith("Find what")
    assert cli_verbs(specs) == {"aw mem recall": "memory.recall"}
    assert schedules(specs) == [{"op": "memory.compact", "cron": "0 */4 * * *"}]


# --- estate planner --------------------------------------------------------------------

NODES = {
    "nodes": {
        "dgx": {"cpu": 20, "mem": "128G", "gpus": ["120G"],
                "labels": {"zone": "home", "disk.ssd": True, "trust": "high"}},
        "desk": {"cpu": 16, "mem": "64G", "gpus": ["32G"],
                 "labels": {"zone": "home", "disk.ssd": True, "trust": "high"},
                 "tags": ["owner-gaming"]},
        "hz1": {"cpu": 8, "mem": "32G", "labels": {"zone": "fsn", "trust": "high"}},
        "hz2": {"cpu": 8, "mem": "32G", "labels": {"zone": "hel", "trust": "low"}},
    }
}


def test_plan_spreads_constrains_and_yields():
    estate = load({
        "cells": {
            "identity": {"replicas": 3, "spread": "zone"},
            "vault": {"replicas": 2, "place": {"trust": "high"}, "spread": "zone"},
            "memory": {"replicas": 2, "place": {"disk.ssd": True}},
            "inference": {"per_gpu": True, "vram": "24G", "place": {"gpu.vram": ">=16G"},
                          "yield_to": ["owner-gaming"]},
        }
    }, NODES)
    placed = plan(estate)
    assert len(set(placed["identity"])) == 3
    zones = {estate.nodes[n].labels["zone"] for n in placed["identity"]}
    assert zones == {"home", "fsn", "hel"}
    assert "hz2" not in placed["vault"] and len(placed["vault"]) == 2
    assert set(placed["memory"]) == {"dgx", "desk"}
    assert placed["inference"] == ["dgx"]  # desk is gaming, so inference yields


def test_plan_lists_every_problem():
    estate = load({
        "cells": {
            "vault": {"replicas": 3, "place": {"trust": "high"}, "spread": "zone"},
            "giant": {"vram": "200G"},
        }
    }, NODES)
    with pytest.raises(PlanError) as err:
        plan(estate)
    text = "\n".join(err.value.problems)
    assert "vault replica 3/3" in text and "giant replica 1/1" in text


def test_diff_starts_before_stops():
    actions = diff({"memory": ["hz1", "dgx"]}, {"memory": ["dgx", "desk"]})
    assert actions == [
        {"action": "start", "cell": "memory", "node": "desk"},
        {"action": "stop", "cell": "memory", "node": "hz1"},
    ]


def test_cli_exit_codes(tmp_path, capsys):
    nodes = tmp_path / "nodes.yaml"
    nodes.write_text(yaml.safe_dump(NODES), encoding="utf-8")
    good = tmp_path / "estate.yaml"
    good.write_text(yaml.safe_dump({"cells": {"relay": {"replicas": 2}}}), encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"cells": {"relay": {"replicas": 9}}}), encoding="utf-8")

    assert cells_main(["plan", str(good), str(nodes)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["placement"]["relay"]) == 2 and len(out["actions"]) == 2
    assert cells_main(["plan", str(bad), str(nodes)]) == 1
    assert cells_main(["plan", str(tmp_path / "missing.yaml"), str(nodes)]) == 2
