"""Artifact detection and session registry for agent tool outputs."""

from __future__ import annotations

import json
import mimetypes
import os
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Artifact:
    """An artifact produced by an agent tool call."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    name: str = ""
    type: str = "file"  # image, code, document, file
    path: str | None = None
    url: str | None = None
    mime: str | None = None
    size: int | None = None
    tool: str | None = None
    base64: str | None = None
    message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None and v != {}}


class ArtifactRegistry:
    """Per-session artifact collection."""

    def __init__(self):
        self._sessions: dict[str, list[Artifact]] = {}

    def add(self, session_id: str, artifact: Artifact):
        self._sessions.setdefault(session_id, []).append(artifact)

    def get(self, session_id: str) -> list[Artifact]:
        return self._sessions.get(session_id, [])

    def get_by_id(self, artifact_id: str) -> Artifact | None:
        for arts in self._sessions.values():
            for a in arts:
                if a.id == artifact_id:
                    return a
        return None

    def clear(self, session_id: str):
        self._sessions.pop(session_id, None)


_registry = ArtifactRegistry()


def get_registry() -> ArtifactRegistry:
    return _registry


def detect_artifact(tool_name: str, tool_result: str) -> Artifact | None:
    """Detect if a tool result contains an artifact (file, image, etc).

    Tries JSON parsing first, then falls back to path detection in output text.
    """
    # Try JSON
    try:
        data = json.loads(tool_result)
        if isinstance(data, dict):
            path = data.get("path") or data.get("file_path") or data.get("output_path")
            url = data.get("url") or data.get("download_url")
            if path or url:
                name = (
                    data.get("name")
                    or data.get("filename")
                    or (os.path.basename(path) if path else "artifact")
                )
                mime = data.get("mime") or data.get("mime_type")
                if not mime and path:
                    mime, _ = mimetypes.guess_type(path)
                art_type = _classify_type(mime, path or url or "")
                _skip = {
                    "path", "file_path", "output_path", "url", "download_url",
                    "name", "filename", "mime", "mime_type", "size",
                    "message", "description", "base64",
                }
                return Artifact(
                    name=name,
                    type=art_type,
                    path=path,
                    url=url,
                    mime=mime,
                    size=data.get("size"),
                    tool=tool_name,
                    message=data.get("message") or data.get("description"),
                    base64=data.get("base64"),
                    metadata={k: v for k, v in data.items() if k not in _skip},
                )
    except (json.JSONDecodeError, TypeError):
        pass

    # Path detection fallback
    _extensions = (
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
        ".pdf", ".zip", ".tar.gz",
        ".py", ".js", ".ts", ".md", ".txt", ".html", ".css",
    )
    for line in tool_result.split("\n"):
        line = line.strip()
        if any(line.lower().endswith(ext) for ext in _extensions):
            if os.path.sep in line or "/" in line:
                name = os.path.basename(line)
                mime, _ = mimetypes.guess_type(line)
                return Artifact(
                    name=name,
                    type=_classify_type(mime, line),
                    path=line,
                    mime=mime,
                    tool=tool_name,
                )

    return None


def _classify_type(mime: str | None, path: str) -> str:
    """Classify artifact type from MIME or path."""
    if mime:
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("text/") or mime in ("application/json", "application/javascript"):
            return "code"
        if mime == "application/pdf":
            return "document"
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"):
        return "image"
    if ext in (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp", ".h"):
        return "code"
    if ext in (".md", ".txt", ".pdf", ".doc", ".docx", ".html"):
        return "document"
    return "file"
