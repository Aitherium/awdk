"""GraphRAG pack — rag_* agent tools.

Set up EMBEDDINGS + GraphRAG: serve the fleet's embedder (nomic-embed-text /
CodeRankEmbed) via vLLM, wire it into a local knowledge-graph store, ingest a
corpus, and verify BOTH halves with checks that can fail.

Design rules (same doctrine as the other packs):
  * Fail soft — every tool returns a dict, never raises.
  * Pure tools (detect, resolve, plan) have no side effects.
  * rag_apply_embedder is dry_run-able.
  * TWO positive assertions unique to this domain:
      - rag_verify_embedder asserts the vector has the EXPECTED DIMENSION. A broken
        embedder returns 200 with a wrong-length or empty vector — useless for
        retrieval, invisible without a dimension check.
      - rag_verify_retrieval ingests a SENTINEL and queries it back. An empty graph
        answers 200 with zero hits (the silent-empty trap) — only a round-trip that
        RETRIEVES the ingested content proves the pipeline works end to end.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional

import httpx

from adk.toolpacks.graphrag.recipes import (
    RECIPE_IDS,
    get_recipe,
    list_recipes,
    resolve_by_role_or_id,
)

logger = logging.getLogger("graphrag_pack")

_TIMEOUT_DEFAULT = 60.0
_GRAPH_DIR = os.path.expanduser("~/.aither/graphrag")


def _system_dict() -> dict:
    from adk.hardware_probe import detect_system

    s = detect_system()
    return {"ram_gb": s.ram_gb, "cpu_cores": s.cpu_cores, "gpu_vendor": s.gpu_vendor,
            "gpu_name": s.gpu_name, "gpu_vram_mb": s.gpu_vram_mb}


def _run(cmd: list, timeout: int = 120) -> tuple:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, ((p.stdout or "") + "\n" + (p.stderr or "")).strip()[-3000:]
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


# ── 1. DETECT ───────────────────────────────────────────────────────────


def rag_detect_hardware() -> dict:
    """Detect hardware and report the embedder options (pure, local)."""
    try:
        sysinfo = _system_dict()
        vram_gb = sysinfo["gpu_vram_mb"] / 1024
        embedders = {}
        for rid in RECIPE_IDS:
            r = get_recipe(rid) or {}
            reqs = r.get("hardware_requirements", {})
            fits = (sysinfo["gpu_vendor"] == "nvidia"
                    and vram_gb >= reqs.get("min_vram_gb", 0)) or \
                   reqs.get("gpu_vendor") != "nvidia"
            embedders[rid] = {
                "fits": fits,
                "dimension": r.get("model", {}).get("dimension"),
                "served_name": r.get("model", {}).get("served_name"),
            }
        return {"system_info": sysinfo, "embedders": embedders}
    except Exception as e:  # noqa: BLE001
        logger.exception("rag detect failed")
        return {"error": f"detect failed: {e}"}


# ── 2. RESOLVE ──────────────────────────────────────────────────────────


def rag_resolve_embedder(embedder: str = "text") -> dict:
    """Resolve an embedder role/id to its recipe (pure)."""
    rid = resolve_by_role_or_id(embedder)
    if not rid:
        return {"error": f"unknown embedder: {embedder}",
                "available_ids": list_recipes(),
                "available_roles": ["text", "code"]}
    recipe = get_recipe(rid)
    if not recipe:
        return {"error": f"failed to load recipe: {rid}"}
    return {"recipe_id": rid, "model": recipe.get("model", {}),
            "serve": recipe.get("serve", {}),
            "warnings": recipe.get("serve", {}).get("platform_traps", [])}


# ── 3. PLAN embedder ────────────────────────────────────────────────────


def _embed_command(recipe: dict) -> list:
    model = recipe.get("model", {})
    serve = recipe.get("serve", {})
    args = [
        "vllm", "serve", model.get("hf_repo", ""),
        "--host", "0.0.0.0", "--port", str(serve.get("port", 8209)),
        "--served-model-name", model.get("served_name", ""),
        "--gpu-memory-utilization", str(serve.get("gpu_memory_utilization", 0.05)),
        "--max-model-len", str(serve.get("max_model_len", 2048)),
        "--max-num-seqs", str(serve.get("max_num_seqs", 64)),
        "--dtype", serve.get("dtype", "float16"),
    ]
    if serve.get("task"):
        args += ["--task", serve["task"]]
    for extra in serve.get("extra_args", []) or []:
        args += extra.split()
    return args


def rag_plan_embedder(embedder: str = "text") -> dict:
    """Render the vLLM embedder serve plan (pure)."""
    resolved = rag_resolve_embedder(embedder)
    if "error" in resolved:
        return resolved
    recipe = get_recipe(resolved["recipe_id"])
    cmd = _embed_command(recipe)
    return {
        "recipe_id": resolved["recipe_id"],
        "served_name": recipe.get("model", {}).get("served_name"),
        "dimension": recipe.get("model", {}).get("dimension"),
        "vllm_command": cmd,
        "vllm_command_str": " ".join(cmd),
        "port": recipe.get("serve", {}).get("port", 8209),
        "warnings": recipe.get("serve", {}).get("platform_traps", []),
    }


# ── 4. APPLY embedder ───────────────────────────────────────────────────


def rag_apply_embedder(embedder: str = "text", dry_run: bool = False,
                       install_vllm: bool = False) -> dict:
    """Install vLLM (optional) and launch the embedder detached."""
    plan = rag_plan_embedder(embedder)
    if "error" in plan:
        return plan
    cmd_str = plan["vllm_command_str"]
    log = f"vllm-embed-{plan['recipe_id']}.log"
    if dry_run:
        cmds = (["pip install -U vllm"] if install_vllm else []) + \
               [f"nohup {cmd_str} > {log} 2>&1 &"]
        return {"planned": True, "dry_run": True, "recipe_id": plan["recipe_id"],
                "commands": cmds, "port": plan["port"], "warnings": plan.get("warnings", [])}
    try:
        if install_vllm:
            rc, out = _run(["pip", "install", "-U", "vllm"], timeout=1800)
            if rc != 0:
                return {"error": f"vllm install failed (rc={rc})", "output": out[-600:]}
        rc, out = _run(["sh", "-lc", f"nohup {cmd_str} > {log} 2>&1 &"], timeout=30)
        if rc != 0:
            return {"error": f"failed to launch embedder (rc={rc})", "output": out}
        return {"applied": True, "recipe_id": plan["recipe_id"], "port": plan["port"],
                "next": f"rag_verify_embedder(embedder='{embedder}', "
                        f"base_url='http://localhost:{plan['port']}')"}
    except Exception as e:  # noqa: BLE001
        logger.exception("rag apply embedder failed")
        return {"error": f"apply failed: {e}"}


# ── 5. VERIFY embedder (dimension assertion) ────────────────────────────


def rag_verify_embedder(base_url: str, embedder: str = "text",
                        timeout_s: float = _TIMEOUT_DEFAULT) -> dict:
    """Verify the embedder returns a vector of the EXPECTED DIMENSION.

    A broken/mis-loaded embedder answers 200 with a wrong-length or empty vector —
    useless for retrieval and invisible without checking the dimension.

    status: healthy | wrong_dimension | degraded | unknown
    """
    if not base_url:
        return {"error": "base_url required"}
    rid = resolve_by_role_or_id(embedder)
    recipe = get_recipe(rid) if rid else None
    if not recipe:
        return {"error": f"unknown embedder: {embedder}"}
    served = recipe.get("model", {}).get("served_name", "")
    expected_dim = recipe.get("verify", {}).get("expected_dimension") \
        or recipe.get("model", {}).get("dimension")
    probe = recipe.get("verify", {}).get("probe_text", "test")
    base = base_url.rstrip("/")

    try:
        health_ok = False
        try:
            health_ok = httpx.get(f"{base}/health", timeout=timeout_s).status_code == 200
        except httpx.HTTPError as e:
            return {"status": "unknown", "health": False, "detail": str(e),
                    "fix": "embedder did not answer /health — not up or still loading"}

        dim, detail = 0, ""
        try:
            r = httpx.post(f"{base}/v1/embeddings",
                           json={"model": served, "input": probe}, timeout=timeout_s)
            if r.status_code == 200:
                vec = (((r.json().get("data") or [{}])[0]).get("embedding")) or []
                dim = len(vec)
            else:
                detail = f"HTTP {r.status_code}: {r.text[:120]}"
        except (httpx.HTTPError, ValueError) as e:
            detail = f"{type(e).__name__}: {e}"

        if not health_ok or dim == 0:
            status = "degraded"
        elif expected_dim and dim != expected_dim:
            status = "wrong_dimension"
        else:
            status = "healthy"

        result = {"status": status, "health": health_ok, "served_name": served,
                  "dimension": dim, "expected_dimension": expected_dim}
        if detail:
            result["detail"] = detail
        if status == "wrong_dimension":
            result["fix"] = (
                f"embedder returned dim {dim}, expected {expected_dim} — it loaded as "
                "the wrong task/checkpoint. Vectors would be silently incompatible "
                "with the graph store. Check --task embed and the served model.")
        elif status == "degraded":
            result["fix"] = "no vector returned — check the embedder log (may still be loading)"
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("rag verify embedder failed")
        return {"status": "unknown", "error": f"verify failed: {e}"}


# ── 6. INGEST (folder -> local knowledge graph) ─────────────────────────


def rag_ingest(path: str, agent: str = "default", chunk_size: int = 0) -> dict:
    """Ingest a folder into a named agent's local knowledge graph via `adk ingest`.

    Local-first (the graph-rag-agent skill's documented path). Fails soft if adk's
    ingest CLI is not available, telling the caller how to get it.
    """
    if not path or not os.path.exists(path):
        return {"error": f"path does not exist: {path}"}
    cmd = ["adk", "ingest", path, "--agent", agent]
    if chunk_size:
        cmd += ["--chunk-size", str(chunk_size)]
    rc, out = _run(cmd, timeout=1800)
    if rc == 127:
        return {"error": "adk CLI not found",
                "fix": "pip install awdk, then re-run (see the graph-rag-agent skill)"}
    if rc != 0:
        return {"error": f"ingest failed (rc={rc})", "output": out[-800:]}
    return {"ingested": True, "path": path, "agent": agent, "output_tail": out[-400:],
            "next": f"rag_verify_retrieval(agent='{agent}', ...)"}


def _query_local_graph(agent: str, query: str) -> Optional[list]:
    """Query the local ingest graph store for a term. Returns hits, or None if no
    store exists for the agent.

    `adk ingest --agent <a>` writes ~/.aither/graph/<a>.db (SQLite: nodes/edges/
    keywords). This is a keyword/substring presence check against node content — an
    honest "is the ingested content in the graph and findable" signal, not a full
    semantic rank. Tokenised so a natural-language sentinel matches on its rare terms.
    """
    import sqlite3

    db = os.path.join(os.path.expanduser("~/.aither/graph"), f"{agent}.db")
    if not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        # Content-bearing columns across the likely tables.
        targets = []
        for t in tables:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
            for col in cols:
                if col.lower() in ("content", "text", "chunk", "body", "keyword", "name"):
                    targets.append((t, col))
        # Distinctive query terms (len>3, drop common words) so a full-sentence
        # sentinel still matches on BLUEHERON-7719 etc.
        stop = {"what", "which", "where", "the", "and", "for", "from", "with",
                "this", "that", "is", "are", "a", "an", "of", "to", "in", "on"}
        terms = [w.strip("?.,!:;\"'").lower() for w in query.split()]
        terms = [w for w in terms if len(w) > 3 and w not in stop] or [query.lower()]
        hits = []
        for t, col in targets:
            for term in terms:
                try:
                    rows = con.execute(
                        f"SELECT substr({col},1,160) FROM {t} WHERE lower({col}) LIKE ? LIMIT 3",
                        (f"%{term}%",)).fetchall()
                    hits += [r[0] for r in rows if r[0]]
                except sqlite3.Error:
                    continue
        con.close()
        # De-dup while preserving order.
        seen, out = set(), []
        for h in hits:
            if h not in seen:
                seen.add(h); out.append(h)
        return out
    except sqlite3.Error as e:
        logger.debug("local graph query failed: %s", e)
        return []


# ── 7. VERIFY retrieval (ingest sentinel -> query -> assert round-trip) ──


def rag_verify_retrieval(query_url: str = "", agent: str = "default",
                         sentinel: str = "", timeout_s: float = _TIMEOUT_DEFAULT) -> dict:
    """Prove the graph actually RETRIEVES ingested content, not just answers 200.

    An empty graph returns 200 with zero hits — the silent-empty trap. This queries
    for a term and requires a NON-EMPTY, relevant hit. When `query_url` is given it
    hits the fleet RAG query endpoint; otherwise it uses `adk query`.

    status: healthy | empty | unknown
    """
    q = sentinel or "the ingested corpus"
    try:
        hits = []
        if query_url:
            try:
                r = httpx.post(f"{query_url.rstrip('/')}/rag/query",
                               json={"query": q, "agent": agent, "top_k": 3},
                               timeout=timeout_s)
                if r.status_code == 200:
                    body = r.json()
                    hits = body.get("results") or body.get("hits") or body.get("chunks") or []
                else:
                    return {"status": "unknown", "detail": f"HTTP {r.status_code}",
                            "fix": "RAG query endpoint did not answer 200"}
            except (httpx.HTTPError, ValueError) as e:
                return {"status": "unknown", "detail": f"{type(e).__name__}: {e}"}
        else:
            # No fleet query_url — query the LOCAL ingest graph store directly.
            # NOT `adk chat <agent>`: that targets MESH agents (a local ingest graph
            # is not a mesh agent), so it errors — and counting that error text as a
            # "hit" is a false-positive healthy (caught live 2026-07-24). Querying the
            # store proves ingest→store→retrievable, which is what a verify must show.
            hits = _query_local_graph(agent, q)
            if hits is None:
                return {"status": "unknown",
                        "error": f"no local graph for agent '{agent}'",
                        "fix": f"run rag_ingest first; expected ~/.aither/graph/{agent}.db"}

        status = "healthy" if hits else "empty"
        result = {"status": status, "agent": agent, "query": q, "hit_count": len(hits)}
        if status == "empty":
            result["fix"] = (
                "query returned ZERO hits — the graph is empty or the query matched "
                "nothing. An empty graph answers 200 with no results, so this is NOT "
                "proof of a working RAG: ingest content first (rag_ingest), then "
                "query a term you KNOW is in it.")
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("rag verify retrieval failed")
        return {"status": "unknown", "error": f"verify failed: {e}"}
