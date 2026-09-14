"""
Deploy Plugin for AitherShell
===============================

Self-service AitherOS deployment: authenticate, pull images from GHCR,
generate compose + .env, boot services.

Usage:
    /deploy [--profile PROFILE] [--version TAG] [--gpu auto|none] [--dry-run]
    /deploy status       — Show deployed services + versions + health
    /deploy update       — Pull newer images, rolling restart
    /deploy stop         — docker compose down
    /deploy logs [svc]   — Tail logs
    /deploy export       — Offline bundle tarball
    /deploy profiles     — List available profiles
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand


class DeployPlugin(SlashCommand):
    name: str = "deploy"
    aliases: List[str] = ["deployment"]
    description: str = "Deploy AitherOS locally via Docker"
    category: str = "system"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='deploy',
            description='Deploy AitherOS locally via Docker',
            aliases=['deployment'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return await self._deploy_interactive(args, ctx)

        sub = args[0].lower()
        dispatch = {
            "status": self._status,
            "update": self._update,
            "stop": self._stop,
            "logs": self._logs,
            "export": self._export,
            "profiles": self._profiles,
            "help": self._help,
        }

        handler = dispatch.get(sub)
        if handler:
            return await handler(args[1:], ctx)

        # Treat as deploy with flags
        return await self._deploy_interactive(args, ctx)

    async def _deploy_interactive(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Main deploy flow with flag parsing."""
        from adk.shell.deployer import Deployer, DeployState
        from adk.shell.registry_auth import RegistryAuth

        # Parse flags
        profile_name = "chat-minimal"
        version = "latest"
        gpu_mode = "auto"
        dry_run = False
        data_dir = None
        offline_path = None

        i = 0
        while i < len(args):
            if args[i] in ("--profile", "-p") and i + 1 < len(args):
                profile_name = args[i + 1]
                i += 2
            elif args[i] in ("--version", "-v") and i + 1 < len(args):
                version = args[i + 1]
                i += 2
            elif args[i] == "--gpu" and i + 1 < len(args):
                gpu_mode = args[i + 1]
                i += 2
            elif args[i] == "--data-dir" and i + 1 < len(args):
                data_dir = Path(args[i + 1])
                i += 2
            elif args[i] == "--offline" and i + 1 < len(args):
                offline_path = Path(args[i + 1])
                i += 2
            elif args[i] == "--dry-run":
                dry_run = True
                i += 1
            else:
                # Could be profile name without flag
                if not args[i].startswith("-"):
                    profile_name = args[i]
                i += 1

        # Auto-resolve "personal" to the right sub-profile based on hardware
        if profile_name == "personal":
            from adk.shell.deployer import _detect_gpu
            gpu_name, vram_gb = _detect_gpu()
            if gpu_name and vram_gb >= 8:
                profile_name = "personal-gpu"
                print(f"  Detected {gpu_name} ({vram_gb:.0f}GB) -> using vLLM (best performance)")
            elif gpu_name and vram_gb >= 6:
                profile_name = "personal-ollama"
                print(f"  Detected {gpu_name} ({vram_gb:.0f}GB) -> using Ollama GPU")
            else:
                profile_name = "personal-cpu"
                print("  No compatible GPU detected -> using CPU inference (32GB+ RAM required)")

        deployer = Deployer(
            version=version,
            gpu_mode=gpu_mode,
            data_dir=data_dir,
        )

        # Offline import
        if offline_path:
            return await self._import_offline(deployer, offline_path)

        # Auth check
        try:
            from adk.shell.auth import AuthStore
            token = AuthStore.get_active_token()
            if token:
                auth = RegistryAuth()
                await auth.ensure_authenticated(token)
        except ImportError:
            pass

        # Run deploy with crash reporting
        from adk.shell.crash_reporter import set_current_command, handle_crash

        set_current_command(f"deploy --profile {profile_name}")

        lines: List[str] = []

        def progress(phase: str, detail: str = ""):
            if phase == "pulling":
                return  # Too noisy per-image
            lines.append(f"  [{phase}] {detail}")

        try:
            result = await deployer.deploy(
                profile_name=profile_name,
                dry_run=dry_run,
                progress_callback=progress,
            )
        except Exception as exc:
            handle_crash(exc, source="deploy")
            return f"Deployment failed: {exc}"

        # Format output
        output_lines = [f"\n**AitherOS Deploy** — {profile_name}\n"]
        output_lines.extend(lines)
        output_lines.append("")

        if result["status"] == "dry_run":
            output_lines.append(f"**DRY RUN** — Would pull {result['image_count']} images:")
            for img in result.get("images", []):
                output_lines.append(f"  - {img}")
            return "\n".join(output_lines)

        if result["status"] == "failed":
            output_lines.append("**FAILED**")
            for err in result.get("errors", []):
                output_lines.append(f"  ERROR: {err}")
            for warn in result.get("warnings", []):
                output_lines.append(f"  WARN: {warn}")
            return "\n".join(output_lines)

        # Success / partial
        health = result.get("health", {})
        output_lines.append("**Services:**")
        for svc, healthy in health.items():
            status = "healthy" if healthy else "unhealthy"
            output_lines.append(f"  {svc}: {status}")

        if result.get("warnings"):
            output_lines.append("\n**Warnings:**")
            for w in result["warnings"]:
                output_lines.append(f"  - {w}")

        output_lines.append(f"\n**Status:** {result['status']}")
        output_lines.append(f"**Images pulled:** {result.get('images_pulled', 0)}")
        output_lines.append("\n**Access:**")
        output_lines.append("  Genesis API:  http://localhost:8001")
        output_lines.append("  Dashboard:    http://localhost:3000")
        output_lines.append("  AitherShell:  Just type! (already connected)")

        return "\n".join(output_lines)

    async def _status(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Show deployment status."""
        from adk.shell.deployer import DeployState

        state = DeployState.load()
        if not state:
            return "No active deployment. Run `/deploy --profile chat-minimal` to get started."

        lines = [
            f"\n**AitherOS Deployment**",
            f"  Profile:  {state.profile}",
            f"  Version:  {state.version}",
            f"  Deployed: {state.deployed_at}",
            f"  Compose:  {state.compose_path}",
            f"  Services: {', '.join(state.services)}",
        ]

        # Live health check
        try:
            from adk.shell.deployer import Deployer
            deployer = Deployer(version=state.version)
            health = await deployer.verify(timeout=10)
            lines.append("\n**Health:**")
            for svc, ok in health.items():
                lines.append(f"  {svc}: {'healthy' if ok else 'unhealthy'}")
        except Exception:
            lines.append("\n  (Could not check live health)")

        return "\n".join(lines)

    async def _update(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Pull newer images and restart."""
        from adk.shell.deployer import Deployer, DeployState

        state = DeployState.load()
        if not state:
            return "No active deployment. Run `/deploy` first."

        version = args[0] if args else "latest"
        deployer = Deployer(version=version)

        lines: List[str] = [f"\n**Updating** {state.profile} → {version}...\n"]

        def progress(phase: str, detail: str = ""):
            if phase not in ("pulling",):
                lines.append(f"  [{phase}] {detail}")

        result = await deployer.update(progress_callback=progress)
        lines.append(f"\n**Result:** {result['status']}")
        lines.append(f"  Images updated: {result.get('images_updated', 0)}")

        return "\n".join(lines)

    async def _stop(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Stop deployment."""
        from adk.shell.deployer import Deployer

        deployer = Deployer()
        ok = await deployer.stop()
        if ok:
            return "AitherOS stopped. Run `/deploy` to start again."
        return "Failed to stop (no active deployment or compose file missing)."

    async def _logs(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Tail service logs."""
        from adk.shell.deployer import DeployState

        state = DeployState.load()
        if not state:
            return "No active deployment."

        compose_path = state.compose_path
        svc = args[0] if args else ""

        cmd = ["docker", "compose", "-f", compose_path, "logs", "--tail=50"]
        if svc:
            cmd.append(svc)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()[-3000:]  # Last 3000 chars
        return f"```\n{output}\n```"

    async def _export(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Export offline bundle."""
        from adk.shell.deployer import Deployer

        profile_name = "chat-minimal"
        output_path = Path.home() / "aitheros-bundle"

        i = 0
        while i < len(args):
            if args[i] in ("--profile", "-p") and i + 1 < len(args):
                profile_name = args[i + 1]
                i += 2
            elif args[i] in ("-o", "--output") and i + 1 < len(args):
                output_path = Path(args[i + 1])
                i += 2
            else:
                i += 1

        deployer = Deployer()
        lines: List[str] = [f"\n**Exporting** {profile_name} bundle...\n"]

        def progress(phase: str, detail: str = ""):
            lines.append(f"  [{phase}] {detail}")

        ok = await deployer.export_bundle(profile_name, output_path, progress_callback=progress)
        if ok:
            lines.append(f"\nBundle saved to: {output_path}.tar.gz")
            lines.append("Transfer to air-gapped machine and run:")
            lines.append(f"  aither deploy --offline {output_path}.tar.gz")
        else:
            lines.append("\nExport failed. Check Docker is running and images are pulled.")

        return "\n".join(lines)

    async def _profiles(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """List available deployment profiles."""
        from adk.shell.deployer import Deployer

        deployer = Deployer()
        profiles = deployer.list_profiles()

        lines = ["\n**Available Deployment Profiles**\n"]
        personal_lines: List[str] = []
        for p in profiles:
            gpu = f"GPU: {p.min_vram_gb}GB VRAM" if p.gpu_required else "No GPU required"
            entry = [
                f"  **{p.name}** — {p.description}",
                f"    RAM: {p.min_ram_gb}GB | Disk: {p.min_disk_gb}GB | {gpu}",
                f"    Containers: ~{p.containers_approx} | Layers: {', '.join(p.layers) or 'none'}",
                "",
            ]
            if p.name.startswith("personal-"):
                personal_lines.extend(entry)
            else:
                lines.extend(entry)

        if personal_lines:
            lines.append("**Personal Agent ($20/mo)**\n")
            lines.extend(personal_lines)
            lines.append("  Tip: `/deploy --profile personal` auto-detects your hardware.\n")

        lines.append("Deploy: `/deploy --profile <name>`")
        return "\n".join(lines)

    async def _import_offline(self, deployer, bundle_path: Path) -> str:
        """Import offline bundle."""
        if not bundle_path.exists():
            return f"Bundle not found: {bundle_path}"

        lines: List[str] = [f"\n**Importing** {bundle_path.name}...\n"]

        def progress(phase: str, detail: str = ""):
            lines.append(f"  [{phase}] {detail}")

        ok = await deployer.import_bundle(bundle_path, progress_callback=progress)
        if ok:
            lines.append("\nOffline import complete. Services starting...")
            lines.append("  Genesis API:  http://localhost:8001")
            lines.append("  Dashboard:    http://localhost:3000")
        else:
            lines.append("\nImport failed.")

        return "\n".join(lines)

    async def _help(self, args: List[str], ctx: Dict[str, Any]) -> str:
        return """**AitherOS Deploy**

Usage:
  `/deploy [--profile PROFILE] [--version TAG] [--gpu auto|none] [--dry-run]`
  `/deploy status`       — Show deployed services + health
  `/deploy update`       — Pull newer images, rolling restart
  `/deploy stop`         — Stop all services
  `/deploy logs [svc]`   — Tail service logs
  `/deploy export`       — Create offline bundle
  `/deploy profiles`     — List available profiles

Profiles: chat-minimal, chat-full, chat-agents, full, dev, inference-only, personal

Examples:
  `/deploy --profile chat-full`
  `/deploy --profile chat-minimal --dry-run`
  `/deploy update latest`
  `/deploy --offline bundle.tar.gz`"""
