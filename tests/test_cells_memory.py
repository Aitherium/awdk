"""The memory cell over a REAL awm store: tenancy comes from the caller, never the payload,
and the same calls work in-process and over HTTPS."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from adk.cells import (
    ANONYMOUS,
    Caller,
    CellCallError,
    Cells,
    HttpsTransport,
    Scope,
    current_caller,
    mcp_tools,
    spec_of,
)
from adk.cells.memory import AwmMemory, MemoryCell, NoWorkspaceError
from adk.cells.server import build_app

pytest.importorskip("awm")


def member(workspace: str | None, subject: str = "agent") -> Caller:
    return Caller(subject=subject, scopes=frozenset({Scope.workspace}), workspace=workspace)


DANA = member("acme:dana:proj", "dana")
EVE = member("rival:eve:proj", "eve")
ACME = member("acme:*:*", "acme-admin")


@pytest.fixture
def store(tmp_path):
    return AwmMemory(tmp_path / "memory.db")


async def test_workspaces_are_isolated_by_the_caller(store):
    cells = Cells().host(store)
    await cells.as_caller(DANA).memory.remember(key="db", value="postgres 17")
    await cells.as_caller(EVE).memory.remember(key="db", value="mysql")

    dana = await cells.as_caller(DANA).memory.recall(query="db")
    eve = await cells.as_caller(EVE).memory.recall(query="db")
    assert [m.value for m in dana] == ["postgres 17"]
    assert [m.value for m in eve] == ["mysql"]
    assert dana[0].scope == "acme:dana:proj"


async def test_parent_scope_is_visible_child_is_not(store):
    cells = Cells().host(store)
    await cells.as_caller(ACME).memory.remember(key="policy", value="tls everywhere")
    await cells.as_caller(DANA).memory.remember(key="draft", value="dana only")

    seen_by_dana = {m.key for m in await cells.as_caller(DANA).memory.recall()}
    seen_by_acme = {m.key for m in await cells.as_caller(ACME).memory.recall()}
    assert seen_by_dana == {"policy", "draft"}
    assert seen_by_acme == {"policy"}


async def test_payload_cannot_name_a_workspace(store):
    cells = Cells().host(store)
    with pytest.raises(ValidationError):
        await cells.as_caller(DANA).memory.recall(query="db", scope="rival:eve:proj")
    with pytest.raises(ValidationError):
        await cells.as_caller(DANA).memory.remember(key="k", value="v", tenant="rival")


async def test_unbound_caller_is_refused_and_context_resets(store):
    cells = Cells().host(store)
    with pytest.raises(NoWorkspaceError):
        await cells.as_caller(member(None)).memory.recall()
    assert current_caller() is ANONYMOUS


async def test_history_and_forget(store):
    me = Cells().host(store).as_caller(DANA).memory
    await me.remember(key="port", value="8001")
    await me.remember(key="port", value="8443")
    versions = await me.history(key="port")
    assert [v.value for v in versions] == ["8001", "8443"]
    assert versions[-1].valid_to is None and versions[0].valid_to is not None
    assert await me.forget(key="port") is True
    assert await me.recall(query="port") == []


async def test_remote_gets_workspace_from_the_token_not_the_client(store):
    tokens = {"dana-token": DANA, "unbound": member(None)}
    app = build_app(Cells().host(store), lambda t: tokens.get(t or "", ANONYMOUS))

    def remote(token: str) -> Cells:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        return Cells().remote(
            MemoryCell, HttpsTransport("https://node-2.test", token=token, client=client)
        )

    # The client claims to be eve; the server acts as whoever the token says: dana.
    await remote("dana-token").as_caller(EVE).memory.remember(key="who", value="dana")
    local = await Cells().host(store).as_caller(DANA).memory.recall(query="who")
    assert [m.value for m in local] == ["dana"]
    assert await Cells().host(store).as_caller(EVE).memory.recall(query="who") == []

    with pytest.raises(CellCallError) as err:
        await remote("unbound").as_caller(DANA).memory.recall()
    assert err.value.status == 403


def test_generated_tools_for_the_memory_cell():
    names = {t["name"] for t in mcp_tools([spec_of(MemoryCell)], DANA)}
    assert names == {"memory_remember", "memory_recall", "memory_forget", "memory_history"}
    assert mcp_tools([spec_of(MemoryCell)], ANONYMOUS) == []
    schema = next(t for t in mcp_tools([spec_of(MemoryCell)], DANA)
                  if t["name"] == "memory_recall")["inputSchema"]
    assert not {"scope", "tenant", "workspace"} & set(schema["properties"])
