"""`adk join` must call each node_bootstrap tool with its REAL signature.

The older tests patched these tools with plain MagicMocks, which accept any
arguments, so `join_mesh` passed a hardware dict as ``prefer_backend``,
``model_override=``/``use_docker=`` keywords that do not exist, an apply dict as
``node_verify``'s ``base_url`` and ``(node_id, mesh_ip, recipe_result)``
positionally to ``node_register_backend`` without any test noticing. Here every
tool is patched with ``autospec=True``: a call that does not bind to the real
signature raises TypeError, the step's broad except turns that into exit 1, and
the success assertions fail.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from adk.commands.join import join_mesh
from adk.toolpacks.node_bootstrap.recipes import get_recipe

_TOOLS = "adk.toolpacks.node_bootstrap.tools"
_TOKEN = "tok_join_call_shapes_0123456789"
_MESH_IP = "100.64.0.7"


def _patch_chain(stack: ExitStack, recipe: dict, apply_result: dict | None = None):
    """Patch every surface `join_mesh` touches; return the tool mocks by name."""
    stack.enter_context(patch(
        "adk.commands.join._github_device_flow_login", new_callable=AsyncMock,
        return_value={"access_token": _TOKEN, "tenant_id": "tenant_xyz"},
    ))
    mocks = {}
    returns = {
        "node_detect_hardware": {"system_info": {"ram_gb": 32}},
        "node_resolve_recipe": {"recipe": recipe, "match_score": 1.0},
        "node_apply": apply_result or {"applied": True, "recipe_id": recipe.get("id")},
        "node_verify": {"status": "healthy", "health": True, "coherent": True},
        "node_register_backend": {"registered": True},
    }
    for name, value in returns.items():
        mocks[name] = stack.enter_context(
            patch(f"{_TOOLS}.{name}", autospec=True, return_value=value)
        )
    mocks["rich_enroll"] = stack.enter_context(patch(
        "adk.enrollment.rich_enroll", autospec=True,
        return_value={"node_id": "node-abc", "bearer_token": "node-bearer"},
    ))
    stack.enter_context(patch(
        "adk.commands.join._obtain_mesh_key", new_callable=AsyncMock,
        return_value="mesh-key",
    ))
    mocks["mesh_join"] = stack.enter_context(patch(
        "adk.mesh.join", autospec=True, return_value={"overlay_ip": _MESH_IP},
    ))
    return mocks


@pytest.fixture
def cpu_llamacpp() -> dict:
    recipe = get_recipe("cpu-llamacpp")
    assert recipe, "cpu-llamacpp recipe must ship with the pack"
    return recipe


@pytest.mark.asyncio
async def test_join_completes_with_real_tool_signatures(cpu_llamacpp, capsys, monkeypatch):
    monkeypatch.setenv("AITHER_GENESIS_URL", "https://genesis.example.test")
    with ExitStack() as stack:
        mocks = _patch_chain(stack, cpu_llamacpp)
        result = await join_mesh(dry_run=False)
    out = capsys.readouterr().out

    assert result == 0, out
    assert "Community node onboarded" in out
    mocks["node_apply"].assert_called_once_with(recipe_id="cpu-llamacpp", dry_run=False)
    verify_kwargs = mocks["node_verify"].call_args.kwargs
    assert verify_kwargs["base_url"] == "http://localhost:8080"
    assert verify_kwargs["recipe_id"] == "cpu-llamacpp"
    mocks["node_register_backend"].assert_called_once_with(
        genesis_url="https://genesis.example.test",
        base_url=f"http://{_MESH_IP}:8080",
        backend_type="llamacpp_remote",
        models=["aither-orchestrator"],
        token=_TOKEN,
    )
    assert _TOKEN not in out


@pytest.mark.asyncio
async def test_register_backend_gets_keywords_not_positionals(cpu_llamacpp, capsys):
    """The join.py:387 bug: positional (node_id, mesh_ip, recipe) never binds right."""
    with ExitStack() as stack:
        mocks = _patch_chain(stack, cpu_llamacpp)
        await join_mesh(dry_run=False)
    call = mocks["node_register_backend"].call_args
    assert call is not None, capsys.readouterr().out
    assert call.args == ()
    assert call.kwargs["base_url"].startswith(f"http://{_MESH_IP}:")
    assert call.kwargs["token"] == _TOKEN
    assert isinstance(call.kwargs["backend_type"], str)


@pytest.mark.asyncio
async def test_model_override_reaches_verify_and_register(cpu_llamacpp):
    with ExitStack() as stack:
        mocks = _patch_chain(stack, cpu_llamacpp)
        assert await join_mesh(model="my-model", dry_run=False) == 0
    assert mocks["node_verify"].call_args.kwargs["model"] == "my-model"
    assert mocks["node_register_backend"].call_args.kwargs["models"] == ["my-model"]


@pytest.mark.asyncio
async def test_delegate_recipe_not_applied_aborts_before_enroll(cpu_llamacpp, capsys):
    with ExitStack() as stack:
        mocks = _patch_chain(
            stack, cpu_llamacpp,
            apply_result={"planned": True, "mode": "delegate", "fix": "run the fleet delegate: x"},
        )
        assert await join_mesh(dry_run=False) == 1
    assert "Recipe was not applied" in capsys.readouterr().out
    mocks["rich_enroll"].assert_not_called()
    mocks["node_register_backend"].assert_not_called()


@pytest.mark.asyncio
async def test_degraded_backend_is_never_registered(cpu_llamacpp, capsys):
    with ExitStack() as stack:
        mocks = _patch_chain(stack, cpu_llamacpp)
        mocks["node_verify"].return_value = {"status": "degraded", "health": True,
                                             "coherent": False}
        assert await join_mesh(dry_run=False) == 1
    assert "backend is degraded" in capsys.readouterr().out
    mocks["node_register_backend"].assert_not_called()


@pytest.mark.asyncio
async def test_cloud_fallback_recipe_serves_nothing(capsys):
    cloud = get_recipe("cloud-api")
    assert cloud
    with ExitStack() as stack:
        mocks = _patch_chain(stack, cloud)
        assert await join_mesh(dry_run=False) == 1
    assert "serves nothing locally" in capsys.readouterr().out
    mocks["node_apply"].assert_not_called()


def test_serving_spec_tolerates_non_dict_recipe():
    from adk.commands.join import _serving_spec

    assert _serving_spec("cuda-vllm-8gb")["port"] == 0  # type: ignore[arg-type]


def test_backend_registration_defaults_to_public_edge(cpu_llamacpp, monkeypatch):
    from adk.commands.join import _backend_registration, _serving_spec

    monkeypatch.delenv("AITHER_GENESIS_URL", raising=False)
    kw = _backend_registration(_serving_spec(cpu_llamacpp), _MESH_IP, _TOKEN)
    assert kw["genesis_url"].startswith("https://")
    assert kw["base_url"] == f"http://{_MESH_IP}:8080"


@pytest.mark.asyncio
async def test_join_output_survives_a_cp1252_stdout(cpu_llamacpp, monkeypatch):
    """A redirected Windows stdout is cp1252: a non-ASCII mark in the success
    summary raised UnicodeEncodeError AFTER the backend registered, and the
    broad except reported the finished join as a failure (exit 1)."""
    import io
    import sys

    buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", buf)
    with ExitStack() as stack:
        mocks = _patch_chain(stack, cpu_llamacpp)
        result = await join_mesh(dry_run=False)
    buf.flush()
    out = buf.buffer.getvalue().decode("cp1252")
    assert result == 0, out
    assert "Community node onboarded" in out
    mocks["node_register_backend"].assert_called_once()
