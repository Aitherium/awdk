"""Offline tests for the M5 forkd Tier-2 fan-out adapter.

The forkd REST daemon is injected as a mock ``http_fn`` (no KVM/Linux/httpx
needed), and account selection / degrade spawning are injected too — so the
fan-out logic, fail-closed scope ceiling, account pinning, the degrade path and
secret-safety are all exercised deterministically offline.

The mock speaks the daemon's REAL router (``crates/forkd-controller/src/http.rs``,
cross-read against the published ``forkd`` 0.5.3 SDK). ``test_only_published_routes``
freezes that: a path the daemon does not serve can never pass this suite again,
which is how the previous fiction (``/fork``, ``/exec/{id}``, ``/children/{id}``,
port 8760) survived so long.

Live e2e (real forkd on a Linux/KVM mesh node) is owner-gated.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from adk.forkd_client import (
    ChildSpec,
    ChildState,
    ForkdClient,
    ForkdError,
    ForkdParentSnapshot,
    ForkResult,
)

TAG = "warm"

#: Every route this client is allowed to touch, from the daemon's router.
ALLOWED_ROUTES = {
    ("GET", "/healthz"),
    ("GET", "/v1/snapshots"),
    ("POST", "/v1/sandboxes"),
    ("POST", "/v1/sandboxes/{id}/exec"),
    ("DELETE", "/v1/sandboxes/{id}"),
}


class MockDaemon:
    """Injectable forkd REST daemon: records calls, returns canned responses."""

    def __init__(self, *, healthy=True, fail_spawn_nth=(), fail_list=False,
                 snapshots=None):
        self.healthy = healthy
        self.fail_spawn_nth = set(fail_spawn_nth)   # 0-based spawn-call indexes
        self.fail_list = fail_list
        self.snapshots = snapshots if snapshots is not None else [
            {"tag": TAG, "status": "ready", "bootable": True, "dir": "/warm"}]
        self.calls = []
        self.execs = {}   # sandbox id -> exec body
        self.killed = []
        self.spawn_bodies = []
        self._spawns = 0

    async def __call__(self, method, url, body):
        path = urlsplit(url).path
        self.calls.append((method, path, body))
        if path == "/healthz":
            return (200 if self.healthy else 503), {"status": "ok" if self.healthy else "down"}
        if path == "/v1/snapshots" and method == "GET":
            if self.fail_list:
                return 500, {"error": "list failed"}
            return 200, list(self.snapshots)
        if path == "/v1/sandboxes" and method == "POST":
            self.spawn_bodies.append(body)
            nth = self._spawns
            self._spawns += 1
            if nth in self.fail_spawn_nth:
                return 500, {"error": "spawn failed"}
            n = int(body.get("n") or 1)
            return 201, [{"id": f"sbx-{nth}-{i}", "snapshot_tag": body.get("snapshot_tag")}
                         for i in range(n)]
        if path.startswith("/v1/sandboxes/") and path.endswith("/exec"):
            sid = path.split("/")[3]
            self.execs[sid] = body
            if "boom" in " ".join(body.get("args") or []):
                return 200, {"exit_code": 3, "stdout": "", "stderr": "boom"}
            return 200, {"exit_code": 0, "stdout": f"done:{sid}", "stderr": ""}
        if path.startswith("/v1/sandboxes/") and method == "DELETE":
            self.killed.append(path.rsplit("/", 1)[-1])
            return 200, {}
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


def _client(d, **kw):
    kw.setdefault("snapshot_tag", TAG)
    return ForkdClient(http_fn=d, account_select=_acct(), **kw)


# ── the contract itself ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_published_routes():
    """Freeze the daemon's router: any invented path fails here, not in production."""
    d = MockDaemon()
    await _client(d).fanout([_child(child_id="c"), _child(child_id="c2", task="boom")])
    used = set()
    for method, path, _body in d.calls:
        parts = path.split("/")
        if len(parts) >= 4 and parts[1:3] == ["v1", "sandboxes"] and parts[3]:
            parts[3] = "{id}"
            path = "/".join(parts)
        used.add((method, path))
    assert used <= ALLOWED_ROUTES, f"routes outside the daemon's router: {used - ALLOWED_ROUTES}"
    assert ("POST", "/v1/snapshots") not in used  # no REST snapshot-create is called


def test_defaults_match_the_daemon(monkeypatch):
    monkeypatch.delenv("FORKD_URL", raising=False)
    monkeypatch.delenv("FORKD_TOKEN", raising=False)
    monkeypatch.setenv("FORKD_TOKEN", "tok-123")
    client = ForkdClient(http_fn=MockDaemon())
    assert client.base_url == "http://127.0.0.1:8889"
    assert client.token == "tok-123"


# ── positive fan-out ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fanout_all_children_complete_and_pin_accounts():
    d = MockDaemon()
    children = [_child(task=f"t{i}", child_id=f"c{i}") for i in range(3)]
    results = await _client(d).fanout(children)
    assert len(results) == 3
    assert all(r.state == ChildState.COMPLETED.value for r in results)
    assert all(r.account_profile == "acct-auto" for r in results)  # auto-selected
    assert all(r.session_id.startswith("sbx-") for r in results)
    assert all(r.result_text.startswith("done:sbx-") for r in results)
    # Every forked VM is reclaimed, and every spawn forked from the warm TAG.
    assert len(d.killed) == 3
    assert [b["snapshot_tag"] for b in d.spawn_bodies] == [TAG] * 3
    assert all(b["per_child_netns"] is True for b in d.spawn_bodies)


@pytest.mark.asyncio
async def test_explicit_account_is_pinned():
    d = MockDaemon()
    [r] = await _client(d).fanout([_child(account="acct-2", child_id="c")])
    assert r.account_profile == "acct-2"
    # The account is OUR policy layer — the daemon body carries no such field.
    assert "account_profile" not in d.spawn_bodies[0]


@pytest.mark.asyncio
async def test_explicit_snapshot_skips_resolution():
    d = MockDaemon()
    snap = ForkdParentSnapshot(snapshot_id="already-warm", allowed_tools=["bash", "python"])
    await _client(d).fanout([_child(child_id="c")], snapshot=snap)
    assert ("GET", "/v1/snapshots", None) not in d.calls
    assert d.spawn_bodies[0]["snapshot_tag"] == "already-warm"


# ── fail-closed: scope ceiling (no escalation) ──────────────────────────────


@pytest.mark.asyncio
async def test_child_asking_outside_parent_scope_is_denied_not_narrowed():
    """The exec surface takes argv, so a wider request cannot be narrowed — deny it."""
    d = MockDaemon()
    snap = ForkdParentSnapshot(snapshot_id="s", allowed_tools=["bash", "python"])
    [r] = await _client(d).fanout([_child(tools=["bash", "admin"], child_id="c")],
                                 snapshot=snap)
    assert r.state == ChildState.DENIED.value
    assert "admin" in r.error
    assert d.execs == {} and d.spawn_bodies == []  # never forked


@pytest.mark.asyncio
async def test_empty_intersection_denies_child():
    d = MockDaemon()
    snap = ForkdParentSnapshot(snapshot_id="s", allowed_tools=["bash"])
    [r] = await _client(d).fanout([_child(tools=["admin"], child_id="c")], snapshot=snap)
    assert r.state == ChildState.DENIED.value
    assert d.execs == {}  # never executed


@pytest.mark.asyncio
async def test_resolved_snapshot_uses_the_child_union_as_ceiling():
    """Auto-resolved parent scope = union of children, so nothing is denied there."""
    d = MockDaemon()
    results = await _client(d).fanout([_child(tools=["bash"], child_id="c1"),
                                       _child(tools=["bash", "python"], child_id="c2")])
    assert all(r.state == ChildState.COMPLETED.value for r in results)


# ── fail-closed: account throttle ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_requested_throttled_account_denied():
    d = MockDaemon()
    client = ForkdClient(http_fn=d, account_select=_acct(throttled=["acct-bad"]),
                         snapshot_tag=TAG)
    [r] = await client.fanout([_child(account="acct-bad", child_id="c")])
    assert r.state == ChildState.DENIED.value
    assert "throttled" in r.error
    assert d.execs == {}


# ── degrade path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_degrade_to_spawn_when_daemon_down():
    d = MockDaemon(healthy=False)
    spawned = []

    async def degrade_spawn(child, account):
        spawned.append((child.child_id, account))
        return ForkResult(child_id=child.child_id, state=ChildState.COMPLETED.value,
                          account_profile=account, result_text="degraded-ok")

    client = ForkdClient(http_fn=d, account_select=_acct(), degrade_spawn=degrade_spawn,
                         snapshot_tag=TAG)
    results = await client.fanout([_child(child_id="c0"), _child(child_id="c1")])
    assert len(results) == 2
    assert all(r.degraded for r in results)
    assert all(r.state == ChildState.COMPLETED.value for r in results)
    assert len(spawned) == 2  # ran via the sequential spawner, not forkd


@pytest.mark.asyncio
async def test_degrade_without_spawner_fails_closed():
    d = MockDaemon(healthy=False)
    [r] = await _client(d).fanout([_child(child_id="c")])
    assert r.state == ChildState.FAILED.value
    assert r.degraded and "no degrade spawner" in r.error


@pytest.mark.asyncio
async def test_daemon_down_no_degrade_returns_failed():
    client = _client(MockDaemon(healthy=False), degrade_on_error=False)
    [r] = await client.fanout([_child(child_id="c")])
    assert r.state == ChildState.FAILED.value
    assert "unavailable" in r.error


@pytest.mark.asyncio
async def test_no_warm_tag_degrades_without_a_fork_attempt(caplog):
    d = MockDaemon()
    client = ForkdClient(http_fn=d, account_select=_acct(), snapshot_tag="")
    with caplog.at_level("INFO", logger="forkd_client"):
        [r] = await client.fanout([_child(child_id="c")])
    assert r.degraded and "snapshot tag" in caplog.text  # the reason is logged
    assert [c for c in d.calls if c[0] == "POST"] == []


@pytest.mark.asyncio
async def test_unknown_snapshot_tag_degrades():
    d = MockDaemon()
    client = ForkdClient(http_fn=d, account_select=_acct(), snapshot_tag="missing")

    async def spawn(child, account):
        return ForkResult(child_id=child.child_id, state=ChildState.COMPLETED.value,
                          degraded=True)

    client._degrade_spawn = spawn
    [r] = await client.fanout([_child(child_id="c")])
    assert r.degraded and r.state == ChildState.COMPLETED.value


@pytest.mark.asyncio
async def test_snapshot_list_failure_degrades_not_raises():
    d = MockDaemon(fail_list=True)
    spawned = []

    async def spawn(child, account):
        spawned.append(child.child_id)
        return ForkResult(child_id=child.child_id, state=ChildState.COMPLETED.value)

    client = ForkdClient(http_fn=d, account_select=_acct(), degrade_spawn=spawn,
                         snapshot_tag=TAG)
    results = await client.fanout([_child(child_id="c")])
    assert len(results) == 1 and results[0].degraded
    assert spawned == ["c"]


# ── partial failure + reclaim ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_partial_spawn_failure_isolated_and_no_reclaim():
    d = MockDaemon(fail_spawn_nth=[1])
    results = {r.child_id: r for r in await _client(d).fanout(
        [_child(child_id="c0"), _child(child_id="c1"), _child(child_id="c2")])}
    assert results["c0"].state == ChildState.COMPLETED.value
    assert results["c1"].state == ChildState.FAILED.value
    assert results["c2"].state == ChildState.COMPLETED.value
    assert len(d.killed) == 2  # a sandbox that never spawned is not reclaimed


@pytest.mark.asyncio
async def test_nonzero_exit_is_a_failure_with_stderr():
    d = MockDaemon()
    [r] = await _client(d).fanout([_child(task="boom now", child_id="c")])
    assert r.state == ChildState.FAILED.value
    assert r.exit_code == 3 and "boom" in r.error
    assert d.killed  # failed children are still reclaimed


@pytest.mark.asyncio
async def test_exec_argv_carries_the_task():
    d = MockDaemon()
    await _client(d).fanout([_child(task="echo hi", child_id="c")])
    body = next(iter(d.execs.values()))
    assert body["args"] == ["sh", "-lc", "echo hi"]
    assert body["timeout_secs"] == 300


# ── secret-safety + validation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_secret_in_child_definition_refused():
    d = MockDaemon()
    bad = _child(task="use sk-ant-abcd1234efgh5678 to auth", child_id="c")
    with pytest.raises(ForkdError, match="secret"):
        await _client(d).fanout([bad])
    assert d.calls == []  # nothing dispatched


@pytest.mark.asyncio
async def test_secret_in_metadata_refused():
    d = MockDaemon()
    bad = _child(child_id="c", metadata={"token": "ghp_0123456789abcdefghij"})
    with pytest.raises(ForkdError, match="secret"):
        await _client(d).fanout([bad])


@pytest.mark.asyncio
async def test_no_secret_reaches_the_wire():
    d = MockDaemon()
    await _client(d).fanout([_child(task="call get_secret('k'), never hardcode",
                                    child_id="c")])
    import json as _json

    wire = _json.dumps(d.calls, default=str)
    for pat in ("sk-ant-", "ghp_", "xoxb-", "aither_sk_live_"):
        assert pat not in wire


@pytest.mark.asyncio
async def test_empty_task_and_tools_rejected():
    client = _client(MockDaemon())
    with pytest.raises(ForkdError):
        await client.fanout([_child(task="   ", child_id="c")])
    with pytest.raises(ForkdError):
        await client.fanout([_child(tools=[], child_id="c")])


@pytest.mark.asyncio
async def test_empty_children_returns_empty():
    assert await _client(MockDaemon()).fanout([]) == []


# ── degrade path is NOT a hole in the fork-path guarantees ──────────────────


@pytest.mark.asyncio
async def test_degrade_intersects_tools_no_escalation():
    """Forkd down: a degraded child still can't exceed the parent envelope."""
    received = {}

    async def spawn(child, account):
        received[child.child_id] = list(child.allowed_tools)
        return ForkResult(child_id=child.child_id, state=ChildState.COMPLETED.value,
                          account_profile=account)

    client = _client(MockDaemon(healthy=False), degrade_spawn=spawn)
    snap = ForkdParentSnapshot(snapshot_id="s", allowed_tools=["bash", "python"])
    await client.fanout([_child(tools=["bash", "admin"], child_id="c")], snapshot=snap)
    assert received["c"] == ["bash"]  # admin intersected away even in degrade


@pytest.mark.asyncio
async def test_degrade_empty_intersection_denied():
    async def spawn(child, account):
        return ForkResult(child_id=child.child_id, state=ChildState.COMPLETED.value)

    client = _client(MockDaemon(healthy=False), degrade_spawn=spawn)
    snap = ForkdParentSnapshot(snapshot_id="s", allowed_tools=["bash"])
    [r] = await client.fanout([_child(tools=["admin"], child_id="c")], snapshot=snap)
    assert r.state == ChildState.DENIED.value


@pytest.mark.asyncio
async def test_injected_http_non_forkderror_does_not_escape():
    async def bad_http(method, url, body):
        if url.endswith("/healthz"):
            return 200, {}
        if url.endswith("/v1/snapshots"):
            return 200, [{"tag": TAG, "bootable": True}]
        raise ValueError("boom")  # a non-ForkdError from the injected transport

    client = ForkdClient(http_fn=bad_http, account_select=_acct(), snapshot_tag=TAG)
    [r] = await client.fanout([_child(child_id="c")])
    assert r.state == ChildState.FAILED.value  # normalized, never escapes
