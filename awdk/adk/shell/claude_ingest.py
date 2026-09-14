"""
Claude Code session ingest — conversations into the local KB / company brain.
==============================================================================

Turns Claude Code session journals into knowledge: user prompts + assistant
prose are extracted per session (tool noise and system-reminders excluded),
chunked, secret-guarded, stored in the node-local GraphMemory and optionally
pushed to the CompanyBrain hub as deltas — the same pipeline `adk ingest`
uses for files.

Incremental by construction: journals are append-only, so a per-session byte
watermark in ``~/.aither/claude_sessions/ingest_state.json`` means each run
only processes what's new since the last one. ``--watch`` loops it for
auto-sync.

Secret guard is per-chunk (not per-file): one leaked key in a tool-heavy
session skips that chunk, not the whole conversation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from adk.ingest import SecretGuard, TextChunker
from adk.shell import claude_sessions as cs

logger = logging.getLogger("adk.shell.claude_ingest")

INGEST_STATE = cs.STATE_DIR / "ingest_state.json"


@dataclass
class SessionIngestResult:
    sessions_seen: int = 0
    sessions_ingested: int = 0
    chunks_created: int = 0
    chunks_skipped_secrets: int = 0
    chunks_synced: int = 0
    brain_synced: bool = False
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sessions_seen": self.sessions_seen,
            "sessions_ingested": self.sessions_ingested,
            "chunks_created": self.chunks_created,
            "chunks_skipped_secrets": self.chunks_skipped_secrets,
            "chunks_synced": self.chunks_synced,
            "brain_synced": self.brain_synced,
            "errors": self.errors,
        }


def _head_fingerprint(path: Path, nbytes: int = 256) -> str:
    """Hash of the journal's first bytes — changes iff the file was rewritten
    (size alone misses a rewrite that ends up larger than the watermark)."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read(nbytes)).hexdigest()[:16]
    except OSError:
        return ""


def _ends_complete(path: Path) -> bool:
    """True if the journal's last byte is a newline (no torn line in flight)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return True
            f.seek(size - 1)
            return f.read(1) == b"\n"
    except OSError:
        return False


def _load_state(path: Path = None) -> dict:
    p = path or INGEST_STATE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_state(state: dict, path: Path = None) -> None:
    p = path or INGEST_STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


def extract_new_text(meta: cs.SessionMeta, from_byte: int) -> tuple[str, int]:
    """Conversation text appended since ``from_byte``, plus the new watermark.

    Turns render as ``[you]`` / ``[claude]`` blocks under a one-line session
    header so chunks stay attributable after retrieval.
    """
    parts: list[str] = []
    end = from_byte
    for role, text, ts, pos in cs.iter_journal_turns(meta.file, from_byte=from_byte):
        who = "you" if role == "user" else "claude"
        stamp = f" {ts}" if ts else ""
        parts.append(f"[{who}{stamp}]\n{text}")
        end = pos
    if not parts:
        return "", end
    header = (
        f"Claude Code session: {meta.title}\n"
        f"project: {meta.cwd}" + (f" (branch {meta.branch})" if meta.branch else "")
    )
    return header + "\n\n" + "\n\n".join(parts), end


async def ingest_sessions(
    days: float = 7,
    projects_root: Optional[Path] = None,
    classification: str = "internal",
    brain_sync: bool = False,
    brain_url: Optional[str] = None,
    workspace_id: str = "default",
    agent_name: str = "default",
    dry_run: bool = False,
    state_path: Optional[Path] = None,
    min_new_bytes: int = 512,
) -> SessionIngestResult:
    """Incrementally ingest recent sessions' conversation text.

    Only journals with at least ``min_new_bytes`` of growth are touched, so a
    watch loop is nearly free when nothing happened.
    """
    result = SessionIngestResult()
    state = _load_state(state_path)
    root = projects_root or cs.DEFAULT_PROJECTS_ROOT
    guard = SecretGuard()
    chunker = TextChunker()

    sessions = cs.scan_sessions(
        projects_root=root, scan=400, top=400,
        lookback_hours=days * 24 if days > 0 else 0,
        detect_live=False,
    )
    result.sessions_seen = len(sessions)

    tenant_id = ""
    if brain_sync and not dry_run:
        try:
            from adk.fleet_enroll import _load_node_auth
            tenant_id = _load_node_auth().get("tenant_id", "")
        except Exception as exc:
            logger.debug("node auth unavailable: %s", exc)
        if not tenant_id:
            logger.warning("Node not enrolled; brain sync disabled. Run 'adk enroll'.")
            brain_sync = False

    chunks_all: list[dict] = []
    for meta in sessions:
        entry = state.get(meta.id) or {}
        watermark = int(entry.get("bytes", 0))
        try:
            size = Path(meta.file).stat().st_size
        except OSError:
            continue
        head = _head_fingerprint(Path(meta.file))
        if size < watermark or (entry.get("head") and entry["head"] != head):
            watermark = 0     # journal rewritten/truncated — start over
        if size - watermark < min_new_bytes:
            continue
        try:
            text, end = extract_new_text(meta, watermark)
        except OSError as exc:
            result.errors.append(f"{meta.id}: {exc}")
            continue
        if not text.strip():
            # Growth was all non-conversational entries (attachments, tool
            # results) — jump the watermark to EOF so the region isn't
            # re-parsed every tick, unless a line is mid-append.
            if not dry_run:
                new_mark = size if _ends_complete(Path(meta.file)) else max(end, watermark)
                state[meta.id] = {"bytes": new_mark, "head": head, "title": meta.title}
            continue

        session_chunks = []
        for chunk in chunker.chunk(text, source=f"claude-session:{meta.id}"):
            if guard.scan_for_secrets(chunk["text"]):
                result.chunks_skipped_secrets += 1
                continue
            chunk_id = hashlib.sha256(
                f"claude-session:{meta.id}:{watermark}:{chunk['offset']}".encode()
            ).hexdigest()[:16]
            session_chunks.append({
                "chunk_id": chunk_id,
                "text": chunk["text"],
                "source": f"claude-session:{meta.id}",
                "offset": watermark + chunk["offset"],
                "classification": classification,
                "session_title": meta.title,
                "session_cwd": meta.cwd,
            })
        if session_chunks:
            chunks_all.extend(session_chunks)
            result.sessions_ingested += 1
        if not dry_run:
            state[meta.id] = {"bytes": end, "head": head, "title": meta.title}

    result.chunks_created = len(chunks_all)
    if dry_run or not chunks_all:
        return result

    # Store locally (same GraphMemory shape as `adk ingest`).
    try:
        from adk.graph_memory import GraphMemory
        graph = GraphMemory(agent_name=agent_name)
        for chunk in chunks_all:
            await graph.add_node(
                label=f"chunk:{chunk['chunk_id']}",
                content=chunk["text"],
                node_type="fact",
                metadata={
                    "source": chunk["source"],
                    "offset": chunk["offset"],
                    "classification": chunk["classification"],
                    "session_title": chunk["session_title"],
                    "session_cwd": chunk["session_cwd"],
                    "brain_synced": False,
                },
            )
    except Exception as exc:
        logger.error("Failed to store session chunks locally: %s", exc)
        result.errors.append(f"Local storage failed: {exc}")

    if brain_sync:
        try:
            import os
            from adk.sync.brain import BrainSyncClient, SyncDeltaItem
            client = BrainSyncClient(
                brain_url=brain_url or os.getenv("AITHER_BRAIN_HUB_URL", "http://localhost:8001"),
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
            deltas = [
                SyncDeltaItem(
                    op="upsert",
                    chunk_id=c["chunk_id"],
                    vector=None,
                    metadata={
                        "text": c["text"],
                        "source": c["source"],
                        "offset": c["offset"],
                        "session_title": c["session_title"],
                    },
                    classification=c["classification"],
                )
                for c in chunks_all
            ]
            response = await client.post_deltas(deltas)
            result.chunks_synced = response.accepted
            result.brain_synced = True
        except Exception as exc:
            logger.error("Brain sync failed: %s", exc)
            result.errors.append(f"Brain sync failed: {exc}")

    # Watermarks only persist once the batch has landed (or was local-only).
    _save_state(state, state_path)
    return result


def watch_sessions(
    interval: float = 300.0,
    on_event=None,
    **ingest_kwargs,
) -> None:
    """Auto-sync loop: ingest new session content every ``interval`` seconds."""
    emit = on_event or (lambda msg: None)
    while True:
        try:
            result = asyncio.run(ingest_sessions(**ingest_kwargs))
            if result.chunks_created:
                emit(f"ingested {result.chunks_created} chunk(s) from "
                     f"{result.sessions_ingested} session(s)"
                     + (f", {result.chunks_synced} synced to brain"
                        if result.brain_synced else ""))
        except Exception as exc:
            emit(f"ingest error: {exc}")
            logger.exception("session ingest tick failed")
        time.sleep(interval)
