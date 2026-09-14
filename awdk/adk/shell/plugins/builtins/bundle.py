"""
Bundle Plugin for AitherShell
==============================

Customer-side companion to the Genesis /api/tenants/{slug}/bundle.tar.gz
endpoint. Lets a tenant operator pull, inspect, and install a sovereign
deployment bundle for their workspace in one slash command.

Usage:
    /bundle                          — List available local bundles + last-used
    /bundle info <tenant>            — Manifest probe (cheap, no download)
    /bundle pull <tenant>            — Download + extract to ./aitheros-<tenant>
    /bundle install <tenant>         — Pull + run install.sh / install.ps1
    /bundle install <path-or-url>    — Install from local .tar.gz or URL

Flags:
    --profile NAME       chat-minimal | chat-full | chat-agents | brain | mesh
    --no-playbooks       Skip AitherZero playbooks
    --include-models     Include model weights manifest (still references only)
    --license-days N     Sovereign license duration (default 365)
    --target PATH        Extraction directory (default ./aitheros-<tenant>)
    --json               JSON output

Auth:
    Uses the same Genesis client auth as `/connect`. Falls back to
    $AITHER_TOKEN env var.

Aliases: /pkg, /sovereign
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand


def _genesis_base(ctx: Dict[str, Any]) -> str:
    client = ctx.get("client")
    if client is not None:
        url = getattr(client, "base_url", None) or getattr(client, "url", None)
        if url:
            return str(url).rstrip("/")
    return os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001").rstrip("/")


def _auth_headers(ctx: Dict[str, Any]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    token = os.environ.get("AITHER_TOKEN") or os.environ.get("AITHER_PORTAL_TOKEN")
    if not token:
        portal = Path.home() / ".aither" / "portal.token"
        if portal.is_file():
            try:
                token = portal.read_text(encoding="utf-8").strip()
            except Exception:
                pass
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _http_get(url: str, headers: Dict[str, str], stream_to: Optional[Path] = None) -> Any:
    """GET helper using httpx if available, urllib otherwise."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as cli:
            if stream_to:
                async with cli.stream("GET", url, headers=headers) as r:
                    r.raise_for_status()
                    with stream_to.open("wb") as fh:
                        async for chunk in r.aiter_bytes(chunk_size=65536):
                            fh.write(chunk)
                    return {"status": r.status_code, "headers": dict(r.headers)}
            r = await cli.get(url, headers=headers)
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            if "application/json" in ct:
                return r.json()
            return r.text
    except ImportError:
        # urllib fallback (sync, wrapped)
        from urllib.request import Request, urlopen
        req = Request(url, headers=headers)

        def _do() -> Any:
            with urlopen(req, timeout=300) as resp:
                if stream_to:
                    with stream_to.open("wb") as fh:
                        shutil.copyfileobj(resp, fh)
                    return {"status": resp.status, "headers": dict(resp.headers)}
                data = resp.read()
                ct = resp.headers.get("Content-Type", "")
                if "application/json" in ct:
                    return json.loads(data.decode("utf-8"))
                return data.decode("utf-8", errors="replace")

        return await asyncio.to_thread(_do)


def _summarize_manifest(m: Dict[str, Any]) -> str:
    sm = m.get("model_manifest_summary", {})
    return (
        f"tenant={m.get('tenant_slug')} profile={m.get('profile')}\n"
        f"  agents:         {m.get('agent_count', 0)}\n"
        f"  playbooks:      {m.get('playbook_count', 0)}\n"
        f"  vllm workers:   {sm.get('vllm_workers', 0)}\n"
        f"  vllm backends:  {sm.get('vllm_backends', 0)}\n"
        f"  ollama models:  {sm.get('ollama_models', 0)}\n"
        f"  license signed: {m.get('license_signed', False)}\n"
        f"  files:          {len(m.get('files', {}))}\n"
    )


@dataclass
class BundlePlugin(SlashCommand):
    name: str = "bundle"
    description: str = "Pull / inspect / install a sovereign deployment bundle"
    aliases: List[str] = field(default_factory=lambda: ["pkg", "sovereign"])
    category: str = "deploy"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='bundle',
            description='Pull / inspect / install a sovereign deployment bundle',
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self._list_local()

        sub = args[0].lower()
        rest = args[1:]
        dispatch = {
            "info": self._info,
            "pull": self._pull,
            "install": self._install,
            "help": self._help,
        }
        handler = dispatch.get(sub)
        if handler:
            return await handler(rest, ctx)
        # Unknown — treat as `info <tenant>`
        return await self._info([sub, *rest], ctx)

    # ───────────────────────── subcommands ──────────────────────────────

    def _list_local(self) -> str:
        cwd = Path.cwd()
        found = sorted(cwd.glob("aitheros-*"))
        lines = ["Local sovereign bundles in CWD:"]
        if not found:
            lines.append("  (none)")
        else:
            for p in found:
                bj = p / "bundle.json"
                tag = ""
                if bj.is_file():
                    try:
                        m = json.loads(bj.read_text(encoding="utf-8"))
                        tag = f"  ({m.get('profile')}, {m.get('agent_count', 0)} agents)"
                    except Exception:
                        pass
                lines.append(f"  {p.name}{tag}")
        lines.append("")
        lines.append("Usage: /bundle pull <tenant> | /bundle install <tenant>")
        return "\n".join(lines)

    async def _info(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /bundle info <tenant> [--profile NAME]"
        tenant, opts = self._parse(args)
        url = f"{_genesis_base(ctx)}/api/tenants/{tenant}/bundle/manifest?{opts['_query']}"
        try:
            data = await _http_get(url, _auth_headers(ctx))
        except Exception as e:
            return f"Failed to fetch manifest: {e}"
        if opts.get("json"):
            return json.dumps(data, indent=2)
        return _summarize_manifest(data) if isinstance(data, dict) else str(data)

    async def _pull(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /bundle pull <tenant> [--profile NAME] [--target PATH]"
        tenant, opts = self._parse(args)
        target = Path(opts.get("target") or f"./aitheros-{tenant}").resolve()
        tarball = target.parent / f"{target.name}.tar.gz"
        target.parent.mkdir(parents=True, exist_ok=True)

        url = f"{_genesis_base(ctx)}/api/tenants/{tenant}/bundle.tar.gz?{opts['_query']}"
        print(f"==> Downloading {url} → {tarball}")
        try:
            await _http_get(url, _auth_headers(ctx), stream_to=tarball)
        except Exception as e:
            return f"Download failed: {e}"

        print(f"==> Extracting → {target}")
        target.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(tarball, "r:gz") as tf:
                # Strip top-level component to match the installer
                members = tf.getmembers()
                top = None
                for m in members:
                    parts = m.name.split("/", 1)
                    if not top:
                        top = parts[0]
                    if len(parts) == 2:
                        m.name = parts[1]
                        tf.extract(m, target)
        except Exception as e:
            return f"Extract failed: {e}"

        tarball.unlink(missing_ok=True)
        result = {
            "tenant": tenant,
            "target": str(target),
            "next_step": (
                f"pwsh -File {target}/install.ps1"
                if platform.system() == "Windows"
                else f"bash {target}/install.sh"
            ),
        }
        if opts.get("json"):
            return json.dumps(result, indent=2)
        return (
            f"==> Bundle extracted to {target}\n"
            f"    Next: {result['next_step']}\n"
            f"    Or:   /bundle install {tenant}"
        )

    async def _install(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /bundle install <tenant|path|url>"
        first = args[0]
        # Local path
        p = Path(first)
        if p.is_file() and first.endswith(".tar.gz"):
            return await self._install_from_file(p, args[1:], ctx)
        if p.is_dir() and (p / "install.sh").is_file():
            return await self._run_installer(p, args[1:], ctx)
        if first.startswith(("http://", "https://")):
            return await self._install_from_url(first, args[1:], ctx)

        # Treat as tenant slug
        pull_out = await self._pull(args, ctx)
        # Parse tenant + target from args again
        tenant, opts = self._parse(args)
        target = Path(opts.get("target") or f"./aitheros-{tenant}").resolve()
        if not (target / "install.sh").is_file() and not (target / "install.ps1").is_file():
            return pull_out + "\n[!] No installer found after extract."
        run_out = await self._run_installer(target, [], ctx)
        return pull_out + "\n" + run_out

    async def _install_from_file(self, tar_path: Path, _: List[str], ctx: Dict[str, Any]) -> str:
        target = tar_path.with_suffix("").with_suffix("")  # strip .tar.gz
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar_path, "r:gz") as tf:
            for m in tf.getmembers():
                parts = m.name.split("/", 1)
                if len(parts) == 2:
                    m.name = parts[1]
                    tf.extract(m, target)
        return await self._run_installer(target, [], ctx)

    async def _install_from_url(self, url: str, _: List[str], ctx: Dict[str, Any]) -> str:
        tmp = Path.cwd() / "_bundle.tar.gz"
        await _http_get(url, _auth_headers(ctx), stream_to=tmp)
        out = await self._install_from_file(tmp, [], ctx)
        tmp.unlink(missing_ok=True)
        return out

    async def _run_installer(self, target: Path, _: List[str], __: Dict[str, Any]) -> str:
        if platform.system() == "Windows" and (target / "install.ps1").is_file():
            cmd = ["pwsh", "-File", str(target / "install.ps1")]
        elif (target / "install.sh").is_file():
            cmd = ["bash", str(target / "install.sh")]
        else:
            return f"[!] No installer in {target}"
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(target),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return out.decode("utf-8", errors="replace")

    def _help(self, _args: List[str], _ctx: Dict[str, Any]) -> str:
        return __doc__ or ""

    # ───────────────────────── helpers ──────────────────────────────────

    @staticmethod
    def _parse(args: List[str]) -> tuple[str, Dict[str, Any]]:
        """Return (tenant_slug, opts) where opts includes _query string."""
        tenant = ""
        opts: Dict[str, Any] = {
            "profile": "chat-minimal",
            "include_playbooks": True,
            "include_models": False,
            "license_days": 365,
        }
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("--profile", "-p") and i + 1 < len(args):
                opts["profile"] = args[i + 1]; i += 2; continue
            if a == "--no-playbooks":
                opts["include_playbooks"] = False; i += 1; continue
            if a == "--include-models":
                opts["include_models"] = True; i += 1; continue
            if a == "--license-days" and i + 1 < len(args):
                opts["license_days"] = int(args[i + 1]); i += 2; continue
            if a == "--target" and i + 1 < len(args):
                opts["target"] = args[i + 1]; i += 2; continue
            if a == "--json":
                opts["json"] = True; i += 1; continue
            if not tenant and not a.startswith("-"):
                tenant = a
            i += 1

        opts["_query"] = (
            f"profile={opts['profile']}"
            f"&include_playbooks={'true' if opts['include_playbooks'] else 'false'}"
            f"&include_models={'true' if opts['include_models'] else 'false'}"
            f"&license_days={opts['license_days']}"
        )
        return tenant, opts
