"""The model catalogue `adk models` reads, the licence gate, and the recommendation.

ONE FILE, GENERATED. ``adk/data/models_catalogue.json`` is written by the monorepo's
``gen_adk_model_catalogue.py`` from the awnix model catalogue (sizes, URLs, digests, the
ladder), ``model_licenses.yaml`` (licence records) and a small overlay (purpose, runtime).
Nothing here restates a size or a licence flag: this module only reads that file.

THE LICENCE GATE FAILS CLOSED. ``licence_verdict`` permits a model only when its record
exists AND says ``redistribution_ok: true``. No record, ``false``, and "the record does
not say" are all refusals, each with its own one-line reason. ``list`` shows every model;
``pull`` and ``use`` call the gate; ``recommend`` only ever considers permitted models.

THE SIZING RULE is the appliance selector's (``awnix-model-select.py`` ``select()``): the
largest ladder model whose weights fit ``budget / headroom_divisor``, and nothing at all
below ``min_budget_mb``. Both numbers come from the catalogue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

CATALOGUE_PATH = Path(__file__).resolve().parent.parent / "data" / "models_catalogue.json"


class CatalogueError(RuntimeError):
    """The catalogue is missing, unreadable, or does not define what was asked for."""


@dataclass(frozen=True)
class Verdict:
    """The licence gate's answer for one model."""

    allowed: bool
    reason: str  # one line; names the record and the flag when refused


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    """The catalogue. Raises rather than returning an empty one.

    Args:
        path: An alternative catalogue file (tests). Default: the packaged one.

    Raises:
        CatalogueError: The file is absent, not JSON, or defines no models.
    """
    p = path or CATALOGUE_PATH
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CatalogueError(f"model catalogue not found at {p} ({exc})") from exc
    except ValueError as exc:
        raise CatalogueError(f"model catalogue at {p} is not JSON ({exc})") from exc
    if not isinstance(doc, dict) or not doc.get("models"):
        raise CatalogueError(f"model catalogue at {p} defines no models")
    return doc


def get(cat: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    """One model's entry.

    Raises:
        CatalogueError: No such id; the message lists the ids that exist.
    """
    m = (cat.get("models") or {}).get(model_id)
    if not m:
        raise CatalogueError(f"no model '{model_id}' in the catalogue "
                             f"(have: {', '.join(sorted(cat.get('models') or {}))})")
    return m


def licence_verdict(model: Dict[str, Any]) -> Verdict:
    """May this model's weights be handed to a customer? Fails closed.

    Only a record that says ``redistribution_ok: true`` permits. A record that omits the
    flag is NOT a grant: nobody wrote down that redistribution is allowed.
    """
    lic = model.get("licence")
    lic_id = model.get("licence_id") or model.get("id")
    if not isinstance(lic, dict):
        return Verdict(False, f"no licence record for '{lic_id}' in model_licenses.yaml")
    name = lic.get("name") or "unknown licence"
    record = lic.get("record") or lic_id
    flag = lic.get("redistribution_ok")
    if flag is True:
        return Verdict(True, f"{name} (record '{record}', redistribution permitted)")
    if flag is False:
        return Verdict(False, f"licence record '{record}' ({name}) says redistribution_ok: false")
    return Verdict(False, f"licence record '{record}' ({name}) does not state "
                          "redistribution_ok, so redistribution is not granted")


def floor_mb(cat: Dict[str, Any], model: Dict[str, Any]) -> int:
    """The memory budget (VRAM, or RAM on CPU) this model needs under the sizing rule."""
    divisor = int(cat.get("headroom_divisor") or 0) or 2
    return max(int(model["size_mb"]) * divisor, int(cat.get("min_budget_mb") or 0))


def fits(cat: Dict[str, Any], model: Dict[str, Any], budget_mb: int) -> bool:
    """Do this model's weights fit ``budget_mb`` with the catalogue's headroom?"""
    return budget_mb > 0 and budget_mb >= floor_mb(cat, model)


#: On a CPU, memory is not the limit, speed is. A 27B model fits 24 GB+ of RAM and
#: was measured at 0.43 tokens/s on a CPU (2026-10-02, awdk 3.8.44); the installer
#: names the 4B there. So a CPU-only recommendation is capped at the 4B's size.
CPU_RECOMMEND_MAX_MB = 1100


def recommend(cat: Dict[str, Any], budget_mb: int, role: str = "chat",
              cpu_only: bool = False) -> Optional[str]:
    """The largest PERMITTED ladder model that fits ``budget_mb``, or None.

    The ladder is the catalogue's; a rung the licence gate refuses is skipped, never
    picked, so a recommendation can always be pulled. ``cpu_only`` caps the pick at
    ``CPU_RECOMMEND_MAX_MB`` so a CPU is never handed a model too slow to use.
    """
    best: Optional[str] = None
    for mid in cat.get("ladder") or []:
        m = (cat.get("models") or {}).get(mid)
        if not m or m.get("role", "chat") != role:
            continue
        if not licence_verdict(m).allowed or not fits(cat, m, budget_mb):
            continue
        if cpu_only and int(m["size_mb"]) > CPU_RECOMMEND_MAX_MB:
            continue
        if best is None or int(m["size_mb"]) > int(cat["models"][best]["size_mb"]):
            best = mid
    return best


def refused_rungs(cat: Dict[str, Any], budget_mb: int) -> List[str]:
    """Ladder models that FIT ``budget_mb`` but the licence gate refuses.

    Reported next to a recommendation so "nothing fits" and "something fits but may not
    be shipped" are never the same message.
    """
    out = []
    for mid in cat.get("ladder") or []:
        m = (cat.get("models") or {}).get(mid)
        if m and fits(cat, m, budget_mb) and not licence_verdict(m).allowed:
            out.append(mid)
    return out


def rows(cat: Dict[str, Any], budget_mb: int) -> List[Dict[str, Any]]:
    """Every model as a display row, ladder order first, then the rest by size."""
    ladder = list(cat.get("ladder") or [])
    models = cat.get("models") or {}
    order = ladder + sorted((k for k in models if k not in ladder),
                            key=lambda k: (models[k].get("role") != "chat",
                                           int(models[k]["size_mb"])))
    out = []
    for mid in order:
        m = models[mid]
        v = licence_verdict(m)
        lic = m.get("licence") or {}
        out.append({
            "id": mid, "role": m.get("role", "chat"), "size_mb": int(m["size_mb"]),
            "purpose": m.get("purpose", ""), "runtime": m.get("runtime", ""),
            "floor_mb": floor_mb(cat, m), "fits": fits(cat, m, budget_mb),
            "licence": lic.get("name") or "none recorded",
            "allowed": v.allowed, "reason": v.reason,
            "on_ladder": mid in ladder, "sha256": bool(m.get("sha256")),
        })
    return out
