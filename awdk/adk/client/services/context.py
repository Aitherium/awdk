"""LiveContext service client (port 8098)."""

from typing import Optional, List

from adk.client._base import ServiceClient


class ContextClient(ServiceClient):
    """Client for the LiveContext service."""

    async def status(self, session_id: str) -> dict:
        """Get context window status for a session."""
        return await self._get(f"/window/{session_id}")

    async def sources(self, session_id: str) -> dict:
        """Get context sources breakdown (flame chart data)."""
        return await self._get(f"/window/{session_id}/sources")

    async def add(
        self,
        session_id: str,
        content: str,
        source: str,
        priority: str = "normal",
        score: float = 0.7,
        tags: Optional[List[str]] = None,
    ) -> dict:
        """Add a context item to the window."""
        return await self._post(
            f"/window/{session_id}/add",
            json={
                "content": content,
                "source": source,
                "priority": priority,
                "score": score,
                "tags": tags or ["sdk"],
            },
        )

    async def clear(self, session_id: str, keep_pinned: bool = True) -> dict:
        """Clear non-pinned context items."""
        q = "?keep_pinned=true" if keep_pinned else ""
        return await self._delete(f"/window/{session_id}/clear{q}")

    async def pin(self, session_id: str, item_id: str, pinned: bool = True) -> dict:
        """Pin or unpin a context item."""
        q = f"?pinned={'true' if pinned else 'false'}"
        return await self._post(f"/window/{session_id}/pin/{item_id}{q}")

    async def remove(self, session_id: str, item_id: str) -> dict:
        """Remove a specific context item."""
        return await self._delete(f"/window/{session_id}/item/{item_id}")

    async def assemble(self, session_id: str) -> dict:
        """Assemble the full context payload."""
        return await self._get(f"/window/{session_id}/assemble")

    async def import_file(
        self,
        session_id: str,
        file_path: str,
        summarize: bool = True,
        max_tokens: int = 2000,
        tags: Optional[List[str]] = None,
    ) -> dict:
        """Import a file into the context window."""
        return await self._post(
            f"/window/{session_id}/import/file",
            json={
                "file_path": file_path,
                "summarize": summarize,
                "max_tokens": max_tokens,
                "tags": tags or ["imported", "sdk"],
            },
            timeout=30.0,
        )
