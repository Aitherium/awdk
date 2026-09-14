"""Feed the graph from awgit's operation log instead of the filesystem.

A code graph normally re-indexes by FILE: something's mtime moved, so parse it
again and replace its nodes. That is wasteful and, worse, it is lossy in the one
case that matters most for memory.

**Renames destroy history under file-based indexing.** Move a function to
another module and a file-keyed indexer sees one file shrink and another grow.
The old node disappears, a new node appears, and everything attached to the old
one — provenance, retrieval statistics, whatever the agent had learned about it
— is silently discarded. The symbol did not change. Its address did.

awgit already records the answer. Its `NodeChange` is keyed on a stable node id
and carries `renamed_from` and `moved_from`, so the graph can follow a symbol
across a move rather than re-discovering it as a stranger. That is why wiring
awgit into graph-RAG is a memory improvement and not just an indexing
optimisation: it is the difference between a graph that remembers a function and
one that only remembers where a function used to live.

Three things this provides, all keyed on symbol identity:

  stale_nodes()       what actually changed since a commit — re-index THAT, not
                      every file that was touched
  rename_chain()      a node's prior identities, so history survives a move
  apply_provenance()  who changed a node, when, and the summary they gave,
                      attached to the graph node itself

Everything degrades to empty rather than raising when awgit is absent or the
directory is not a repo. adk must keep working outside a git tree, and a
retriever that explodes because there is no oplog is worse than one that simply
has no change information.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

#: Change types that mean "the body moved but the symbol is the same". Treating
#: these as new nodes is exactly the history loss this module exists to prevent.
_IDENTITY_PRESERVING = {"renamed", "moved"}


@dataclass
class NodeProvenance:
    """Who last touched a node, when, and why."""

    node_id: str
    actor: str
    verified: bool
    git_sha: str
    ts: Any
    summary: str
    change_type: str
    previous_ids: tuple[str, ...] = ()

    def as_metadata(self) -> dict:
        """Shape written onto `GraphNode.metadata` under `awgit`."""
        return {
            "actor": self.actor,
            "actor_verified": self.verified,
            "git_sha": self.git_sha,
            "ts": self.ts,
            "summary": self.summary,
            "change_type": self.change_type,
            "previous_ids": list(self.previous_ids),
        }


def _entries(oplog: Any, since_sha: Optional[str] = None) -> list:
    """Ledger entries, newest last. Empty (never an exception) if unavailable."""
    if oplog is None:
        return []
    try:
        entries = list(oplog.all_ops())
    except Exception:  # noqa: BLE001 - absence of history is not an error here
        return []

    if not since_sha:
        return entries

    # Everything AFTER the given commit. An unknown sha means we cannot say what
    # is new, and answering "nothing changed" would be a confident lie that
    # leaves the graph stale forever — so treat it as "all of it".
    for i, e in enumerate(entries):
        if getattr(e, "git_sha", None) == since_sha:
            return entries[i + 1:]
    return entries


def stale_nodes(oplog: Any, since_sha: Optional[str] = None) -> set[str]:
    """Node ids whose body actually changed. Re-index these, not whole files.

    Deliberately node ids and not file paths: a one-line edit to a 4,000-line
    module invalidates one symbol here, where a file-keyed indexer re-parses and
    re-embeds the entire module.
    """
    out: set[str] = set()
    for entry in _entries(oplog, since_sha):
        for change in getattr(entry, "node_changes", None) or []:
            node_id = getattr(change, "node_id", None)
            if not node_id:
                continue
            # A pure rename does not need re-embedding — the body is identical.
            # Re-embedding it would spend the expensive half of indexing to
            # arrive at the vector already stored.
            ctype = getattr(change, "change_type", "")
            same_body = (
                getattr(change, "old_body_sha", None)
                and getattr(change, "old_body_sha", None) == getattr(change, "new_body_sha", None)
            )
            if ctype in _IDENTITY_PRESERVING and same_body:
                continue
            out.add(node_id)
    return out


def rename_chain(oplog: Any, node_id: str) -> list[str]:
    """Prior identities of a node, oldest first.

    This is the half a file-based indexer cannot reconstruct at all. Without it
    a moved function is a brand-new node, and everything the graph had learned
    about it is thrown away at the moment it becomes most confusing to lose.
    """
    chain: list[str] = []
    current = node_id
    seen = {node_id}

    for entry in reversed(_entries(oplog)):
        for change in getattr(entry, "node_changes", None) or []:
            if getattr(change, "node_id", None) != current:
                continue
            prior = getattr(change, "renamed_from", None) or getattr(change, "moved_from", None)
            if prior and prior not in seen:
                chain.append(prior)
                seen.add(prior)
                current = prior
    chain.reverse()
    return chain


def apply_provenance(store: Any, oplog: Any, since_sha: Optional[str] = None) -> int:
    """Attach last-touched provenance to graph nodes. Returns how many were updated.

    Written into `metadata["awgit"]` rather than onto the node itself so an
    existing graph keeps its shape and nothing downstream has to know this ran.
    """
    updated = 0
    for prov in latest_provenance(oplog, since_sha).values():
        node = None
        try:
            node = store.get_node(prov.node_id)
        except Exception:  # noqa: BLE001 - a store without this node is normal
            node = None
        if node is None:
            continue
        meta = getattr(node, "metadata", None)
        if meta is None:
            continue
        meta["awgit"] = prov.as_metadata()
        updated += 1
    return updated


def latest_provenance(oplog: Any, since_sha: Optional[str] = None) -> dict[str, NodeProvenance]:
    """node_id -> its most recent change. Later entries win."""
    out: dict[str, NodeProvenance] = {}
    for entry in _entries(oplog, since_sha):
        for change in getattr(entry, "node_changes", None) or []:
            node_id = getattr(change, "node_id", None)
            if not node_id:
                continue
            prior = getattr(change, "renamed_from", None) or getattr(change, "moved_from", None)
            out[node_id] = NodeProvenance(
                node_id=node_id,
                # `verified_actor` is the cryptographically checked one; `actor`
                # is self-reported. Recording which it was matters — provenance
                # nobody can trust is decoration.
                actor=getattr(entry, "verified_actor", None) or getattr(entry, "actor", "") or "",
                verified=bool(getattr(entry, "actor_verified", False)),
                git_sha=getattr(entry, "git_sha", "") or "",
                ts=getattr(entry, "ts", None),
                summary=getattr(entry, "summary", "") or "",
                change_type=getattr(change, "change_type", "") or "",
                previous_ids=(prior,) if prior else (),
            )
    return out


def open_oplog(data_root: Optional[Any] = None) -> Any:
    """awgit's OpLog, or None when awgit is absent.

    None rather than a raise: adk runs on machines with no git tree, and the
    retriever should lose change information rather than stop working.
    """
    try:
        from awgit import OpLog
    except ImportError:
        return None
    try:
        return OpLog(data_root) if data_root is not None else OpLog()
    except Exception:  # noqa: BLE001 - not a repo, no store yet, etc.
        return None


def summarize(oplog: Any, since_sha: Optional[str] = None) -> dict:
    """A short report of what the graph would do with this history."""
    entries = _entries(oplog, since_sha)
    stale = stale_nodes(oplog, since_sha)
    renames = sum(
        1
        for e in entries
        for c in (getattr(e, "node_changes", None) or [])
        if getattr(c, "renamed_from", None) or getattr(c, "moved_from", None)
    )
    return {
        "commits": len(entries),
        "stale_nodes": len(stale),
        "identity_preserved": renames,
        "available": oplog is not None,
    }


def iter_changed_symbols(oplog: Any, since_sha: Optional[str] = None) -> Iterable[tuple[str, str]]:
    """(symbol, path) for each change — what a re-index would target."""
    for entry in _entries(oplog, since_sha):
        for change in getattr(entry, "node_changes", None) or []:
            symbol = getattr(change, "symbol", None)
            if symbol:
                yield symbol, getattr(change, "path", "") or ""
