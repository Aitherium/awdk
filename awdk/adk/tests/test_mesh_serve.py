"""Tests for adk.mesh_serve (S3 — role-based Kimi-K3 mesh serving)."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
from adk.mesh_serve import (
    parse_nodes_spec,
    serve_coordinator,
    serve_plan,
    serve_rpc_backend,
)

NODES_640 = "n1:10.77.1.2:128:0,n2:10.77.1.3:128:0,n3:10.77.1.4:128:0," \
    "n4:10.77.1.5:128:0,n5:10.77.1.6:128:0"
NODES_712 = "n1:10.77.1.2:256:24,n2:10.77.1.3:256:48,n3:10.77.1.4:128:0"


class TestParseNodesSpec:
    def test_parses_valid_spec(self):
        nodes = parse_nodes_spec("a:10.77.0.1:64:24, b:10.77.0.2:128:0")
        assert [n.node_id for n in nodes] == ["a", "b"]
        assert nodes[0].total_gb == 88.0

    def test_rejects_wrong_arity(self):
        with pytest.raises(ValueError, match="id:host:ram_gb:vram_gb"):
            parse_nodes_spec("a:10.77.0.1:64")

    def test_rejects_non_numeric(self):
        with pytest.raises(ValueError, match="numbers"):
            parse_nodes_spec("a:10.77.0.1:lots:24")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            parse_nodes_spec("")

    def test_rejects_empty_id_or_host(self):
        with pytest.raises(ValueError):
            parse_nodes_spec(":10.77.0.1:64:0")


class TestServePlan:
    def test_plan_selects_largest_fitting_quant(self):
        plan = serve_plan(NODES_712)
        assert plan["quant"] == "UD-IQ1_M"  # 712 >= 665, < 726
        assert plan["download_gb"] == 649

    def test_plan_refuses_small_pool(self):
        with pytest.raises(ValueError):
            serve_plan("a:10.77.0.1:128:0")

    def test_plan_carries_research_edge_note(self):
        plan = serve_plan(NODES_640)
        assert "unauthenticated" in plan["note"]


class TestServeRpcBackend:
    def test_public_bind_refused_before_anything_runs(self):
        with pytest.raises(ValueError, match="rpc-server bind rejected"):
            serve_rpc_backend(bind="8.8.8.8")

    def test_zero_bind_refused(self):
        with pytest.raises(ValueError):
            serve_rpc_backend(bind="0.0.0.0")

    def test_dry_run_renders_but_never_starts(self, tmp_path):
        report = serve_rpc_backend(
            bind="10.77.1.2", build_dir=tmp_path, dry_run=True
        )
        assert report["started"] is False
        assert report["command"][0].endswith("rpc-server")
        assert "--host" in report["command"]
        assert report["build"].get("would_build") is True  # no binaries in tmp

    def test_execute_starts_process(self, tmp_path):
        fake_popen = MagicMock(return_value=MagicMock(pid=4242))
        with patch("adk.mesh_serve.ensure_build") as mock_build:
            mock_build.return_value = {"built": False, "bin_dir": str(tmp_path)}
            report = serve_rpc_backend(
                bind="10.77.1.2", build_dir=tmp_path, dry_run=False,
                _popen=fake_popen,
            )
        assert report["started"] is True
        assert report["pid"] == 4242
        bound = fake_popen.call_args[0][0]
        assert "10.77.1.2" in bound


class TestServeCoordinator:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_dry_run_returns_ordered_steps(self):
        report = asyncio.run(serve_coordinator(
            nodes_spec=NODES_712,
            backends="10.77.1.2:50052,10.77.1.4:50052",
            dry_run=True,
        ))
        steps = [s["step"] for s in report["steps"]]
        assert steps == ["download", "build", "serve", "health-gate", "advertise"]
        assert report["served"] is False

    def test_no_advertise_marks_step_skipped(self):
        report = asyncio.run(serve_coordinator(
            nodes_spec=NODES_712,
            backends="10.77.1.2:50052,10.77.1.4:50052",
            dry_run=True,
            advertise=False,
        ))
        assert "advertise-skipped" in [s["step"] for s in report["steps"]]

    def test_missing_backends_refused(self):
        with pytest.raises(ValueError, match="rpc backends"):
            asyncio.run(serve_coordinator(
                nodes_spec=NODES_712,
                backends="10.77.1.2:50052",  # plan needs 2
                dry_run=True,
            ))

    def test_public_backend_refused(self):
        with pytest.raises(ValueError):
            asyncio.run(serve_coordinator(
                nodes_spec=NODES_712,
                backends="8.8.8.8:50052,10.77.1.4:50052",
                dry_run=True,
            ))

    def test_public_coordinator_bind_refused(self):
        # 8.8.8.8, not a TEST-NET address: Python's ipaddress marks the
        # 203.0.113.0/24 documentation range is_private=True, so it PASSES
        # the private-bind validator — a genuinely public IP is the real case.
        with pytest.raises(ValueError):
            asyncio.run(serve_coordinator(
                nodes_spec=NODES_712,
                backends="10.77.1.2:50052,10.77.1.4:50052",
                bind="8.8.8.8",
                dry_run=True,
            ))

    def test_coordinator_command_pins_kimi_params(self):
        report = asyncio.run(serve_coordinator(
            nodes_spec=NODES_712,
            backends="10.77.1.2:50052,10.77.1.4:50052",
            dry_run=True,
        ))
        serve_step = next(s for s in report["steps"] if s["step"] == "serve")
        cmd = serve_step["command"]
        cmd_s = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        assert "--mmproj" in cmd_s
        assert "1.0" in cmd_s      # temp pin
        assert "0.95" in cmd_s     # top_p pin
        assert "--rpc" in cmd_s

    def test_execute_advertises_through_provide(self, tmp_path):
        """Full execute path with every heavy step mocked — asserts the
        advertise leg reuses mesh_provider.provide with kimi-k3."""
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        provide_calls = {}

        async def fake_provide(**kwargs):
            provide_calls.update(kwargs)
            return {"ok": True}

        fake_popen = MagicMock(return_value=MagicMock(pid=7))
        with patch("adk.mesh_serve.list_kimi_shards") as mock_list, \
                patch("adk.mesh_serve.download_shards") as mock_dl, \
                patch("adk.mesh_serve.ensure_build") as mock_build, \
                patch("adk.mesh_serve._health_gate") as mock_gate:
            mock_list.return_value = []  # nothing needed → download skipped
            mock_build.return_value = {"built": False, "bin_dir": str(tmp_path)}
            mock_gate.return_value = {"ok": True, "rpc_devices": 2}
            report = asyncio.run(serve_coordinator(
                nodes_spec=NODES_712,
                backends="10.77.1.2:50052,10.77.1.4:50052",
                model_dir=model_dir,
                build_dir=tmp_path,
                dry_run=False,
                tenant_id="tenant-x",
                _provide=fake_provide,
                _popen=fake_popen,
            ))
        assert report["served"] is True
        assert mock_dl.call_count == 0
        assert provide_calls["inference_model"] == "kimi-k3"
        assert provide_calls["tenant_id"] == "tenant-x"

    def test_execute_fails_hard_on_local_only_trap(self, tmp_path):
        """Health gate reporting missing RPC devices must abort, not degrade."""
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        fake_popen = MagicMock(return_value=MagicMock(pid=7))
        with patch("adk.mesh_serve.list_kimi_shards") as mock_list, \
                patch("adk.mesh_serve.ensure_build") as mock_build, \
                patch("adk.mesh_serve._health_gate") as mock_gate:
            mock_list.return_value = []
            mock_build.return_value = {"built": False, "bin_dir": str(tmp_path)}
            mock_gate.return_value = {"ok": False, "error": "local-only"}
            with pytest.raises(RuntimeError, match="health gate"):
                asyncio.run(serve_coordinator(
                    nodes_spec=NODES_712,
                    backends="10.77.1.2:50052,10.77.1.4:50052",
                    model_dir=model_dir,
                    build_dir=tmp_path,
                    dry_run=False,
                    _popen=fake_popen,
                ))


class TestHealthGate:
    def test_gate_counts_rpc_devices(self):
        from adk.mesh_serve import _health_gate

        payload = MagicMock(
            __enter__=lambda s: s,
            __exit__=lambda s, *a: None,
            read=lambda: b'{"devices": [{"name": "CUDA0"}, {"name": "RPC0"}]}',
        )
        gate = _health_gate(
            "http://127.0.0.1:8080", expected_rpc_devices=1,
            timeout_s=1.0, poll_s=0.0, _urlopen=MagicMock(return_value=payload),
            _sleep=lambda _s: None,
        )
        assert gate["ok"] is True

    def test_gate_times_out_when_devices_missing(self):
        from adk.mesh_serve import _health_gate

        payload = MagicMock(
            __enter__=lambda s: s,
            __exit__=lambda s, *a: None,
            read=lambda: b'{"devices": [{"name": "CUDA0"}]}',
        )
        gate = _health_gate(
            "http://127.0.0.1:8080", expected_rpc_devices=2,
            timeout_s=0.2, poll_s=0.0, _urlopen=MagicMock(return_value=payload),
            _sleep=lambda _s: None,
        )
        assert gate["ok"] is False
        assert "local-only" in gate["error"]
