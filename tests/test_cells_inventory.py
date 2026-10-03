"""adk.cells.inventory: a node measures itself, and the planner places on that report."""

from __future__ import annotations

import pytest
import yaml

from adk.cells import load, plan
from adk.cells.__main__ import main as cells_main
from adk.cells.inventory import ProbeError, gpus_vram_gb, local_node, node_doc


def smi(output: str):
    calls = []

    def run(cmd: list[str]) -> str:
        calls.append(cmd)
        return output

    return run, calls


def present(_name: str) -> str:
    return "/usr/bin/nvidia-smi"


def test_gpus_parsed_per_card_in_gb():
    run, calls = smi("32607\n122880\n")
    assert gpus_vram_gb(run, present) == [31.8, 120.0]
    assert calls[0][0] == "nvidia-smi"


def test_no_nvidia_smi_means_no_gpu():
    run, calls = smi("unused")
    assert gpus_vram_gb(run, lambda _n: None) == []
    assert calls == []


def test_broken_driver_is_an_error_not_a_gpu_less_node():
    def failing(cmd: list[str]) -> str:
        raise ProbeError("nvidia-smi exited 9: NVML driver/library mismatch")

    with pytest.raises(ProbeError):
        gpus_vram_gb(failing, present)
    with pytest.raises(ProbeError, match="unparseable"):
        gpus_vram_gb(smi("[N/A]\n")[0], present)


def test_local_node_measures_and_operator_labels_win():
    run, _ = smi("24576\n")
    node = local_node("box-1", {"zone": "fsn", "os": "custom"}, {"owner-gaming"},
                      run=run, which=present)
    assert node.name == "box-1" and node.gpus == [24.0]
    assert node.cpu >= 1 and node.mem_gb > 0
    assert node.labels["zone"] == "fsn" and node.labels["os"] == "custom"
    assert "arch" in node.labels and node.tags == {"owner-gaming"}


def test_report_round_trips_into_the_planner():
    run, _ = smi("32607\n")
    gamer = local_node("desk", {"zone": "home"}, {"owner-gaming"}, run=run, which=present)
    idle = local_node("dgx", {"zone": "home"}, run=smi("122880\n")[0], which=present)
    nodes = {"nodes": {**node_doc(gamer)["nodes"], **node_doc(idle)["nodes"]}}
    estate = load({"cells": {
        "inference": {"per_gpu": True, "vram": "24G", "yield_to": ["owner-gaming"]},
        "relay": {"replicas": 2},
    }}, yaml.safe_load(yaml.safe_dump(nodes)))
    placed = plan(estate)
    assert placed["inference"] == ["dgx"]
    assert sorted(placed["relay"]) == ["desk", "dgx"]


def test_cli_inventory_and_bad_label(capsys):
    assert cells_main(["inventory", "--name", "probe-host", "--label", "zone=home"]) == 0
    doc = yaml.safe_load(capsys.readouterr().out)
    entry = doc["nodes"]["probe-host"]
    assert entry["labels"]["zone"] == "home" and float(entry["mem"].rstrip("G")) > 0
    assert cells_main(["inventory", "--label", "novalue"]) == 2
