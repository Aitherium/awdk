"""PromptBridge — resolve a spec ``prompts:`` map into concrete prompt strings.

Each entry in the map is ``key -> ref``. A ``ref`` is resolved one of two ways:

  * FILE ref  — contains ``/`` or ``\\`` OR ends with ``.md`` / ``.txt``. Read as UTF-8
    text; a relative path is resolved against ``base_dir`` (the directory of the spec).
  * DOT-KEY   — anything else. Treated as an AitherPrompts dot-key and resolved via a
    LAZY ``get_prompt`` import. AitherPrompts is OPTIONAL (it lives in the AitherOS
    monorepo and is absent in a pure-adk env). When it is unavailable and a dot-key ref
    is present, a clear error is raised that NAMES the missing key so the operator knows
    exactly which prompt could not be resolved.

No @-syntax, no dispatch table — the file-vs-dotkey decision is purely structural.
"""

import os
from pathlib import Path
from typing import Optional, Union


class PromptBridge:
    """Static resolver from a spec ``prompts:`` map to concrete strings."""

    @staticmethod
    def _looks_like_file(ref: str) -> bool:
        """A ref is a file when it carries a path separator or a text extension."""
        if not ref:
            return False
        if "/" in ref or "\\" in ref:
            return True
        low = ref.lower()
        return low.endswith(".md") or low.endswith(".txt")

    @staticmethod
    def _lazy_get_prompt():
        """Lazily import AitherPrompts.get_prompt. Returns the callable, or None when
        AitherPrompts is not importable in this environment (pure-adk install)."""
        # AitherPrompts may be exposed under a few module paths depending on how the
        # monorepo is placed on sys.path. Try each; the first that imports wins.
        candidates = (
            "AitherPrompts",
            "lib.core.AitherPrompts",
            "core.AitherPrompts",
            "AitherOS.lib.core.AitherPrompts",
        )
        for modname in candidates:
            try:
                mod = __import__(modname, fromlist=["get_prompt"])
            except Exception:
                continue
            fn = getattr(mod, "get_prompt", None)
            if callable(fn):
                return fn
            # Fall back to the AitherPrompts static accessor if present.
            cls = getattr(mod, "AitherPrompts", None)
            getter = getattr(cls, "get", None) if cls is not None else None
            if callable(getter):
                return getter
        return None

    @staticmethod
    def resolve(
        prompt_map: dict,
        base_dir: Optional[Union[str, Path]] = None,
    ) -> dict:
        """Resolve every ``key -> ref`` into ``key -> text``.

        Args:
            prompt_map: mapping of prompt key to ref (file path or AitherPrompts dot-key).
            base_dir:   directory relative file refs are resolved against (usually the
                        directory holding the spec). None → current working directory.

        Returns:
            dict of ``key -> resolved prompt text``.

        Raises:
            FileNotFoundError: a file ref does not exist.
            RuntimeError:      a dot-key ref was requested but AitherPrompts is not
                               importable, OR AitherPrompts raised while resolving it.
                               The message names the offending key.
        """
        resolved: dict = {}
        if not prompt_map:
            return resolved

        base = Path(base_dir) if base_dir is not None else Path.cwd()
        _get_prompt = None  # lazily resolved only if/when a dot-key ref appears

        for key, ref in prompt_map.items():
            if not isinstance(ref, str):
                # Already-inlined text (e.g. spec authored the prompt directly).
                resolved[key] = "" if ref is None else str(ref)
                continue

            if PromptBridge._looks_like_file(ref):
                p = Path(ref)
                if not p.is_absolute():
                    p = base / p
                if not p.exists():
                    raise FileNotFoundError(
                        f"prompt '{key}': file ref not found: {p} "
                        f"(ref={ref!r}, base_dir={str(base)!r})"
                    )
                resolved[key] = p.read_text(encoding="utf-8")
                continue

            # Dot-key ref → AitherPrompts.
            if _get_prompt is None:
                _get_prompt = PromptBridge._lazy_get_prompt()
            if _get_prompt is None:
                raise RuntimeError(
                    f"prompt '{key}': ref {ref!r} is an AitherPrompts dot-key but "
                    f"AitherPrompts is not available in this environment. Install/"
                    f"expose lib/core/AitherPrompts.py, or point '{key}' at a "
                    f".md/.txt file instead."
                )
            try:
                resolved[key] = _get_prompt(ref)
            except Exception as exc:  # noqa: BLE001 - surface with the key named
                raise RuntimeError(
                    f"prompt '{key}': AitherPrompts failed to resolve dot-key "
                    f"{ref!r}: {exc}"
                ) from exc

        return resolved
