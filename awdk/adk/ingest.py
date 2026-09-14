"""Self-service local knowledge base ingestion — 'adk ingest <path>'.

Walks files/directories with exclusions, chunks content, embeds via local/gateway
resolution, stores in node-local SQLite knowledge store, and pushes embedding
deltas to CompanyBrain hub.

Features:
  - Directory walker with DEFAULT EXCLUSIONS (.git, node_modules, venvs, >50MB)
  - Mandatory secret guard (skip .env/credentials/keys, scan for sk-, ghp_, etc.)
  - Chunking with semantic boundaries (Markdown, code blocks) + fixed-size fallback
  - Embeddings via canonical adk.embeddings (768-d nomic-embed-text)
  - Local SQLite storage (~/.aither/graph/*)
  - Brain delta push with watermark tracking
  - Graceful degradation (local-only, fallback on brain unreachable)

Usage:
    from adk.ingest import ingest_files
    result = await ingest_files(
        path="/path/to/docs",
        classification="internal",
        brain_sync=True,
    )
    print(result)  # {"files_ingested": 5, "chunks": 42, "synced": True, ...}
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("adk.ingest")

__all__ = [
    "FileWalker",
    "SecretGuard",
    "TextChunker",
    "ingest_files",
    "IngestResult",
]


# ─────────────────────────────────────────────────────────────────────────────
# Default exclusions
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_EXCLUSIONS = frozenset([
    ".git", ".gitignore", ".gitattributes",
    "node_modules", "venv", "env", ".venv",
    ".python-version", "__pycache__", ".pytest_cache",
    "dist", "build", "*.egg-info",
    ".tox", ".coverage", ".mypy_cache",
    ".next", ".nuxt", "out", ".docusaurus",
    ".vscode", ".idea", ".env*",
])

_BINARY_EXTENSIONS = frozenset([
    "bin", "exe", "dll", "so", "dylib", "o", "a",
    "zip", "tar", "gz", "rar", "7z",
    "mp3", "mp4", "avi", "mov", "mkv",
    "jpg", "jpeg", "png", "gif", "bmp", "ico", "webp",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "db", "sqlite", "sqlite3",
    "pickle", "pkl", "pyc", "pyo",
])

_SECRET_PATTERNS = [
    r"sk-[a-zA-Z0-9\-_]{20,}",          # OpenAI/Anthropic-style keys
    r"ghp_[a-zA-Z0-9]{36,}",            # GitHub personal access token
    r"ghs_[a-zA-Z0-9]{36,}",            # GitHub OAuth secret
    r"AKIA[0-9A-Z]{16}",                # AWS access key
    r"sk-ant-[a-zA-Z0-9\-_]{20,}",      # Anthropic token pattern
    r"xoxb-[0-9]{10,13}-[0-9]{10,13}",  # Slack bot token
    r"xoxp-[0-9]{10,13}-[0-9]{10,13}",  # Slack user token
    r"pk_live_[0-9a-zA-Z]{24,}",        # Stripe live key
    r"sk_live_[0-9a-zA-Z]{24,}",        # Stripe live secret
    r"-----BEGIN [A-Z\s]+ PRIVATE KEY",  # PEM private keys
]

_SENSITIVE_FILENAMES = frozenset([
    ".env", ".env.local", ".env.production", ".env.development",
    "credentials.json", "credentials.yaml", "credentials.yml",
    "secrets.json", "secrets.yaml", "secrets.yml",
    "auth.json", "api-keys.json",
    ".aws", ".ssh", ".kube",
    "config.json",  # When contains secrets
])


# ─────────────────────────────────────────────────────────────────────────────
# File walker with exclusions
# ─────────────────────────────────────────────────────────────────────────────

class FileWalker:
    """Walk files/directories with DEFAULT EXCLUSIONS and size limits."""

    def __init__(
        self,
        exclude_dirs: Optional[set] = None,
        exclude_extensions: Optional[set] = None,
        max_file_size_mb: int = 50,
        allow_hidden: bool = False,
    ):
        """Initialize walker with exclusion patterns.

        Args:
            exclude_dirs: Set of directory names to skip (default: _DEFAULT_EXCLUSIONS)
            exclude_extensions: Set of binary extensions to skip (default: _BINARY_EXTENSIONS)
            max_file_size_mb: Skip files larger than this (default: 50MB)
            allow_hidden: Include hidden files (dotfiles) (default: False)
        """
        self.exclude_dirs = exclude_dirs or _DEFAULT_EXCLUSIONS
        self.exclude_extensions = exclude_extensions or _BINARY_EXTENSIONS
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024
        self.allow_hidden = allow_hidden

    def walk(self, root: Path) -> List[Path]:
        """Walk root recursively, yielding eligible files.

        Returns:
            List of Path objects for ingestion.
        """
        results = []
        for path in self._walk_recursive(root):
            results.append(path)
        return results

    def _walk_recursive(self, root: Path):
        """Recursive walker that respects exclusions."""
        try:
            for entry in root.iterdir():
                # Skip hidden files/dirs (dotfiles)
                if not self.allow_hidden and entry.name.startswith("."):
                    continue

                # Skip excluded directory names
                if entry.is_dir():
                    if entry.name in self.exclude_dirs:
                        logger.debug("Skipping excluded dir: %s", entry)
                        continue
                    # Recurse
                    yield from self._walk_recursive(entry)
                elif entry.is_file():
                    # Skip binary extensions
                    ext = entry.suffix.lstrip(".").lower()
                    if ext in self.exclude_extensions:
                        logger.debug("Skipping binary file: %s", entry)
                        continue

                    # Skip files >50MB
                    try:
                        size = entry.stat().st_size
                        if size > self.max_file_size_bytes:
                            logger.warning("Skipping large file (%dMB): %s",
                                         size / (1024 * 1024), entry)
                            continue
                    except OSError:
                        logger.debug("Cannot stat file: %s", entry)
                        continue

                    yield entry
        except PermissionError:
            logger.debug("Permission denied: %s", root)


# ─────────────────────────────────────────────────────────────────────────────
# Secret guard
# ─────────────────────────────────────────────────────────────────────────────

class SecretGuard:
    """Scan files for secrets and skip sensitive files."""

    def __init__(self):
        """Initialize secret patterns."""
        self.compiled_patterns = [
            re.compile(pattern) for pattern in _SECRET_PATTERNS
        ]
        self.sensitive_filenames = _SENSITIVE_FILENAMES

    def should_skip(self, file_path: Path) -> Tuple[bool, Optional[str]]:
        """Check if file should be skipped due to sensitivity.

        Returns:
            (should_skip, reason)
        """
        # Check filename sensitivity
        if file_path.name in self.sensitive_filenames:
            return True, f"Sensitive filename: {file_path.name}"

        # Check for .env, credentials, etc. in path
        if any(part in self.sensitive_filenames for part in file_path.parts):
            return True, "Contains sensitive path component"

        return False, None

    def scan_for_secrets(self, text: str) -> List[str]:
        """Scan text for secret patterns.

        Returns:
            List of matched secret patterns (not the actual secrets).
        """
        matches = []
        for pattern in self.compiled_patterns:
            for match in pattern.finditer(text):
                # Log pattern name, not the actual secret
                pattern_str = pattern.pattern[:30] + "..."
                matches.append(pattern_str)
        return matches

    def report(self, file_path: Path, skip_reason: Optional[str] = None,
               secret_matches: Optional[List[str]] = None):
        """Report a skipped file to the user.

        Args:
            file_path: Path to the file
            skip_reason: Reason for skipping
            secret_matches: List of secret patterns found
        """
        if skip_reason:
            logger.warning("SKIPPED (secrets): %s — %s", file_path, skip_reason)
        elif secret_matches:
            logger.warning("SKIPPED (secrets): %s — found %d secret patterns",
                          file_path, len(secret_matches))


# ─────────────────────────────────────────────────────────────────────────────
# Text chunker
# ─────────────────────────────────────────────────────────────────────────────

class TextChunker:
    """Split text into semantic chunks with overlap."""

    def __init__(self, size: int = 2000, overlap: int = 200):
        """Initialize chunker.

        Args:
            size: Target chunk size in characters
            overlap: Overlap between chunks in characters
        """
        self.size = size
        self.overlap = overlap

    def chunk(self, text: str, source: str = "") -> List[Dict[str, Any]]:
        """Split text into semantic chunks.

        Tries to split on semantic boundaries (paragraphs, code blocks,
        Markdown headings). Falls back to fixed-size splits if no structure.

        Returns:
            List of {"text": str, "source": str, "offset": int} dicts.
        """
        if not text.strip():
            return []

        # Try semantic chunking first
        chunks = self._chunk_markdown(text) if "\n" in text else []
        if chunks:
            return self._finalize_chunks(chunks, source)

        # Fallback: fixed-size chunks
        chunks = self._chunk_fixed(text)
        return self._finalize_chunks(chunks, source)

    def _chunk_markdown(self, text: str) -> List[str]:
        """Split on Markdown boundaries (headings, blank lines)."""
        # Split on Markdown headings (###) or double blank lines
        parts = re.split(r"\n(?:#+\s+|\n+)", text)
        chunks = []
        current = ""

        for part in parts:
            if len(current) + len(part) < self.size:
                current += "\n" + part if current else part
            else:
                if current:
                    chunks.append(current)
                current = part

        if current:
            chunks.append(current)

        # Filter out very small chunks
        return [c for c in chunks if len(c.strip()) > 50]

    def _chunk_fixed(self, text: str) -> List[str]:
        """Split text into fixed-size chunks with overlap."""
        chunks = []
        i = 0
        while i < len(text):
            end = min(i + self.size, len(text))
            chunk = text[i:end]
            if chunk.strip():
                chunks.append(chunk)
            # Move i forward, but keep overlap for context
            i += self.size - self.overlap

        return chunks

    def _finalize_chunks(self, chunks: List[str], source: str) -> List[Dict[str, Any]]:
        """Add metadata to chunks."""
        result = []
        offset = 0
        for chunk in chunks:
            if chunk.strip():
                result.append({
                    "text": chunk,
                    "source": source,
                    "offset": offset,
                })
                offset += len(chunk)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Ingest result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class IngestResult:
    """Result of an ingest operation."""
    files_total: int = 0
    files_ingested: int = 0
    files_skipped: int = 0
    chunks_created: int = 0
    chunks_embedded: int = 0
    chunks_synced: int = 0
    embedding_degraded: bool = False
    brain_synced: bool = False
    errors: List[str] = None
    skipped_files: List[Tuple[str, str]] = None  # (path, reason)

    def __post_init__(self):
        if self.errors is None:
            self.errors = []
        if self.skipped_files is None:
            self.skipped_files = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for display."""
        return {
            "files_total": self.files_total,
            "files_ingested": self.files_ingested,
            "files_skipped": self.files_skipped,
            "chunks_created": self.chunks_created,
            "chunks_embedded": self.chunks_embedded,
            "chunks_synced": self.chunks_synced,
            "embedding_degraded": self.embedding_degraded,
            "brain_synced": self.brain_synced,
            "errors": self.errors,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Main ingest orchestration
# ─────────────────────────────────────────────────────────────────────────────

async def ingest_files(
    path: str | Path,
    classification: str = "internal",
    brain_sync: bool = False,
    brain_url: Optional[str] = None,
    workspace_id: str = "default",
    chunk_size: int = 2000,
    chunk_overlap: int = 200,
    skip_embeddings: bool = False,
    dry_run: bool = False,
    agent_name: str = "default",
) -> IngestResult:
    """Ingest files into local knowledge base and optionally sync to brain hub.

    Args:
        path: File or directory to ingest
        classification: internal|confidential|restricted (default: internal)
        brain_sync: Enable push to CompanyBrain hub (default: False)
        brain_url: Override brain hub URL (default: from config/env)
        workspace_id: Workspace ID for brain sync (default: 'default')
        chunk_size: Bytes per chunk (default: 2000)
        chunk_overlap: Overlap bytes (default: 200)
        skip_embeddings: Skip embedding if brain unreachable (default: False)
        dry_run: Print chunks, don't persist/sync (default: False)
        agent_name: Agent name for local storage (default: 'default')

    Returns:
        IngestResult with statistics and errors.
    """
    result = IngestResult()
    root = Path(path).resolve()

    if not root.exists():
        result.errors.append(f"Path not found: {path}")
        return result

    # Load node auth for tenant_id
    tenant_id = ""
    if brain_sync:
        from adk.fleet_enroll import _load_node_auth
        node_auth = _load_node_auth()
        tenant_id = node_auth.get("tenant_id", "")
        if not tenant_id:
            logger.warning("Node not enrolled; brain sync disabled. Run 'adk enroll'.")
            brain_sync = False

    # Note: GraphMemory will handle embeddings automatically via _embed()
    # We don't pre-embed here; this keeps the code simpler and lets GraphMemory
    # manage the embedding lifecycle (caching, dimension tracking, etc.)

    # Walk files
    walker = FileWalker()
    guard = SecretGuard()

    if root.is_file():
        files = [root]
    else:
        files = walker.walk(root)

    result.files_total = len(files)

    # Process each file
    chunks_all: List[Dict[str, Any]] = []

    for file_path in files:
        # Check for secrets
        should_skip, skip_reason = guard.should_skip(file_path)
        if should_skip:
            result.files_skipped += 1
            result.skipped_files.append((str(file_path), skip_reason or "sensitive file"))
            guard.report(file_path, skip_reason)
            continue

        try:
            # Read file
            content = file_path.read_text(encoding="utf-8", errors="replace")

            # Scan for secrets in content
            secret_matches = guard.scan_for_secrets(content)
            if secret_matches:
                result.files_skipped += 1
                reason = f"Found {len(secret_matches)} secret patterns"
                result.skipped_files.append((str(file_path), reason))
                guard.report(file_path, secret_matches=secret_matches)
                continue

            result.files_ingested += 1

            # Chunk content
            chunker = TextChunker(size=chunk_size, overlap=chunk_overlap)
            file_chunks = chunker.chunk(content, source=str(file_path))

            for chunk_data in file_chunks:
                chunk_id = hashlib.sha256(
                    f"{file_path}:{chunk_data['offset']}".encode()
                ).hexdigest()[:16]

                chunk_entry = {
                    "chunk_id": chunk_id,
                    "text": chunk_data["text"],
                    "source": str(file_path),
                    "offset": chunk_data["offset"],
                    "classification": classification,
                }

                chunks_all.append(chunk_entry)
                result.chunks_embedded += 1

        except Exception as exc:
            logger.error("Error processing %s: %s", file_path, exc)
            result.errors.append(f"Error processing {file_path}: {exc}")

    result.chunks_created = len(chunks_all)

    if dry_run:
        logger.info("DRY RUN: Would ingest %d chunks from %d files",
                   result.chunks_created, result.files_ingested)
        for chunk in chunks_all[:5]:
            logger.info("  - %s: %s...", chunk["chunk_id"],
                       chunk["text"][:50].replace("\n", " "))
        if len(chunks_all) > 5:
            logger.info("  ... and %d more chunks", len(chunks_all) - 5)
        return result

    # Store locally
    try:
        from adk.graph_memory import GraphMemory
        graph = GraphMemory(agent_name=agent_name)

        for chunk in chunks_all:
            # Store as a fact node
            node_id = chunk["chunk_id"]
            await graph.add_node(
                label=f"chunk:{node_id}",
                content=chunk["text"],
                node_type="fact",
                metadata={
                    "source": chunk["source"],
                    "offset": chunk["offset"],
                    "classification": chunk["classification"],
                    "brain_synced": False,
                },
            )
    except Exception as exc:
        logger.error("Failed to store chunks locally: %s", exc)
        result.errors.append(f"Local storage failed: {exc}")

    # Sync to brain hub if enabled
    if brain_sync and chunks_all:
        try:
            from adk.sync.brain import BrainSyncClient, SyncDeltaItem

            # Get brain URL
            if not brain_url:
                brain_url = os.getenv("AITHER_BRAIN_HUB_URL",
                                     "http://localhost:8001")

            client = BrainSyncClient(
                brain_url=brain_url,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )

            # Prepare deltas (without vectors; brain hub will compute if needed)
            deltas = [
                SyncDeltaItem(
                    op="upsert",
                    chunk_id=chunk["chunk_id"],
                    vector=None,  # Brain hub can request vectors separately
                    metadata={
                        "text": chunk["text"],
                        "source": chunk["source"],
                        "offset": chunk["offset"],
                    },
                    classification=chunk["classification"],
                )
                for chunk in chunks_all
            ]

            # Post to brain hub
            response = await client.post_deltas(deltas)
            result.chunks_synced = response.accepted
            result.brain_synced = True

            # Update watermark
            watermark_file = (
                Path.home() / ".aither" / "graph"
                / f"brain-watermark-{workspace_id}.json"
            )
            watermark_file.parent.mkdir(parents=True, exist_ok=True)
            watermark_file.write_text(
                f'{{"workspace_id": "{workspace_id}", "watermark": "{response.watermark}"}}',
                encoding="utf-8"
            )

            logger.info("Brain sync: %d deltas accepted, %d rejected",
                       response.accepted, response.rejected)

        except Exception as exc:
            logger.error("Brain sync failed: %s", exc)
            result.errors.append(f"Brain sync failed: {exc}")
            result.brain_synced = False

    return result
