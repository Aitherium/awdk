"""
AitherOS Deployment Engine
============================

Pure Python deployment engine — no AitherOS-internal imports.
Handles prerequisites, image pulling, env generation, compose writing, and boot.

Used by the /deploy plugin and `aither deploy` CLI entry point.
"""

import asyncio
import json
import logging
import os
import platform
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# ─── Paths ────────────────────────────────────────────────────────────────────

AITHER_DIR = Path.home() / ".aither"
DEPLOY_DIR = AITHER_DIR / "deployment"
STATE_FILE = DEPLOY_DIR / "state.json"
GHCR_TOKEN_FILE = AITHER_DIR / "ghcr-token.json"

# ─── Data Loading ─────────────────────────────────────────────────────────────


def _data_dir() -> Path:
    """Resolve embedded data directory (works in both dev and Nuitka builds)."""
    # Nuitka sets __compiled__ and bundles data/ relative to the binary
    pkg_dir = Path(__file__).parent
    data = pkg_dir / "data"
    if data.is_dir():
        return data
    # Fallback: check AITHER_DATA_PATH env
    env_path = os.environ.get("AITHER_SHELL_DATA")
    if env_path and Path(env_path).is_dir():
        return Path(env_path)
    raise FileNotFoundError(f"Cannot find embedded data directory (checked {data})")


def load_image_map() -> Dict[str, Any]:
    return yaml.safe_load((_data_dir() / "image_map.yaml").read_text(encoding="utf-8"))


def load_profiles() -> Dict[str, Any]:
    return yaml.safe_load((_data_dir() / "deploy_profiles.yaml").read_text(encoding="utf-8"))


def load_env_template() -> str:
    return (_data_dir() / "env.template").read_text(encoding="utf-8")


# ─── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class DeployProfile:
    """Resolved deployment profile."""
    name: str
    description: str
    layers: List[str]
    specialized: List[str]
    infrastructure: List[str]
    min_ram_gb: int
    min_disk_gb: int
    gpu_required: bool
    min_vram_gb: int
    containers_approx: int
    compose_profiles: List[str]

    @classmethod
    def from_yaml(cls, name: str, data: Dict[str, Any]) -> "DeployProfile":
        return cls(
            name=name,
            description=data.get("description", ""),
            layers=data.get("layers", []),
            specialized=data.get("specialized", []),
            infrastructure=data.get("infrastructure", []),
            min_ram_gb=data.get("min_ram_gb", 8),
            min_disk_gb=data.get("min_disk_gb", 30),
            gpu_required=data.get("gpu_required", False),
            min_vram_gb=data.get("min_vram_gb", 0),
            containers_approx=data.get("containers_approx", 20),
            compose_profiles=data.get("compose_profiles", []),
        )


@dataclass
class PrereqResult:
    """Result of prerequisites check."""
    docker_ok: bool = False
    docker_version: str = ""
    ram_gb: float = 0.0
    disk_free_gb: float = 0.0
    gpu_name: Optional[str] = None
    gpu_vram_gb: float = 0.0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


@dataclass
class DeployState:
    """Persisted deployment state."""
    profile: str
    version: str
    deployed_at: str
    compose_path: str
    data_dir: str
    image_digests: Dict[str, str] = field(default_factory=dict)
    services: List[str] = field(default_factory=list)

    def save(self) -> None:
        DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(self.__dict__, indent=2, default=str), encoding="utf-8"
        )

    @classmethod
    def load(cls) -> Optional["DeployState"]:
        if not STATE_FILE.exists():
            return None
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return cls(**data)
        except (json.JSONDecodeError, TypeError):
            return None


# ─── Deployer Engine ──────────────────────────────────────────────────────────


class Deployer:
    """AitherOS deployment engine."""

    def __init__(
        self,
        version: str = "latest",
        registry: Optional[str] = None,
        data_dir: Optional[Path] = None,
        gpu_mode: str = "auto",
    ):
        self.version = version
        image_map = load_image_map()
        self.registry = registry or image_map.get("registry", "ghcr.io/aitherium")
        self.data_dir = data_dir or DEPLOY_DIR / "data"
        self.gpu_mode = gpu_mode
        self._image_map = image_map
        self._profiles = load_profiles()

    def get_profile(self, name: str) -> DeployProfile:
        """Look up a deployment profile by name."""
        profiles = self._profiles.get("profiles", {})
        if name not in profiles:
            available = ", ".join(profiles.keys())
            raise ValueError(f"Unknown profile '{name}'. Available: {available}")
        return DeployProfile.from_yaml(name, profiles[name])

    def list_profiles(self) -> List[DeployProfile]:
        """List all available profiles."""
        profiles = self._profiles.get("profiles", {})
        return [DeployProfile.from_yaml(k, v) for k, v in profiles.items()]

    # ─── Phase 1: Prerequisites ───────────────────────────────────────────

    async def check_prerequisites(self, profile: DeployProfile) -> PrereqResult:
        """Check Docker, disk, RAM, GPU."""
        result = PrereqResult()

        # Docker
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "version", "--format", "{{.Server.Version}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                result.docker_ok = True
                result.docker_version = stdout.decode().strip()
            else:
                result.errors.append("Docker is not running. Install or start Docker Desktop.")
        except FileNotFoundError:
            result.errors.append("Docker not found. Install Docker: https://docs.docker.com/get-docker/")

        # RAM
        result.ram_gb = _get_ram_gb()
        if result.ram_gb < profile.min_ram_gb:
            result.errors.append(
                f"Insufficient RAM: {result.ram_gb:.1f}GB available, "
                f"{profile.min_ram_gb}GB required for '{profile.name}'"
            )

        # Disk
        result.disk_free_gb = _get_disk_free_gb(self.data_dir)
        if result.disk_free_gb < profile.min_disk_gb:
            result.errors.append(
                f"Insufficient disk: {result.disk_free_gb:.1f}GB free, "
                f"{profile.min_disk_gb}GB required for '{profile.name}'"
            )

        # GPU
        if profile.gpu_required and self.gpu_mode != "none":
            gpu_name, vram = _detect_gpu()
            if gpu_name:
                result.gpu_name = gpu_name
                result.gpu_vram_gb = vram
                if vram < profile.min_vram_gb:
                    result.warnings.append(
                        f"GPU VRAM: {vram:.0f}GB available, "
                        f"{profile.min_vram_gb}GB recommended"
                    )
            else:
                if self.gpu_mode == "auto":
                    result.warnings.append(
                        "No NVIDIA GPU detected. LLM inference will use CPU (slow)."
                    )
                else:
                    result.errors.append(
                        "GPU required but not detected. Install NVIDIA drivers or use --gpu none."
                    )
        elif profile.gpu_required and self.gpu_mode == "none":
            result.warnings.append("GPU disabled. LLM inference will use CPU (slow).")

        return result

    # ─── Phase 2: Image Pull ──────────────────────────────────────────────

    def get_images_for_profile(self, profile: DeployProfile) -> List[str]:
        """Return list of full image references to pull for a profile."""
        images: List[str] = []

        # Layer images
        for layer_name in profile.layers:
            layer_data = self._image_map.get("layers", {}).get(layer_name)
            if layer_data:
                img = layer_data["image"]
                images.append(f"{self.registry}/{img}:{self.version}")

        # Specialized images
        specialized_map = self._image_map.get("specialized", {})
        for spec_name in profile.specialized:
            spec_data = specialized_map.get(spec_name)
            if spec_data:
                img = spec_data["image"]
                images.append(f"{self.registry}/{img}:{self.version}")

        return images

    async def pull_images(
        self,
        profile: DeployProfile,
        progress_callback=None,
    ) -> Tuple[List[str], List[str]]:
        """Pull all images for a profile. Returns (succeeded, failed)."""
        images = self.get_images_for_profile(profile)
        succeeded: List[str] = []
        failed: List[str] = []

        # Pull up to 3 in parallel
        sem = asyncio.Semaphore(3)

        async def _pull_one(image: str) -> bool:
            async with sem:
                if progress_callback:
                    progress_callback("pulling", image)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "docker", "pull", image,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()
                    if proc.returncode == 0:
                        succeeded.append(image)
                        if progress_callback:
                            progress_callback("pulled", image)
                        return True
                    else:
                        failed.append(image)
                        logger.warning("Failed to pull %s: %s", image, stderr.decode()[:200])
                        if progress_callback:
                            progress_callback("failed", image)
                        return False
                except Exception as e:
                    failed.append(image)
                    logger.error("Error pulling %s: %s", image, e)
                    return False

        await asyncio.gather(*[_pull_one(img) for img in images])

        # Re-tag layer images as individual service names
        for layer_name in profile.layers:
            layer_data = self._image_map.get("layers", {}).get(layer_name)
            if not layer_data:
                continue
            layer_image = f"{self.registry}/{layer_data['image']}:{self.version}"
            if layer_image not in succeeded:
                continue
            for svc in layer_data.get("services", []):
                await _retag(layer_image, f"{svc}:latest")

        return succeeded, failed

    # ─── Phase 3: Env Generation ──────────────────────────────────────────

    def generate_env(
        self,
        profile: DeployProfile,
        extra_vars: Optional[Dict[str, str]] = None,
    ) -> str:
        """Generate .env file content from template with auto-generated secrets."""
        template = load_env_template()

        # Replace auto-generate markers
        replacements = {
            "${AITHER_VERSION:-latest}": self.version,
            "${AITHER_REGISTRY:-ghcr.io/aitherium}": self.registry,
            "${AITHER_DATA_DIR:-~/.aither/data}": str(self.data_dir),
            "${AITHER_MASTER_KEY:-__AUTO_GENERATE__}": secrets.token_hex(32),
            "${AITHER_JWT_SECRET:-__AUTO_GENERATE__}": secrets.token_hex(32),
            "${POSTGRES_PASSWORD:-__AUTO_GENERATE__}": secrets.token_hex(16),
            "${AITHER_GPU_MODE:-auto}": self.gpu_mode,
        }

        content = template
        for marker, value in replacements.items():
            content = content.replace(marker, value)

        # Inject extra vars
        if extra_vars:
            content += "\n# ─── User-supplied ────────────────────────────────────────────────────────\n"
            for k, v in extra_vars.items():
                content += f"{k}={v}\n"

        return content

    # ─── Phase 4: Compose Generation ─────────────────────────────────────

    def generate_compose(self, profile: DeployProfile) -> str:
        """Generate a distribution docker-compose.yml for the profile."""
        # Check for pre-generated dist compose template
        template_path = _data_dir() / "compose_templates" / f"docker-compose.{profile.name}.yml"
        if template_path.exists():
            return template_path.read_text(encoding="utf-8")

        # Fallback: generate minimal compose from profile
        return self._build_compose(profile)

    def _build_compose(self, profile: DeployProfile) -> str:
        """Build a minimal compose file from profile definition."""
        services: Dict[str, Any] = {}

        # Infrastructure
        if "redis" in profile.infrastructure:
            services["redis"] = {
                "image": "redis:7-alpine",
                "ports": ["6379:6379"],
                "volumes": ["redis-data:/data"],
                "healthcheck": {
                    "test": ["CMD", "redis-cli", "ping"],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 3,
                },
            }

        if "postgres" in profile.infrastructure:
            services["postgres"] = {
                "image": "postgres:16-alpine",
                "environment": {
                    "POSTGRES_USER": "${POSTGRES_USER:-aither}",
                    "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
                    "POSTGRES_DB": "${POSTGRES_DB:-aitheros}",
                },
                "ports": ["5432:5432"],
                "volumes": ["postgres-data:/var/lib/postgresql/data"],
                "healthcheck": {
                    "test": ["CMD-SHELL", "pg_isready -U aither"],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 5,
                },
            }

        if "qdrant" in profile.infrastructure:
            services["qdrant"] = {
                "image": "qdrant/qdrant:latest",
                "ports": ["6333:6333"],
                "volumes": ["qdrant-data:/qdrant/storage"],
            }

        # Layer services (each layer image runs multiple services via AITHER_SERVICE env)
        for layer_name in profile.layers:
            layer_data = self._image_map.get("layers", {}).get(layer_name)
            if not layer_data:
                continue
            for svc_name in layer_data.get("services", []):
                short_name = svc_name.replace("aitheros-", "")
                services[short_name] = {
                    "image": f"${{AITHER_REGISTRY}}/{layer_data['image']}:${{AITHER_VERSION}}",
                    "environment": {
                        "AITHER_SERVICE": short_name,
                        "AITHER_DOCKER_MODE": "true",
                        "AITHER_MASTER_KEY": "${AITHER_MASTER_KEY}",
                    },
                    "restart": "unless-stopped",
                }

        # Specialized services
        specialized_map = self._image_map.get("specialized", {})
        for spec_name in profile.specialized:
            spec_data = specialized_map.get(spec_name)
            if not spec_data:
                continue
            svc_cfg: Dict[str, Any] = {
                "image": f"${{AITHER_REGISTRY}}/{spec_data['image']}:${{AITHER_VERSION}}",
                "environment": {
                    "AITHER_DOCKER_MODE": "true",
                    "AITHER_MASTER_KEY": "${AITHER_MASTER_KEY}",
                },
                "restart": "unless-stopped",
            }
            # Add port mappings for key services
            if spec_name == "genesis":
                svc_cfg["ports"] = ["${AITHER_GENESIS_PORT:-8001}:8001"]
                svc_cfg["depends_on"] = {"redis": {"condition": "service_healthy"}}
            elif spec_name == "veil":
                svc_cfg["ports"] = ["${AITHER_VEIL_PORT:-3000}:3000"]
            elif spec_name == "microscheduler":
                svc_cfg["ports"] = ["8150:8150"]
                if self.gpu_mode != "none":
                    svc_cfg["deploy"] = {
                        "resources": {
                            "reservations": {"devices": [{"capabilities": ["gpu"]}]}
                        }
                    }
            services[spec_name] = svc_cfg

        # Volumes
        volumes: Dict[str, Any] = {}
        if "redis" in profile.infrastructure:
            volumes["redis-data"] = {}
        if "postgres" in profile.infrastructure:
            volumes["postgres-data"] = {}
        if "qdrant" in profile.infrastructure:
            volumes["qdrant-data"] = {}

        compose = {
            "version": "3.8",
            "services": services,
            "volumes": volumes,
        }

        # Use yaml.dump for clean output
        return yaml.dump(compose, default_flow_style=False, sort_keys=False)

    # ─── Phase 5: Boot ────────────────────────────────────────────────────

    async def boot(self, compose_path: Path, env_path: Path) -> bool:
        """Start services via docker compose."""
        cmd = [
            "docker", "compose",
            "-f", str(compose_path),
            "--env-file", str(env_path),
            "up", "-d", "--no-build",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("Boot failed: %s", stderr.decode()[:500])
            return False
        return True

    # ─── Phase 6: Verify ──────────────────────────────────────────────────

    async def verify(self, timeout: float = 120.0) -> Dict[str, bool]:
        """Health check key services. Returns service→healthy map."""
        import httpx

        services_to_check = {
            "genesis": "http://localhost:8001/live",
            "veil": "http://localhost:3000",
            "microscheduler": "http://localhost:8150/health",
        }

        results: Dict[str, bool] = {}
        deadline = asyncio.get_event_loop().time() + timeout

        async with httpx.AsyncClient(timeout=5.0) as client:
            for name, url in services_to_check.items():
                healthy = False
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        resp = await client.get(url)
                        if resp.status_code < 500:
                            healthy = True
                            break
                    except (httpx.ConnectError, httpx.ReadTimeout):
                        pass
                    await asyncio.sleep(3)
                results[name] = healthy

        return results

    # ─── Phase 6b: Ollama Model Pull ─────────────────────────────────────

    async def pull_ollama_model(
        self, profile: DeployProfile, progress_callback=None
    ) -> bool:
        """Pull Ollama model after boot (for personal-ollama/personal-cpu profiles)."""
        if profile.name not in ("personal-ollama", "personal-cpu"):
            return True  # Not applicable

        container = "aither-ollama-personal"
        if profile.name == "personal-cpu":
            container = "aither-ollama-personal-cpu"
        model = "nemotron-orchestrator:8b-q4_K_M"

        if progress_callback:
            progress_callback("model_pull", f"Pulling {model} into {container}...")

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", container, "ollama", "pull", model,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
            return proc.returncode == 0
        except (asyncio.TimeoutError, FileNotFoundError):
            return False

    # ─── Phase 7: Stop ────────────────────────────────────────────────────

    async def stop(self) -> bool:
        """Stop deployed services."""
        state = DeployState.load()
        if not state:
            return False
        compose_path = Path(state.compose_path)
        if not compose_path.exists():
            return False

        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", str(compose_path), "down",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0

    # ─── Full Deploy Flow ─────────────────────────────────────────────────

    async def deploy(
        self,
        profile_name: str,
        extra_vars: Optional[Dict[str, str]] = None,
        dry_run: bool = False,
        progress_callback=None,
    ) -> Dict[str, Any]:
        """
        Execute full deployment flow. Returns result dict.

        progress_callback(phase: str, detail: str) is called at each step.
        """
        result: Dict[str, Any] = {"status": "pending", "errors": [], "warnings": []}

        def _progress(phase: str, detail: str = ""):
            if progress_callback:
                progress_callback(phase, detail)

        # 1. Resolve profile
        try:
            profile = self.get_profile(profile_name)
        except ValueError as e:
            result["status"] = "failed"
            result["errors"].append(str(e))
            return result

        result["profile"] = profile.name
        _progress("profile", f"{profile.name}: {profile.description}")

        # 2. Prerequisites
        _progress("prerequisites", "Checking Docker, RAM, disk, GPU...")
        prereqs = await self.check_prerequisites(profile)
        result["prerequisites"] = {
            "docker": prereqs.docker_version,
            "ram_gb": prereqs.ram_gb,
            "disk_gb": prereqs.disk_free_gb,
            "gpu": prereqs.gpu_name,
        }
        if not prereqs.ok:
            result["status"] = "failed"
            result["errors"] = prereqs.errors
            result["warnings"] = prereqs.warnings
            return result
        result["warnings"] = prereqs.warnings

        if dry_run:
            images = self.get_images_for_profile(profile)
            result["status"] = "dry_run"
            result["images"] = images
            result["image_count"] = len(images)
            result["compose_preview"] = self.generate_compose(profile)[:500]
            return result

        # 3. Pull images
        _progress("pull", f"Pulling {len(self.get_images_for_profile(profile))} images...")
        succeeded, failed = await self.pull_images(profile, progress_callback=_progress)
        result["images_pulled"] = len(succeeded)
        result["images_failed"] = len(failed)
        if failed:
            result["warnings"].append(f"Failed to pull {len(failed)} images: {failed}")

        # 4. Generate env
        _progress("env", "Generating environment configuration...")
        DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
        env_content = self.generate_env(profile, extra_vars)
        env_path = DEPLOY_DIR / ".env"
        env_path.write_text(env_content, encoding="utf-8")

        # 5. Generate compose
        _progress("compose", "Generating docker-compose.yml...")
        compose_content = self.generate_compose(profile)
        compose_path = DEPLOY_DIR / "docker-compose.yml"
        compose_path.write_text(compose_content, encoding="utf-8")

        # 6. Boot
        _progress("boot", "Starting services...")
        boot_ok = await self.boot(compose_path, env_path)
        if not boot_ok:
            result["status"] = "failed"
            result["errors"].append("docker compose up failed")
            return result

        # 6b. Ollama model pull (personal profiles)
        if profile.name in ("personal-ollama", "personal-cpu"):
            _progress("model_pull", "Downloading Nemotron-Orchestrator-8B model...")
            model_ok = await self.pull_ollama_model(profile, progress_callback=_progress)
            if not model_ok:
                result["warnings"].append("Model pull failed — will download on first use")

        # 7. Verify
        _progress("verify", "Waiting for services to become healthy...")
        health = await self.verify(timeout=120)
        result["health"] = health

        # 8. Save state
        state = DeployState(
            profile=profile.name,
            version=self.version,
            deployed_at=datetime.now(timezone.utc).isoformat(),
            compose_path=str(compose_path),
            data_dir=str(self.data_dir),
            services=list(health.keys()),
        )
        state.save()

        all_healthy = all(health.values())
        result["status"] = "success" if all_healthy else "partial"
        _progress("done", f"Deployment {'complete' if all_healthy else 'partial'}")

        return result

    # ─── Update Flow ──────────────────────────────────────────────────────

    async def update(self, progress_callback=None) -> Dict[str, Any]:
        """Pull newer images and restart."""
        state = DeployState.load()
        if not state:
            return {"status": "failed", "error": "No deployment found. Run `aither deploy` first."}

        profile = self.get_profile(state.profile)

        if progress_callback:
            progress_callback("update", f"Updating {state.profile}...")

        succeeded, failed = await self.pull_images(profile, progress_callback=progress_callback)

        # Restart
        compose_path = Path(state.compose_path)
        env_path = compose_path.parent / ".env"
        if compose_path.exists() and env_path.exists():
            await self.boot(compose_path, env_path)

        state.version = self.version
        state.deployed_at = datetime.now(timezone.utc).isoformat()
        state.save()

        return {
            "status": "success",
            "images_updated": len(succeeded),
            "images_failed": len(failed),
        }

    # ─── Export (Offline Bundle) ──────────────────────────────────────────

    async def export_bundle(
        self,
        profile_name: str,
        output_path: Path,
        progress_callback=None,
    ) -> bool:
        """Export profile images + compose as offline tarball."""
        profile = self.get_profile(profile_name)
        images = self.get_images_for_profile(profile)

        if progress_callback:
            progress_callback("export", f"Saving {len(images)} images...")

        # docker save all images to a single tar
        images_tar = output_path.with_suffix(".images.tar")
        proc = await asyncio.create_subprocess_exec(
            "docker", "save", "-o", str(images_tar), *images,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("docker save failed: %s", stderr.decode()[:300])
            return False

        # Write compose + env alongside
        compose_content = self.generate_compose(profile)
        env_content = self.generate_env(profile)

        bundle_dir = output_path.with_suffix(".bundle")
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "docker-compose.yml").write_text(compose_content, encoding="utf-8")
        (bundle_dir / ".env").write_text(env_content, encoding="utf-8")
        shutil.move(str(images_tar), str(bundle_dir / "images.tar"))

        # Create final tarball
        shutil.make_archive(str(output_path.with_suffix("")), "gztar", str(bundle_dir))
        shutil.rmtree(bundle_dir, ignore_errors=True)

        if progress_callback:
            progress_callback("export_done", str(output_path.with_suffix(".tar.gz")))
        return True

    # ─── Offline Import ───────────────────────────────────────────────────

    async def import_bundle(self, bundle_path: Path, progress_callback=None) -> bool:
        """Import an offline bundle and boot."""
        import tarfile

        if progress_callback:
            progress_callback("import", f"Extracting {bundle_path.name}...")

        extract_dir = DEPLOY_DIR / "bundle"
        extract_dir.mkdir(parents=True, exist_ok=True)

        with tarfile.open(str(bundle_path), "r:gz") as tf:
            tf.extractall(str(extract_dir))

        # Load images
        images_tar = extract_dir / "images.tar"
        if images_tar.exists():
            if progress_callback:
                progress_callback("import", "Loading Docker images...")
            proc = await asyncio.create_subprocess_exec(
                "docker", "load", "-i", str(images_tar),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        # Copy compose + env to deployment dir
        compose_src = extract_dir / "docker-compose.yml"
        env_src = extract_dir / ".env"
        if compose_src.exists():
            shutil.copy2(str(compose_src), str(DEPLOY_DIR / "docker-compose.yml"))
        if env_src.exists():
            shutil.copy2(str(env_src), str(DEPLOY_DIR / ".env"))

        # Clean up extracted bundle
        shutil.rmtree(extract_dir, ignore_errors=True)

        # Boot
        compose_path = DEPLOY_DIR / "docker-compose.yml"
        env_path = DEPLOY_DIR / ".env"
        if compose_path.exists() and env_path.exists():
            return await self.boot(compose_path, env_path)
        return False


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _get_ram_gb() -> float:
    """Get total RAM in GB."""
    try:
        if platform.system() == "Windows":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            c_ulonglong = ctypes.c_ulonglong
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", c_ulonglong),
                    ("ullAvailPhys", c_ulonglong),
                    ("ullTotalPageFile", c_ulonglong),
                    ("ullAvailPageFile", c_ulonglong),
                    ("ullTotalVirtual", c_ulonglong),
                    ("ullAvailVirtual", c_ulonglong),
                    ("ullAvailExtendedVirtual", c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullTotalPhys / (1024**3)
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / (1024**2)
    except Exception:
        pass
    return 0.0


def _get_disk_free_gb(path: Path) -> float:
    """Get free disk space at path in GB."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(str(path))
        return usage.free / (1024**3)
    except Exception:
        return 0.0


def _detect_gpu() -> Tuple[Optional[str], float]:
    """Detect NVIDIA GPU. Returns (name, vram_gb) or (None, 0)."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            line = result.stdout.strip().split("\n")[0]
            parts = [p.strip() for p in line.split(",")]
            name = parts[0] if parts else None
            vram = float(parts[1]) / 1024 if len(parts) > 1 else 0.0
            return name, vram
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return None, 0.0


async def _retag(source: str, target: str) -> bool:
    """Re-tag a Docker image."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "tag", source, target,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0
