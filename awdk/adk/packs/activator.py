"""PackActivator — eager tool-pack activation with a required/optional contract.

Why eager only: adk exposes tools to the LLM by their presence in the agent's
``ToolRegistry`` at call time (``agent._tools.to_openai_format()``). A tool that has not
been registered is INVISIBLE to the model — there is no lazy/deferred path that would let
the LLM discover it later. So a pack is either activated now (its tools registered) or it
is not a capability at all. PackActivator therefore activates eagerly and reports the
exact tool count each pack contributed.

Contract:
  * required packs — each MUST register >0 tools, else ``PackUnavailable`` is raised
    (tool_pack_loader absent OR the pack is unlicensed OR its module won't import — all
    surface identically as "the pack contributed no tools").
  * optional packs — activated best-effort; any failure is swallowed (soft-degrade).

There is no unload — activation is monotonic within a process.
"""

import logging

logger = logging.getLogger("adk.packs.activator")


class PackUnavailable(RuntimeError):
    """A required tool pack could not be activated (no tools registered)."""


class PackActivator:
    """Activate tool packs onto an already-constructed ``AitherAgent``."""

    def __init__(self, agent):
        self.agent = agent
        # pack id -> tool count contributed (populated on successful activation)
        self.active: dict = {}

    def _tool_count(self) -> int:
        """Current number of tools registered on the agent (best-effort)."""
        try:
            return len(self.agent._tools.list_tools())
        except Exception:
            return 0

    def _activate(self, pid: str) -> bool:
        """Activate one pack eagerly.

        Returns True when the pack registered >0 tools (marking it active and recording
        the count), False otherwise. Idempotent: a pack already active returns True.
        """
        if pid in self.active:
            return True

        # register_tool_packs returns the number of tools it registered. We also diff the
        # registry size as a cross-check: register_on_adk_agent adds to the SAME registry,
        # so the delta and the returned count should agree — we take the max to stay
        # robust to any loader that under-reports.
        try:
            from adk.builtin_tools import register_tool_packs
        except ImportError:
            logger.debug("register_tool_packs unavailable; cannot activate pack %s", pid)
            return False

        before = self._tool_count()
        try:
            registered = register_tool_packs(self.agent, pack_ids=[pid])
        except Exception as exc:  # noqa: BLE001 - treated as "pack unavailable"
            logger.debug("Pack %s activation raised: %s", pid, exc)
            registered = 0
        delta = max(0, self._tool_count() - before)

        count = max(int(registered or 0), delta)
        if count > 0:
            self.active[pid] = count
            logger.info("Activated tool pack '%s' (+%d tools)", pid, count)
            return True
        logger.debug("Pack '%s' contributed no tools", pid)
        return False

    def ensure(self, packs: dict) -> dict:
        """Activate the packs declared by a spec.

        Args:
            packs: mapping with optional ``required`` and ``optional`` lists of pack ids.

        Returns:
            ``self.active`` — mapping of activated pack id -> tool count.

        Raises:
            PackUnavailable: a required pack registered no tools.
        """
        packs = packs or {}
        for pid in packs.get("required", []) or []:
            if not self._activate(pid):
                raise PackUnavailable(
                    f"{pid}: tool_pack_loader absent OR unlicensed OR module unimportable"
                )
        for pid in packs.get("optional", []) or []:
            # Soft-degrade: an optional pack that can't activate is simply absent.
            try:
                self._activate(pid)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Optional pack '%s' failed to activate: %s", pid, exc)
        return self.active
