"""Conversation management client (via LiveContext port 8098)."""

from typing import Optional, List

from adk.client._base import ServiceClient


class ConversationClient(ServiceClient):
    """Client for conversation management endpoints on the LiveContext service."""

    async def list(self, limit: int = 20) -> dict:
        """List saved conversations."""
        return await self._get(f"/conversations?limit={limit}")

    async def save(self, session_id: str, name: str, tags: Optional[List[str]] = None) -> dict:
        """Save the current conversation."""
        return await self._post(
            f"/window/{session_id}/conversation/save",
            json={"name": name, "tags": tags or ["sdk"]},
        )

    async def load(self, session_id: str, conv_id: str) -> dict:
        """Load a saved conversation into a session."""
        return await self._post(f"/window/{session_id}/conversation/load/{conv_id}")

    async def show(self, conv_id: str) -> dict:
        """Get full conversation details."""
        return await self._get(f"/conversation/{conv_id}")

    async def delete(self, conv_id: str) -> dict:
        """Delete a saved conversation."""
        return await self._delete(f"/conversation/{conv_id}")

    async def formats(self) -> dict:
        """List supported conversation import formats."""
        return await self._get("/conversation/formats")

    async def import_file(
        self,
        session_id: str,
        file_path: str,
        name: Optional[str] = None,
        save_to_strata: bool = True,
        load_into_session: bool = True,
    ) -> dict:
        """Import a conversation from a file."""
        return await self._post(
            f"/conversation/import/file?session_id={session_id}",
            json={
                "file_path": file_path,
                "name": name,
                "save_to_strata": save_to_strata,
                "load_into_session": load_into_session,
            },
            timeout=30.0,
        )

    async def import_text(
        self,
        session_id: str,
        content: str,
        name: Optional[str] = None,
        format: str = "auto",
        save_to_strata: bool = True,
        load_into_session: bool = True,
        tags: Optional[List[str]] = None,
    ) -> dict:
        """Import a conversation from raw text."""
        return await self._post(
            f"/conversation/import?session_id={session_id}",
            json={
                "content": content,
                "name": name,
                "format": format,
                "save_to_strata": save_to_strata,
                "load_into_session": load_into_session,
                "tags": tags or ["sdk-import"],
            },
            timeout=30.0,
        )
