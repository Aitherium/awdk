"""Offline tests for the M5 forkd Tier-2 fan-out adapter.

The forkd REST daemon is injected as a mock ``http_fn`` (no KVM/Linux/httpx
needed), and account selection / degrade spawning are injected too — so the
fan-out logic, fail-closed scope intersection, account pinning, degrade path,
and secret-safety are all exercised deterministically offline.

Live e2e (real forkd on a Linux/KVM mesh node) is owner-gated.
"""

from __future__ import annotations

import pytest

from adk.forkd_client import (
    ChildSpec,
    ChildState,
    ForkdClient,
    ForkResult,
    ForkdError,
)


class MockDaemon:
    """Injectable forkd REST daemon: records calls, returns canned responses."""

    def __init__(self, *, healthy=True, fork_fail_ids=()):
        self.healthy = healthy
        self.fork_fail_ids = set(fork_fail_ids)
        self.calls = []
        self.exec_payloads = {}  # child_id -> body sent to /exec

    async def __call__(self, method, url, body):
        self.calls.append((method, url, body))
        path = url.split("8760", 1)[-1]
        if path == "/health":
            return (200 if self.healthy else 503), {"status": "ok" if self.healthy else "down"}
        if path == "/snapshots":
            return 201, {"snapshot_id": "snap-1"}
        if path == "/fork":
            cid = body.get("child_id")
            if cid in self.fork_fail_ids:
                return 500, {"error": "fork failed"}
            return 201, {"child_id": cid, "session_id": f"sess-{cid}"}
        if path.startswith("/exec/"):
            cid = path.split("/exec/", 1)[1]
            self.exec_payloads[cid] = body
            return 200, {"exit_code": 0, "stdout": f"done:{cid}", "stderr": ""}
        if path.startswith("/children/"):
            return 200, {"status": "reclaimed"}
        return 404, {}


def _acct(throttled=()):
    throttled = set(throttled)

    def select(cands):
        if cands is None:
            return "acct-auto"
        c = cands[0]
        return "" if c in throttled else c

    return select


def _child(task="do it", tools=("bash", "python"), account="", **kw):
    return ChildSpec(task=task, allowed_tools=list(tools), account_profile=account, **kw)


# ── positive fan-out ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fanout_all_children_complete_and_pin_accounts():
    d = MockDaemon()
    client = ForkdClient(http_fn=d, account_select=_acct())
    children = [_child(task=f"t{i}", child_id=f"c{i}") for i in range(3)]
    results = await client.fanout(children)
    assert len(results) == 3
    assert all(r.state == ChildState.COMPLETED.value for r in results)
    assert all(r.account_profile == "acct-auto" for r in results)  # auto-selected
    assert all(r.session_id.startswith("sess-") for r in results)


@pytest.mark.asyncio
async def test_explicit_account_is_pinned():
    d = MockDaemon()
    client = ForkdClient(http_fn=d, account_select=_acct())
    [r] = await client.fanout([_child(account="acct-2", child_id="c")])
    assert r.account_profile == "acct-2"


# ── fail-closed: scope intersection (no escalation) ─────────────────────────


@pytest.mark.asyncio
async def test_child_scope_is_intersection_not_escalation():
    d = MockDaemon()
    client = ForkdClient(http_fn=d, account_select=_acct())
    # Parent envelope = union of children tools. This child requests admin, but a
    # separate child bounds the snapshot; the exec payload must exclude admin.
    children = [
        _child(tools=["bash", "admin"], child_id="c1"),   # wants admin
        _child(tools=["bash", "python"], child_id="c2"),
    ]
    # Provide an explicit snapshot whose scope EXCLUDES admin (the warm parent).
    from adk.forkd_client import ForkdParentSnapshot

    snap = ForkdParentSnapshot(snapshot_id="s", allowed_tools=["bash", "python"])
    await client.fanout(children, snapshot=snap)
    # c1 forked+exec'd, but admin was intersected away.
    assert "admin" not in d.exec_payloads["c1"]["allowed_tools"]
    assert d.exec_payloads["c1"]["allowed_tools"] == ["bash"]


@pytest.mark.asyncio
async def test_empty_intersection_denies_child():
    d = MockDaemon()
    client = ForkdClient(http_fn=d, account_select=_acct())
    from adk.forkd_client import ForkdParentSnapshot

    snap = ForkdParentSnapshot(snapshot_id="s", allowed_tools=["bash"])
    [r] = await client.fanout([_child(tools=["admin"], child_id="c")], snapshot=snap)
    assert r.state == ChildState.DENIED.value
    assert "c" not in d.exec_payloads  # never executed


# ── fail-closed: account throttle ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_requested_throttled_account_denied():
    d = MockDaemon()
    client = ForkdClient(http_fn=d, account_select=_acct(throttled=["acct-bad"]))
    [r] = await client.fanout([_child(account="acct-bad", child_id="c")])
    assert r.state == ChildState.DENIED.value
    assert "throttled" in r.error
    assert "c" not in d.exec_payloads


# ── degrade path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_degrade_to_spawn_when_daemon_down():
    d = MockDaemon(healthy=False)
    spawned = []

    async def degrade_spawn(child, account):
        spawned.append((child.child_id, account))
        return ForkResult(child_id=child.child_id, state=ChildState.COMPLETED.value,
                          account_profile=account, result_text="degraded-ok")

    client = ForkdClient(http_fn=d, account_select=_acct(), degrade_spawn=degrade_spawn)
    results = await client.fanout([_child(child_id="c0"), _child(child_id="c1")])
    assert len(results) == 2
    assert all(r.degraded for r in results)
    assert all(r.state == ChildState.COMPLETED.value for r in results)
    assert len(spawned) == 2  # ran via the sequential spawner, not forkd


@pytest.mark.asyncio
async def test_degrade_without_spawner_fails_closed():
    d = MockDaemon(healthy=False)
    client = ForkdClient(http_fn=d, account_select=_acct())  # no degrade_spawn
    [r] = await client.fanout([_child(child_id="c")])
    assert r.state == ChildState.FAILED.value
    assert r.degraded and "no degrade spawner" in r.error


@pytest.mark.asyncio
async def test_daemon_down_no_degrade_returns_failed():
    d = MockDaemon(healthy=False)
    client = ForkdClient(http_fn=d, account_select=_acct(), degrade_on_error=False)
    [r] = await client.fanout([_child(child_id="c")])
    assert r.state == ChildState.FAILED.value
    assert "unavailable" in r.error


# ── partial failure ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_partial_fork_failure_isolated():
    d = MockDaemon(fork_fail_ids=["c1"])
    client = ForkdClient(http_fn=d, account_select=_acct())
    children = [_child(child_id="c0"), _child(child_id="c1"), _child(child_id="c2")]
    results = {r.child_id: r for r in await client.fanout(children)}
    assert results["c0"].state == ChildState.COMPLETED.value
    assert results["c1"].state == ChildState.FAILED.value
    assert results["c2"].state == ChildState.COMPLETED.value


# ── secret-safety + validation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_secret_in_child_definition_refused():
    d = MockDaemon()
    client = ForkdClient(http_fn=d, account_select=_acct())
    bad = _child(task="use sk-ant-abcd1234efgh5678 to auth", child_id="c")
    with pytest.raises(ForkdError, match="secret"):
        await client.fanout([bad])
    assert d.calls == []  # nothing dispatched


@pytest.mark.asyncio
async def test_secret_in_metadata_refused():
    d = MockDaemon()
    client = ForkdClient(http_fn=d, account_select=_acct())
    bad = _child(child_id="c", metadata={"token": "ghp_0123456789abcdefghij"})
    with pytest.raises(ForkdError, match="secret"):
        await client.fanout([bad])


@pytest.mark.asyncio
async def test_no_secret_reaches_the_wire():
    d = MockDaemon()
    client = ForkdClient(http_fn=d, account_select=_acct())
    await client.fanout([_child(task="call get_secret('k'), never hardcode", child_id="c")])
    import json as _json

    wire = _json.dumps(d.calls, default=str)
    for pat in ("sk-ant-", "ghp_", "xoxb-", "aither_sk_live_"):
        assert pat not in wire


@pytest.mark.asyncio
async def test_empty_task_and_tools_rejected():
    client = ForkdClient(http_fn=MockDaemon(), account_select=_acct())
    with pytest.raises(ForkdError):
        await client.fanout([_child(task="   ", child_id="c")])
    with pytest.raises(ForkdError):
        await client.fanout([_child(tools=[], child_id="c")])


@pytest.mark.asyncio
async def test_empty_children_returns_empty():
    client = ForkdClient(http_fn=MockDaemon(), account_select=_acct())
    assert await client.fanout([]) == []


# ── degrade path is NOT a hole in the fork-path guarantees ──────────────────


@pytest.mark.asyncio
async def test_degrade_intersects_tools_no_escalation():
    """Forkd down: a degraded child still can't exceed the parent envelope."""
    from adk.forkd_client import ForkdParentSnapshot

    received = {}

    async def spawn(child, account):
        received[child.child_id] = list(child.allowed_tools)
        return ForkResult(child_id=child.child_id, state=ChildState.COMPLETED.value,
                          account_profile=account)

    client = ForkdClient(http_fn=MockDaemon(healthy=False), account_select=_acct(),
                         degrade_spawn=spawn)
    snap = ForkdParentSnapshot(snapshot_id="s", allowed_tools=["bash", "python"])
    await client.fanout([_child(tools=["bash", "admin"], child_id="c")], snapshot=snap)
    assert received["c"] == ["bash"]  # admin intersected away even in degrade


@pytest.mark.asyncio
async def test_degrade_empty_intersection_denied():
    from adk.forkd_client import ForkdParentSnapshot

    async def spawn(child, account):
        return ForkResult(child_id=child.child_id, state=ChildState.COMPLETED.value)

    client = ForkdClient(http_fn=MockDaemon(healthy=False), account_select=_acct(),
                         degrade_spawn=spawn)
    snap = ForkdParentSnapshot(snapshot_id="s", allowed_tools=["bash"])
    [r] = await client.fanout([_child(tools=["admin"], child_id="c")], snapshot=snap)
    assert r.state == ChildState.DENIED.value


@pytest.mark.asyncio
async def test_snapshot_create_failure_degrades_not_raises():
    class D2(MockDaemon):
        async def __call__(self, method, url, body):
            if url.split("8760", 1)[-1] == "/snapshots":
                return 500, {"error": "boom"}
            return await super().__call__(method, url, body)

    spawned = []

    async def spawn(child, account):
        spawned.append(child.child_id)
        return ForkResult(child_id=child.child_id, state=ChildState.COMPLETED.value)

    client = ForkdClient(http_fn=D2(), account_select=_acct(), degrade_spawn=spawn)
    results = await client.fanout([_child(child_id="c")])  # auto-snapshot fails
    assert len(results) == 1 and results[0].degraded
    assert spawned == ["c"]


@pytest.mark.asyncio
async def test_injected_http_non_forkderror_does_not_escape():
    async def bad_http(method, url, body):
        if url.endswith("/health"):
            return 200, {}
        if url.endswith("/snapshots"):
            return 201, {"snapshot_id": "s"}
        raise ValueError("boom")  # a non-ForkdError from the injected transport

    client = ForkdClient(http_fn=bad_http, account_select=_acct())
    [r] = await client.fanout([_child(child_id="c")])
    assert r.state == ChildState.FAILED.value  # normalized, never escapes
