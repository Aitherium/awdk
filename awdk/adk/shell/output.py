"""
AitherShell Output Formatting
===============================
File output, tables, diff, markdown, truncation.
"""

import os
import difflib
from pathlib import Path
from typing import List, Dict, Any, Optional


def write_to_file(text: str, path: str):
    """Write text to a file, creating parent dirs."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def format_as_table(data: List[Dict[str, Any]], max_col_width: int = 40) -> str:
    """Format list of dicts as ASCII table."""
    if not data:
        return "(no data)"
    keys = list(data[0].keys())
    widths = {k: min(max(len(k), max(len(str(row.get(k, ""))[:max_col_width]) for row in data)), max_col_width) for k in keys}
    header = " | ".join(k.ljust(widths[k]) for k in keys)
    sep = "-+-".join("-" * widths[k] for k in keys)
    rows = []
    for row in data:
        rows.append(" | ".join(str(row.get(k, ""))[:max_col_width].ljust(widths[k]) for k in keys))
    return "\n".join([header, sep] + rows)


def format_diff(old_text: str, new_text: str, label_old: str = "before", label_new: str = "after") -> str:
    """Colored unified diff."""
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=label_old, tofile=label_new,
    )
    return "".join(diff)


def render_markdown(text: str):
    """Render markdown in terminal if Rich is available."""
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        console = Console()
        console.print(Markdown(text))
    except ImportError:
        print(text)


def truncate_for_context(text: str, max_tokens: int = 4000) -> str:
    """Estimate tokens and truncate if needed (~4 chars per token)."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n... (truncated at ~{max_tokens} tokens, {len(text)} total chars)"


def format_usage_stats(stats: Dict[str, Any]) -> str:
    """Format usage stats for display."""
    lines = [
        f"Today:  {stats.get('today', {}).get('queries', 0)} queries, {stats.get('today', {}).get('tokens', 0)} tokens",
        f"Week:   {stats.get('week', {}).get('queries', 0)} queries, {stats.get('week', {}).get('tokens', 0)} tokens",
        f"Total:  {stats.get('total', {}).get('queries', 0)} queries, {stats.get('total', {}).get('tokens', 0)} tokens",
    ]
    return "\n".join(lines)
