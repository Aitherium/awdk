"""Put adk's whole agent behind GobboNet's chat box.

The point of shipping a pack in adk is to LIFT the host app, not to sit beside
it. GobboNet is a very good local-first chat client whose maintainer has been
specific about where it stops — complex agents, intense harnesses, work that
outgrows one context window. All of that is what adk already is.

The seam that makes this possible is that GobboNet speaks the
OpenAI-compatible chat API. So the pack does not need GobboNet to change, or to
learn about tools, or to render anything new: it answers
`/v1/chat/completions` by running adk's ReAct loop and streaming the result
back as ordinary assistant tokens. From the UI it looks like a very capable
model. Underneath it is `AitherAgent` — identity, memory, and the tool registry.

WHAT THE USER GETS, measured rather than asserted (`TOOL_CATEGORIES`, 20):

    code, creative, decisions, file_io, formbridge, git, graph, notebooks,
    persona, python, repowise, safety, secrets, self, shell, structured_ml,
    swarm, voice, web, workspace

plus whatever `load_packs=True` finds installed — graph-RAG retrieval
(`adk.graph_rag`), agent notebooks (`adk.notebook_tools`), aeon (`adk.aeon`),
Strata (`adk.strata`), Lockbox (`adk.vault_lockbox`), the node/MCP surface
(`adk.node`, `adk.mcp_server`), AitherShell (`adk.shell`) and the awgit
git plane (micro-commits, semantic diff, oplog, path leases).

Capabilities that live on the PLATFORM rather than in this wheel — beadspace,
Saga autocards, the hosted marketplace — are reachable through the MCP endpoint
tools when a node is configured, and are deliberately NOT claimed here. A pack
that advertises a capability the wheel cannot deliver is the same defect as an
advertised command that is not a console script.

STREAMING ACROSS THE ASYNC BOUNDARY. `stream_react` is async and pushes events
to a callback; the `Engine` contract is a sync iterator. Bridging them with
`asyncio.run()` and collecting the whole answer first would work and would also
destroy the only property that makes a local model bearable: seeing tokens
arrive. So the loop runs on its own thread and hands tokens back through a
queue, and the iterator yields them as they land.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Iterator

#: Sentinel distinguishing "the stream ended" from "a token that happens to be
#: falsy". An empty string is a legitimate delta; None is not.
_DONE = object()

#: Tool activity is surfaced as a short inline note. GobboNet renders whatever
#: text arrives, so this is how a user sees the agent working rather than
#: watching a silent pause and assuming it hung.
_SHOW_TOOL_CALLS = True


class AgenticEngineMixin:
    """`stream_chat` backed by adk's ReAct loop.

    Mixed into the pack's engine rather than replacing it, so the search and
    state behaviour that already works is untouched.
    """

    #: Overridden by the engine that mixes this in.
    agent_identity: str = "gobbonet"
    max_steps: int = 6

    _agent = None
    _agent_lock = threading.Lock()
    _campaign_memory = None

    def _get_campaign_memory(self):
        """Campaign notes, once per engine. Fail-soft: without awm installed
        this returns a memory whose brief is empty and whose tools say what to
        install — the chat path is identical to before either way."""
        if self._campaign_memory is None:
            import os

            from adk.packs.gobbonet.campaign_memory import CampaignMemory
            self._campaign_memory = CampaignMemory(
                campaign=os.environ.get("AITHER_GOBBONET_CAMPAIGN", "default"))
        return self._campaign_memory

    def _get_agent(self):
        """Build the agent once, lazily.

        Lazily because constructing it loads packs and tools, which is work a
        user who only wanted the UI and search should not pay for, and which
        must not make `serve()` fail on a machine where a backend appears a few
        seconds later.
        """
        with self._agent_lock:
            if self._agent is None:
                from adk.agent import AitherAgent

                self._agent = AitherAgent(
                    name=self.agent_identity,
                    builtin_tools=True,
                    load_packs=True,
                )
                # The pen: the agent records and consults campaign notes
                # itself (campaign_note / campaign_recall). Registration is
                # fail-soft; the loop works without it.
                from adk.packs.gobbonet.campaign_memory import (
                    register_campaign_tools,
                )
                register_campaign_tools(self._agent,
                                        self._get_campaign_memory())
                # And the mod-pack pair: a character card people already trade
                # imports as SCOPED notes, and what the harness learns exports
                # back into one. Same fail-soft contract as above.
                from adk.packs.gobbonet.cards import register_card_tools
                register_card_tools(self._agent, self._get_campaign_memory())
            return self._agent

    def stream_chat(self, messages: list[dict], **opts: Any) -> Iterator[str]:
        agent = self._get_agent()

        history = [m for m in messages[:-1] if m.get("role") in ("user", "assistant", "system")]
        last = messages[-1] if messages else {}
        prompt = last.get("content") or ""

        # The scene brief: durable campaign notes, scoped to who is PRESENT in
        # the recent turns. Absent characters' knowledge is never fetched — the
        # scopes are siblings and the store will not cross them. Empty store or
        # no awm ⇒ empty brief ⇒ history is exactly what it always was.
        mem = self._get_campaign_memory()
        campaign_brief = mem.brief(
            mem.present_in(messages),
            # The scene itself, so the budget carries the notes this turn
            # is about rather than whichever came back first.
            scene=prompt) if mem.available() else ""
        if campaign_brief:
            history = [{"role": "system", "content": campaign_brief}] + history

        out: "queue.Queue[Any]" = queue.Queue()

        def on_event(ev: dict) -> None:
            kind = ev.get("type")
            if kind == "token":
                out.put(ev.get("text") or "")
            elif kind == "tool" and _SHOW_TOOL_CALLS:
                # Without this the UI shows a long silence while a tool runs,
                # which reads as a hang. Naming the tool is also the only way a
                # user can tell an agentic answer from a plain one.
                out.put(f"\n`{ev.get('name')}`…\n")
            elif kind == "error":
                out.put(f"\n[error] {ev.get('error')}\n")

        def run() -> None:
            import asyncio

            try:
                asyncio.run(
                    agent.stream_react(
                        message=prompt,
                        on_event=on_event,
                        history=history,
                        max_steps=int(opts.get("max_steps") or self.max_steps),
                    )
                )
            except Exception as e:  # noqa: BLE001 - must reach the user, not the log
                # A traceback on a background thread with no reader is how an
                # agent failure becomes "it stopped typing".
                out.put(f"\n[agent error] {type(e).__name__}: {e}\n")
            finally:
                out.put(_DONE)

        worker = threading.Thread(target=run, daemon=True, name="gobbonet-react")
        worker.start()

        while True:
            item = out.get()
            if item is _DONE:
                return
            yield item


def describe_capabilities() -> dict:
    """What this pack can actually do on THIS machine, by looking.

    Reported rather than hardcoded: a capability list that cannot go stale is
    worth more than one that reads well. A module that is absent from the wheel
    is reported absent instead of being quietly dropped from the list.
    """
    import importlib

    surface = {
        "graph-RAG retrieval": "adk.graph_rag",
        "agent notebooks": "adk.notebook_tools",
        "aeon": "adk.aeon",
        "Strata": "adk.strata",
        "Lockbox": "adk.vault_lockbox",
        "node / MCP hosting": "adk.mcp_server",
        "AitherShell": "adk.shell",
        "sync": "adk.sync",
        # The git plane: micro-commits, semantic diff, oplog, path leases.
        # A declared dependency as of 3.3.x; before that it was present only
        # on a monorepo checkout, which made five guarded adk features
        # silently absent for everyone who pip-installed.
        "awgit (git plane)": "awgit",
    }
    present: dict[str, bool] = {}
    for label, mod in surface.items():
        try:
            importlib.import_module(mod)
            present[label] = True
        except Exception:  # noqa: BLE001 - absence is the answer, not an error
            present[label] = False

    try:
        from adk.builtin_tools import TOOL_CATEGORIES

        present["tool categories"] = sorted(TOOL_CATEGORIES)
    except ImportError:
        present["tool categories"] = []
    return present


def account_surface() -> dict:
    """The OPTIONAL account plane: secrets, lockbox, sync, Awconnect.

    Everything here is opt-in and stays off until the user runs `adk login`.
    That is not a limitation to work around — GobboNet's whole promise is that
    it runs on your machine and nothing leaves it, and a pack that quietly
    started syncing would take away the reason someone chose it. So this
    REPORTS what is available and never initiates anything.

    Reported rather than hidden, though, because the opposite failure is real
    too: a user who wants their secrets on two machines should not have to
    discover that the capability existed all along.

        adk login              browser device flow (portal.aitherium.com)
        adk secret sync        bidirectional secrets, vault-backed
        adk deploy connect     the Awconnect browser extension
        adk onboard            interactive: detect, configure, integrate
    """
    import importlib

    surface = {
        "secrets (get/set/list)": "adk.builtin_tools",
        "vault lockbox": "adk.vault_lockbox",
        "secret sync": "adk.sync.secrets",
        "lockbox sync": "adk.sync.lockbox",
        "session sync": "adk.sync.sessions",
        "device identity": "adk.sync.device_identity",
        "drive client": "adk.sync.drive_client",
        "identity / login": "adk.auth",
    }
    out: dict[str, Any] = {}
    for label, mod in surface.items():
        try:
            importlib.import_module(mod)
            out[label] = True
        except Exception:  # noqa: BLE001 - absence is the answer
            out[label] = False

    # Whether the user has actually linked. Distinguished from "the capability
    # exists" on purpose: those are different questions and conflating them is
    # how a local-first user gets told they are signed in when they are not.
    out["linked"] = _is_linked()
    out["opt_in"] = True
    return out


def _is_linked() -> bool:
    """Has this machine been linked to an account? Never initiates a login."""
    try:
        from adk.config import Config

        cfg = Config.from_env()
        return bool(getattr(cfg, "api_key", None) or getattr(cfg, "token", None))
    except Exception:  # noqa: BLE001 - not linked is the safe reading
        return False


def ledger_status() -> dict:
    """This pack's own knowledge ledger, and the prime it merges into.

    Every agent, persona and pack keeps its OWN awgit oplog and ships it to a
    prime log — git's model applied to what agents learn rather than to what
    they write. Keeping them separate is what preserves attribution: a single
    shared log answers "what changed" but never "which agent decided this".

    Reported here so the pack can say what it is contributing to rather than
    doing it invisibly.
    """
    try:
        from adk import agent_ledger

        st = agent_ledger.status()
        st["this_agent"] = "gobbonet"
        st["my_ops"] = st.get("agents", {}).get("gobbonet", 0)
        return st
    except ImportError:
        return {"available": False, "this_agent": "gobbonet", "agents": {}, "prime_ops": 0}
