"""World Model tools — observe transitions, compute surprise, check status.

All tools fail-soft dict-returners. Network errors are logged once per process
(never repeatedly). AITHER_OFFLINE=1 or service unreachable => use in-process
MLPWorldModel fallback.

Design rules:
  * Every tool returns a dict, never raises.
  * Missing URL/token => {"ok": False, "reason": ...}, loud, never anon.
  * In-process engine fallback is silent (no error to the caller) when available.
  * Degraded conditions are returned as {"ok": False, "reason": "..."}.
  * Network errors are logged once; further attempts try the service each call.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("world_model_pack")

# Singleton state: _warn_once is used to log network errors only once per process
_warn_once: bool = False
_offline_mode: Optional[bool] = None  # None = undecided, True = offline, False = online


def _get_wm_config() -> tuple[str, str]:
    """Get WM service URL and auth token from environment.

    Returns (url, token). If either is missing, returns (url, "") and caller
    decides whether that's acceptable.
    """
    url = os.environ.get(
        "AITHER_WORLD_MODEL_URL",
        "https://aitheros-world-model:8197"
    )
    token = os.environ.get("AITHER_WM_INTERNAL_TOKEN", "")
    return url, token


def _is_offline_mode() -> bool:
    """Check if we should run in offline mode (in-process fallback).

    Returns True if AITHER_OFFLINE=1 or if we failed to contact the service.
    Cached after first check.
    """
    global _offline_mode
    if _offline_mode is not None:
        return _offline_mode

    if os.environ.get("AITHER_OFFLINE", "").lower() in ("1", "true", "yes"):
        _offline_mode = True
        return True

    # Try to import the in-process engine. If it's not available, we'll use
    # online mode and fail loudly when the service is unreachable.
    try:
        _import_mlp_world_model()
        _offline_mode = False  # available, but not forced by env
        return False
    except ImportError:
        _offline_mode = False
        return False


def _import_mlp_world_model() -> type:
    """Import MLPWorldModel from packages/world-model.

    The package is optional; if missing, callers can still use the remote
    service. Raises ImportError if the package is not found.

    Adds packages/world-model to sys.path if needed.
    """
    import sys
    from pathlib import Path

    try:
        from world_model.core.mlp import MLPWorldModel
        return MLPWorldModel
    except (ImportError, ModuleNotFoundError) as exc:
        logger.debug("world_model not importable directly (%s); trying path discovery", exc)

    try:
        # Look for packages/world-model relative to current working directory
        pkg_path = Path.cwd() / "packages" / "world-model"
        if pkg_path.exists():
            sys_path_str = str(pkg_path)
            if sys_path_str not in sys.path:
                sys.path.insert(0, sys_path_str)
            from world_model.core.mlp import MLPWorldModel
            return MLPWorldModel
    except (ImportError, ModuleNotFoundError) as exc:
        logger.debug("world_model not importable via cwd path (%s)", exc)

    # Last try: packages/ lives at the MONOREPO root, which is the parent of
    # the awdk checkout: tools.py -> world_model -> packs -> adk ->
    # awdk -> <repo root>.
    try:
        repo_root = Path(__file__).resolve().parents[4]
        pkg_path = repo_root / "packages" / "world-model"
        if pkg_path.exists():
            sys_path_str = str(pkg_path)
            if sys_path_str not in sys.path:
                sys.path.insert(0, sys_path_str)
            from world_model.core.mlp import MLPWorldModel
            return MLPWorldModel
    except (ImportError, ModuleNotFoundError) as exc:
        logger.debug("world_model not importable via repo-root path (%s)", exc)

    raise ImportError(
        "packages/world-model not available: could not import MLPWorldModel. "
        "Ensure packages/world-model is installed or in sys.path."
    )


def _get_offline_engine() -> Optional[Any]:
    """Get or create the in-process MLPWorldModel singleton.

    Returns None if the package is unavailable. The singleton is created
    once per process.
    """
    global _offline_engine, _offline_engine_tried
    if _offline_engine is None and _offline_engine_tried:
        return None  # Already tried and failed
    if _offline_engine is not None:
        return _offline_engine
    try:
        mlp_model = _import_mlp_world_model()
        _offline_engine = mlp_model()
        logger.info("Initialized in-process MLPWorldModel")
        return _offline_engine
    except ImportError as e:
        _offline_engine_tried = True
        logger.debug("In-process MLPWorldModel unavailable: %s", e)
        return None


_offline_engine: Optional[Any] = None
_offline_engine_tried: bool = False


def wm_observe(
    obs: Any,
    action: Any,
    next_obs: Any,
    reward: float = 0.0,
    done: bool = False,
    domain: str = "sandbox",
) -> Dict[str, Any]:
    """Observe one (obs, action, next_obs) transition.

    Records the transition for training. The observation format is domain-
    specific; adapters handle the encoding.

    Args:
        obs: Current observation (any format the engine accepts).
        action: Action taken (string or int; will be encoded).
        next_obs: Resulting observation.
        reward: Reward for the transition (default 0.0).
        done: Whether the episode is terminal (default False).
        domain: Domain tag, e.g. "sandbox" (default "sandbox").

    Returns:
        {"ok": True/False, "reason": "...", "transitions_buffered": int}
        ok=True => transition buffered (or redundantly observed).
        ok=False => model is degraded or offline unavailable; reason explains.
    """
    global _warn_once

    if _is_offline_mode():
        engine = _get_offline_engine()
        if engine is None:
            return {
                "ok": False,
                "reason": "AITHER_OFFLINE=1 but in-process MLPWorldModel unavailable"
                          " (packages/world-model not installed)",
                "transitions_buffered": 0,
            }
        try:
            # Offline engine observe() returns None; we count by hand
            engine.observe(
                state_hash=hash(str(obs)) if not isinstance(obs, (int, float))
                else int(obs),
                action=action,
                next_state_hash=hash(str(next_obs)) if not isinstance(next_obs,
                                                                      (int, float))
                else int(next_obs),
                reward=float(reward),
                done=bool(done),
                state_desc=str(obs),
                next_state_desc=str(next_obs),
            )
            count = engine.get_transition_count()
            return {
                "ok": True,
                "reason": "transition buffered in-process",
                "transitions_buffered": count,
            }
        except Exception as e:
            logger.exception("In-process wm_observe failed")
            return {
                "ok": False,
                "reason": f"in-process observe failed: {e}",
                "transitions_buffered": 0,
            }

    # Online mode: POST to service
    url, token = _get_wm_config()
    if not token:
        return {
            "ok": False,
            "reason": "AITHER_WM_INTERNAL_TOKEN not set; cannot contact world model service",
            "transitions_buffered": 0,
        }

    try:
        import httpx
    except ImportError:
        return {
            "ok": False,
            "reason": "httpx not available; cannot make HTTP requests",
            "transitions_buffered": 0,
        }

    try:
        payload = {
            "domain": domain,
            "obs": obs,
            "action": action,
            "next_obs": next_obs,
            "reward": float(reward),
            "done": bool(done),
        }
        headers = {"X-WM-Token": token}
        with httpx.Client() as client:
            resp = client.post(
                f"{url}/domain/observe",
                json=payload,
                headers=headers,
                timeout=2.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    "wm_observe POST failed: %d %s",
                    resp.status_code, resp.text[:100]
                )
                return {
                    "ok": False,
                    "reason": f"service returned {resp.status_code}",
                    "transitions_buffered": 0,
                }
            data = resp.json()
            return {
                "ok": data.get("ok", False),
                "reason": data.get("reason", ""),
                "transitions_buffered": data.get("transitions_buffered", 0),
            }
    except httpx.TimeoutException:
        if not _warn_once:
            _warn_once = True
            logger.warning(
                "wm_observe: service %s timed out; will retry on next call",
                url
            )
        return {
            "ok": False,
            "reason": f"service timed out (2s) at {url}",
            "transitions_buffered": 0,
        }
    except Exception as e:
        if not _warn_once:
            _warn_once = True
            logger.warning("wm_observe network error: %s (will retry next call)", e)
        return {
            "ok": False,
            "reason": f"network error: {e}",
            "transitions_buffered": 0,
        }


def wm_surprise(
    items: List[Dict[str, Any]],
    domain: str = "sandbox",
) -> Dict[str, Any]:
    """Compute prediction error (surprise) for a batch of transitions.

    Each item must have {id, obs, action, next_obs}. Surprise is the prediction
    error in latent space: 0.0 = predicted exactly, 1.0 = predicted wrong,
    None = unseen (no prediction available).

    Args:
        items: List of {"id": <str>, "obs": <obs>, "action": <action>,
               "next_obs": <obs>}.
        domain: Domain tag (default "sandbox").

    Returns:
        {"ok": True/False, "reason": "...", "surprises": {id: float|null, ...}}
        ok=True => all items scored (some may be None = unseen).
        ok=False => model degraded or service unavailable.
    """
    global _warn_once

    if _is_offline_mode():
        engine = _get_offline_engine()
        if engine is None:
            return {
                "ok": False,
                "reason": "AITHER_OFFLINE=1 but in-process MLPWorldModel unavailable",
                "surprises": {},
            }
        try:
            surprises = {}
            for item in items:
                item_id = item.get("id", "")
                obs = item.get("obs")
                action = item.get("action")
                next_obs = item.get("next_obs")
                obs_hash = hash(str(obs)) if not isinstance(obs, (int, float)) else int(
                    obs
                )
                next_obs_hash = (hash(str(next_obs)) if not isinstance(next_obs,
                                                                       (int, float))
                                 else int(next_obs))
                s = engine.surprise(obs_hash, action, next_obs_hash)
                surprises[item_id] = s
            return {
                "ok": True,
                "reason": "scored in-process",
                "surprises": surprises,
            }
        except Exception as e:
            logger.exception("In-process wm_surprise failed")
            return {
                "ok": False,
                "reason": f"in-process surprise failed: {e}",
                "surprises": {},
            }

    # Online mode
    url, token = _get_wm_config()
    if not token:
        return {
            "ok": False,
            "reason": "AITHER_WM_INTERNAL_TOKEN not set",
            "surprises": {},
        }

    try:
        import httpx
    except ImportError:
        return {
            "ok": False,
            "reason": "httpx not available",
            "surprises": {},
        }

    try:
        payload = {
            "domain": domain,
            "items": items,
        }
        headers = {"X-WM-Token": token}
        with httpx.Client() as client:
            resp = client.post(
                f"{url}/domain/surprise",
                json=payload,
                headers=headers,
                timeout=2.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    "wm_surprise POST failed: %d %s",
                    resp.status_code, resp.text[:100]
                )
                return {
                    "ok": False,
                    "reason": f"service returned {resp.status_code}",
                    "surprises": {},
                }
            data = resp.json()
            return {
                "ok": data.get("ok", False),
                "reason": data.get("reason", ""),
                "surprises": data.get("surprises", {}),
            }
    except httpx.TimeoutException:
        if not _warn_once:
            _warn_once = True
            logger.warning(
                "wm_surprise: service %s timed out; will retry on next call",
                url
            )
        return {
            "ok": False,
            "reason": f"service timed out (2s) at {url}",
            "surprises": {},
        }
    except Exception as e:
        if not _warn_once:
            _warn_once = True
            logger.warning("wm_surprise network error: %s", e)
        return {
            "ok": False,
            "reason": f"network error: {e}",
            "surprises": {},
        }


def wm_status() -> Dict[str, Any]:
    """Get world model status: mode, training state, health.

    Returns:
        {"ok": True, "mode": <str>, "transition_count": <int>, ...}
        or {"ok": False, "reason": "..."} if degraded.
    """
    global _warn_once

    if _is_offline_mode():
        engine = _get_offline_engine()
        if engine is None:
            return {
                "ok": False,
                "reason": "AITHER_OFFLINE=1 but in-process MLPWorldModel unavailable",
            }
        try:
            status = engine.get_status()
            status["ok"] = True
            return status
        except Exception as e:
            logger.exception("In-process wm_status failed")
            return {
                "ok": False,
                "reason": f"in-process status failed: {e}",
            }

    # Online mode
    url, token = _get_wm_config()
    if not token:
        return {
            "ok": False,
            "reason": "AITHER_WM_INTERNAL_TOKEN not set",
        }

    try:
        import httpx
    except ImportError:
        return {
            "ok": False,
            "reason": "httpx not available",
        }

    try:
        headers = {"X-WM-Token": token}
        with httpx.Client() as client:
            resp = client.get(
                f"{url}/domain/status",
                headers=headers,
                timeout=2.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    "wm_status GET failed: %d %s",
                    resp.status_code, resp.text[:100]
                )
                return {
                    "ok": False,
                    "reason": f"service returned {resp.status_code}",
                }
            data = resp.json()
            return {**data, "ok": True}
    except httpx.TimeoutException:
        if not _warn_once:
            _warn_once = True
            logger.warning(
                "wm_status: service %s timed out; will retry on next call",
                url
            )
        return {
            "ok": False,
            "reason": f"service timed out (2s) at {url}",
        }
    except Exception as e:
        if not _warn_once:
            _warn_once = True
            logger.warning("wm_status network error: %s", e)
        return {
            "ok": False,
            "reason": f"network error: {e}",
        }
