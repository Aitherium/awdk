"""DataPlane client — source registration, node config sync (port 8170)."""

from typing import Any, Dict, List, Optional

from adk.client._base import ServiceClient


class DataPlaneClient(ServiceClient):
    """Client for the TenantDataPlane service."""

    async def register_source(
        self,
        name: str,
        connector_type: str,
        connection_config: Dict[str, Any],
        *,
        credential_key: str = "",
        sync_schedule: str = "manual",
        target_collection: str = "",
        file_patterns: Optional[List[str]] = None,
    ) -> dict:
        """Register a new data source. Returns {id, ...}."""
        payload: Dict[str, Any] = {
            "name": name,
            "connector_type": connector_type,
            "connection_config": connection_config,
            "sync_schedule": sync_schedule,
        }
        if credential_key:
            payload["credential_key"] = credential_key
        if target_collection:
            payload["target_collection"] = target_collection
        if file_patterns:
            payload["file_patterns"] = file_patterns
        return await self._post("/data-plane/sources", json=payload, timeout=15.0)

    async def delete_source(self, source_id: str) -> dict:
        """Delete a registered data source."""
        return await self._delete(f"/data-plane/sources/{source_id}")

    async def list_sources(self) -> list:
        """List all registered data sources."""
        result = await self._get("/data-plane/sources")
        if isinstance(result, list):
            return result
        return result.get("sources", [])

    async def trigger_sync(self, source_id: str) -> dict:
        """Trigger an immediate sync for a data source."""
        return await self._post(f"/data-plane/sources/{source_id}/sync")

    async def get_node_config(self, node_id: str) -> dict:
        """Get sync config for a node (stored in Strata)."""
        return await self._get(f"/data-plane/nodes/{node_id}/config")

    async def put_node_config(self, node_id: str, config: Dict[str, Any]) -> dict:
        """Update sync config for a node."""
        return await self._post(
            f"/data-plane/nodes/{node_id}/config", json=config, timeout=10.0,
        )
