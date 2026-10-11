"""Expeditions service client (AitherExpedition, port 8785).

Paths are the service's own routes, served at its root: /list, /submit, /{id},
/{id}/pause|resume|cancel. This client used to prefix them with /expedition
(the Genesis router's prefix), so every call 404'd against the service it targets.
"""

from typing import Optional

from adk.client._base import ServiceClient


class ExpeditionClient(ServiceClient):
    """Client for the Expeditions autonomous research service."""

    async def list(self) -> dict:
        """List all expeditions."""
        return await self._get("/list")

    async def submit(
        self,
        objective: str,
        max_loops: int = 10,
        max_hours: float = 8,
        token_budget: int = 500000,
        effort: str = "deep",
        work_mode: str = "thorough",
        preset: Optional[str] = None,
        context: Optional[str] = None,
        use_domain_strategy: bool = False,
        strategy_hint: Optional[str] = None,
    ) -> dict:
        """Submit a new expedition."""
        payload = {
            "objective": objective,
            "max_outer_loops": max_loops,
            "max_duration_hours": max_hours,
            "token_budget": token_budget,
            "research_effort": effort,
            "work_mode": work_mode,
            "use_domain_strategy": use_domain_strategy,
        }
        if context:
            payload["additional_context"] = context
        if strategy_hint:
            payload["strategy_hint"] = strategy_hint
        return await self._post("/submit", json=payload, timeout=60.0)

    async def status(self, expedition_id: str) -> dict:
        """Get expedition status."""
        return await self._get(f"/{expedition_id}")

    async def pause(self, expedition_id: str) -> dict:
        """Pause a running expedition."""
        return await self._post(f"/{expedition_id}/pause")

    async def resume(self, expedition_id: str) -> dict:
        """Resume a paused expedition."""
        return await self._post(f"/{expedition_id}/resume")

    async def cancel(self, expedition_id: str) -> dict:
        """Cancel an expedition."""
        return await self._post(f"/{expedition_id}/cancel")
