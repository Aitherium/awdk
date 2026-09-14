"""LyraWiki-style self-managed semantic knowledge over GraphMemory.

The LLM-wiki pattern (AitherLyra, Karpathy llm-wiki): immutable raw memories are
CONSOLIDATED by the agent's own LLM into curated, embedded wiki ARTICLES with
``[[wikilinks]]`` — and knowledge that decays out of relevance is TRULY DELETED
(tombstone first, hard-delete after a retention window), not merely downranked.

Everything here is OPT-IN: nothing constructs :class:`MemoryWiki` unless a
caller does, so default GraphMemory / AitherAgent behaviour is byte-identical.

Design:
    - Articles are ``wiki_article`` nodes in the SAME GraphMemory db (title as
      label, curated markdown as content, embedding via the graph's embedder,
      slug/revision/sources in metadata, durable tier).
    - ``CONSOLIDATED_FROM`` edges point from an article to every source memory
      it represents; ``[[wikilinks]]`` between articles become ``wiki_link``
      edges; revisions are ledgered through the governance MutationLedger.
    - ``consolidate(llm)`` clusters unconsolidated episodic/fact nodes
      (embedding + keyword), drafts/updates articles via the INJECTED llm
      callable (str -> str or messages -> str; sync or async — no hard model
      dependency), cites source node ids, marks contradicted sources
      superseded, then DEMOTES consolidated sources to a fast-decay tier via
      :meth:`GraphMemory.promote` (represented knowledge doesn't need raw
      copies — the sweep collects them).
    - ``lint()`` runs deterministic health checks (orphan wikilinks, empty /
      stale articles, live-but-superseded contradiction pairs) plus an
      optional LLM pass.
    - ``prune()`` computes article relevance = tier-freshness × reinforcement
      (reinforce-on-recall) + link-degree to LIVE memories; below the floor →
      reversible governance tombstone; entombed articles older than the
      window → HARD-DELETE (the tombstone snapshot itself is purged — content
      and embedding are irrecoverably gone).
    - ``recall()`` is article-first RAG: curated articles rank above raw nodes.

Usage:
    from adk.graph_memory import GraphMemory
    from adk.memory_wiki import MemoryWiki

    graph = GraphMemory(agent_name="atlas")
    wiki = MemoryWiki(graph)
    stats = await wiki.consolidate(my_llm)      # llm: (str)->str or (messages)->str
    findings = await wiki.lint()
    stats = await wiki.prune()
    nodes = await wiki.recall("what do I know about the harbour?")
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

from adk.graph_memory import (
    GraphMemory,
    GraphNode,
    cosine_similarity,
    extract_keywords,
)

logger = logging.getLogger("adk.memory_wiki")

#: node_type used for wiki articles inside the graph db.
WIKI_NODE_TYPE = "wiki_article"
#: edge relation from an article to each raw memory it consolidates.
EDGE_CONSOLIDATED_FROM = "consolidated_from"
#: edge relation between articles created from ``[[wikilinks]]``.
EDGE_WIKI_LINK = "wiki_link"
#: reason prefix on tombstones created by :meth:`MemoryWiki.prune` — the
#: hard-delete pass only ever purges its OWN tombstones.
PRUNE_REASON_PREFIX = "wiki_prune"

# Roles that are never demoted after consolidation (mirrors sweep's
# preserve_roles contract: prefs/corrections lose rank but never silently die).
_DEMOTE_EXEMPT_ROLES = frozenset({"preference", "identity", "correction"})

_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|[^\[\]]*)?\]\]")


def _slugify(title: str) -> str:
    """Filesystem/wikilink-safe slug (mirrors adk.skills._slugify)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    return slug or "untitled"


def _parse_json_block(text: str) -> Dict[str, Any]:
    """Extract the first balanced ``{...}`` block from *text* and parse it.

    Repair-tolerant: returns ``{}`` when nothing parses (callers fall back to
    treating the raw text as markdown).
    """
    if not text:
        return {}
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : i + 1])
                        if isinstance(data, dict):
                            return data
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return {}


async def _call_llm(llm: Callable, system: str, user: str) -> str:
    """Invoke an injected llm callable — ``(messages)->str`` or ``(str)->str``,
    sync or async. Returns ``""`` on total failure (callers skip the item)."""
    if llm is None:
        return ""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    prompt = f"{system}\n\n{user}" if system else user
    for arg in (messages, prompt):
        try:
            out = llm(arg)
            if inspect.isawaitable(out):
                out = await out
            if isinstance(out, str) and out.strip():
                return out
        except Exception as exc:  # noqa: BLE001 — try the other calling convention
            logger.debug("memory_wiki llm call failed (%s arg): %s",
                         "messages" if arg is messages else "str", exc)
            continue
    return ""


_DRAFT_SYSTEM = (
    "You maintain a personal knowledge wiki. You consolidate raw memory "
    "evidence into ONE curated, encyclopedic markdown article. Merge "
    "duplicates, prefer newer evidence over older on conflict, link related "
    "concepts with [[wikilinks]], and cite evidence inline as (src: <node-id>). "
    "Respond ONLY with JSON: {\"title\": \"...\", \"markdown\": \"...\", "
    "\"contradicted_sources\": [\"<node-id>\", ...]} where contradicted_sources "
    "lists ids of evidence the consolidated knowledge now supersedes."
)

_UPDATE_SYSTEM = (
    "You maintain a personal knowledge wiki. Update the CURRENT ARTICLE with "
    "the NEW EVIDENCE: merge it in naturally, keep what is still true, replace "
    "what is contradicted, keep the title stable unless it is clearly wrong, "
    "keep/extend the [[wikilinks]], cite new evidence inline as "
    "(src: <node-id>). Respond ONLY with JSON: {\"title\": \"...\", "
    "\"markdown\": \"...\", \"contradicted_sources\": [\"<node-id>\", ...]}."
)

_LINT_SYSTEM = (
    "You audit a personal knowledge wiki. Given the article index, report "
    "issues (contradictions between articles, obvious gaps, duplicated "
    "topics). Respond ONLY with a JSON list: "
    "[{\"kind\": \"...\", \"article\": \"<slug>\", \"message\": \"...\"}]."
)


class MemoryWiki:
    """Self-managed semantic knowledge base layered over a :class:`GraphMemory`.

    All operations are async; the LLM is injected per-call (a callable taking
    either a prompt string or an OpenAI-style messages list and returning a
    string, sync or async). There is NO hard model dependency.
    """

    def __init__(
        self,
        graph: GraphMemory,
        *,
        article_tier: str = "trace",
        demote_tier: str = "session",
        cluster_similarity: float = 0.60,
        article_match_similarity: float = 0.55,
        keyword_overlap: int = 3,
        min_cluster: int = 2,
        link_weight: float = 0.1,
        max_evidence_chars: int = 6000,
    ) -> None:
        self.graph = graph
        self.article_tier = article_tier
        self.demote_tier = demote_tier
        self.cluster_similarity = cluster_similarity
        self.article_match_similarity = article_match_similarity
        self.keyword_overlap = keyword_overlap
        self.min_cluster = min_cluster
        self.link_weight = link_weight
        self.max_evidence_chars = max_evidence_chars

    # ─── internal db helpers (friend of GraphMemory — same package) ────────

    def _rows(self, where: str = "", params: tuple = ()) -> list:
        with self.graph._connect() as conn:
            return conn.execute(
                "SELECT id, label, node_type, content, tags, tier, role, "
                "created_at, updated_at, metadata, embedding FROM nodes "
                + where,
                params,
            ).fetchall()

    @staticmethod
    def _md_of(row) -> Dict[str, Any]:
        try:
            md = json.loads(row[9] or "{}")
        except ValueError:
            md = {}
        return md if isinstance(md, dict) else {}

    def _merge_node_metadata(self, node_id: str, patch: Dict[str, Any]) -> None:
        """Merge *patch* into a node's metadata JSON (never replaces wholesale)."""
        try:
            with self.graph._connect() as conn:
                row = conn.execute(
                    "SELECT metadata FROM nodes WHERE id = ?", (node_id,)
                ).fetchone()
                if not row:
                    return
                try:
                    md = json.loads(row[0] or "{}")
                except ValueError:
                    md = {}
                md.update(patch)
                conn.execute(
                    "UPDATE nodes SET metadata = ? WHERE id = ?",
                    (json.dumps(md), node_id),
                )
        except Exception as exc:  # noqa: BLE001 — metadata patch is best-effort
            logger.debug("wiki metadata patch failed for %s: %s", node_id, exc)

    async def _articles(self) -> List[GraphNode]:
        rows = self._rows("WHERE node_type = ?", (WIKI_NODE_TYPE,))
        out: List[GraphNode] = []
        for r in rows:
            node = await self.graph.get_node(r[0])
            if node:
                out.append(node)
        return out

    async def _article_by_slug(self, slug: str) -> Optional[GraphNode]:
        for art in await self._articles():
            if (art.metadata or {}).get("slug") == slug:
                return art
        return None

    # ─── CONSOLIDATE ────────────────────────────────────────────────────────

    async def consolidate(
        self,
        llm: Callable,
        since: Optional[float] = None,
        budget: Optional[int] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Cluster unconsolidated episodic/fact memories and turn each cluster
        into a drafted or updated wiki article via the injected *llm*.

        - ``since``: only consider nodes created/updated at/after this unix
          timestamp (None = all unconsolidated nodes).
        - ``budget``: max clusters (articles drafted/updated) per run
          (None = 5).
        - Sources of a written article are DEMOTED to :attr:`demote_tier`
          (fast decay) via :meth:`GraphMemory.promote`, except PERMANENT-tier
          nodes and preserve-roles (preference/identity/correction) which are
          only marked consolidated. Contradicted sources are marked
          superseded by the article.

        Returns ``{examined, clusters, articles_created, articles_updated,
        sources_consolidated, sources_demoted, skipped}``.
        """
        now = now if now is not None else time.time()
        budget = 5 if budget is None else max(1, int(budget))

        candidates = self._collect_candidates(since)
        stats: Dict[str, Any] = {
            "examined": len(candidates), "clusters": 0,
            "articles_created": [], "articles_updated": [],
            "sources_consolidated": 0, "sources_demoted": 0, "skipped": 0,
        }
        if not candidates:
            return stats

        clusters = self._cluster(candidates)
        stats["clusters"] = len(clusters)

        articles = await self._articles()
        done = 0
        for cluster in clusters:
            if done >= budget:
                stats["skipped"] += 1
                continue
            target = self._match_article(cluster, articles)
            if target is None and len(cluster["rows"]) < self.min_cluster:
                stats["skipped"] += 1
                continue  # lone memory with no matching article — stays raw

            evidence = self._evidence_block(cluster["rows"])
            if target is None:
                raw = await _call_llm(
                    llm, _DRAFT_SYSTEM, f"MEMORY EVIDENCE:\n{evidence}")
            else:
                raw = await _call_llm(
                    llm, _UPDATE_SYSTEM,
                    f"CURRENT ARTICLE ({target.label}):\n"
                    f"{(target.content or '')[:4000]}\n\n"
                    f"NEW EVIDENCE:\n{evidence}",
                )
            parsed = _parse_json_block(raw)
            markdown = str(parsed.get("markdown", "") or "").strip()
            title = str(parsed.get("title", "") or "").strip()
            contradicted = [
                str(x) for x in (parsed.get("contradicted_sources") or [])
                if isinstance(x, (str, int))
            ]
            if not markdown:
                # Repair-tolerant: a non-empty non-JSON reply IS the article.
                markdown = raw.strip()
            if not markdown:
                stats["skipped"] += 1
                continue  # llm gave nothing — never demote unconsolidated raw
            if not title:
                title = target.label if target is not None else self._default_title(cluster)

            source_ids = [r[0] for r in cluster["rows"]]
            article = await self._write_article(
                title, markdown, source_ids,
                contradicted=[c for c in contradicted if c in source_ids],
                existing=target, now=now,
            )
            if article is None:
                stats["skipped"] += 1
                continue
            if target is None:
                stats["articles_created"].append(article.label)
                articles.append(article)  # later clusters can match it
            else:
                stats["articles_updated"].append(article.label)
                if article.id != target.id:
                    articles.append(article)

            demoted = self._consume_sources(cluster["rows"], article.id)
            stats["sources_consolidated"] += len(source_ids)
            stats["sources_demoted"] += demoted
            done += 1
        return stats

    def _collect_candidates(self, since: Optional[float]) -> list:
        """Unconsolidated, live, non-article nodes (episodics/facts/entities)."""
        rows = self._rows("WHERE node_type != ?", (WIKI_NODE_TYPE,))
        out = []
        for r in rows:
            md = self._md_of(r)
            if md.get("consolidated_into"):
                continue
            if md.get("superseded_by") or md.get("stale"):
                continue
            if since is not None:
                latest = max(float(r[7] or 0.0), float(r[8] or 0.0))
                if latest < since:
                    continue
            out.append(r)
        out.sort(key=lambda r: (float(r[7] or 0.0), r[0]))  # deterministic
        return out

    def _cluster(self, rows: list) -> List[Dict[str, Any]]:
        """Greedy embedding+keyword clustering (deterministic given row order)."""
        from adk.graph_memory import _blob_to_embed

        clusters: List[Dict[str, Any]] = []
        for r in rows:
            emb = _blob_to_embed(r[10]) if r[10] else []
            kws = set(extract_keywords(f"{r[1]} {r[3] or ''}"))
            best = None
            best_score = 0.0
            for cl in clusters:
                sim = 0.0
                if emb and cl["centroid"]:
                    sim = cosine_similarity(emb, cl["centroid"])
                overlap = len(kws & cl["keywords"])
                if sim >= self.cluster_similarity or overlap >= self.keyword_overlap:
                    score = sim + 0.05 * overlap
                    if score > best_score:
                        best, best_score = cl, score
            if best is None:
                clusters.append({
                    "rows": [r], "keywords": set(kws),
                    "centroid": list(emb), "n": 1 if emb else 0,
                })
            else:
                best["rows"].append(r)
                best["keywords"] |= kws
                if emb:
                    if best["centroid"] and len(best["centroid"]) == len(emb):
                        n = best["n"]
                        best["centroid"] = [
                            (c * n + e) / (n + 1)
                            for c, e in zip(best["centroid"], emb)
                        ]
                        best["n"] = n + 1
                    elif not best["centroid"]:
                        best["centroid"] = list(emb)
                        best["n"] = 1
        return clusters

    def _match_article(
        self, cluster: Dict[str, Any], articles: List[GraphNode],
    ) -> Optional[GraphNode]:
        """Existing article this cluster is new evidence FOR (else None)."""
        if not articles:
            return None
        # Slug match on any candidate label wins outright.
        label_slugs = {_slugify(r[1]) for r in cluster["rows"]}
        for art in articles:
            if (art.metadata or {}).get("slug") in label_slugs:
                return art
        best = None
        best_sim = 0.0
        for art in articles:
            art_kws = set(extract_keywords(f"{art.label} {art.content or ''}"))
            overlap = len(cluster["keywords"] & art_kws)
            sim = 0.0
            if cluster["centroid"]:
                emb = self._embedding_of(art.id)
                if emb:
                    sim = cosine_similarity(cluster["centroid"], emb)
            if sim >= self.article_match_similarity or overlap >= self.keyword_overlap + 1:
                score = sim + 0.05 * overlap
                if score > best_sim:
                    best, best_sim = art, score
        return best

    def _embedding_of(self, node_id: str) -> list:
        from adk.graph_memory import _blob_to_embed
        try:
            with self.graph._connect() as conn:
                row = conn.execute(
                    "SELECT embedding FROM nodes WHERE id = ?", (node_id,)
                ).fetchone()
            return _blob_to_embed(row[0]) if row and row[0] else []
        except Exception:  # noqa: BLE001
            return []

    def _evidence_block(self, rows: list) -> str:
        lines = []
        total = 0
        for r in rows:
            line = f"- id={r[0]} [{r[6] or 'fact'}/{r[5] or 'persistent'}] {r[1]}: {(r[3] or '')[:400]}"
            total += len(line)
            if total > self.max_evidence_chars:
                break
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _default_title(cluster: Dict[str, Any]) -> str:
        labels = [str(r[1]) for r in cluster["rows"] if r[1]]
        return labels[0][:80] if labels else "Consolidated notes"

    async def _write_article(
        self,
        title: str,
        markdown: str,
        source_ids: List[str],
        *,
        contradicted: Optional[List[str]] = None,
        existing: Optional[GraphNode] = None,
        now: Optional[float] = None,
    ) -> Optional[GraphNode]:
        """Create or revise a wiki article node: metadata revision bookkeeping,
        CONSOLIDATED_FROM + wiki_link edges, governance-ledgered revision,
        supersession of contradicted sources. Returns the article node."""
        now = now if now is not None else time.time()
        title = title.strip()[:120] or "Consolidated notes"
        slug = _slugify(title)
        contradicted = contradicted or []

        # Deterministic citation footer — sources are cited even if the llm
        # ignored the instruction.
        if source_ids:
            cited = ", ".join(source_ids)
            markdown = f"{markdown.rstrip()}\n\nSources: {cited}\n"

        prev_md: Dict[str, Any] = dict(existing.metadata or {}) if existing else {}
        revision = int(prev_md.get("revision", 0) or 0) + 1
        sources = list(dict.fromkeys(list(prev_md.get("sources", []) or []) + source_ids))
        metadata = {
            k: v for k, v in prev_md.items()
            if k not in ("_authority_labels",)
        }
        metadata.update({
            "slug": slug, "revision": revision, "sources": sources[-200:],
            "wiki": True, "last_consolidated": now,
        })

        try:
            article = await self.graph.add_node(
                label=title, node_type=WIKI_NODE_TYPE, content=markdown,
                tags=["wiki_article", slug], tier=self.article_tier,
                role="insight", metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("wiki article write failed (%s): %s", title, exc)
            return None

        # Title changed on update → the old article node is superseded by this one.
        if existing is not None and existing.id != article.id:
            self._merge_node_metadata(existing.id, {
                "superseded_by": article.id, "stale": True,
                "supersede_reason": "wiki: article retitled on consolidation",
            })
            try:
                await self.graph.add_edge(article.id, existing.id, "supersedes", 1.0)
            except Exception:  # noqa: BLE001
                pass

        # CONSOLIDATED_FROM provenance edges.
        for sid in source_ids:
            try:
                await self.graph.add_edge(article.id, sid, EDGE_CONSOLIDATED_FROM, 1.0)
            except Exception:  # noqa: BLE001
                pass

        # Contradicted sources → superseded by the article (governed knowledge wins).
        for sid in contradicted:
            self._merge_node_metadata(sid, {
                "superseded_by": article.id, "stale": True,
                "supersede_reason": f"wiki: contradicted by article {slug}",
            })
            try:
                await self.graph.add_edge(article.id, sid, "supersedes", 1.0)
            except Exception:  # noqa: BLE001
                pass

        # [[wikilinks]] between articles → wiki_link edges.
        try:
            for link in set(_WIKILINK_RE.findall(markdown)):
                target = await self._article_by_slug(_slugify(link))
                if target and target.id != article.id:
                    await self.graph.add_edge(article.id, target.id, EDGE_WIKI_LINK, 0.8)
        except Exception as exc:  # noqa: BLE001
            logger.debug("wiki link edges failed: %s", exc)

        # Governance ledger: every revision is auditable.
        ledger, _tombs = self.graph._governance_stores()
        if ledger is not None:
            try:
                from adk.graph_rag.governance import MutationType
                ledger.record(
                    MutationType.STORE if revision == 1 else MutationType.UPDATE,
                    node_id=article.id,
                    after={"id": article.id, "slug": slug, "revision": revision,
                           "title": title},
                    source=f"memory_wiki:{self.graph._agent}",
                    reason="wiki consolidate",
                    related_ids=source_ids,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("wiki revision ledger failed: %s", exc)
        return article

    def _consume_sources(self, rows: list, article_id: str) -> int:
        """Mark sources consolidated + demote non-exempt ones to the fast-decay
        tier via ``promote()``. Returns the number demoted."""
        demoted = 0
        for r in rows:
            nid = r[0]
            self._merge_node_metadata(nid, {"consolidated_into": article_id})
            tier = (r[5] or "persistent").strip().lower()
            role = (r[6] or "").strip().lower()
            if tier == "permanent" or role in _DEMOTE_EXEMPT_ROLES:
                continue
            try:
                if self.graph.promote(nid, tier=self.demote_tier):
                    demoted += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("wiki demote failed for %s: %s", nid, exc)
        return demoted

    # ─── LINT ────────────────────────────────────────────────────────────────

    async def lint(
        self,
        llm: Optional[Callable] = None,
        stale_after_days: float = 30.0,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Deterministic wiki health checks + an optional LLM audit pass.

        Findings: ``orphan_link`` (a [[wikilink]] with no target article),
        ``empty_article``, ``stale_article`` (not updated in *stale_after_days*),
        ``contradiction`` (a source still live in the graph while marked
        superseded by an article), plus llm-reported issues verbatim.
        """
        now = now if now is not None else time.time()
        findings: List[Dict[str, Any]] = []
        articles = await self._articles()
        slugs = {(a.metadata or {}).get("slug") or _slugify(a.label) for a in articles}

        for art in articles:
            slug = (art.metadata or {}).get("slug") or _slugify(art.label)
            content = (art.content or "").strip()
            if len(content) < 40:
                findings.append({
                    "kind": "empty_article", "article": slug,
                    "message": f"Article '{slug}' has no substantive content",
                })
            for link in set(_WIKILINK_RE.findall(content)):
                if _slugify(link) not in slugs:
                    findings.append({
                        "kind": "orphan_link", "article": slug, "link": link,
                        "message": f"[[{link}]] in '{slug}' has no target article",
                    })
            updated = float(art.updated_at or art.created_at or now)
            if (now - updated) > stale_after_days * 86400.0:
                findings.append({
                    "kind": "stale_article", "article": slug,
                    "message": f"Article '{slug}' not updated in {stale_after_days:.0f}+ days",
                })

        # Contradiction pairs: nodes marked superseded by an article but still live.
        article_ids = {a.id for a in articles}
        for r in self._rows("WHERE node_type != ?", (WIKI_NODE_TYPE,)):
            md = self._md_of(r)
            sup = md.get("superseded_by")
            if sup in article_ids:
                findings.append({
                    "kind": "contradiction", "node": r[0], "article": sup,
                    "message": f"Live memory {r[0]} ('{r[1]}') is contradicted/"
                               f"superseded by article {sup}",
                })

        if llm is not None and articles:
            index = "\n".join(
                f"- [[{(a.metadata or {}).get('slug') or _slugify(a.label)}]]: "
                f"{(a.content or '')[:200]}"
                for a in articles
            )
            raw = await _call_llm(llm, _LINT_SYSTEM, f"ARTICLE INDEX:\n{index}")
            if raw:
                try:
                    start = raw.find("[")
                    end = raw.rfind("]")
                    items = json.loads(raw[start : end + 1]) if start != -1 and end > start else []
                    for item in items:
                        if isinstance(item, dict) and item.get("message"):
                            item.setdefault("kind", "llm")
                            findings.append(item)
                except ValueError:
                    findings.append({"kind": "llm", "article": "",
                                     "message": raw.strip()[:500]})
        return findings

    # ─── PRUNE (tombstone → hard-delete) ────────────────────────────────────

    async def prune(
        self,
        relevance_floor: float = 0.05,
        hard_delete_after_days: float = 14.0,
        now: Optional[float] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Delete decayed knowledge — for real.

        Article relevance = tier freshness × (1 + reinforcement bonus)
        (reinforce-on-recall keeps used articles alive) **+**
        ``link_weight × link-degree to LIVE memories`` (an article whose
        sources/links still exist and aren't stale never starves).

        Below ``relevance_floor`` → the article is ENTOMBED (reversible
        governance tombstone + FORGET ledger entry) and removed from the graph
        (node row incl. embedding, edges, keywords). Tombstones from THIS
        pruner older than ``hard_delete_after_days`` are then PURGED — the
        snapshot itself is destroyed, so the content/embedding are
        irrecoverable (the true-deletion requirement). A node is never
        deleted without a tombstone first.

        Returns ``{examined, tombstoned, hard_deleted, kept, would_prune,
        tombstones}``.
        """
        from adk.unified_contract import MemoryRecord

        now = now if now is not None else time.time()
        stats: Dict[str, Any] = {
            "examined": 0, "tombstoned": 0, "hard_deleted": 0, "kept": 0,
            "would_prune": [], "tombstones": {},
        }
        ledger, tombs = self.graph._governance_stores()

        rows = self._rows("WHERE node_type = ?", (WIKI_NODE_TYPE,))
        stats["examined"] = len(rows)
        for r in rows:
            nid = r[0]
            md = self._md_of(r)
            last = float(md.get("last_reinforced", r[8] or r[7] or now) or now)
            reinf = int(md.get("reinforcement_count", 0) or 0)
            tier = (r[5] or self.article_tier).strip().lower()
            try:
                rec = MemoryRecord(content="", tier=tier, last_reinforced=last,
                                   reinforcement_count=reinf)
            except ValueError:
                rec = MemoryRecord(content="", last_reinforced=last,
                                   reinforcement_count=reinf)
            base = rec.freshness(now) * (1.0 + rec.reinforcement_bonus())
            live = self._live_link_degree(nid)
            relevance = base + self.link_weight * min(live, 10)
            if relevance >= relevance_floor:
                stats["kept"] += 1
                continue
            stats["would_prune"].append(nid)
            if dry_run:
                continue
            if tombs is None:
                logger.debug("wiki prune: no tombstone store — keeping %s", nid)
                stats["kept"] += 1
                continue  # never delete without a tombstone
            snap = {
                "id": nid, "label": r[1], "node_type": r[2], "content": r[3],
                "tags": r[4], "tier": tier, "role": r[6],
                "created_at": r[7], "updated_at": r[8], "metadata": md,
                # Logical prune clock — the hard-delete window is measured
                # against THIS (injectable ``now``), not wall-clock, so a
                # tombstone always survives at least one full window even
                # under a fake/fast-forwarded clock.
                "pruned_at": now,
            }
            try:
                with self.graph._connect() as conn:
                    erows = conn.execute(
                        "SELECT source_id, target_id, relation, weight FROM edges "
                        "WHERE source_id = ? OR target_id = ?", (nid, nid),
                    ).fetchall()
                if erows:
                    snap["edges"] = [
                        {"source_id": e[0], "target_id": e[1],
                         "relation": e[2], "weight": e[3]} for e in erows]
            except Exception as exc:  # noqa: BLE001
                logger.debug("wiki prune edge snapshot failed for %s: %s", nid, exc)
            try:
                tomb_id = tombs.entomb(
                    snap,
                    reason=f"{PRUNE_REASON_PREFIX}: relevance "
                           f"{relevance:.4f} < floor {relevance_floor}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("wiki prune entomb failed for %s — kept: %s", nid, exc)
                stats["kept"] += 1
                continue
            if ledger is not None:
                try:
                    from adk.graph_rag.governance import MutationType
                    ledger.record(
                        MutationType.FORGET, node_id=nid, before=snap,
                        source=f"memory_wiki:{self.graph._agent}",
                        reason="wiki prune", related_ids=[tomb_id],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("wiki prune ledger failed for %s: %s", nid, exc)
            with self.graph._connect() as conn:
                conn.execute(
                    "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
                    (nid, nid),
                )
                conn.execute("DELETE FROM keywords WHERE node_id = ?", (nid,))
                conn.execute("DELETE FROM nodes WHERE id = ?", (nid,))
            stats["tombstoned"] += 1
            stats["tombstones"][nid] = tomb_id

        # Hard-delete pass: purge OUR tombstones past the retention window.
        if not dry_run and tombs is not None:
            cutoff = now - hard_delete_after_days * 86400.0
            for rec in list(tombs.all()):
                reason = str(rec.get("reason", "") or "")
                if not reason.startswith(PRUNE_REASON_PREFIX):
                    continue
                snap_at = (rec.get("snapshot") or {}).get("pruned_at")
                entombed_at = float(snap_at or rec.get("timestamp", now) or now)
                if entombed_at > cutoff:
                    continue
                tomb_id = rec.get("tombstone_id", "")
                purged = False
                try:
                    purged = tombs.purge(tomb_id)
                except AttributeError:
                    logger.debug("tombstone store has no purge(); skipping hard-delete")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("wiki hard-delete failed for %s: %s", tomb_id, exc)
                if purged:
                    stats["hard_deleted"] += 1
                    if ledger is not None:
                        try:
                            from adk.graph_rag.governance import MutationType
                            # Deliberately NO before/after snapshot — the point
                            # of a hard-delete is that the content is gone.
                            ledger.record(
                                MutationType.FORGET,
                                node_id=str(rec.get("node_id", "")),
                                source=f"memory_wiki:{self.graph._agent}",
                                reason="wiki prune: hard-delete after retention window",
                                related_ids=[tomb_id],
                            )
                        except Exception:  # noqa: BLE001
                            pass
        return stats

    def _live_link_degree(self, node_id: str) -> int:
        """Count outgoing links (consolidated_from / wiki_link / supersedes) whose
        target still exists in the graph and isn't stale/superseded."""
        live = 0
        try:
            with self.graph._connect() as conn:
                rows = conn.execute(
                    "SELECT n.metadata FROM edges e JOIN nodes n ON e.target_id = n.id "
                    "WHERE e.source_id = ?", (node_id,),
                ).fetchall()
            for (md_json,) in rows:
                try:
                    md = json.loads(md_json or "{}")
                except ValueError:
                    md = {}
                if not md.get("superseded_by") and not md.get("stale"):
                    live += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("wiki live-link degree failed for %s: %s", node_id, exc)
        return live

    # ─── RECALL (article-first RAG) ─────────────────────────────────────────

    async def recall(
        self, query: str, limit: int = 8, reinforce: bool = True,
    ) -> List[GraphNode]:
        """Article-first recall: curated wiki articles rank ABOVE raw nodes.

        Returned articles are reinforced (``reinforce=True``) so recall feeds
        the prune relevance — used knowledge stays, unused decays out.
        """
        if not query.strip() or limit <= 0:
            return []
        try:
            ranked = await self.graph.recall_with_activation(
                query, limit=max(limit * 3, 12), reinforce=False,
            )
        except Exception:  # noqa: BLE001 — degraded environments fall to search
            ranked = await self.graph.search(query, limit=max(limit * 3, 12))
        articles = [n for n in ranked if n.node_type == WIKI_NODE_TYPE]
        raw = [n for n in ranked if n.node_type != WIKI_NODE_TYPE]
        out = (articles + raw)[:limit]
        if reinforce and articles:
            try:
                self.graph._reinforce_nodes(
                    [a.id for a in articles if a in out])
            except Exception as exc:  # noqa: BLE001
                logger.debug("wiki recall reinforce failed: %s", exc)
        return out
