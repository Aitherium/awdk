"""MCP UI Resources — serve RenderBlocks as ui:// protocol resources.

Bridges AitherOS RenderBlocks (server-driven UI primitives) into the MCP
resource protocol, making agent-generated UI accessible to any MCP client
that supports the ui:// URI scheme.

The format is compatible with Claude's native RenderBlocks rendering:
24 block types (markdown, header, table, code, form, approve, slider, etc.)
with Python<->TS parity gated by check_render_block_contract.py.

Usage:
    from adk.mcp_ui_resources import RenderBlocksMCPServer

    server = RenderBlocksMCPServer()

    # Register UI responses from agents:
    server.register_ui_resource(
        uri="ui://agent/review_result",
        blocks=[
            {"type": "header", "text": "Review Results", "level": 2},
            {"type": "scores", "scores": {"security": 0.92, "style": 0.78}},
            {"type": "table", "columns": [...], "rows": [...]},
        ],
    )

    # Mount into an MCP server or FastAPI app:
    # server.mount(app)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("adk.mcp_ui_resources")

# Block types from lib.core.RenderBlocks.BlockType enum
VALID_BLOCK_TYPES = {
    # Display blocks (read-only)
    "markdown", "header", "kv", "table", "code", "diff",
    "scores", "progress", "timeline", "callout", "image",
    "chart", "list", "tree", "actions", "notebook",
    # Interactive blocks (data flows back via /ui/action)
    "form", "approve", "select", "slider", "file_upload",
    "live", "panel",
}


class RenderBlocksValidator:
    """Validate RenderBlocks against the schema."""

    @staticmethod
    def validate_block(block: dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Check if a block is valid RenderBlocks.

        Returns:
            (is_valid, error_reason_or_None)
        """
        if not isinstance(block, dict):
            return False, "Block must be a dict"

        block_type = block.get("type")
        if block_type not in VALID_BLOCK_TYPES:
            return False, f"Unknown block type: {block_type}"

        # Basic schema validation by type
        # (TS parity gate ensures schema on the wire)
        if block_type == "header":
            if not isinstance(block.get("text"), str):
                return False, "header requires 'text' string"
        elif block_type == "code":
            if not isinstance(block.get("text"), str):
                return False, "code requires 'text' string"
        elif block_type == "table":
            if not isinstance(block.get("columns"), list):
                return False, "table requires 'columns' array"
            if not isinstance(block.get("rows"), list):
                return False, "table requires 'rows' array"
        elif block_type == "form":
            if not isinstance(block.get("fields"), list):
                return False, "form requires 'fields' array"

        return True, None

    @staticmethod
    def validate_blocks(blocks: list[dict[str, Any]]) -> tuple[bool, Optional[str]]:
        """Validate a full block list.

        Returns:
            (is_valid, error_reason_or_None)
        """
        if not isinstance(blocks, list):
            return False, "Blocks must be a list"

        for i, block in enumerate(blocks):
            valid, err = RenderBlocksValidator.validate_block(block)
            if not valid:
                return False, f"Block {i}: {err}"

        return True, None


class RenderBlocksUIResource:
    """A single UI resource serving RenderBlocks via MCP."""

    def __init__(
        self,
        uri: str,
        blocks: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ):
        """Initialize a UI resource.

        Args:
            uri: MCP resource URI (e.g., "ui://agent/review_result")
            blocks: List of RenderBlock dicts
            metadata: Optional metadata (title, description, etc.)
        """
        self.uri = uri
        self.blocks = blocks
        self.metadata = metadata or {}

        # Validate at construction
        valid, err = RenderBlocksValidator.validate_blocks(blocks)
        if not valid:
            raise ValueError(f"Invalid RenderBlocks for {uri}: {err}")

    def to_mcp_resource(self) -> dict[str, Any]:
        """Serialize to MCP resource format.

        The resource content is the JSON-serialized block list, suitable
        for MCP clients that understand the ui:// scheme.
        """
        return {
            "uri": self.uri,
            "mimeType": "application/vnd.aitheros.renderblocks+json",
            "contents": {
                "text": json.dumps(self.blocks, separators=(",", ":")),
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """Export as a plain dict (for API responses, etc.)."""
        return {
            "uri": self.uri,
            "blocks": self.blocks,
            "metadata": self.metadata,
        }


class RenderBlocksMCPServer:
    """MCP server for ui:// RenderBlocks resources.

    Mount into a FastAPI app or use as a standalone MCP server.
    """

    def __init__(self, base_url: str = "ui://adk/"):
        """Initialize the server.

        Args:
            base_url: Base URI for resources (default "ui://adk/")
        """
        self.base_url = base_url
        self.resources: dict[str, RenderBlocksUIResource] = {}

    def register_ui_resource(
        self,
        uri: str,
        blocks: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a UI resource.

        Args:
            uri: MCP resource URI (e.g., "ui://agent/review_result")
            blocks: RenderBlocks list
            metadata: Optional metadata

        Raises:
            ValueError: If blocks are invalid
        """
        resource = RenderBlocksUIResource(uri, blocks, metadata)
        self.resources[uri] = resource
        logger.debug(f"Registered UI resource: {uri} ({len(blocks)} blocks)")

    def get_resource(self, uri: str) -> Optional[RenderBlocksUIResource]:
        """Retrieve a registered resource."""
        return self.resources.get(uri)

    def list_resources(self) -> list[dict[str, Any]]:
        """List all registered resources (for MCP /resources endpoint)."""
        return [
            {
                "uri": r.uri,
                "name": r.metadata.get("name", r.uri.split("/")[-1]),
                "description": r.metadata.get("description", ""),
                "mimeType": "application/vnd.aitheros.renderblocks+json",
            }
            for r in self.resources.values()
        ]

    def from_agent_response(
        self,
        agent_name: str,
        task_id: str,
        blocks: list[dict[str, Any]],
        title: str = "",
    ) -> str:
        """Helper: register blocks from an agent response.

        Generates a URI and returns it.

        Args:
            agent_name: Name of the agent providing the blocks
            task_id: Task/session identifier
            blocks: RenderBlocks
            title: Optional title for the resource

        Returns:
            The registered URI
        """
        uri = f"ui://agent/{agent_name}/{task_id}"
        metadata = {"title": title, "agent": agent_name, "task_id": task_id} if title else {}
        self.register_ui_resource(uri, blocks, metadata)
        return uri

    # ─────────────────────────────────────────────────────────────────────────
    # FastAPI mount (optional)
    # ─────────────────────────────────────────────────────────────────────────

    def mount(self, app: Any) -> None:
        """Mount MCP UI endpoints into a FastAPI app.

        Adds:
        - GET /mcp/resources — list registered resources
        - GET /mcp/resources/{uri} — fetch a resource
        - POST /mcp/resources — register new resources
        """
        try:
            from fastapi import HTTPException
        except ImportError:
            logger.warning("FastAPI not available; mount skipped")
            return

        @app.get("/mcp/resources")
        async def list_mcp_resources():
            """List all registered UI resources."""
            return {"resources": self.list_resources()}

        @app.get("/mcp/resources/{uri:path}")
        async def get_mcp_resource(uri: str):
            """Fetch a resource by URI."""
            # URI comes in as path parameter; reconstruct with ui:// scheme
            full_uri = f"ui://{uri}" if not uri.startswith("ui://") else uri
            resource = self.get_resource(full_uri)
            if not resource:
                raise HTTPException(status_code=404, detail="Resource not found")
            return resource.to_mcp_resource()

        @app.post("/mcp/resources")
        async def register_mcp_resource(payload: dict[str, Any]):
            """Register a new UI resource."""
            uri = payload.get("uri")
            blocks = payload.get("blocks")
            metadata = payload.get("metadata")

            if not uri or not blocks:
                raise HTTPException(status_code=400, detail="Missing 'uri' or 'blocks'")

            try:
                self.register_ui_resource(uri, blocks, metadata)
                return {"ok": True, "uri": uri}
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        logger.info("MCP UI resources mounted at /mcp/resources")


# Convenience helpers for common block patterns
def create_header_block(text: str, level: int = 2, icon: str = "") -> dict[str, Any]:
    """Create a header block."""
    return {"type": "header", "text": text, "level": level, **({"icon": icon} if icon else {})}


def create_code_block(
    text: str, language: str = "text", compact: bool = False
) -> dict[str, Any]:
    """Create a code block."""
    return {"type": "code", "text": text, "language": language, "compact": compact}


def create_table_block(
    columns: list[str], rows: list[list[Any]], sortable: bool = True
) -> dict[str, Any]:
    """Create a table block."""
    return {"type": "table", "columns": columns, "rows": rows, "sortable": sortable}


def create_markdown_block(text: str, compact: bool = False) -> dict[str, Any]:
    """Create a markdown block."""
    return {"type": "markdown", "text": text, "compact": compact}


def create_callout_block(
    text: str, variant: str = "info", icon: str = ""
) -> dict[str, Any]:
    """Create a callout/alert block."""
    block: dict[str, Any] = {"type": "callout", "text": text, "variant": variant}
    if icon:
        block["icon"] = icon
    return block


def create_scores_block(scores: dict[str, float]) -> dict[str, Any]:
    """Create a scores/metrics block."""
    return {"type": "scores", "scores": scores}


def create_form_block(
    fields: list[dict[str, Any]], button_label: str = "Submit"
) -> dict[str, Any]:
    """Create a form block."""
    return {"type": "form", "fields": fields, "button_label": button_label}


def create_approve_block(
    title: str = "Approve?", approve_label: str = "Approve", reject_label: str = "Reject"
) -> dict[str, Any]:
    """Create an approve/reject block (human-in-the-loop)."""
    return {
        "type": "approve",
        "title": title,
        "approve_label": approve_label,
        "reject_label": reject_label,
    }
