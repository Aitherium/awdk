"""Report generation — JSON and human-readable output.

Generates evaluation reports in multiple formats: JSON (machine-readable)
and human-readable tables (terminal-friendly).

Usage:
    report = EvalReport()
    report.add_tool_result(name, success, message)
    print(report.human_format())
    return report.json_format()
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PackEvalResult:
    """Evaluation result for a pack."""
    pack_name: str
    pack_path: str = ""
    pack_id: str = ""
    success: bool = True
    tools_declared: int = 0
    tools_found: int = 0
    tools_missing: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def all_declared_found(self) -> bool:
        """All declared tools exist on gateway."""
        return len(self.tools_missing) == 0

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class ToolEvalResult:
    """Evaluation result for a single tool."""
    tool_name: str
    callable: bool = True
    safe: bool = True
    message: str = ""
    invoked: bool = False
    invoke_status: str = ""  # "success", "callable", "callable_degraded", "error"
    error_type: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)


class EvalReport:
    """Collect and format evaluation results."""

    def __init__(
        self,
        gateway_url: str = "",
        authenticated: bool = False,
        tier: str = "free",
    ):
        """Initialize a report.

        Args:
            gateway_url: Gateway being tested
            authenticated: Whether authentication was successful
            tier: Access tier
        """
        self.gateway_url = gateway_url
        self.authenticated = authenticated
        self.tier = tier
        self.tools: dict[str, ToolEvalResult] = {}
        self.packs: dict[str, PackEvalResult] = {}
        self.total_tools = 0
        self.total_safe = 0
        self.total_callable = 0

    def add_tool(
        self,
        name: str,
        callable: bool = True,
        safe: bool = True,
        message: str = "",
    ) -> None:
        """Record a tool evaluation."""
        result = ToolEvalResult(
            tool_name=name,
            callable=callable,
            safe=safe,
            message=message,
        )
        self.tools[name] = result
        self.total_tools += 1
        if callable:
            self.total_callable += 1
        if safe:
            self.total_safe += 1

    def add_invoke_result(
        self,
        tool_name: str,
        success: bool,
        status: str,
        message: str = "",
        error_type: str = "",
    ) -> None:
        """Record tool invocation result."""
        if tool_name not in self.tools:
            self.add_tool(tool_name)

        result = self.tools[tool_name]
        result.invoked = True
        result.invoke_status = status
        result.message = message
        result.error_type = error_type
        result.callable = status in ("callable", "callable_degraded", "success")

    def add_pack(self, pack_result: PackEvalResult) -> None:
        """Record pack evaluation."""
        self.packs[pack_result.pack_name] = pack_result

    def json_format(self) -> str:
        """Return report as JSON string."""
        data = {
            "gateway": self.gateway_url,
            "authenticated": self.authenticated,
            "tier": self.tier,
            "summary": {
                "total_tools": self.total_tools,
                "callable_tools": self.total_callable,
                "safe_tools": self.total_safe,
                "invoked_tools": sum(1 for t in self.tools.values() if t.invoked),
            },
            "tools": {
                name: result.to_dict()
                for name, result in self.tools.items()
            },
            "packs": {
                name: result.to_dict()
                for name, result in self.packs.items()
            },
        }
        return json.dumps(data, indent=2)

    def human_format(self) -> str:
        """Return report as human-readable text."""
        lines = []
        lines.append("")
        lines.append("=" * 70)
        lines.append("MCP Evaluation Report")
        lines.append("=" * 70)
        lines.append("")

        # Gateway info
        lines.append(f"Gateway:       {self.gateway_url or '(local)'}")
        lines.append(f"Authenticated: {'Yes' if self.authenticated else 'No'}")
        lines.append(f"Tier:          {self.tier}")
        lines.append("")

        # Summary — only when tools were actually evaluated. A pack-only run has
        # an empty tools section, and rendering "0/0 callable" next to a pack
        # PASS reads as a vacuous verdict rather than a different mode.
        if self.tools:
            lines.append("Summary:")
            lines.append(f"  Total tools:      {self.total_tools}")
            lines.append(f"  Callable:         {self.total_callable} ({self._pct(self.total_callable, self.total_tools)}%)")
            lines.append(f"  Safe to invoke:   {self.total_safe} ({self._pct(self.total_safe, self.total_tools)}%)")
            invoked = sum(1 for t in self.tools.values() if t.invoked)
            lines.append(f"  Invoked:          {invoked}")
            lines.append("")

        # Failed tools
        failed = [t for t in self.tools.values() if not t.callable]
        if failed:
            lines.append("FAILED TOOLS:")
            for tool in sorted(failed, key=lambda t: t.tool_name):
                lines.append(f"  {tool.tool_name}")
                if tool.message:
                    lines.append(f"    → {tool.message}")
            lines.append("")

        # Pack results
        if self.packs:
            lines.append("Pack Verification:")
            for pack_name, pack in sorted(self.packs.items()):
                status = "✓ PASS" if pack.success else "✗ FAIL"
                lines.append(f"  {status}: {pack_name}")
                if pack.tools_missing:
                    lines.append(f"    Missing {len(pack.tools_missing)} declared tools:")
                    for tool_name in sorted(pack.tools_missing)[:5]:
                        lines.append(f"      - {tool_name}")
                    if len(pack.tools_missing) > 5:
                        lines.append(f"      ... and {len(pack.tools_missing) - 5} more")
            lines.append("")

        # Verdict — per mode. Pack-only runs are judged on declared-vs-served,
        # never on a tools-callable ratio they did not measure.
        lines.append("Verdict:")
        if self.tools:
            if self.total_callable == self.total_tools and self.total_tools > 0:
                lines.append("  ✓ All tools callable")
            else:
                pct = self._pct(self.total_callable, self.total_tools)
                lines.append(f"  ⚠ {pct}% of tools callable ({self.total_callable}/{self.total_tools})")
        if self.packs:
            bad = [p for p in self.packs.values() if not p.success]
            if not bad:
                lines.append(f"  ✓ {len(self.packs)} pack(s): every declared tool is served")
            else:
                lines.append(f"  ✗ {len(bad)} of {len(self.packs)} pack(s) declare tools the gateway does not serve")
        if not self.tools and not self.packs:
            lines.append("  ⚠ nothing evaluated — this is NOT a pass")

        lines.append("")
        lines.append("=" * 70)
        lines.append("")

        return "\n".join(lines)

    def summary_table(self) -> str:
        """Return a brief summary table."""
        lines = []
        lines.append("")
        lines.append("Tool Status Summary:")
        lines.append("-" * 50)

        # Group by status
        callable_safe = [t for t in self.tools.values() if t.callable and t.safe]
        callable_unsafe = [t for t in self.tools.values() if t.callable and not t.safe]
        not_callable = [t for t in self.tools.values() if not t.callable]

        lines.append(f"  ✓ Callable & Safe:      {len(callable_safe):3d}")
        lines.append(f"  ⚠ Callable (mutating):  {len(callable_unsafe):3d}")
        lines.append(f"  ✗ Not callable:         {len(not_callable):3d}")
        lines.append("-" * 50)

        return "\n".join(lines)

    @staticmethod
    def _pct(num: int, denom: int) -> int:
        """Calculate percentage."""
        if denom == 0:
            return 0
        return int(100 * num / denom)
