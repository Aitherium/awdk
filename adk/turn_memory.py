"""Per-turn memory providers for the streaming loop: graph memory, skills, after-turn writes.

This module is a CONTRACT. ``AitherAgent.stream_react`` calls these three coroutines;
the bodies are filled in independently. Until then each returns its neutral value, so
a spine that already calls them behaves exactly as one that does not.

Rules every implementation keeps:

- FAIL OPEN. A memory fault must never kill a turn: catch, log, return the neutral
  value. The caller ALSO wraps each call in ``asyncio.wait_for`` plus try/except and
  emits ``{"type": "degraded", "stage": "<name>"}`` on its event channel, so a provider
  needs no event channel of its own.
- BUDGET. A block is one system message the context assembler may keep or evict whole.
  Return at most ``max_chars`` characters (default below); never pad.
- NO SIDE EFFECTS BEFORE THE ANSWER. ``graph_block`` and ``skills_block`` only read;
  writes happen in ``after_turn``, which runs after the final answer is streamed.
- NEVER STORE MODEL PROSE AS MEMORY. ``after_turn`` crystallizes tool OBSERVATION text
  only, and ingests only the user's turn into the graph. A model answer written back as
  a fact is recalled next turn as ground truth and elaborated: one fabrication compounds
  into a permanent one (the same guard ``chat()`` keeps on its graph ingest).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Any

logger = logging.getLogger("adk.turn_memory")

# Char caps per block. Context blocks together get at most 20% of the compaction window;
# graph memory and skills are two of six sources, so each stays well under that share
# on an 8k-token model (8k tokens * 4 chars * 20% = ~6.4k chars for all blocks).
GRAPH_BLOCK_MAX_CHARS = 900
SKILLS_BLOCK_MAX_CHARS = 900

# A skill must share at least this many distinct query words. SkillStore.search returns
# anything with ONE substring hit, so "the" or "a" alone matched every stored skill and
# an irrelevant procedure got injected ahead of the first tool call.
SKILL_MIN_SCORE = 2
SKILL_MAX_K = 3
_SKILL_STOP = frozenset({
    "the", "and", "for", "you", "can", "with", "this", "that", "what", "how", "are",
    "was", "please", "from", "into", "about", "your", "have", "has", "not", "but",
    "all", "any", "out", "get", "now", "its", "our", "who", "why", "when", "where",
})

# What after_turn writes, bounded: one turn must not flood the shared fact store.
MAX_CRYSTAL_FACTS_PER_TURN = 12
DIGEST_OBS_CHARS = 300
DIGEST_MAX_CHARS = 1500

# Tools whose observation IS model prose or recalled memory. A reasoner's advice is
# another model's text, and a recall result is memory already stored; crystallizing
# either re-writes model output (or an old fact) as new ground truth.
_PROSE_TOOL = re.compile(r"reason|^ask_|delegate|think|summar|^chat", re.I)
_MEMORY_TOOL = re.compile(r"recall|remember|memory", re.I)
# Tools whose output is a credential or private store: never crystallized at all.
_SECRET_TOOL = re.compile(r"secret|credential|lockbox|vault|token|password|keyring|auth", re.I)
# The streaming loop appends a consulted reasoner's advice to the failing tool's own
# observation; everything from this marker on is model prose, not tool output.
_REASONER_MARK = "REASONER ("
_STEP_LIMIT_TEXT = "reached step limit"


async def graph_block(agent: Any, message: str, limit: int = 5) -> str:
    """Knowledge-graph memory relevant to ``message``, as one system block.

    Args:
        agent: the ``AitherAgent`` running the turn (read its graph/memory planes).
        message: the user's message for this turn.
        limit: at most this many graph nodes in the block.

    Returns a block of at most ``GRAPH_BLOCK_MAX_CHARS`` characters, or "" when the
    graph is absent, empty, or raised (fail open, logged).
    """
    graph = getattr(agent, "_graph", None)
    if graph is None or not (message or "").strip():
        return ""
    try:
        nodes = await _graph_search(graph, message, max(1, int(limit)))
    except Exception as exc:  # noqa: BLE001 -- fail open: a graph fault never kills a turn
        logger.warning("[TURN-MEMORY] graph recall failed: %s", exc)
        return ""
    header = ("[MEMORY GRAPH] Facts you already know. Answer from these; if a detail is "
              "not here, say you don't have it on record.")
    lines: list[str] = []
    used = len(header)
    for node in nodes or []:
        content = " ".join(str(getattr(node, "content", "") or "").split())
        if not content:
            continue
        md = getattr(node, "metadata", None)
        labels = md.get("_authority_labels") if isinstance(md, dict) else None
        prefix = "".join(f"[{lab}] " for lab in labels) if labels else ""
        line = f"- {prefix}{getattr(node, 'label', '') or ''}: {content[:300]}"
        if used + 1 + len(line) > GRAPH_BLOCK_MAX_CHARS:
            break
        lines.append(line)
        used += 1 + len(line)
    return header + "\n" + "\n".join(lines) if lines else ""


async def _graph_search(graph: Any, message: str, limit: int) -> list:
    """The same recall ``chat()`` runs: activation recall when unified memory is on."""
    try:
        from adk.graph_memory import unified_memory_mode
        unified = unified_memory_mode() == "on"
    except Exception as exc:  # noqa: BLE001 -- flag unreadable: plain search
        logger.debug("[TURN-MEMORY] unified-memory flag unreadable: %s", exc)
        unified = False
    if unified and hasattr(graph, "recall_with_activation"):
        return await graph.recall_with_activation(message, limit=limit)
    return await graph.search(message, limit=limit)


def _skill_tokens(message: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]+", (message or "").lower())
            if len(t) >= 3 and t not in _SKILL_STOP}


def _skill_score(skill: Any, tokens: set[str]) -> int:
    blob = " ".join([
        str(getattr(skill, "name", "") or ""),
        str(getattr(skill, "description", "") or ""),
        " ".join(getattr(skill, "steps", None) or []),
        " ".join(getattr(skill, "tags", None) or []),
        " ".join(getattr(skill, "tools_used", None) or []),
    ]).lower()
    return sum(1 for t in tokens if t in blob)


async def skills_block(agent: Any, message: str, k: int = 3) -> str:
    """The ``k`` learned skills/procedures that best match ``message``, as one block.

    Args:
        agent: the ``AitherAgent`` running the turn.
        message: the user's message for this turn.
        k: at most this many skills.

    Returns a block of at most ``SKILLS_BLOCK_MAX_CHARS`` characters, or "" when no
    skill matches or the skill store raised (fail open, logged).
    """
    store = getattr(agent, "_skills", None)
    tokens = _skill_tokens(message)
    if store is None or not tokens:
        return ""
    k = max(1, min(int(k), SKILL_MAX_K))
    try:
        # Over-fetch, then re-score on distinct non-stopword tokens: the store's own
        # ranking counts every substring hit and does not expose the score.
        # SkillStore.search globs and parses every skill JSON file: run it off the event
        # loop, since this block is built before the turn's first token.
        candidates = await asyncio.to_thread(store.search, message, max(10, k * 4)) or []
    except Exception as exc:  # noqa: BLE001 -- fail open
        logger.warning("[TURN-MEMORY] skill search failed: %s", exc)
        return ""
    scored = [(_skill_score(s, tokens), s) for s in candidates]
    matches = [s for score, s in sorted(scored, key=lambda p: p[0], reverse=True)
               if score >= SKILL_MIN_SCORE][:k]
    if not matches:
        return ""
    header = "[LEARNED SKILLS - proven procedures you can reuse]"
    lines: list[str] = []
    used = len(header)
    kept: list = []
    for s in matches:
        tools = ", ".join((getattr(s, "tools_used", None) or [])[:6])
        line = (f"- {getattr(s, 'name', '')}: {getattr(s, 'description', '')} "
                f"(used {getattr(s, 'success_count', 0)}x; tools: {tools})")
        line = line[:SKILLS_BLOCK_MAX_CHARS - len(header) - 1]
        if lines and used + 1 + len(line) > SKILLS_BLOCK_MAX_CHARS:
            break
        lines.append(line)
        kept.append(s)
        used += 1 + len(line)
    # Not a store write: _learn_after reads this to reinforce the skills the turn reused.
    try:
        agent._recalled_skills = kept
    except Exception as exc:  # noqa: BLE001 -- a frozen agent only loses reinforcement
        logger.debug("[TURN-MEMORY] could not note recalled skills: %s", exc)
    return header + "\n" + "\n".join(lines)


def _tool_failed(obs: str) -> bool:
    try:
        from adk.agent import tool_result_failed
        return bool(tool_result_failed(obs))
    except Exception as exc:  # noqa: BLE001 -- judge by the error prefix alone
        logger.debug("[TURN-MEMORY] tool_result_failed unavailable: %s", exc)
        text = (obs or "").strip()
        return not text or text.startswith("(tool error:")


def _pairs(tools: list, observations: list) -> list[tuple[str, str]]:
    """(tool name, observation text) per call; observations may be strings aligned with
    ``tools``, ``{"name", "obs"|"result"}`` dicts, or ``(name, obs)`` pairs.

    One exception to the alignment: the streaming loop records a consulted reasoner in
    ``tools`` but folds its advice into the failing tool's observation after
    ``REASONER (`` and appends no observation of its own. That reasoner entry takes the
    folded advice and consumes no observation, or every later tool would be paired with
    the observation of the tool before it."""
    out: list[tuple[str, str]] = []
    obs_list = list(observations or [])
    j = 0
    advice: str | None = None  # reasoner text folded into the previous observation
    folded = len(obs_list) < len(tools or [])  # a caller that lists advice separately
    for raw in tools or []:
        name = str((raw.get("name") if isinstance(raw, dict) else raw) or "").strip()
        if folded and advice is not None and _PROSE_TOOL.search(name):
            out.append((name, advice.strip()))
            advice = None
            continue
        advice = None
        o: Any = obs_list[j] if j < len(obs_list) else ""
        j += 1
        if isinstance(o, dict):
            name = name or str(o.get("name") or "")
            o = o.get("obs", o.get("result", o.get("text", "")))
        elif isinstance(o, (tuple, list)) and len(o) == 2:
            name = name or str(o[0])
            o = o[1]
        text = str(o or "")
        cut = text.find(_REASONER_MARK)
        if cut >= 0:
            advice = text[cut:]
            text = text[:cut]
        if name:
            out.append((name, text.strip()))
    return out


#: An observation LINE reporting a failure. tool_result_failed judges the whole result;
#: a meta tool (call_tool) can return ok text whose line is "Unknown tool ..." --
#: measured 2026-10-14, such lines were crystallized and recalled as facts.
_ERROR_LINE = re.compile(
    r"(?i)\b(?:unknown tool|not found|no such|error|exception|traceback|failed|"
    r"timed? ?out|refused|unauthori[sz]ed|forbidden|denied)\b")


def _observation_facts(pairs: list[tuple[str, str]]) -> str:
    """Bulleted fact lines from successful, non-prose tool observations only."""
    try:
        from adk import crystal as _crystal
        fact_min, fact_max = int(_crystal.FACT_MIN_CHARS), int(_crystal.FACT_MAX_CHARS)
    except Exception as exc:  # noqa: BLE001 -- crystal's bounds unavailable: its defaults
        logger.debug("[TURN-MEMORY] crystal fact bounds unavailable: %s", exc)
        fact_min, fact_max = 24, 400
    facts: list[str] = []
    for name, obs in pairs:
        if (_PROSE_TOOL.search(name) or _MEMORY_TOOL.search(name)
                or _SECRET_TOOL.search(name) or _tool_failed(obs)):
            continue
        for raw in obs.splitlines():
            line = " ".join(raw.split())
            if _ERROR_LINE.search(line):
                continue
            clean = _redact(line)
            if clean != line:  # a credential shape: the whole line stays out
                continue
            fact = f"{name}: {line}"
            if fact_min <= len(fact) <= fact_max and len(line) >= 8:
                facts.append(f"- {fact}")
            if len(facts) >= MAX_CRYSTAL_FACTS_PER_TURN:
                return "\n".join(facts)
    return "\n".join(facts)


def _redact(text: str) -> str:
    try:
        from adk.log_redact import redact_log_line
        return redact_log_line(text)
    except Exception as exc:  # noqa: BLE001 -- no redactor: keep nothing that looks keyed
        logger.debug("[TURN-MEMORY] redactor unavailable: %s", exc)
        return "" if re.search(r"(?i)(key|token|secret|passw)", text) else text


def _digest(pairs: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for name, obs in pairs:
        if _PROSE_TOOL.search(name):
            shown = "(model advice, not recorded)"
        elif _SECRET_TOOL.search(name):
            shown = "(private, not recorded)"
        else:
            shown = _redact(" ".join(obs.split()))[:DIGEST_OBS_CHARS]
        parts.append(f"{name} -> {shown}")
    return ("[tools: " + " | ".join(parts) + "]")[:DIGEST_MAX_CHARS]


def _hit_step_limit(agent: Any, answer: str) -> bool:
    # The spine may mark the finish explicitly; without it, an empty answer after tool
    # calls or the loop's own "(reached step limit)" line is the only signal.
    finish = str(getattr(agent, "_last_turn_finish", "") or "")
    text = answer or ""
    return finish == "step_limit" or _STEP_LIMIT_TEXT in text or not text.strip()


def _turn_intent(agent: Any, message: str) -> str:
    decision = getattr(agent, "_turn_decision", None)
    intent = getattr(decision, "intent", None)
    if intent:
        return str(intent)
    try:
        from adk.intent import coarse_code_intent
        return coarse_code_intent(message)
    except Exception as exc:  # noqa: BLE001 -- intent is a hint, not required
        logger.debug("[TURN-MEMORY] coarse intent unavailable: %s", exc)
        return "DEFAULT"


async def after_turn(
    agent: Any,
    *,
    message: str,
    answer: str,
    tools: list,
    observations: list,
    sid: str | None,
) -> dict:
    """Write what this turn learned: memory, graph edges, skill candidates.

    Args:
        agent: the ``AitherAgent`` that ran the turn.
        message: the user's message.
        answer: the final answer streamed to the user.
        tools: names of the tools called this turn, in call order.
        observations: the tool results the loop observed (already truncated).
        sid: the session id, or None.

    Returns a summary for the turn trace, e.g. ``{"memory": 1, "graph": 2}`` (counts
    of writes per plane); ``{}`` when nothing was written. Never raises: a failed
    write is logged and reported as ``{"degraded": ["<plane>"]}``.
    """
    out: dict = {}
    degraded: list[str] = []
    try:
        pairs = _pairs(tools, observations)
        failed = [_tool_failed(obs) for _, obs in pairs]
    except Exception as exc:  # noqa: BLE001 -- unreadable inputs: learn nothing, still return
        logger.warning("[TURN-MEMORY] after_turn inputs unreadable: %s", exc)
        return {"degraded": ["inputs"]}

    # 1. Tool digest into session history: the answer row alone left the next turn blind
    # to what the tools returned. FIRST, before any slow write: the spine runs after_turn
    # fire-and-forget, so a digest queued behind crystal's embedding calls landed after
    # the NEXT turn's user row and the replayed history read out of order.
    mem = getattr(agent, "memory", None)
    if mem is not None and sid and pairs:
        try:
            await mem.add_message(sid, "assistant", _digest(pairs),
                                  metadata={"fold": "previous"})
            out["history"] = 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TURN-MEMORY] tool digest write failed: %s", exc)
            degraded.append("history")

    # 2. Crystallize facts from tool OBSERVATIONS (never the answer), only when tools ran.
    crystal = getattr(agent, "crystal", None)
    if crystal is not None and pairs:
        try:
            summary = _observation_facts(pairs)
            if summary:
                n = await crystal.crystallize(summary)
                if n:
                    out["crystal"] = int(n)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TURN-MEMORY] crystallize failed: %s", exc)
            degraded.append("crystal")

    # 3. Graph ingest: the USER turn only, and never a memory question (a question is not
    # a fact; ingesting it lets it self-ground on the next ask).
    graph = getattr(agent, "_graph", None)
    if graph is not None and (message or "").strip():
        try:
            from adk.coherence import is_memory_question
            if not is_memory_question(message):
                await graph.ingest_conversation(sid, [{"role": "user", "content": message}])
                out["graph"] = 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TURN-MEMORY] graph ingest failed: %s", exc)
            degraded.append("graph")

    # 4. Skills + world model. A failed call goes as {"ok": False} so _learn_after does
    # not count it as a working step of an extracted procedure.
    learn = getattr(agent, "_learn_after", None)
    if learn is not None:
        try:
            calls = [{"name": name, "ok": not bad} for (name, _), bad in zip(pairs, failed)]
            await learn(message, answer or "", calls)
            out["learn"] = len(calls)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TURN-MEMORY] _learn_after failed: %s", exc)
            degraded.append("learn")

    # 5. Last intent + tools per session, so a follow-up like "do it again" can carry
    # the previous turn's intent instead of re-deriving it from three words.
    if sid:
        try:
            state = {"intent": _turn_intent(agent, message),
                     "tools": [n for n, _ in pairs], "ts": round(time.time())}
            last = getattr(agent, "_last_turn_by_sid", None)
            if not isinstance(last, dict):
                last = {}
                agent._last_turn_by_sid = last
            last[sid] = state
            # In process only: Memory.remember also pushes every key to Spirit and the
            # fleet vector store, which made each turn's routing state a cross-session
            # "memory" plus two network calls per turn.
            out["session_state"] = 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TURN-MEMORY] session state write failed: %s", exc)
            degraded.append("session_state")

    # 6. A lesson when the turn ran out of steps or ended on two failures in a row, so the
    # decisions block warns the next turn off the same dead calls.
    typed = getattr(agent, "_typed", None)
    streak = len(failed) >= 2 and failed[-1] and failed[-2]
    limited = bool(pairs) and _hit_step_limit(agent, answer)
    if typed is not None and pairs and (streak or limited):
        try:
            bad = sorted({n for (n, _), f in zip(pairs, failed) if f}) or [n for n, _ in pairs]
            why = "ran out of steps" if limited else "failed twice in a row"
            ask = " ".join((message or "").split())[:160]
            lesson = (f"never repeat {', '.join(bad[:5])} unchanged for requests like "
                      f'"{ask}" - last time the turn {why}; pick a different tool or '
                      "ask_reasoner")
            key = hashlib.sha1((",".join(bad) + "|" + ask.lower()).encode()).hexdigest()[:12]
            await typed.remember(lesson, role="correction", confidence=0.5,
                                 metadata={"src": "after_turn"}, id_=f"lesson:{key}")
            out["lesson"] = 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TURN-MEMORY] lesson write failed: %s", exc)
            degraded.append("lesson")

    if degraded:
        out["degraded"] = degraded
    return out
