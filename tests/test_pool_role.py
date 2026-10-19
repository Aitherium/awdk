"""adk.pool_role: the probe-free planner behind `adk grid join`."""

from __future__ import annotations

import argparse

import pytest

from adk import pool_role
from adk.pool_role import POOL_SPECS, GpuCaps, NodeCaps, plan_roles

SPEC = POOL_SPECS["deepseek-v4.1-flash-1017"]


def _host(rtt=3.0, free_vram=30.0, avail=56.0):
    return NodeCaps("linux", 64.0, avail, 16, 32, 1,
                    [GpuCaps(0, "RTX 5090", free_vram, 32.6)], rtt_ms=rtt)


def test_self_test_passes():
    assert pool_role.self_test() == 0


def test_self_test_fails_when_bind_gate_is_broken(monkeypatch):
    # The self-test has to catch a planner that hands rpc roles to public binds.
    monkeypatch.setattr(pool_role, "validate_rpc_bind", lambda _h: True)
    assert pool_role.self_test() == 1


def test_self_test_fails_when_latency_gate_is_loosened(monkeypatch):
    # plan_roles binds its RTT default at definition time, so wrap it.
    orig = pool_role.plan_roles

    def loose(*a, **kw):
        kw.setdefault("max_split_rtt_ms", 1000.0)
        return orig(*a, **kw)

    monkeypatch.setattr(pool_role, "plan_roles", loose)
    assert pool_role.self_test() == 1


def test_self_test_fails_when_unified_gpu_is_dropped(monkeypatch):
    # The DGX bug: "[N/A]" memory made the GB10 vanish and the node plan as CPU-only.
    orig = pool_role.parse_nvidia_smi
    monkeypatch.setattr(pool_role, "parse_nvidia_smi",
                        lambda text: [g for g in orig(text) if not g.unified])
    assert pool_role.self_test() == 1


def test_unified_gpu_does_not_double_count_ram():
    gpus = pool_role.parse_nvidia_smi("0, NVIDIA GB10, [N/A], [N/A]")
    spark = NodeCaps("linux", 121.7, 112.4, 20, 20, 1, gpus, rtt_ms=0.5)
    p = plan_roles(spark, SPEC, "100.64.0.38")
    assert p.roles == ["rpc-vram"]
    assert p.backends[0].command[5:7] == ["-d", "CUDA0"]
    assert p.backends[0].budget_gib <= spark.ram_available_gib
    # Asking for the CPU tier explicitly still works (an operator choice).
    assert plan_roles(spark, SPEC, "100.64.0.38", want="rpc-cpu").roles == ["rpc-cpu"]


def test_blocks_never_exceed_the_model():
    # Measured 2026-10-19: GB10 with 112 GiB free planned 50 blocks of a 43-block model
    # (the 0731 spec, whose small blocks make the cap bind on a GB10).
    small = POOL_SPECS["deepseek-v4-flash"]
    gpus = pool_role.parse_nvidia_smi("0, NVIDIA GB10, [N/A], [N/A]")
    spark = NodeCaps("linux", 121.0, 112.0, 20, 20, 1, gpus, rtt_ms=0.1)
    p = plan_roles(spark, small, "100.64.0.38")
    assert p.backends[0].blocks == small.total_blocks
    assert any("model has" in r for r in p.reasons)
    big = NodeCaps("linux", 1024.0, 1000.0, 64, 128, 1, [], rtt_ms=1.0)
    assert plan_roles(big, SPEC, "10.0.0.5").backends[0].blocks == SPEC.total_blocks


def test_unified_gpu_short_reason_names_ram_not_vram():
    gpus = pool_role.parse_nvidia_smi("0, NVIDIA GB10, [N/A], [N/A]")
    starved = NodeCaps("linux", 121.0, 10.0, 20, 20, 1, gpus, rtt_ms=0.1)
    reasons = plan_roles(starved, SPEC, "100.64.0.38").reasons
    assert any("usable RAM (unified)" in r for r in reasons)


def test_discrete_gpu_parse_unchanged():
    g = pool_role.parse_nvidia_smi("0, NVIDIA GeForce RTX 5090, 30720, 32607")
    assert len(g) == 1 and not g[0].unified and abs(g[0].free_gib - 30.0) < 0.01


def test_split_host_gets_vram_and_cpu_backends():
    p = plan_roles(_host(), SPEC, "192.168.1.50")
    assert p.admission == "SPLIT_ELIGIBLE"
    assert p.roles == ["rpc-vram", "rpc-cpu"]
    assert p.backends[0].command[:5] == ["rpc-server", "-H", "192.168.1.50", "-p", "50054"]
    assert p.backends[1].command[-1] == "-c"


@pytest.mark.parametrize("rtt", [10.5, None])
def test_replica_only_never_holds_layers(rtt):
    p = plan_roles(_host(rtt=rtt), SPEC, "100.64.0.7")
    assert p.admission == "REPLICA_ONLY"
    assert p.roles == ["kvholder"]


def test_public_bind_refused():
    p = plan_roles(_host(), SPEC, "8.8.8.8")
    assert p.admission == "REFUSED"
    assert not [r for r in p.roles if r.startswith("rpc")]


def test_unknown_model_cannot_judge():
    args = argparse.Namespace(model="nope", apply=False, coordinator="", bind="",
                              rpc_server="rpc-server", role="auto", ram_cap_gib=None, json=True)
    assert pool_role.run(args, probe=lambda _c: _host()) == 2


class _Admitted:
    admitted = True
    exit_code = 0

    def message(self):
        return "admission   ADMITTED (test)"

    def to_dict(self):
        return {"verdict": "ADMITTED"}


def _admit(_args, _model):
    return _Admitted()


def test_unmeasured_ram_cannot_judge():
    args = argparse.Namespace(model="v41", apply=False, coordinator="", bind="10.0.0.2",
                              rpc_server="rpc-server", role="auto", ram_cap_gib=None, json=True)
    dead = NodeCaps("linux", 0.0, 0.0, 1, 1)
    assert pool_role.run(args, probe=lambda _c: dead, admit=_admit) == 2


def test_apply_is_refused_until_registration_exists():
    args = argparse.Namespace(model="v41", apply=True, coordinator="", bind="10.0.0.2",
                              rpc_server="rpc-server", role="auto", ram_cap_gib=None, json=True)
    assert pool_role.run(args, probe=lambda _c: _host(), admit=_admit) == 2


def test_grid_join_is_registered_on_the_cli():
    from adk.cli import _register_grid_parser

    root = argparse.ArgumentParser()
    _register_grid_parser(root.add_subparsers(dest="command"))
    ns = root.parse_args(["grid", "join", "--model", "v41", "--bind", "10.0.0.2", "--json"])
    assert ns.grid_command == "join" and ns.model == "v41" and ns.json


# ── coordinator-local (the #13509 PARTIAL) ──────────────────────────────────


def _spark():
    gpus = pool_role.parse_nvidia_smi("0, NVIDIA GB10, [N/A], [N/A]")
    return NodeCaps("linux", 121.7, 112.4, 20, 20, 1, gpus, rtt_ms=0.05)


def test_coordinator_gb10_is_local_not_rpc_server():
    p = plan_roles(_spark(), SPEC, "100.64.0.38", is_coordinator=True)
    assert p.admission == "COORDINATOR"
    assert p.roles == ["local"]
    assert p.backends[0].command == []
    assert "CUDA0" in p.backends[0].note and "-ngl 99" in p.backends[0].note
    assert p.backends[0].blocks <= SPEC.total_blocks


def test_loopback_coordinator_is_detected_by_run(capsys):
    args = argparse.Namespace(model="v41", apply=False, coordinator="127.0.0.1:8001",
                              bind="100.64.0.38", rpc_server="rpc-server", role="auto",
                              ram_cap_gib=None, json=True)
    assert pool_role.run(args, probe=lambda _c: _spark(), admit=_admit) == 0
    import json

    got = json.loads(capsys.readouterr().out)
    assert got["admission"] == "COORDINATOR" and got["roles"] == ["local"]
    assert all(b["command"] == [] for b in got["backends"])


def test_self_test_fails_when_coordinator_is_planned_as_rpc(monkeypatch):
    # Reintroduce the #13509 bug: the coordinator flag is ignored.
    orig = pool_role.plan_roles

    def ignore(*a, **kw):
        kw.pop("is_coordinator", None)
        return orig(*a, **kw)

    monkeypatch.setattr(pool_role, "plan_roles", ignore)
    assert pool_role.self_test() == 1


# ── --register (a record only) ──────────────────────────────────────────────


def _reg_args(**kw):
    base = dict(model="v41", apply=False, coordinator="", bind="192.168.1.50",
                rpc_server="rpc-server", role="auto", ram_cap_gib=None, json=True,
                register="v41", registry_url="https://reg.example/comet", node_id="n1")
    base.update(kw)
    return argparse.Namespace(**base)


def test_register_posts_plan_and_reports(monkeypatch, capsys):
    monkeypatch.setenv("AITHER_NODE_TOKEN", "tok")
    seen = {}

    def send(url, body, headers):
        seen.update(url=url, body=body, headers=headers)
        return 200, '{"ok": true, "registered": ["192.168.1.50:50054"], "refused": []}'

    assert pool_role.run(_reg_args(), probe=lambda _c: _host(), admit=_admit, send=send) == 0
    assert seen["url"] == "https://reg.example/comet/pools/v41/backends"
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["body"]["node_id"] == "n1"
    assert seen["body"]["plan"]["admission"] == "SPLIT_ELIGIBLE"
    assert "registered  192.168.1.50:50054" in capsys.readouterr().out


def test_register_nothing_to_record_for_replica_only(monkeypatch):
    def send(*_a, **_k):
        raise AssertionError("must not call the registry")

    assert pool_role.run(_reg_args(), probe=lambda _c: _host(rtt=40.0), admit=_admit, send=send) == 1


def test_register_coordinator_records_nothing(monkeypatch):
    def send(*_a, **_k):
        raise AssertionError("the coordinator is not its own backend")

    args = _reg_args(coordinator="127.0.0.1:8001")
    assert pool_role.run(args, probe=lambda _c: _spark(), admit=_admit, send=send) == 1


@pytest.mark.parametrize("reply", [(401, "no"), (200, "not json"),
                                   (200, '{"ok": false, "registered": [], "refused": ["x"]}')])
def test_register_refusal_is_could_not_judge(monkeypatch, reply):
    monkeypatch.setenv("AITHER_NODE_TOKEN", "tok")
    assert pool_role.run(_reg_args(), probe=lambda _c: _host(),
                         admit=_admit, send=lambda *a, **k: reply) == 2


def test_register_without_url_or_token_cannot_judge(monkeypatch):
    monkeypatch.delenv("ADK_POOL_REGISTRY_URL", raising=False)
    assert pool_role.run(_reg_args(registry_url=""), probe=lambda _c: _host(), admit=_admit) == 2
    plan = plan_roles(_host(), SPEC, "192.168.1.50")
    assert pool_role.register_plan(plan, "v41", "https://x", bearer="") == 2


def test_register_flags_on_the_cli():
    from adk.cli import _register_grid_parser

    root = argparse.ArgumentParser()
    _register_grid_parser(root.add_subparsers(dest="command"))
    ns = root.parse_args(["grid", "join", "--register", "v41", "--registry-url", "https://r",
                          "--coordinator-local"])
    assert ns.register == "v41" and ns.registry_url == "https://r" and ns.coordinator_local


# ── block math from the inference profile (grid join piece 4) ──────────────


def test_v41_builtin_is_measured_not_provisional_0731_math():
    # Before: V4.1 planned with the 0731 block (1.969 GiB x 43) and measured=False.
    assert SPEC.measured is True
    assert SPEC.total_blocks == 40
    assert SPEC.block_gib > 3.0 and SPEC.other_gib > 0
    assert SPEC.coordinator_reserve_gib == 16.0


def test_coordinator_gb10_budget_matches_the_placement_planner():
    # The placement planner: coordinator budget = MemAvailable - 16384 MiB, non-block tensors first.
    p = plan_roles(_spark(), SPEC, "100.64.0.38", is_coordinator=True)
    want = int((112.4 - 16.0 - SPEC.other_gib) // SPEC.block_gib)
    assert p.backends[0].blocks == want
    assert f"--dgx-free-mb {int(112.4 * 1024)} --dgx-reserve-mb 16384" in p.backends[0].note


def test_self_test_fails_when_coordinator_ignores_non_block_tensors(monkeypatch):
    def no_other(budget_gib, spec):
        return min(spec.total_blocks, int(budget_gib // spec.block_gib))

    monkeypatch.setattr(pool_role, "coordinator_blocks", no_other)
    assert pool_role.self_test() == 1


def _profile(**quant):
    q = {"id": "Q", "pool_block_gib": 10.0, "pool_blocks": 40, "pool_other_gib": 1.0,
         "built": True}
    q.update(quant)
    return {"profile": {"id": "deepseek-v4.1-flash-1017"},
            "pool": {"default_quant": "Q", "rpc_overhead_gib": 0.6,
                     "rpc_cpu_rss_factor": 2.75, "coordinator_reserve_gib": 16.0},
            "quants": [q]}


def test_profile_flag_supplies_the_block_math(tmp_path, capsys):
    import json

    f = tmp_path / "prof.json"
    f.write_text(json.dumps(_profile()), encoding="utf-8")
    args = argparse.Namespace(model="v41", apply=False, coordinator="", bind="192.168.1.50",
                              rpc_server="rpc-server", role="auto", ram_cap_gib=None,
                              json=True, profile=str(f), quant="")
    assert pool_role.run(args, probe=lambda _c: _host(), admit=_admit) == 0
    got = json.loads(capsys.readouterr().out)
    vram = [b for b in got["backends"] if b["role"] == "rpc-vram"][0]
    assert vram["blocks"] == int((30.0 - 0.6 - 1.0) // 10.0)
    assert "quant Q" in got["spec_source"]


@pytest.mark.parametrize("prof", [
    _profile(pool_block_gib=None),
    {**_profile(), "profile": {"id": "some-other-model"}},
    "::: not yaml or json :::\n\t- [",
])
def test_bad_or_mismatched_profile_cannot_judge(tmp_path, prof):
    import json

    f = tmp_path / "prof.txt"
    f.write_text(prof if isinstance(prof, str) else json.dumps(prof), encoding="utf-8")
    args = argparse.Namespace(model="v41", apply=False, coordinator="", bind="192.168.1.50",
                              rpc_server="rpc-server", role="auto", ram_cap_gib=None,
                              json=True, profile=str(f), quant="")
    assert pool_role.run(args, probe=lambda _c: _host(), admit=_admit) == 2


def test_yaml_profile_loads(tmp_path):
    import yaml

    f = tmp_path / "p.yaml"
    f.write_text(yaml.safe_dump(_profile()), encoding="utf-8")
    spec = pool_role.load_profile_spec(str(f))
    assert (spec.block_gib, spec.total_blocks, spec.quant) == (10.0, 40, "Q")
