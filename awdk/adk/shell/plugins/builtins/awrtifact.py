"""
awrtifact Plugin for AitherShell
=================================

The artifact release store: chunk artifacts into GitHub release assets and
fetch them back byte-verified. Wraps the Genesis router (read/verify/fetch)
and shells to the `awrtifact` CLI when installed (mirror/upload).

Usage:
    /awrtifact specs                 # what the store holds
    /awrtifact spec NAME             # one artifact's entry
    /awrtifact verify NAME           # live mirror probe
    /awrtifact url NAME              # mint the public fetch URL
    /awrtifact mirror URL --release TAG   # feed it a link (needs the CLI)
    /awrtifact help

Aliases: /artifacts
"""

import json
import os
from typing import Any, Dict, List, Optional

from adk._tls import tls_verify
from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _genesis_url() -> str:
    return os.environ.get("AITHER_GENESIS_URL", "http://localhost:8100")


def _api_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


async def _get(path: str) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=20, verify=tls_verify()) as c:
        resp = await c.get(f"{_genesis_url()}{path}", headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


BASE = "/api/v1/awrtifact"


class AwrtifactPlugin(SlashCommand):
    name: str = "awrtifact"
    aliases: List[str] = ["artifacts"]
    description: str = "awrtifact — chunk artifacts into releases, fetch byte-verified"
    category: str = "utility"

    def __init__(self) -> None:
        # Explicit init — the dataclass base would shadow the class attr
        # with "" and register under the empty string (the commerce lesson).
        super().__init__(
            name="awrtifact",
            description="awrtifact — chunk artifacts into releases, fetch byte-verified",
            aliases=["artifacts"],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self.get_help()
        sub = args[0].lower()
        rest = args[1:]
        handlers = {
            "specs": self._specs,
            "spec": self._spec,
            "verify": self._verify,
            "url": self._url,
            "mirror": self._mirror,
            "help": lambda _: self.get_help(),
        }
        handler = handlers.get(sub)
        if handler:
            return await handler(rest)
        return f"Unknown subcommand: {sub}\n\n{self.get_help()}"

    async def _specs(self, _args: List[str]) -> str:
        data = await _get(f"{BASE}/specs")
        store = data.get("store", {})
        lines = [
            f"Store: {store.get('name', '?')} (repo {store.get('repo', '?')})",
            f"Mirror: {store.get('mirror_host', '?')}",
            "",
        ]
        for art in data.get("artifacts", []):
            lines.append(
                f"  {art['name']}  {art['total']:,} B  parts={art['parts']}  "
                f"release={art.get('release', '?')}"
            )
        return "\n".join(lines) if data.get("artifacts") else "Store is empty."

    async def _spec(self, args: List[str]) -> str:
        if not args:
            return "Usage: /awrtifact spec NAME"
        return json.dumps(await _get(f"{BASE}/specs/{args[0]}"), indent=2)

    async def _verify(self, args: List[str]) -> str:
        if not args:
            return "Usage: /awrtifact verify NAME"
        data = await _get(f"{BASE}/specs/{args[0]}/verify")
        lines = [f"verify {data.get('name', args[0])}: {'OK' if data.get('ok') else 'FAILED'}"]
        for check in data.get("checks", []):
            mark = "ok" if check.get("ok") else "FAIL"
            lines.append(f"  {check.get('check')}: {mark} — {check.get('detail', '')}")
        return "\n".join(lines)

    async def _url(self, args: List[str]) -> str:
        if not args:
            return "Usage: /awrtifact url NAME"
        return await self._post_fetch(args[0])

    async def _post_fetch(self, name: str) -> str:
        import httpx

        async with httpx.AsyncClient(timeout=20, verify=tls_verify()) as c:
            resp = await c.post(
                f"{_genesis_url()}{BASE}/fetch-url",
                json={"name": name},
                headers=_api_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        return data.get("url", json.dumps(data))

    async def _mirror(self, args: List[str]) -> str:
        """Feed it a URL or file — mirrors to GitHub. Needs the awrtifact CLI."""
        if not args:
            return "Usage: /awrtifact mirror URL|FILE --release TAG"
        release = ""
        source = None
        for i, arg in enumerate(args):
            if arg == "--release" and i + 1 < len(args):
                release = args[i + 1]
            elif not arg.startswith("--"):
                source = arg
        if not source or not release:
            return "Usage: /awrtifact mirror URL|FILE --release TAG"
        import shutil

        if not shutil.which("awrtifact"):
            return ("The `awrtifact` CLI is not installed here. "
                    "pip install -e AitherOS/packages/awrtifact[spec]")
        # asyncio, never subprocess.run: this is an async shell plugin and a
        # blocking child parks the whole shell loop.
        import asyncio

        proc = await asyncio.create_subprocess_exec(
            "awrtifact", "mirror", source, "--release", release,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        text = (out or b"").decode("utf-8", "replace").strip()
        if text:
            return text
        return (err or b"").decode("utf-8", "replace").strip() or "mirror failed"
