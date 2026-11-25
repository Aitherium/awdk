"""antigravity toolpack — fail-soft tools for inspecting, spawning, steering,
and auditing Google Antigravity conversations from any awdk / awsh agent.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

PACK_ID = "antigravity"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_TOOL_NAMES = (
    "antigravity_sessions",
    "antigravity_spawn",
    "antigravity_steer",
    "antigravity_doctor",
    "antigravity_setup_status",
)


def _aither_home() -> Path:
    raw = os.environ.get("AITHER_HOME", "").strip()
    return Path(raw) if raw else Path.home() / ".aither"


def antigravity_sessions(max_age_seconds: float = 86400.0) -> dict[str, Any]:
    """List live Google Antigravity conversations recorded by antigravity_hooks."""
    try:
        from adk.harnesses.discovery import discover_antigravity_sessions

        rows = discover_antigravity_sessions(max_age_seconds=max_age_seconds)
        return {
            "ok": True,
            "count": len(rows),
            "sessions": [
                {
                    "id": s.id,
                    "title": s.name,
                    "cwd": s.cwd,
                    "status": s.status,
                    "harness": s.harness,
                    "transcript_path": s.transcript_path,
                }
                for s in rows
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "sessions": []}


def antigravity_spawn(
    prompt: str,
    model: str = "flash",
    title: str = "",
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Start a new Google Antigravity conversation via ``agentapi`` or ``agy``."""
    text = (prompt or "").strip()
    if not text:
        return {"ok": False, "error": "prompt is required"}
    try:
        from adk.harnesses import registry

        spec = registry.get("antigravity")
        binary = registry.resolve_binary(spec)
        if not binary:
            return {"ok": False, "error": f"Antigravity CLI not found ({spec.install_hint})"}
        launch = registry.LaunchSpec(prompt=text, model=model, title=title)
        argv = spec.argv(launch)
        argv[0] = binary
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "binary": binary,
            "output": out[:2000],
            **({"error": err[:500]} if proc.returncode != 0 else {}),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def antigravity_steer(
    conversation_id: str,
    message: str,
    *,
    mode: str = "mailbox",
    sender: str = "awdk-agent",
) -> dict[str, Any]:
    """Steer a live Antigravity conversation via ~/.aither/steer/<id>/ or agentapi send-message.

    ``mode="mailbox"`` (default): writes a framed peer steer markdown file drained on the
    conversation's next PreInvocation / Stop hook.
    ``mode="direct"``: invokes ``agentapi send-message <conversation_id> <message>``.
    """
    cid = (conversation_id or "").strip()
    text = (message or "").strip()
    if not cid or not _SAFE_ID_RE.match(cid):
        return {"ok": False, "error": f"invalid conversation_id: {conversation_id!r}"}
    if not text:
        return {"ok": False, "error": "message is required"}

    if mode == "direct":
        try:
            from adk.harnesses import registry

            spec = registry.get("antigravity")
            binary = registry.resolve_binary(spec)
            if not binary:
                return {"ok": False, "error": "agentapi not found; use mode='mailbox'"}
            proc = subprocess.run(
                [binary, "send-message", cid, text],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            return {
                "ok": proc.returncode == 0,
                "mode": "direct",
                "conversation_id": cid,
                "output": (proc.stdout or "").strip()[:1000],
                **({"error": (proc.stderr or "").strip()[:400]} if proc.returncode != 0 else {}),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    try:
        box = _aither_home() / "steer" / cid
        box.mkdir(parents=True, exist_ok=True)
        safe_sender = re.sub(r"[^A-Za-z0-9 ._:@/-]", "", sender or "")[:80] or "awdk-agent"
        fname = f"{int(time.time() * 1000)}-antigravity-peer.md"
        target = box / fname
        body = (
            f'<!-- aither-steer v1 authority="peer" from="{safe_sender}" kind="antigravity" -->\n\n'
            f"{text}\n"
        )
        target.write_text(body, encoding="utf-8")
        return {"ok": True, "mode": "mailbox", "conversation_id": cid, "path": str(target)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def antigravity_doctor(config_dir: str = "", offline: bool = False) -> dict[str, Any]:
    """Run ``adk antigravity doctor`` (AGD001-AGD006) and return structured findings."""
    try:
        from adk.antigravity_setup import doctor_antigravity

        cfg: Optional[Path] = Path(config_dir) if config_dir else None
        rep = doctor_antigravity(cfg, run_live=not offline)
        return {
            "ok": rep.exit_code == 0,
            "exit_code": rep.exit_code,
            "notes": rep.notes,
            "findings": [f.__dict__ for f in rep.findings],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": 2, "error": str(exc)}


def antigravity_setup_status(config_dir: str = "", plugin_name: str = "aitheros") -> dict[str, Any]:
    """Report whether the Antigravity AitherOS/awdk plugin and bundled antigravity_mod exist."""
    try:
        from adk.antigravity_setup import default_config_dir

        cfg = Path(config_dir) if config_dir else default_config_dir()
        pdir = cfg / "plugins" / plugin_name
        mod_dir = Path(__file__).resolve().parents[2] / "harnesses" / "antigravity_mod"
        return {
            "ok": True,
            "config_dir": str(cfg),
            "plugin_dir": str(pdir),
            "plugin_installed": (pdir / "plugin.json").is_file(),
            "hooks_installed": (pdir / "hooks.json").is_file(),
            "mcp_installed": (pdir / "mcp_config.json").is_file(),
            "bundled_mod_dir": str(mod_dir),
            "bundled_mod_present": (mod_dir / "plugin.json").is_file() and (mod_dir / "assets" / "logo.svg").is_file(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
