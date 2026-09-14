"""Connect-template schema fidelity — a wrong key is a SILENT no-op.

Closes the residual "connect templates never validated". A template that
renders valid YAML can still be completely ignored by the target framework if the
key shape is wrong. That actually happened: the hermes template emitted `models:`
(a list) while hermes' real cli-config uses `model:` (a mapping) — so the render
looked fine and would have configured nothing.

These assertions are pinned to the shapes in each framework's own example config.
"""
import yaml

from adk.connect import SUPPORTED_FRAMEWORKS, render_connect

GATEWAY = "https://gateway.aitherium.com/v1"
MCP = "https://mcp.aitherium.com/mcp"
KEY = "aither_sk_live_TESTKEY"


def _render(fw: str) -> tuple[str, dict]:
    out = render_connect(fw, gateway_url=GATEWAY, mcp_url=MCP, api_key=KEY)
    return out, yaml.safe_load(out)


def test_all_frameworks_render_valid_yaml_mappings():
    for fw in SUPPORTED_FRAMEWORKS:
        out, doc = _render(fw)
        assert isinstance(doc, dict) and doc, f"{fw}: template is not a YAML mapping"
        assert "{" not in out, f"{fw}: unsubstituted placeholder left in output"
        # The whole point of the rail: the gateway + key must actually appear.
        assert GATEWAY in out, f"{fw}: gateway_url not in rendered config"
        assert KEY in out, f"{fw}: api_key not in rendered config"


def test_hermes_uses_singular_model_mapping():
    """hermes cli-config.yaml.example: `model:` is a MAPPING; `models:` is ignored."""
    _, doc = _render("hermes")
    assert "model" in doc, "hermes needs top-level `model:`"
    assert "models" not in doc, "hermes has no `models:` key — a list would be ignored"
    assert isinstance(doc["model"], dict), "hermes `model:` must be a mapping"
    # provider "custom" is hermes' name for any OpenAI-compatible endpoint
    assert doc["model"].get("provider") == "custom"
    assert doc["model"].get("base_url") == GATEWAY
    assert "mcp_servers" in doc and isinstance(doc["mcp_servers"], dict)
    # hermes MCP entries are keyed by server name, with url: (HTTP) or command/args
    entry = next(iter(doc["mcp_servers"].values()))
    assert "url" in entry or "command" in entry


def test_deer_flow_uses_models_list():
    """deer-flow config.example.yaml: top-level `models:`."""
    _, doc = _render("deer_flow")
    assert "models" in doc, "deer-flow needs top-level `models:`"


def test_nooa_and_openclaw_render_expected_roots():
    _, nooa = _render("nooa")
    assert nooa, "nooa template empty"
    _, oc = _render("openclaw")
    assert oc, "openclaw template empty"


def test_unknown_framework_raises():
    import pytest

    with pytest.raises((ValueError, KeyError, FileNotFoundError)):
        render_connect("not-a-framework", gateway_url=GATEWAY, mcp_url=MCP, api_key=KEY)
