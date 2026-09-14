"""AitherOS framework connect templates.

Provides zero-code configuration bridges to repoint external frameworks
(hermes, deer-flow, nooa, openclaw) to use the AitherOS gateway for LLM
calls and MCP tools.
"""

import re
from pathlib import Path
from typing import Literal


SUPPORTED_FRAMEWORKS = ("hermes", "deer_flow", "nooa", "openclaw")


def render_connect(
    framework: Literal["hermes", "deer_flow", "nooa", "openclaw"],
    gateway_url: str,
    mcp_url: str,
    api_key: str,
) -> str:
    """Load and render a framework connect template with substituted values.

    Loads the YAML template for the specified framework and substitutes
    placeholders (gateway_url, mcp_url, api_key) with actual values.

    Args:
        framework: Framework name (hermes, deer_flow, nooa, openclaw).
        gateway_url: AitherOS gateway URL (e.g., http://localhost:8001).
        mcp_url: AitherOS MCP gateway URL (e.g., http://localhost:8182/mcp).
        api_key: API key for authentication.

    Returns:
        Rendered YAML configuration string ready to be written to config file.

    Raises:
        ValueError: If framework is not supported.
    """
    if framework not in SUPPORTED_FRAMEWORKS:
        raise ValueError(
            f"Unsupported framework: {framework}. "
            f"Supported: {', '.join(SUPPORTED_FRAMEWORKS)}"
        )

    # Normalize framework name for file lookup
    filename = f"{framework}.yaml.tmpl"
    template_path = Path(__file__).parent / "templates" / filename

    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    # Load template
    template_content = template_path.read_text(encoding="utf-8")

    # Substitute placeholders
    rendered = template_content.format(
        gateway_url=gateway_url,
        mcp_url=mcp_url,
        api_key=api_key,
    )

    return rendered


__all__ = ["render_connect", "SUPPORTED_FRAMEWORKS"]
