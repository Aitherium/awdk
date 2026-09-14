"""Code locator — amortized-index localization instead of grep chains.

MEASURED MOTIVATION (context-cost bench, 18 verified question->file pairs over the
AitherOS monorepo, 2026-07-24 re-run against healed indexes): a grep-agent pays
197,757 tokens to localize them; this ensemble answers 13/18 for 33,298 tokens —
83% saved, ~150 tokens per hit vs ~11k per grep chain. The savings live in the
expensive tail (p95 localizations are 12-36k tokens each).

The ensemble (ported from the bench's winning "engine" lane):
  1. RepoWise structural node search (optional; tried first when configured)
  2. CodeGraph semantic search over AST chunks + embeddings (the primary lane)
  3. A persisted landmark map (dir -> purpose one-liners) for cross-lane agreement
UNION + rank by agreement, then render a COMPACT candidate block — paths plus a
one-line note each. A MISS costs one cheap call; the agent falls back to grep no
worse off than before.

Decoupling (adk ships standalone): everything is env-configured, all lanes are
optional, HTTP is lazy httpx with short timeouts, and every failure degrades to an
empty lane — never an exception. No AitherOS imports.

Env:
  AITHER_CODEGRAPH_URL   e.g. https://127.0.0.1:8153/codegraph/search  (primary lane)
  AITHER_REPOWISE_URL    e.g. http://127.0.0.1:7337  (optional; /api/graph/<id>/nodes/search)
  AITHER_REPOWISE_REPO   repo id for the RepoWise workspace (optional)
  AITHER_PROJECT_MAP     path to a project_map json (optional)
  AITHER_CODE_LOCATOR    "1" registers the locate_code tool on agents (default off)
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger("adk.code_locator")

_STOP = frozenset(
    "the a an is are was were be been being where how what which who when why does do "
    "did for of in on at to from with and or not no it its this that these those".split()
)


def _terms(query: str) -> list[str]:
    return [t for t in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", query.lower()) if t not in _STOP][:8]


def _get_json(url: str, timeout: float = 6.0) -> Optional[Any]:
    """GET json; None on ANY failure (down, TLS, 404, junk). Never raises.

    Verification goes through `tls_verify()`, which trusts the AitherNet CA
    bundle when present rather than disabling verification. A bare
    `verify=False` here was silently MITM-exposing every codegraph/repowise
    query — and it is what the repo's own TLS gate
    (`tests/test_tls_verify.py::test_no_bare_verify_false_in_adk_package`)
    blocks, which is why public CI has been red since 2.41.0.

    The import is local and guarded so this module keeps its "no AitherOS
    imports, never raises" contract: if `_tls` is somehow unavailable, fall back
    to plain verification (True) — the SECURE default, never False.
    """
    try:
        import httpx
        try:
            from ._tls import tls_verify
            verify = tls_verify()
        except Exception:  # noqa: BLE001
            verify = True
        r = httpx.get(url, timeout=timeout, verify=verify)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001
        return None


class CodeLocator:
    """`localize(question)` -> ranked candidate files + a compact block to hand an LLM.

    Costs one HTTP round-trip per configured lane (~150-300 output tokens on a hit)
    instead of a grep chain (measured avg ~11k tokens per localization on a monorepo).
    """

    def __init__(
        self,
        codegraph_url: Optional[str] = None,
        repowise_url: Optional[str] = None,
        repowise_repo: Optional[str] = None,
        map_path: Optional[str] = None,
        k: int = 5,
    ) -> None:
        self.codegraph_url = codegraph_url or os.environ.get("AITHER_CODEGRAPH_URL", "")
        self.repowise_url = (repowise_url or os.environ.get("AITHER_REPOWISE_URL", "")).rstrip("/")
        self.repowise_repo = repowise_repo or os.environ.get("AITHER_REPOWISE_REPO", "")
        self.map_path = map_path or os.environ.get("AITHER_PROJECT_MAP", "")
        self.k = max(1, int(k))
        self._map_cache: Optional[dict] = None

    # ── lanes (each: list of (path, note); empty on any failure) ─────────────

    def _lane_codegraph(self, query: str) -> list[tuple[str, str]]:
        if not self.codegraph_url:
            return []
        from urllib.parse import quote
        j = _get_json(f"{self.codegraph_url}?q={quote(query)}&limit={self.k * 2}")
        out: list[tuple[str, str]] = []
        seen = set()
        for r in (j or {}).get("results", []) if isinstance(j, dict) else []:
            f = r.get("file") or r.get("source_path") or r.get("path")
            if f and f not in seen:
                seen.add(f)
                note = r.get("name") or r.get("signature") or r.get("type") or "match"
                out.append((str(f), str(note)[:60]))
        return out[: self.k * 2]

    def _lane_repowise(self, query: str) -> list[tuple[str, str]]:
        if not (self.repowise_url and self.repowise_repo):
            return []
        from urllib.parse import quote
        j = _get_json(
            f"{self.repowise_url}/api/graph/{self.repowise_repo}/nodes/search"
            f"?q={quote(query)}&limit={self.k * 2}")
        out: list[tuple[str, str]] = []
        seen = set()
        for r in j or [] if isinstance(j, list) else []:
            f = r.get("file_path") or r.get("path") or r.get("file")
            if f and f not in seen:
                seen.add(f)
                out.append((str(f), str(r.get("name") or r.get("kind") or "node")[:60]))
        return out[: self.k * 2]

    def _lane_map(self, query: str) -> list[tuple[str, str]]:
        """Persisted landmark map: keyword-scored dirs with purpose one-liners."""
        if not self.map_path:
            return []
        try:
            if self._map_cache is None:
                with open(self.map_path, encoding="utf-8") as f:
                    self._map_cache = json.load(f)
            dirs = self._map_cache.get("dirs") or {}
            terms = _terms(query)
            if not terms:
                return []
            scored: list[tuple[float, str, str]] = []
            for path, meta in dirs.items():
                text = " ".join(
                    str(meta.get(k, "")) for k in ("purpose", "why", "landmarks", "name")
                ).lower()
                s = sum(1.0 for t in terms if t in text)
                if s > 0:
                    scored.append((s, str(path), str(meta.get("purpose", ""))[:60]))
            scored.sort(key=lambda x: -x[0])
            return [(p, n) for _, p, n in scored[: self.k]]
        except Exception:  # noqa: BLE001
            return []

    # ── the ensemble ─────────────────────────────────────────────────────────

    def localize(self, query: str) -> dict:
        """Rank candidates by cross-lane agreement. Returns
        {"candidates": [paths], "block": compact-str, "lanes": {name: n_hits}}.
        Never raises; empty candidates = honest miss (caller falls back to grep)."""
        rw = self._lane_repowise(query)
        cg = self._lane_codegraph(query)
        primary = rw if rw else cg          # RepoWise narrows better when present
        dirs = self._lane_map(query)

        score: dict[str, float] = {}
        note: dict[str, str] = {}
        for i, (f, n) in enumerate(primary):
            score[f] = score.get(f, 0.0) + (self.k * 2 - i)
            note.setdefault(f, n)
        # cross-lane agreement: a candidate file inside a map-flagged dir gets a boost
        for d, n in dirs:
            d_norm = d.replace("\\", "/").rstrip("/")
            for f in list(score):
                if f.replace("\\", "/").startswith(d_norm):
                    score[f] += self.k
            if not any(f.replace("\\", "/").startswith(d_norm) for f in score):
                score.setdefault(d, 1.0)    # dir-level hint still beats nothing
                note.setdefault(d, n or "map hit")

        ranked = sorted(score.items(), key=lambda kv: -kv[1])[: self.k]
        lines = [f"{p}  [{note.get(p, '?')}]" for p, _ in ranked]
        return {
            "candidates": [p for p, _ in ranked],
            "block": "\n".join(lines) if lines else "(no candidates -- fall back to grep)",
            "lanes": {"repowise": len(rw), "codegraph": len(cg), "map": len(dirs)},
        }


def locator_enabled() -> bool:
    """The locate_code tool registers only when explicitly armed (or a lane is configured)."""
    if os.environ.get("AITHER_CODE_LOCATOR", "0") == "1":
        return True
    return bool(os.environ.get("AITHER_CODEGRAPH_URL"))


def register_locator_tool(agent: Any) -> int:
    """Register `locate_code` on an agent. Returns tools registered (0 or 1). Never raises."""
    try:
        if not locator_enabled():
            return 0
        locator = CodeLocator()

        def locate_code(question: str) -> str:
            """Locate which files implement something, WITHOUT grepping. One cheap
            indexed lookup (~150 tokens) over the fleet code index; falls back
            honestly when it has no answer.

            question: what you are looking for, e.g. 'where is the tool registry
                      that calls MCP servers?'
            """
            try:
                r = locator.localize(question)
                lanes = ", ".join(f"{k}:{v}" for k, v in r["lanes"].items())
                return f"{r['block']}\n(lanes {lanes}; verify with a targeted read, not a tree-wide grep)"
            except Exception as e:  # noqa: BLE001
                return f"(locator unavailable: {e} -- use grep)"

        agent._tools.register(
            locate_code, name="locate_code",
            description="Find which files implement something via the code index "
                        "(one ~150-token lookup instead of a grep chain)")
        return 1
    except Exception as e:  # noqa: BLE001
        logger.debug("locate_code registration skipped: %s", e)
        return 0
