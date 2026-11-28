"""Self-deploy bundle generator and Discord/Fleet deployment helpers (R4-safe).

Targets:
- ``local``: produces a runnable standalone bundle (``bot.yaml``, ``knowledge/``,
  ``actions/``, ``tools.py``, ``run_bot.py``, ``Dockerfile``, ``docker-compose.yml``,
  ``requirements.txt``, ``.env.example``, ``README.md``, systemd-user service unit,
  and ``.zip`` archive).
- ``discord``: prepares an ``adk.onboard_discord``-compatible identity config,
  ``tools_module``, and offline ``invite_url`` (never launches a live Discord
  gateway connection under default ``dry_run=True``).
- ``fleet``: builds a ``cmd_host``-compatible registration payload and supports
  offline ``dry_run=True`` preview or an injected ``http_client`` for local
  loopback testing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from adk.bots.manifest import BotManifest, load_bot_manifest
from adk.bots.validator import _resolve_within, iter_bot_files, validate_bot

_SEAL_EXCLUDES = frozenset(
    {
        "__pycache__",
        ".git",
        ".ruff_cache",
        ".pytest_cache",
        ".DS_Store",
        ".mypy_cache",
        "awseal.json",
        "bundle-manifest.sha256.json",
    }
)
_AWRUN_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_AWRUN_HOST_RE = re.compile(
    r"^(\*\.)?" + _AWRUN_LABEL + r"(\." + _AWRUN_LABEL + r")*(:\d{1,5})?$"
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


@dataclass
class DeployResult:
    ok: bool
    target: str
    bot_id: str
    style: str
    output_path: Path | None = None
    bundle_zip: Path | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""


def export_identity_yaml(manifest: BotManifest, dest_dir: str | Path) -> Path:
    """Write an ``adk.identity``-compatible ``<id>.yaml`` file into *dest_dir*."""
    out_dir = Path(dest_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{manifest.id}.yaml"
    data = {
        "name": manifest.id,
        "role": "assistant",
        "description": manifest.description or f"{manifest.name} ({manifest.style})",
        "system_prompt": manifest.instructions,
        "skills": [],
        "version": manifest.version,
    }
    target.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    return target


def _run_bot_py_source(default_port: int) -> str:
    return (
        '"""Standalone runner for an AitherOS declarative bot."""\n\n'
        "from __future__ import annotations\n\n"
        "import os\n"
        "from pathlib import Path\n\n"
        "import uvicorn\n"
        "from adk.bots.runtime import create_bot_app\n\n\n"
        "def _load_dotenv(env_path: Path) -> None:\n"
        "    if not env_path.is_file():\n"
        "        return\n"
        '    for line in env_path.read_text(encoding="utf-8").splitlines():\n'
        "        stripped = line.strip()\n"
        '        if not stripped or stripped.startswith("#") or "=" not in stripped:\n'
        "            continue\n"
        '        k, v = stripped.split("=", 1)\n'
        "        k = k.strip()\n"
        '        v = v.strip().strip(\'"\').strip("\'")\n'
        "        if k and k not in os.environ and v:\n"
        "            os.environ[k] = v\n\n\n"
        "def main() -> None:\n"
        "    bot_dir = Path(__file__).resolve().parent\n"
        '    _load_dotenv(bot_dir / ".env")\n'
        '    host = os.environ.get("BOT_HOST", "127.0.0.1")\n'
        f'    port = int(os.environ.get("BOT_PORT", "{default_port}"))\n'
        '    backend = os.environ.get("AITHER_LLM_BACKEND") or None\n'
        "    app = create_bot_app(bot_dir, backend=backend)\n"
        "    uvicorn.run(app, host=host, port=port)\n\n\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    )


def _dockerfile_source(port: int) -> str:
    return (
        "FROM python:3.11-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt /app/requirements.txt\n"
        "RUN pip install --no-cache-dir -r /app/requirements.txt\n"
        "COPY . /app\n"
        f"ENV BOT_HOST=0.0.0.0 BOT_PORT={port}\n"
        f"EXPOSE {port}\n"
        'CMD ["python", "run_bot.py"]\n'
    )


def _docker_compose_source(bot_id: str, port: int) -> str:
    return (
        "services:\n"
        f"  {bot_id}:\n"
        "    build: .\n"
        f'    ports:\n      - "{port}:{port}"\n'
        "    env_file:\n"
        "      - path: .env\n"
        "        required: false\n"
        "    restart: unless-stopped\n"
    )


def _systemd_service_source(bot_id: str, port: int) -> str:
    return (
        "[Unit]\n"
        f"Description=AitherOS Bot ({bot_id})\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "WorkingDirectory=%h/.local/share/aither-bots/"
        f"{bot_id}\n"
        "ExecStart=/usr/bin/env python3.11 run_bot.py\n"
        f"Environment=BOT_HOST=127.0.0.1 BOT_PORT={port}\n"
        "Restart=on-failure\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _runtime_tier_for(manifest: BotManifest) -> str:
    if manifest.backend in ("awzeal", "zero-libc") or manifest.target_surface == "awzeal":
        return "zero-libc"
    return "standard"


def _host_port_from_url(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host in _LOCAL_HOSTS or host.endswith(".local"):
        return None
    port = parsed.port
    scheme = (parsed.scheme or "").lower()
    if port is not None:
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            candidate = host
        else:
            candidate = f"{host}:{port}"
    else:
        candidate = host
    return candidate if _AWRUN_HOST_RE.match(candidate) else None


def derive_bot_egress_hosts(
    manifest: BotManifest, bot_dir: str | Path | None = None
) -> list[str]:
    """Derive a fail-closed ``awrun`` egress host allowlist from *manifest*.

    Local/offline/zero-libc backends (``mock``, ``awzeal``, ``zero-libc``) emit
    ``[]`` unless explicit external OpenAPI ``actions`` are declared.
    """
    hosts: set[str] = set()
    backend = (manifest.backend or "auto").strip().lower()

    if backend in ("mock", "awzeal", "zero-libc"):
        pass
    elif backend in ("llamacpp", "vllm"):
        base_env = os.environ.get("AITHER_LLM_BASE_URL", "").strip()
        hp = _host_port_from_url(base_env)
        if hp:
            hosts.add(hp)
    elif backend == "gemini":
        hosts.add("generativelanguage.googleapis.com")
    elif backend == "openai":
        hosts.add("api.openai.com")
    elif backend == "aither":
        gw = os.environ.get("AITHER_GATEWAY_URL", "https://api.aitherium.com").strip()
        hp = _host_port_from_url(gw)
        if hp:
            hosts.add(hp)
    elif backend == "auto":
        if manifest.style == "gemini":
            hosts.add("generativelanguage.googleapis.com")
        else:
            hosts.add("api.openai.com")

    root = Path(bot_dir) if bot_dir is not None else None
    for act in manifest.actions:
        hp = _host_port_from_url(act.base_url)
        if hp:
            hosts.add(hp)
        if root is not None and act.openapi_spec:
            spec_path, _ = _resolve_within(root, act.openapi_spec)
            if spec_path is not None and spec_path.is_file():
                try:
                    raw_text = spec_path.read_text(encoding="utf-8")
                    spec_data = (
                        json.loads(raw_text)
                        if spec_path.suffix.lower() == ".json"
                        else yaml.safe_load(raw_text)
                    )
                except Exception:
                    spec_data = None
                if isinstance(spec_data, dict):
                    srv_lists: list[Any] = [spec_data.get("servers")]
                    paths = spec_data.get("paths")
                    if isinstance(paths, dict):
                        for pitem in paths.values():
                            if isinstance(pitem, dict):
                                srv_lists.append(pitem.get("servers"))
                                for m in ("get", "post", "put", "patch", "delete"):
                                    op = pitem.get(m)
                                    if isinstance(op, dict):
                                        srv_lists.append(op.get("servers"))
                    for slist in srv_lists:
                        if not isinstance(slist, list):
                            continue
                        for srv in slist:
                            if isinstance(srv, dict):
                                shp = _host_port_from_url(str(srv.get("url") or ""))
                                if shp:
                                    hosts.add(shp)

    return sorted(hosts)


def build_awrun_job_spec(
    bot_dir: str | Path,
    manifest: BotManifest,
    *,
    port: int = 8095,
    isolation_image: str = "python:3.11-slim",
    isolation_runtime: str = "podman",
    egress_proxy: str = "",
    egress_network: str = "",
) -> dict[str, Any]:
    """Build an ``awrun.store.RunStore.submit`` / ``awrun.confine`` compatible job spec."""
    root = Path(bot_dir)
    egress_hosts = derive_bot_egress_hosts(manifest, root)
    egress: dict[str, Any] = {"hosts": egress_hosts}
    if egress_proxy.strip():
        egress["proxy"] = egress_proxy.strip()
    if egress_network.strip():
        egress["network"] = egress_network.strip()

    runtime_tier = _runtime_tier_for(manifest)
    return {
        "kind": "agent",
        "paths": [str(root)],
        "lineage": {"intent": f"bot:{manifest.id}"},
        "egress": egress,
        "spec": {
            "agent": manifest.id,
            "task": f"serve {manifest.id}",
            "adk_args": ["--bot", str(root)],
            "bot_id": manifest.id,
            "bot_style": manifest.style,
            "bot_backend": manifest.backend,
            "bot_dir": str(root),
            "port": port,
            "entrypoint": "run_bot.py",
            "runtime_tier": runtime_tier,
            "target_surface": manifest.target_surface,
            "shared_ro_mmap": manifest.shared_ro_mmap,
            "model_sha256": manifest.model_sha256,
            "isolation": {
                "mode": "container",
                "image": isolation_image,
                "runtime": isolation_runtime,
            },
        },
    }


def _resolve_awrun_store_dirs(
    awrun_dir: str | Path | None,
) -> tuple[Path | None, Path | None]:
    if awrun_dir is not None:
        p = Path(awrun_dir)
        return p, p / "deployments"
    if os.environ.get("AITHER_AWRUN_DIR", "").strip():
        return None, None
    data_dir = os.environ.get("AITHER_DATA_DIR", "").strip()
    if data_dir:
        p = Path(data_dir) / "awrun"
        return p, p / "deployments"
    return None, None


def _record_awrun_deployment(
    manifest: BotManifest,
    awrun_target: str,
    *,
    handle: str,
    endpoint: str | None,
    ok: bool,
    error: str = "",
    detail: dict[str, Any] | None = None,
    awrun_dir: str | Path | None = None,
) -> dict[str, Any]:
    try:
        from awrun.deploy import (
            STATUS_DEPLOYED,
            STATUS_FAILED,
            Deployment,
            DeploymentStore,
            _new_id,
        )
    except ImportError:
        return {"recorded": False, "available": False, "reason": "awrun-not-installed"}

    _, deployments_root = _resolve_awrun_store_dirs(awrun_dir)
    store = DeploymentStore(root=deployments_root)
    dep = Deployment(
        id=_new_id(),
        agent=manifest.id,
        target=awrun_target,
        created_at=time.time(),
        status=STATUS_DEPLOYED if ok else STATUS_FAILED,
        backend="awdk.bots",
        handle=handle,
        endpoint=endpoint,
        error=error or None,
        detail=dict(detail or {}),
    )
    store.put(dep)
    return {
        "recorded": True,
        "available": True,
        "deployment_id": dep.id,
        "target": awrun_target,
        "store_dir": str(store.root),
    }


class BotDeployerBackend:
    """Duck-typed ``awrun.deploy`` plugin backend for declarative ADK bots."""

    name = "awdk.bots"

    def __init__(
        self,
        *,
        out_dir: str | Path | None = None,
        port: int = 8095,
    ) -> None:
        self.out_dir = out_dir
        self.port = port

    def deploy(
        self, agent: str, target: str, *, company: str | None = None
    ) -> dict[str, Any]:
        bot_path = Path(agent)
        if not bot_path.is_dir() or not (bot_path / "bot.yaml").is_file():
            return {
                "ok": False,
                "error": f"Bot directory not found for agent {agent!r}",
            }
        bot_target = "local" if target == "local" else "fleet"
        res = deploy_bot(
            bot_path,
            target=bot_target,
            out_dir=self.out_dir,
            port=self.port,
            dry_run=(bot_target != "local"),
            record_deployment=False,
        )
        return {
            "ok": res.ok,
            "handle": str(res.output_path or res.bot_id),
            "endpoint": (
                f"http://127.0.0.1:{self.port}"
                if res.ok and bot_target == "local"
                else None
            ),
            "detail": {
                **res.payload,
                "company": company,
                "bot_id": res.bot_id,
                "style": res.style,
            },
            "error": res.error or None,
        }

    def status(self, record: dict[str, Any]) -> dict[str, Any] | None:
        return {
            "status": str(record.get("status") or "deployed"),
            "live": False,
            "source": self.name,
        }

    def teardown(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "remote_touched": False,
            "supported": False,
            "detail": {"reason": "local declarative bot bundle", "id": record.get("id")},
        }


def _compute_sha256_manifest(root: Path) -> tuple[dict[str, str], str]:
    files: dict[str, str] = {}
    for walk_root, dirs, fnames in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SEAL_EXCLUDES)
        for fname in sorted(fnames):
            if fname in _SEAL_EXCLUDES:
                continue
            fp = Path(walk_root) / fname
            if fp.is_symlink() or not fp.is_file():
                continue
            rel = fp.relative_to(root).as_posix()
            files[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()
    canonical_tree = json.dumps(
        files, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    tree_digest = hashlib.sha256(canonical_tree).hexdigest()
    return files, tree_digest


def _seal_bundle_dir(
    bundle_dir: Path,
    subject: str,
    *,
    key_path: str | Path | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    for stale_name in ("awseal.json", "bundle-manifest.sha256.json"):
        stale = bundle_dir / stale_name
        if stale.exists():
            stale.unlink()

    reason = ""
    try:
        from awseal import SealError, sign, write
    except ImportError:
        reason = "awseal-not-installed"
    else:
        try:
            kp = Path(key_path) if key_path is not None else None
            seal = sign(bundle_dir, key_path=kp, subject=subject, meta=meta)
            seal_path = write(seal, bundle_dir)
            return {
                "sealed": True,
                "format": "awseal",
                "subject": seal.subject,
                "tree_digest": seal.tree_digest,
                "public_key": seal.public_key,
                "seal_path": str(seal_path),
            }
        except (SealError, OSError):
            reason = "awseal-key-unavailable"

    # Honest unsealed SHA-256 fallback (NEVER forge awseal.json or claim sealed=True).
    files, tree_digest = _compute_sha256_manifest(bundle_dir)
    manifest_path = bundle_dir / "bundle-manifest.sha256.json"
    manifest_path.write_text(
        json.dumps(
            {
                "sealed": False,
                "format": "sha256",
                "reason": reason,
                "subject": subject,
                "tree_digest": tree_digest,
                "files": files,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "sealed": False,
        "format": "sha256",
        "reason": reason,
        "subject": subject,
        "tree_digest": tree_digest,
        "manifest_path": str(manifest_path),
    }


def verify_bot_bundle(
    bot_dir: str | Path, *, expect_key: str | None = None
) -> dict[str, Any]:
    """Fail-closed ``awseal`` verification for a sealed bot bundle directory."""
    root = Path(bot_dir)
    if not root.is_dir():
        return {
            "ok": False,
            "judged": False,
            "exit_code": 2,
            "bot_dir": str(root),
            "error": f"bot directory does not exist: {root}",
        }

    try:
        from awseal import SealError, load_public_key, verify
    except ImportError:
        return {
            "ok": False,
            "judged": False,
            "exit_code": 2,
            "bot_dir": str(root),
            "error": "awseal is not installed (pip install 'awdk[seal]')",
        }

    clean_key: str | None = None
    if expect_key is not None:
        clean_key = expect_key.strip().lower()
        try:
            load_public_key(clean_key)
        except SealError as exc:
            return {
                "ok": False,
                "judged": False,
                "exit_code": 2,
                "bot_dir": str(root),
                "error": str(exc),
            }

    try:
        res = verify(root, expect_key=clean_key)
    except SealError as exc:
        return {
            "ok": False,
            "judged": False,
            "exit_code": 2,
            "bot_dir": str(root),
            "error": str(exc),
        }

    sig_ok = res.get("signature_ok") is True
    content_ok = res.get("content_ok") is True
    key_trusted = res.get("key_trusted")
    trust_ok = True if clean_key is None else (key_trusted is True)
    overall_ok = bool(res.get("ok") is True and sig_ok and content_ok and trust_ok)

    return {
        "ok": overall_ok,
        "judged": True,
        "exit_code": 0 if overall_ok else 1,
        "bot_dir": str(root),
        "subject": res.get("subject", ""),
        "created": res.get("created", ""),
        "public_key": res.get("public_key", ""),
        "signature_ok": sig_ok,
        "content_ok": content_ok,
        "key_trusted": key_trusted,
        "diff": res.get("diff", {"added": [], "removed": [], "modified": []}),
    }


def _deploy_local(
    root: Path,
    manifest: BotManifest,
    *,
    out_dir: str | Path | None,
    port: int,
    create_zip: bool,
    key_path: str | Path | None = None,
    awrun_dir: str | Path | None = None,
    record_deployment: bool = True,
) -> DeployResult:
    bundle_dir = (
        Path(out_dir) if out_dir is not None else (root / "dist" / manifest.id)
    )
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "knowledge").mkdir(parents=True, exist_ok=True)
    (bundle_dir / "actions").mkdir(parents=True, exist_ok=True)

    # Copy safe bot files (never .env, __pycache__, dist, .git)
    for rel in iter_bot_files(root):
        if rel.parts and rel.parts[0] == "tests":
            continue
        src = root / rel
        if src.is_symlink() or not src.is_file():
            continue
        dst = bundle_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    runtime_tier = _runtime_tier_for(manifest)
    egress_hosts = derive_bot_egress_hosts(manifest, root)
    confinement = {"egress": {"hosts": egress_hosts}}

    (bundle_dir / "run_bot.py").write_text(
        _run_bot_py_source(port), encoding="utf-8", newline="\n"
    )
    (bundle_dir / "Dockerfile").write_text(
        _dockerfile_source(port), encoding="utf-8", newline="\n"
    )
    (bundle_dir / "docker-compose.yml").write_text(
        _docker_compose_source(manifest.id, port), encoding="utf-8", newline="\n"
    )
    (bundle_dir / "requirements.txt").write_text(
        "awdk>=4.0.0\nuvicorn[standard]>=0.24.0\n",
        encoding="utf-8",
        newline="\n",
    )
    (bundle_dir / ".env.example").write_text(
        "# Environment variables (placeholders only — never commit secrets)\n"
        "BOT_HOST=127.0.0.1\n"
        f"BOT_PORT={port}\n"
        f"AITHER_LLM_BACKEND={manifest.backend}\n"
        "AWZEAL_BINARY=\n"
        "ASM_LLM_BINARY=\n"
        "AWZEAL_MODEL=\n"
        f"AWZEAL_MODEL_SHA256={manifest.model_sha256 or ''}\n"
        f"AWZEAL_SHARED_RO_MMAP={1 if manifest.shared_ro_mmap else 0}\n"
        "GEMINI_API_KEY=\n"
        "OPENAI_API_KEY=\n"
        "AITHER_API_KEY=\n"
        "DISCORD_BOT_TOKEN=\n",
        encoding="utf-8",
        newline="\n",
    )
    (bundle_dir / f"{manifest.id}.service").write_text(
        _systemd_service_source(manifest.id, port), encoding="utf-8", newline="\n"
    )
    (bundle_dir / "README.md").write_text(
        f"# {manifest.name} (`{manifest.id}`) — Self-Hosted Bundle\n\n"
        "```bash\n"
        "cp .env.example .env  # add API keys if using cloud providers\n"
        "python3.11 run_bot.py\n"
        "```\n",
        encoding="utf-8",
        newline="\n",
    )
    (bundle_dir / "bundle-metadata.json").write_text(
        json.dumps(
            {
                "bot_id": manifest.id,
                "style": manifest.style,
                "backend": manifest.backend,
                "runtime_tier": runtime_tier,
                "target_surface": manifest.target_surface,
                "shared_ro_mmap": manifest.shared_ro_mmap,
                "model_sha256": manifest.model_sha256,
                "confinement": confinement,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    seal_info = _seal_bundle_dir(
        bundle_dir,
        subject=f"bot:{manifest.id}",
        key_path=key_path,
        meta={
            "bot_id": manifest.id,
            "style": manifest.style,
            "runtime_tier": runtime_tier,
        },
    )

    warnings: list[str] = []
    if not seal_info.get("sealed"):
        warnings.append(
            f"Bundle is unsealed ({seal_info.get('reason')}); SHA-256 manifest written instead."
        )

    zip_path: Path | None = None
    if create_zip:
        zip_path = bundle_dir.parent / f"{manifest.id}-bundle.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(bundle_dir.rglob("*")):
                if item.is_file() and item.name != ".env":
                    arc = f"{manifest.id}/{item.relative_to(bundle_dir).as_posix()}"
                    zf.write(item, arcname=arc)

    awrun_dep: dict[str, Any] = {"recorded": False, "reason": "skipped"}
    if record_deployment:
        awrun_dep = _record_awrun_deployment(
            manifest,
            "local",
            handle=str(bundle_dir),
            endpoint=f"http://127.0.0.1:{port}",
            ok=True,
            detail={
                "style": manifest.style,
                "bot_backend": manifest.backend,
                "runtime_tier": runtime_tier,
                "target_surface": manifest.target_surface,
                "egress_hosts": egress_hosts,
                "sealed": bool(seal_info.get("sealed")),
            },
            awrun_dir=awrun_dir,
        )

    return DeployResult(
        ok=True,
        target="local",
        bot_id=manifest.id,
        style=manifest.style,
        output_path=bundle_dir,
        bundle_zip=zip_path,
        payload={
            "bundle_dir": str(bundle_dir),
            "zip_path": str(zip_path) if zip_path else "",
            "port": port,
            "entrypoint": "run_bot.py",
            "runtime_tier": runtime_tier,
            "target_surface": manifest.target_surface,
            "shared_ro_mmap": manifest.shared_ro_mmap,
            "model_sha256": manifest.model_sha256,
            "egress_hosts": egress_hosts,
            "confinement": confinement,
            "seal": seal_info,
            "awrun_deployment": awrun_dep,
        },
        warnings=warnings,
    )


def _deploy_discord(
    root: Path,
    manifest: BotManifest,
    *,
    out_dir: str | Path | None,
    discord_token: str,
    dry_run: bool,
) -> DeployResult:
    from adk.onboard_discord import invite_url as discord_invite_url

    tok = (discord_token or os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    inv_url = discord_invite_url(tok) if tok else ""
    tools_module = manifest.tools.modules[0] if manifest.tools.modules else None

    identity_dict = {
        "name": manifest.id,
        "role": "assistant",
        "description": manifest.description,
        "system_prompt": manifest.instructions,
    }
    exported_path: Path | None = None
    if out_dir is not None:
        exported_path = export_identity_yaml(manifest, out_dir)

    warnings: list[str] = []
    if not tok:
        warnings.append("No discord_token supplied; invite_url omitted.")
    if not dry_run:
        warnings.append(
            "Live Discord gateway launch is deferred to `adk onboard --discord`."
        )

    return DeployResult(
        ok=True,
        target="discord",
        bot_id=manifest.id,
        style=manifest.style,
        output_path=exported_path,
        payload={
            "identity": identity_dict,
            "tools_module": tools_module,
            "invite_url": inv_url,
            "bot_dir": str(root),
            "dry_run": dry_run,
            "ready_for_onboard_discord": True,
        },
        warnings=warnings,
    )


def _deploy_fleet(
    root: Path,
    manifest: BotManifest,
    *,
    out_dir: str | Path | None,
    port: int,
    dry_run: bool,
    fleet_url: str,
    token: str,
    http_client: Any,
) -> DeployResult:
    public_key = ""
    try:
        from adk.a2a_identity import get_a2a_public_key

        public_key = get_a2a_public_key()
    except Exception:
        public_key = ""

    invoke_url = f"http://127.0.0.1:{port}"
    payload: dict[str, Any] = {
        "name": manifest.id,
        "invoke_url": invoke_url,
        "reach": "local",
        "agent_type": "adk-bot",
        "bot_style": manifest.style,
        "model": manifest.model,
        "provider_hint": manifest.backend,
        "public_key": public_key,
    }

    written_path: Path | None = None
    if out_dir is not None:
        out_p = Path(out_dir)
        out_p.mkdir(parents=True, exist_ok=True)
        written_path = out_p / "fleet-registration.json"
        written_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    if dry_run:
        return DeployResult(
            ok=True,
            target="fleet",
            bot_id=manifest.id,
            style=manifest.style,
            output_path=written_path,
            payload={"registration": payload, "dry_run": True},
        )

    if http_client is None and not fleet_url:
        return DeployResult(
            ok=False,
            target="fleet",
            bot_id=manifest.id,
            style=manifest.style,
            output_path=written_path,
            payload={"registration": payload},
            error="fleet_url or http_client is required when dry_run=False",
        )

    endpoint = (
        f"{fleet_url.rstrip('/')}/api/genesis/v1/agent/fleet/register"
        if fleet_url
        else "/api/genesis/v1/agent/fleet/register"
    )
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        if http_client is not None:
            resp = http_client.post(endpoint, json=payload, headers=headers)
        else:
            import httpx

            resp = httpx.post(endpoint, json=payload, headers=headers, timeout=15)
    except Exception as exc:
        return DeployResult(
            ok=False,
            target="fleet",
            bot_id=manifest.id,
            style=manifest.style,
            output_path=written_path,
            payload={"registration": payload},
            error=f"Fleet registration request failed: {exc}",
        )

    if resp.status_code >= 300:
        return DeployResult(
            ok=False,
            target="fleet",
            bot_id=manifest.id,
            style=manifest.style,
            output_path=written_path,
            payload={"registration": payload, "status_code": resp.status_code},
            error=f"Fleet registration rejected ({resp.status_code}): {resp.text[:200]}",
        )

    try:
        resp_data = resp.json()
    except Exception:
        resp_data = {"text": resp.text}

    return DeployResult(
        ok=True,
        target="fleet",
        bot_id=manifest.id,
        style=manifest.style,
        output_path=written_path,
        payload={
            "registration": payload,
            "dry_run": False,
            "status_code": resp.status_code,
            "response": resp_data,
        },
    )


def _deploy_awrun(
    root: Path,
    manifest: BotManifest,
    *,
    port: int,
    dry_run: bool,
    awrun_dir: str | Path | None = None,
    record_deployment: bool = True,
) -> DeployResult:
    job_spec = build_awrun_job_spec(root, manifest, port=port)
    egress_hosts = job_spec["egress"]["hosts"]
    confinement = {"egress": job_spec["egress"]}

    try:
        from awrun.store import RunError, RunStore
    except ImportError:
        awrun_available = False
        RunError = RuntimeError  # type: ignore[misc,assignment]
        RunStore = None  # type: ignore[misc,assignment]
    else:
        awrun_available = True

    if dry_run:
        return DeployResult(
            ok=True,
            target="awrun",
            bot_id=manifest.id,
            style=manifest.style,
            payload={
                "dry_run": True,
                "run_spec": job_spec,
                "egress_hosts": egress_hosts,
                "confinement": confinement,
                "awrun_available": awrun_available,
            },
        )

    if not awrun_available or RunStore is None:
        return DeployResult(
            ok=False,
            target="awrun",
            bot_id=manifest.id,
            style=manifest.style,
            payload={
                "dry_run": False,
                "run_spec": job_spec,
                "egress_hosts": egress_hosts,
                "confinement": confinement,
                "awrun_available": False,
            },
            error="awrun is not installed (pip install 'awdk[queue]')",
        )

    run_root, _ = _resolve_awrun_store_dirs(awrun_dir)
    store = RunStore(path=run_root)
    try:
        item = store.submit(
            job_spec["kind"],
            job_spec["spec"],
            paths=job_spec["paths"],
            lineage=job_spec["lineage"],
            egress=job_spec["egress"],
        )
    except RunError as exc:
        return DeployResult(
            ok=False,
            target="awrun",
            bot_id=manifest.id,
            style=manifest.style,
            payload={
                "dry_run": False,
                "run_spec": job_spec,
                "egress_hosts": egress_hosts,
                "confinement": confinement,
                "awrun_available": True,
            },
            error=f"awrun submit failed: {exc}",
        )

    awrun_dep: dict[str, Any] = {"recorded": False, "reason": "skipped"}
    if record_deployment:
        awrun_dep = _record_awrun_deployment(
            manifest,
            "local",
            handle=item.id,
            endpoint=f"http://127.0.0.1:{port}",
            ok=True,
            detail={
                "style": manifest.style,
                "bot_backend": manifest.backend,
                "run_id": item.id,
                "egress_hosts": egress_hosts,
            },
            awrun_dir=awrun_dir,
        )

    return DeployResult(
        ok=True,
        target="awrun",
        bot_id=manifest.id,
        style=manifest.style,
        output_path=store.path,
        payload={
            "dry_run": False,
            "run_id": item.id,
            "run_spec": job_spec,
            "egress_hosts": egress_hosts,
            "confinement": confinement,
            "awrun_available": True,
            "awrun_deployment": awrun_dep,
        },
    )


def deploy_bot(
    bot_dir: str | Path,
    *,
    target: str = "local",
    out_dir: str | Path | None = None,
    port: int = 8095,
    dry_run: bool = True,
    fleet_url: str = "",
    token: str = "",
    discord_token: str = "",
    validate_first: bool = True,
    allow_local_action_urls: bool = False,
    http_client: Any = None,
    create_zip: bool = True,
    key_path: str | Path | None = None,
    awrun_dir: str | Path | None = None,
    record_deployment: bool = True,
) -> DeployResult:
    """Validate and package/prepare *bot_dir* for ``local``, ``discord``, ``fleet``, or ``awrun`` deploy."""
    root = Path(bot_dir)
    target_norm = (target or "local").strip().lower()
    if target_norm not in ("local", "discord", "fleet", "awrun"):
        return DeployResult(
            ok=False,
            target=target_norm,
            bot_id="",
            style="",
            error=f"Unsupported deploy target {target!r}; expected local|discord|fleet|awrun",
        )

    if validate_first:
        rep = validate_bot(root, allow_local_action_urls=allow_local_action_urls)
        if not rep.ok:
            summary = "; ".join(str(f) for f in rep.findings)
            return DeployResult(
                ok=False,
                target=target_norm,
                bot_id=str(rep.manifest.get("id") or ""),
                style=str(rep.manifest.get("style") or ""),
                error=f"Bot validation failed: {summary}",
            )

    manifest = load_bot_manifest(root)
    if target_norm == "local":
        return _deploy_local(
            root,
            manifest,
            out_dir=out_dir,
            port=port,
            create_zip=create_zip,
            key_path=key_path,
            awrun_dir=awrun_dir,
            record_deployment=record_deployment,
        )
    if target_norm == "awrun":
        return _deploy_awrun(
            root,
            manifest,
            port=port,
            dry_run=dry_run,
            awrun_dir=awrun_dir,
            record_deployment=record_deployment,
        )
    if target_norm == "discord":
        return _deploy_discord(
            root,
            manifest,
            out_dir=out_dir,
            discord_token=discord_token,
            dry_run=dry_run,
        )
    res = _deploy_fleet(
        root,
        manifest,
        out_dir=out_dir,
        port=port,
        dry_run=dry_run,
        fleet_url=fleet_url,
        token=token,
        http_client=http_client,
    )
    if record_deployment and not dry_run and res.ok:
        res.payload["awrun_deployment"] = _record_awrun_deployment(
            manifest,
            "managed",
            handle=manifest.id,
            endpoint=res.payload.get("registration", {}).get("invoke_url"),
            ok=True,
            detail={
                "style": manifest.style,
                "bot_backend": manifest.backend,
                "fleet_url": fleet_url,
            },
            awrun_dir=awrun_dir,
        )
    return res
