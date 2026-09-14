"""Built-in bug reporting — CLI, programmatic, and crash handler."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import platform
import sys
import time
import traceback
import urllib.parse
from pathlib import Path

import httpx

from adk import __version__

logger = logging.getLogger("adk.bugreport")

_GATEWAY_URL = "https://gateway.aitherium.com"
_GITHUB_REPO = "Aitherium/awdk"
_REPORTS_DIR = Path.home() / ".aither" / "bug_reports.jsonl"


def _collect_system_info() -> dict:
    """Collect system info for bug reports (NO secrets, NO prompts)."""
    gpu_info = "unknown"
    try:
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            gpu_info = result.stdout.strip()
    except Exception:
        pass

    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "python_version": platform.python_version(),
        "adk_version": __version__,
        "architecture": platform.machine(),
        "gpu": gpu_info,
    }


def _collect_error_info() -> dict | None:
    """Collect last exception info if available."""
    exc_type, exc_value, exc_tb = sys.exc_info()
    if exc_type is None:
        return None
    return {
        "type": exc_type.__name__,
        "message": str(exc_value),
        "traceback": traceback.format_exception(exc_type, exc_value, exc_tb)[-3:],
    }


def build_report(
    description: str,
    agent_name: str = "",
    llm_backend: str = "",
    include_logs: bool = True,
) -> dict:
    """Build a bug report payload (inspectable — no secrets)."""
    report = {
        "description": description,
        "system": _collect_system_info(),
        "agent_name": agent_name,
        "llm_backend": llm_backend,
        "timestamp": time.time(),
    }

    if include_logs:
        error = _collect_error_info()
        if error:
            report["last_error"] = error

    return report


async def submit_bug_report(
    description: str,
    agent_name: str = "",
    llm_backend: str = "",
    include_logs: bool = True,
    dry_run: bool = False,
) -> dict:
    """Submit a bug report to the gateway and save locally.

    Returns: {"submitted": bool, "local_path": str, "github_url": str | None}
    """
    report = build_report(description, agent_name, llm_backend, include_logs)

    if dry_run:
        return {"submitted": False, "report": report, "message": "Dry run — nothing sent"}

    # Always save locally
    _save_local(report)

    # Try gateway
    submitted = False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{_GATEWAY_URL}/v1/bugs", json=report)
            submitted = resp.status_code < 300
    except Exception as e:
        logger.debug(f"Gateway submission failed: {e}")

    # Build GitHub issue URL as fallback
    github_url = _build_github_url(description, report)

    return {
        "submitted": submitted,
        "local_path": str(_REPORTS_DIR),
        "github_url": github_url,
    }


def _save_local(report: dict):
    """Save report to local JSONL file."""
    _REPORTS_DIR.parent.mkdir(parents=True, exist_ok=True)
    with open(_REPORTS_DIR, "a") as f:
        f.write(json.dumps(report) + "\n")


def _build_github_url(description: str, report: dict) -> str:
    """Build a pre-filled GitHub issue URL."""
    sys_info = report.get("system", {})
    body = (
        f"## Bug Report\n\n"
        f"{description}\n\n"
        f"## System\n"
        f"- OS: {sys_info.get('os', '?')} {sys_info.get('os_version', '')}\n"
        f"- Python: {sys_info.get('python_version', '?')}\n"
        f"- ADK: {sys_info.get('adk_version', '?')}\n"
        f"- GPU: {sys_info.get('gpu', 'unknown')}\n"
        f"- Backend: {report.get('llm_backend', '?')}\n"
    )
    error = report.get("last_error")
    if error:
        body += f"\n## Error\n```\n{error['type']}: {error['message']}\n```\n"

    params = urllib.parse.urlencode({
        "title": f"[ADK Bug] {description[:80]}",
        "body": body,
        "labels": "bug,adk",
    })
    return f"https://github.com/{_GITHUB_REPO}/issues/new?{params}"


def main():
    """CLI entry point: aither-bug"""
    parser = argparse.ArgumentParser(description="AitherADK Bug Reporter")
    parser.add_argument("description", nargs="?", help="Bug description")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent without sending")
    parser.add_argument("--no-logs", action="store_true", help="Don't include error logs")
    args = parser.parse_args()

    if not args.description:
        print("AitherADK Bug Reporter")
        print("=" * 40)
        args.description = input("Describe the bug: ").strip()
        if not args.description:
            print("No description provided. Aborting.")
            return

    result = asyncio.run(submit_bug_report(
        description=args.description,
        include_logs=not args.no_logs,
        dry_run=args.dry_run,
    ))

    if args.dry_run:
        print("\nDry run — this is what would be sent:")
        print(json.dumps(result.get("report", {}), indent=2))
        return

    if result.get("submitted"):
        print("Bug report submitted successfully!")
    else:
        print("Could not reach gateway. Report saved locally.")

    print(f"Local copy: {result.get('local_path')}")
    if result.get("github_url"):
        print(f"Or file on GitHub: {result['github_url']}")


if __name__ == "__main__":
    main()
