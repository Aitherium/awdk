"""ModelLicenseRegistry — Per-model license & provenance tracking.

Self-contained port of AitherOS lib/compliance/ModelLicenseRegistry.py.
Reads bundled model_licenses.yaml from package data.

Usage:
    from adk.compliance.model_licenses import get_model_license_registry

    reg = get_model_license_registry()
    info = reg.get_license("qwen3:32b")
    ok = reg.check_commercial_use("qwen3:32b")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("adk.compliance.model_licenses")


@dataclass
class ModelLicenseInfo:
    """License and provenance metadata for a single model."""
    model_id: str = ""
    display_name: str = ""
    license: str = ""
    license_url: str = ""
    license_flags: Dict[str, bool] = field(default_factory=dict)
    provenance: str = ""
    hf_url: str = ""
    compliance_tags: List[str] = field(default_factory=list)
    notes: str = ""

    @property
    def commercial_ok(self) -> bool:
        return self.license_flags.get("commercial_ok", False)

    @property
    def attribution_required(self) -> bool:
        return self.license_flags.get("attribution_required", False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "display_name": self.display_name,
            "license": self.license,
            "license_url": self.license_url,
            "license_flags": self.license_flags,
            "provenance": self.provenance,
            "hf_url": self.hf_url,
            "compliance_tags": self.compliance_tags,
            "notes": self.notes,
            "commercial_ok": self.commercial_ok,
            "attribution_required": self.attribution_required,
        }


def _bundled_licenses_path() -> Path:
    """Resolve the bundled model_licenses.yaml path."""
    # Package data location
    pkg_data = Path(__file__).parent.parent / "data" / "model_licenses.yaml"
    if pkg_data.exists():
        return pkg_data
    # Fallback: AitherOS config (dev mode)
    aitheros_config = Path(__file__).resolve().parents[3] / "AitherOS" / "config" / "model_licenses.yaml"
    if aitheros_config.exists():
        return aitheros_config
    return pkg_data  # Will fail gracefully on load


class ModelLicenseRegistry:
    """Registry for model license metadata."""

    def __init__(self, config_path: Optional[Path] = None):
        self._config_path = config_path or _bundled_licenses_path()
        self._models: Dict[str, ModelLicenseInfo] = {}
        self._loaded_at: float = 0
        self._ttl: float = 300
        self._load()

    def _load(self):
        try:
            import yaml
        except ImportError:
            logger.warning("[ModelLicenseRegistry] PyYAML not available")
            return
        if not self._config_path.exists():
            logger.info("[ModelLicenseRegistry] No model_licenses.yaml at %s", self._config_path)
            return

        with open(self._config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        models = cfg.get("models", {})
        self._models = {}
        for model_id, data in models.items():
            self._models[model_id] = ModelLicenseInfo(
                model_id=model_id,
                display_name=data.get("display_name", model_id),
                license=data.get("license", "unknown"),
                license_url=data.get("license_url", ""),
                license_flags=data.get("license_flags", {}),
                provenance=data.get("provenance", ""),
                hf_url=data.get("hf_url", ""),
                compliance_tags=data.get("compliance_tags", []),
                notes=data.get("notes", ""),
            )
        self._loaded_at = time.time()
        logger.info("[ModelLicenseRegistry] Loaded %d model licenses", len(self._models))

    def _maybe_reload(self):
        if time.time() - self._loaded_at > self._ttl:
            self._load()

    def get_license(self, model_id: str) -> Optional[ModelLicenseInfo]:
        self._maybe_reload()
        if model_id in self._models:
            return self._models[model_id]
        for key, info in self._models.items():
            if model_id.startswith(key) or key.startswith(model_id):
                return info
        return None

    def check_commercial_use(self, model_id: str) -> bool:
        info = self.get_license(model_id)
        if info is None:
            return False
        return info.commercial_ok

    def get_all_licenses(self) -> List[ModelLicenseInfo]:
        self._maybe_reload()
        return list(self._models.values())

    def get_compliance_report(self) -> Dict[str, Any]:
        self._maybe_reload()
        commercial = [m for m in self._models.values() if m.commercial_ok]
        restricted = [m for m in self._models.values() if not m.commercial_ok]
        attribution = [m for m in self._models.values() if m.attribution_required]
        return {
            "total_models": len(self._models),
            "commercial_ok": len(commercial),
            "restricted": len(restricted),
            "attribution_required": len(attribution),
            "models": {k: v.to_dict() for k, v in self._models.items()},
        }


_registry: Optional[ModelLicenseRegistry] = None


def get_model_license_registry(
    config_path: Optional[Path] = None,
) -> ModelLicenseRegistry:
    global _registry
    if _registry is None:
        _registry = ModelLicenseRegistry(config_path)
    return _registry
