"""Brain pack loader — declarative customization for bespoke agents.

A "brain pack" is a small YAML file that turns the generic runtime into a
customer-specific assistant. It defines the brand, system prompt, role names,
document type vocabulary, extraction schemas, and UI labels.

Works with any agent created by `adk create`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_PACK_DIR = Path(__file__).resolve().parent / "packs"


@dataclass(slots=True)
class RoleDef:
    description: str
    permissions: list[str]


@dataclass(slots=True)
class DocTypeDef:
    id: str
    label: str
    extract: bool = False
    extraction_target: str | None = None
    extraction_table: str | None = None


@dataclass(slots=True)
class ExtractionSchema:
    id: str
    description: str


@dataclass(slots=True)
class BrainPack:
    id: str
    app_name: str
    company_name: str
    system_prompt: str
    welcome_message: str = ""
    roles: dict[str, RoleDef] = field(default_factory=dict)
    workspace_role_map: dict[str, str] = field(default_factory=dict)
    default_role: str = "viewer"
    doc_types: dict[str, DocTypeDef] = field(default_factory=dict)
    extraction_schemas: dict[str, ExtractionSchema] = field(default_factory=dict)
    features: dict[str, bool] = field(default_factory=dict)
    ui_labels: dict[str, str] = field(default_factory=dict)
    safety: dict = field(default_factory=dict)
    platform: dict = field(default_factory=dict)

    def feature(self, name: str, *, default: bool = False) -> bool:
        return bool(self.features.get(name, default))

    def role_for_workspace(self, workspace_role: str) -> str:
        return self.workspace_role_map.get(workspace_role, self.default_role)


def _coerce_pack(data: dict[str, Any], pack_id: str) -> BrainPack:
    roles = {
        name: RoleDef(
            description=str(spec.get("description", "")),
            permissions=list(spec.get("permissions", [])),
        )
        for name, spec in (data.get("roles") or {}).items()
    }
    doc_types = {
        spec["id"]: DocTypeDef(
            id=str(spec["id"]),
            label=str(spec.get("label", spec["id"].title())),
            extract=bool(spec.get("extract", False)),
            extraction_target=spec.get("extraction_target"),
            extraction_table=spec.get("extraction_table"),
        )
        for spec in (data.get("doc_types") or [])
    }
    schemas = {
        spec["id"]: ExtractionSchema(
            id=str(spec["id"]),
            description=str(spec["description"]),
        )
        for spec in (data.get("extraction_schemas") or [])
    }
    return BrainPack(
        id=pack_id,
        app_name=str(data.get("app_name", "Company Brain")),
        company_name=str(data.get("company_name", "")),
        system_prompt=str(data.get("system_prompt", "")),
        welcome_message=str(data.get("welcome_message", "")),
        roles=roles,
        workspace_role_map=dict(data.get("workspace_role_map") or {}),
        default_role=str(data.get("default_role", "viewer")),
        doc_types=doc_types,
        extraction_schemas=schemas,
        features=dict(data.get("features") or {}),
        ui_labels=dict(data.get("ui_labels") or {}),
        safety=dict(data.get("safety") or {}),
        platform=dict(data.get("platform") or {}),
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_pack_path() -> Path:
    env_path = os.environ.get("BRAIN_PACK")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return p
        log.warning("BRAIN_PACK=%s not found; falling back", env_path)

    project_root = Path(__file__).resolve().parents[2]
    candidate = project_root / "brain_pack.yaml"
    if candidate.exists():
        return candidate

    # Use first available pack in packs/ directory
    if _PACK_DIR.exists():
        packs = sorted(_PACK_DIR.glob("*.yaml"))
        if packs:
            return packs[0]

    return _PACK_DIR / "default.yaml"


_pack: BrainPack | None = None


def get_brain_pack() -> BrainPack:
    global _pack
    if _pack is None:
        path = _resolve_pack_path()
        log.info("Loading brain pack from %s", path)
        data = _load_yaml(path)
        _pack = _coerce_pack(data, pack_id=path.stem)
    return _pack


def reload_brain_pack() -> BrainPack:
    global _pack
    _pack = None
    return get_brain_pack()
