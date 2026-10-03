"""adk.cells.reconcile: start-before-stop, held stops on failure, digest-only images."""

from __future__ import annotations

import json

import pytest

from adk.cells import diff
from adk.cells.reconcile import PodmanRuntime, ReconcileError, apply, observe

DIGEST = "ghcr.io/example/memory@sha256:" + "a" * 64


class FakeRuntime:
    def __init__(self, running=(), fail_start=()):
        self.cells = set(running)
        self.fail_start = set(fail_start)
        self.log: list[str] = []

    def start(self, cell):
        self.log.append(f"start {cell}")
        if cell in self.fail_start:
            raise RuntimeError("image pull failed")
        self.cells.add(cell)

    def stop(self, cell):
        self.log.append(f"stop {cell}")
        self.cells.discard(cell)

    def running(self):
        return sorted(self.cells)


def test_move_runs_start_then_stop_and_converges():
    nodes = {"hz1": FakeRuntime(running={"memory"}), "dgx": FakeRuntime()}
    actions = diff(observe(nodes), {"memory": ["dgx"]})
    report = apply(actions, nodes)
    assert report.ok
    assert [a["action"] for a in report.done] == ["start", "stop"]
    assert observe(nodes) == {"memory": ["dgx"]}
    assert diff(observe(nodes), {"memory": ["dgx"]}) == []


def test_failed_start_holds_that_cells_stops_only():
    nodes = {
        "hz1": FakeRuntime(running={"memory", "relay"}),
        "dgx": FakeRuntime(fail_start={"memory"}),
    }
    report = apply(diff(observe(nodes), {"memory": ["dgx"], "relay": ["dgx"]}), nodes)
    assert not report.ok
    assert [f["cell"] for f in report.failed] == ["memory"]
    assert [h["cell"] for h in report.held] == ["memory"]
    assert "memory" in nodes["hz1"].cells  # old replica still serving
    assert "relay" not in nodes["hz1"].cells and "relay" in nodes["dgx"].cells


def test_unknown_node_refused_before_any_change():
    nodes = {"hz1": FakeRuntime(running={"memory"})}
    with pytest.raises(ReconcileError, match="ghost"):
        apply([{"action": "start", "cell": "memory", "node": "ghost"},
               {"action": "stop", "cell": "memory", "node": "hz1"}], nodes)
    assert nodes["hz1"].log == []


def test_podman_runtime_requires_digests_and_builds_commands():
    with pytest.raises(ReconcileError, match="digest"):
        PodmanRuntime({"memory": "ghcr.io/example/memory:latest"}, run=lambda c: "")

    calls: list[list[str]] = []

    def run(cmd):
        calls.append(cmd)
        if cmd[1] == "ps":
            return json.dumps([{"Labels": {"aither.cell": "memory"}}, {"Labels": {}}])
        return ""

    rt = PodmanRuntime({"memory": DIGEST}, run=run, network="cells",
                       args={"memory": ["serve", "--port", "8443"]})
    rt.start("memory")
    assert calls[0][-4:] == [DIGEST, "serve", "--port", "8443"]
    assert "aither.cell=memory" in calls[0]
    assert calls[0][calls[0].index("--network") + 1] == "cells"
    rt.stop("memory")
    assert calls[1][:3] == ["podman", "rm", "-f"]
    assert rt.running() == ["memory"]
    with pytest.raises(ReconcileError, match="no image"):
        rt.start("relay")
