"""
Sync Plugin for AitherShell — /sync (AitherDrive)
===================================================

Bidirectional file sync between local directories and AitherOS platform.

Usage:
    /sync                  Show sync status
    /sync init [dir]       Initialize a sync root
    /sync status           Detailed file-level status
    /sync push             Push local changes to platform
    /sync pull             Pull remote changes
    /sync watch            Start background file watcher
    /sync stop             Stop background watcher
    /sync config           Show sync configuration

Aliases: /drive
"""

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand


class SyncPlugin(SlashCommand):
    name = "sync"
    description = "Sync local directory with AitherOS platform (AitherDrive)"
    aliases = ["drive"]

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='sync',
            description='Sync local directory with AitherOS platform (AitherDrive)',
            aliases=['drive'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        action = args[0] if args else "status"

        try:
            from adk.sync import SyncManager, SyncManifest, MANIFEST_FILE
            from adk.client.services.strata import StrataClient
            from adk.client.services.data_plane import DataPlaneClient
        except ImportError:
            return "adk.sync not available. Install awdk: pip install awdk"

        config = ctx.get("config", {})
        if hasattr(config, "__dict__"):
            config = config.__dict__

        tenant_id = (
            config.get("tenant_id", "")
            or os.environ.get("AITHER_TENANT_ID", "")
        )
        token = (
            config.get("access_token", "")
            or config.get("api_key", "")
            or os.environ.get("AITHER_API_KEY", "")
        )

        strata_url = config.get("strata_url", os.environ.get(
            "AITHER_STRATA_URL", "http://localhost:8136"))
        dp_url = config.get("data_plane_url", os.environ.get(
            "AITHER_DATAPLANE_URL", "http://localhost:8170"))

        import httpx
        headers: Dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if tenant_id:
            headers["X-Tenant-ID"] = tenant_id

        async with httpx.AsyncClient(timeout=60.0, headers=headers) as http:
            async def get_client():
                return http
            strata = StrataClient(strata_url, get_client)
            data_plane = DataPlaneClient(dp_url, get_client)

            if action == "init":
                if not tenant_id:
                    return "Not logged in. Run `adk login` first."
                target = Path(args[1] if len(args) > 1 else ".").resolve()
                if not target.is_dir():
                    return f"Directory not found: {target}"
                mgr = SyncManager(target, strata, data_plane, tenant_id)
                result = await mgr.init()
                if result.get("status") == "initialized":
                    return (
                        f"Sync root initialized at {target}\n"
                        f"  Node ID:   {result['node_id']}\n"
                        f"  Source ID: {result.get('source_id', 'n/a')}\n"
                        f"  Files:     {result['files_scanned']}\n"
                        f"\nRun /sync push to upload or /sync watch to auto-sync."
                    )
                return f"Already initialized (node: {result.get('node_id', '?')})"

            # Find sync root
            sync_dir = Path(".").resolve()
            for candidate in [sync_dir] + list(sync_dir.parents):
                if (candidate / MANIFEST_FILE).exists():
                    sync_dir = candidate
                    break
            else:
                return "Not a sync root. Run /sync init first."

            manifest = SyncManifest(sync_dir)
            manifest.load()
            mgr = SyncManager(
                sync_dir, strata, data_plane,
                tenant_id or manifest.tenant_id, manifest.node_id,
            )
            mgr.manifest = manifest

            if action == "status":
                st = mgr.status()
                lines = [
                    f"Sync root: {sync_dir}",
                    f"Node:      {manifest.node_id}",
                    f"Last sync: {manifest.last_sync_at or 'never'}",
                    f"Status:    {st.summary()}",
                ]
                for f in st.new[:5]:
                    lines.append(f"  + {f}")
                for f in st.changed[:5]:
                    lines.append(f"  ~ {f}")
                for f in st.deleted[:5]:
                    lines.append(f"  - {f}")
                return "\n".join(lines)

            elif action == "push":
                result = await mgr.push()
                return (
                    f"Uploaded: {result['uploaded']}  "
                    f"Deleted: {result['deleted']}  "
                    f"Errors: {len(result['errors'])}"
                )

            elif action == "pull":
                result = await mgr.pull()
                msg = f"Downloaded: {result['downloaded']}"
                if result.get("errors"):
                    msg += f"  Errors: {len(result['errors'])}"
                return msg

            elif action == "watch":
                started = await mgr.watch()
                if not started:
                    return "watchdog not installed. Run: pip install awdk[sync]"
                # Store watcher reference in ctx for /sync stop
                ctx["_sync_watcher"] = mgr
                return f"Watching {sync_dir} for changes. Use /sync stop to stop."

            elif action == "stop":
                watcher = ctx.get("_sync_watcher")
                if watcher:
                    watcher.stop()
                    ctx.pop("_sync_watcher", None)
                    return "Watcher stopped."
                return "No active watcher."

            elif action == "config":
                return (
                    f"Sync root:     {sync_dir}\n"
                    f"Node ID:       {manifest.node_id}\n"
                    f"Tenant ID:     {manifest.tenant_id}\n"
                    f"Source ID:     {manifest.source_id}\n"
                    f"Strata prefix: {manifest.strata_prefix}\n"
                    f"Conflict:      {manifest.conflict_strategy}\n"
                    f"Tracked files: {len(manifest.files)}\n"
                    f"Ignore:        {', '.join(manifest.ignore)}"
                )

            else:
                return (
                    "Usage: /sync [init|status|push|pull|watch|stop|config]\n"
                    "  init [dir]  Initialize sync root\n"
                    "  status      Show changed/new/deleted files\n"
                    "  push        Upload local changes\n"
                    "  pull        Download remote changes\n"
                    "  watch       Auto-sync on file changes\n"
                    "  stop        Stop background watcher\n"
                    "  config      Show sync configuration"
                )
