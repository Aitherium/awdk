"""The control loop: unit ticks on fake nodes, then two REAL TLS nodes reconciled by the
controller through their operator-only runtime surface."""

from __future__ import annotations

import threading
import time

import pytest
import yaml

from adk.cells import Cells
from adk.cells.control import run, tick
from adk.cells.node import build_node_app, load_callers
from adk.cells.remote import NodeError, RemoteNode
from tests.test_cells_node import (
    DANA_TOKEN,
    OPERATOR_TOKEN,
    free_port,
    self_signed,
    write_tokens,
)

pytest.importorskip("uvicorn")

ESTATE = {"cells": {"relay": {"replicas": 2}, "memory": {"replicas": 1, "mem": "8G"}}}


class FakeNode:
    def __init__(self, name, mem="16G", running=(), reachable=True, fail_start=()):
        self.name, self.mem = name, mem
        self.cells = set(running)
        self.reachable = reachable
        self.fail_start = set(fail_start)
        self.stops: list[str] = []

    def inventory(self):
        if not self.reachable:
            raise NodeError(self.name, "inventory", None, "connection refused")
        return {"cpu": 4, "mem": self.mem}

    def running(self):
        return sorted(self.cells)

    def start(self, cell):
        if cell in self.fail_start:
            raise RuntimeError("pull failed")
        self.cells.add(cell)

    def stop(self, cell):
        self.stops.append(cell)
        self.cells.discard(cell)


def test_tick_places_applies_and_then_converges():
    a, b = FakeNode("a"), FakeNode("b")
    first = tick(ESTATE, [a, b])
    assert first.report.ok and len(first.actions) == 3
    assert sorted(a.cells | b.cells) == ["memory", "relay"]
    assert "relay" in a.cells and "relay" in b.cells
    second = tick(ESTATE, [a, b])
    assert second.converged and second.actions == []


def test_unreachable_node_is_excluded_and_never_told_to_stop():
    a = FakeNode("a", running={"relay", "memory"})
    gone = FakeNode("gone", running={"relay"}, reachable=False)
    b = FakeNode("b")
    result = tick(ESTATE, [a, gone, b])
    assert "gone" in result.unreachable and not result.converged
    assert gone.stops == [] and gone.cells == {"relay"}
    assert all(act["node"] != "gone" for act in result.actions)


def test_unplaceable_estate_changes_nothing():
    a = FakeNode("a", mem="4G", running={"relay"})
    result = tick({"cells": {"memory": {"mem": "64G"}}}, [a])
    assert result.problems and result.actions == [] and result.report is None
    assert a.cells == {"relay"}


def test_dry_run_plans_without_applying():
    a = FakeNode("a")
    result = tick(ESTATE, [a, FakeNode("b")], dry_run=True)
    assert result.actions and result.report is None and a.cells == set()


def test_run_rereads_the_estate_each_tick():
    a = FakeNode("a")
    estates = iter([{"cells": {"relay": {}}}, {"cells": {}}])
    seen = []
    run(lambda: next(estates), [a], interval=0, on_tick=seen.append, ticks=2,
        sleep=lambda _s: None)
    assert [r.placement for r in seen] == [{"relay": ["a"]}, {}]
    assert a.cells == set() and a.stops == ["relay"]


# --- two real TLS nodes ----------------------------------------------------------------


class MemRuntime:
    def __init__(self, stop_delay: float = 0.0):
        self.cells: set[str] = set()
        self.lock = threading.Lock()
        self.stop_delay = stop_delay

    def start(self, cell):
        with self.lock:
            self.cells.add(cell)

    def stop(self, cell):
        time.sleep(self.stop_delay)
        with self.lock:
            self.cells.discard(cell)

    def running(self):
        with self.lock:
            return sorted(self.cells)


@pytest.fixture
def two_nodes(tmp_path):
    import uvicorn

    cert, key = self_signed(tmp_path)
    authenticate = load_callers(write_tokens(tmp_path / "tokens.yaml"))
    servers, nodes = [], []
    for name in ("n1", "n2"):
        runtime = MemRuntime(stop_delay=1.5)
        app = build_node_app(Cells(), authenticate, node_name=name, runtime=runtime)
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, ssl_certfile=cert, ssl_keyfile=key,
            log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        servers.append(server)
        nodes.append((name, f"https://127.0.0.1:{port}", runtime))
    deadline = time.monotonic() + 15
    while not all(s.started for s in servers):
        if time.monotonic() > deadline:
            raise RuntimeError("nodes did not start")
        time.sleep(0.05)
    yield nodes, cert
    for s in servers:
        s.should_exit = True


def test_controller_reconciles_two_real_tls_nodes(two_nodes):
    nodes, ca = two_nodes
    remotes = [RemoteNode(n, url, OPERATOR_TOKEN, ca=ca) for n, url, _ in nodes]
    estate = {"cells": {"relay": {"replicas": 2}, "memory": {}}}
    first = tick(estate, remotes)
    assert first.report.ok, first.as_dict()
    runtimes = {n: rt for n, _, rt in nodes}
    assert runtimes["n1"].cells | runtimes["n2"].cells == {"relay", "memory"}
    assert "relay" in runtimes["n1"].cells and "relay" in runtimes["n2"].cells
    assert tick(estate, remotes).converged

    shrink = tick({"cells": {"relay": {"replicas": 1}}}, remotes)
    assert shrink.report.ok
    assert sorted(runtimes["n1"].cells | runtimes["n2"].cells) == ["relay"]
    assert len([n for n in runtimes if "relay" in runtimes[n].cells]) == 1


def test_runtime_surface_is_operator_only(two_nodes):
    nodes, ca = two_nodes
    name, url, runtime = nodes[0]
    member = RemoteNode(name, url, DANA_TOKEN, ca=ca)
    with pytest.raises(NodeError) as err:
        member.start("relay")
    assert err.value.status == 403 and runtime.cells == set()
    with pytest.raises(NodeError) as err:
        member.inventory()
    assert err.value.status == 403


def test_cli_control_dry_run_and_missing_token(two_nodes, tmp_path, monkeypatch, capsys):
    from adk.cells.__main__ import main

    nodes, ca = two_nodes
    estate = tmp_path / "estate.yaml"
    estate.write_text(yaml.safe_dump({"cells": {"relay": {}}}), encoding="utf-8")
    argv = ["control", str(estate), "--ca", ca,
            *[a for n, url, _ in nodes for a in ("--node", f"{n}={url}")]]
    monkeypatch.delenv("AITHER_CELLS_OPERATOR_TOKEN", raising=False)
    assert main(argv) == 2
    monkeypatch.setenv("AITHER_CELLS_OPERATOR_TOKEN", OPERATOR_TOKEN)
    capsys.readouterr()
    assert main([*argv, "--dry-run"]) == 0
    out = yaml.safe_load(capsys.readouterr().out)
    assert out["dry_run"] and len(out["actions"]) == 1
    assert all(not rt.cells for _, _, rt in nodes)
    assert main(argv) == 0
    assert sum("relay" in rt.cells for _, _, rt in nodes) == 1


def test_slow_stop_is_not_reported_as_a_failure(two_nodes):
    """A real podman stop sits out its grace period (measured live: past 30 s through
    WSL). Reads keep a short timeout; actions must outlast the runtime."""
    nodes, ca = two_nodes
    name, url, runtime = nodes[0]
    runtime.start("relay")
    quick_reads = RemoteNode(name, url, OPERATOR_TOKEN, ca=ca, timeout=0.5)
    quick_reads.stop("relay")  # takes 1.5 s, longer than the read timeout
    assert runtime.cells == set()
    impatient = RemoteNode(name, url, OPERATOR_TOKEN, ca=ca, timeout=0.5, action_timeout=0.5)
    runtime.start("relay")
    with pytest.raises(NodeError):
        impatient.stop("relay")
