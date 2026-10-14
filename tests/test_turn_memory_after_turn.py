"""after_turn / skills_block: the streaming turn learns from tool evidence, never prose.

A model answer written back as memory is recalled next turn as ground truth and
elaborated, so after_turn may crystallize tool OBSERVATION text only and ingest only the
user's turn into the graph. Each write fails open on its own.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from adk import turn_memory
from adk.agent import AitherAgent
from adk.skills import Skill

OBS_FACT = "disk C: has 41 GB free of 930 GB total on the system volume"
ANSWER_PROSE = "Your disk is nearly empty and the fleet is healthy, nothing to worry about"


def _run(coro):
    return asyncio.run(coro)


class FakeCrystal:
    def __init__(self):
        self.summaries: list[str] = []

    async def crystallize(self, summary: str) -> int:
        self.summaries.append(summary)
        return len(summary.splitlines())


class FakeGraph:
    def __init__(self, raise_on_ingest: bool = False):
        self.ingested: list = []
        self.raise_on_ingest = raise_on_ingest

    async def ingest_conversation(self, sid, messages):
        if self.raise_on_ingest:
            raise RuntimeError("graph store locked")
        self.ingested.append((sid, messages))
        return 1


class FakeMemory:
    def __init__(self):
        self.rows: list = []
        self.kv: dict = {}

    async def add_message(self, sid, role, content, metadata=None):
        self.rows.append((sid, role, content))

    async def remember(self, key, value, category="general", metadata=None):
        self.kv[key] = value


class FakeTyped:
    def __init__(self):
        self.rows: list = []

    async def remember(self, content, **kw):
        self.rows.append((content, kw))
        return kw.get("id_", "x")


class FakeSkillStore:
    def __init__(self, skills=()):
        self.skills = list(skills)
        self.saved: list = []

    def search(self, query, k=5):
        toks = [t.lower() for t in query.split()]
        hits = [s for s in self.skills
                if any(t in f"{s.name} {s.description}".lower() for t in toks)]
        return hits[:k]

    def save(self, skill):
        self.saved.append(skill)


class FakeExtractor:
    def __init__(self):
        self.calls: list = []

    def extract_from_session(self, messages, tools_called=None):
        self.calls.append(list(tools_called or []))
        return Skill(name="learned", description="d", tools_used=list(tools_called or []))


class FakeWM:
    def __init__(self):
        self.records: list = []

    def observe(self, ctx):
        return "s1"

    def record(self, prev, tool, after, ok=True):
        self.records.append((tool, ok))

    def bootstrap(self):
        return "stage"

    def save(self):
        return None


def _agent(**over):
    agent = SimpleNamespace(
        name="t", crystal=FakeCrystal(), _graph=FakeGraph(), memory=FakeMemory(),
        _typed=FakeTyped(), _skills=FakeSkillStore(), _skill_extractor=FakeExtractor(),
        _wm=None, _wm_prev_state=None, _wm_turns=0, _wm_stage=None,
    )
    for k, v in over.items():
        setattr(agent, k, v)
    # the REAL _learn_after, bound to the fake agent
    agent._learn_after = AitherAgent._learn_after.__get__(agent)
    return agent


def test_crystallizes_observation_text_not_answer_prose():
    agent = _agent()
    out = _run(turn_memory.after_turn(
        agent, message="how much disk is free on C?", answer=ANSWER_PROSE,
        tools=["disk_usage"], observations=[OBS_FACT], sid="s1"))
    written = "\n".join(agent.crystal.summaries)
    assert OBS_FACT in written
    assert ANSWER_PROSE not in written
    assert "nearly empty" not in written
    assert out.get("crystal", 0) >= 1


def test_no_crystallize_when_no_tools_ran():
    agent = _agent()
    _run(turn_memory.after_turn(agent, message="hello there friend", answer=ANSWER_PROSE,
                                tools=[], observations=[], sid="s1"))
    assert agent.crystal.summaries == []


def test_reasoner_advice_and_failures_are_not_crystallized():
    agent = _agent()
    obs = [f"{OBS_FACT}\n\nREASONER (a bigger model you consulted): the disk is fine",
           "(tool error: TimeoutError: slow)",
           "The best next step is clearly to restart the whole fleet now"]
    _run(turn_memory.after_turn(agent, message="check disk", answer="",
                                tools=["disk_usage", "shell", "ask_reasoner"],
                                observations=obs, sid="s1"))
    written = "\n".join(agent.crystal.summaries)
    assert OBS_FACT in written
    assert "the disk is fine" not in written
    assert "restart the whole fleet" not in written
    assert "TimeoutError" not in written


def test_graph_ingest_receives_only_the_user_role():
    agent = _agent()
    _run(turn_memory.after_turn(agent, message="my server is named orion", answer=ANSWER_PROSE,
                                tools=["disk_usage"], observations=[OBS_FACT], sid="s1"))
    assert len(agent._graph.ingested) == 1
    _sid, msgs = agent._graph.ingested[0]
    assert [m["role"] for m in msgs] == ["user"]
    assert msgs[0]["content"] == "my server is named orion"
    assert all(ANSWER_PROSE not in m["content"] for m in msgs)


def test_learn_after_accepts_plain_names_and_stream_dicts():
    wm = FakeWM()
    agent = _agent(_wm=wm, _wm_prev_state="s0")
    # plain names (the streaming loop's tools_made)
    _run(agent._learn_after("find and summarise", "done", ["web_search", "read_file"]))
    assert agent._skill_extractor.calls[-1] == ["web_search", "read_file"]
    assert agent._skills.saved, "a two-tool run extracts a skill"
    assert [t for t, _ in wm.records] == ["web_search", "read_file"]
    # dict shape: a failed call is an error, not a procedure step
    agent._skill_extractor.calls.clear()
    wm.records.clear()
    _run(agent._learn_after("find and summarise", "done",
                            [{"name": "web_search", "ok": True},
                             {"name": "read_file", "ok": False}]))
    assert agent._skill_extractor.calls == [], "only one working tool: no skill"
    assert all(isinstance(t, str) for t, _ in wm.records)
    assert ("read_file[failed]", False) in wm.records
    assert ("web_search", True) in wm.records


def test_history_row_carries_the_tool_digest():
    agent = _agent()
    _run(turn_memory.after_turn(agent, message="disk?", answer="41 GB free",
                                tools=["disk_usage"], observations=[OBS_FACT], sid="s1"))
    digests = [c for _sid, _role, c in agent.memory.rows if "[tools:" in c]
    assert digests, agent.memory.rows
    assert "disk_usage -> " in digests[0]
    assert agent._last_turn_by_sid["s1"]["tools"] == ["disk_usage"]
    # per-session routing state stays in process: Memory.remember pushes to Spirit/fleet
    assert not any(k.startswith("turn:last:") for k in agent.memory.kv)


def test_one_raising_substep_does_not_stop_the_others():
    agent = _agent(_graph=FakeGraph(raise_on_ingest=True))
    out = _run(turn_memory.after_turn(agent, message="disk free?", answer="41 GB",
                                      tools=["disk_usage"], observations=[OBS_FACT],
                                      sid="s1"))
    assert "graph" in out.get("degraded", [])
    assert agent.crystal.summaries, "crystal still wrote"
    assert any("[tools:" in c for _s, _r, c in agent.memory.rows), "digest still wrote"
    assert out.get("learn") == 1


def test_lesson_on_failure_streak():
    agent = _agent()
    out = _run(turn_memory.after_turn(
        agent, message="free space on C", answer="I could not tell",
        tools=["shell", "shell"],
        observations=["(tool error: X: a)", "(tool error: X: b)"], sid="s1"))
    assert out.get("lesson") == 1
    content, kw = agent._typed.rows[0]
    assert kw["role"] == "correction" and "shell" in content


def test_skills_block_empty_when_nothing_scores_two():
    weak = Skill(name="deploy website", description="push pages")
    agent = _agent(_skills=FakeSkillStore([weak]))
    # one shared word ("deploy") only: below the floor
    assert _run(turn_memory.skills_block(agent, "deploy the thing", k=3)) == ""
    strong = Skill(name="check disk usage", description="report free disk space on volumes")
    agent = _agent(_skills=FakeSkillStore([weak, strong]))
    block = _run(turn_memory.skills_block(agent, "check free disk space", k=3))
    assert "check disk usage" in block
    assert "deploy website" not in block
    assert len(block) <= turn_memory.SKILLS_BLOCK_MAX_CHARS
    assert agent._recalled_skills == [strong]


class SlowCrystal(FakeCrystal):
    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def crystallize(self, summary: str) -> int:
        await self.release.wait()
        return await super().crystallize(summary)


def test_digest_is_written_before_slow_writes():
    # The spine fires after_turn and moves on; a digest queued behind crystal's
    # embedding calls lands after the next turn's user row.
    async def scenario():
        agent = _agent(crystal=SlowCrystal())
        task = asyncio.ensure_future(turn_memory.after_turn(
            agent, message="disk?", answer="41 GB free", tools=["disk_usage"],
            observations=[OBS_FACT], sid="s1"))
        for _ in range(5):
            await asyncio.sleep(0)
        written_early = any("[tools:" in c for _s, _r, c in agent.memory.rows)
        agent.crystal.release.set()
        await task
        return written_early

    assert _run(scenario()), "digest waited on the crystal write"


def test_skills_search_runs_off_the_event_loop():
    seen: list = []

    class ThreadStore(FakeSkillStore):
        def search(self, query, k=5):
            seen.append(threading.get_ident())
            return super().search(query, k)

    async def scenario():
        agent = _agent(_skills=ThreadStore([Skill(name="check disk usage",
                                                  description="report free disk space")]))
        await turn_memory.skills_block(agent, "check free disk space", k=3)
        return threading.get_ident()

    loop_thread = _run(scenario())
    assert seen and seen[0] != loop_thread


def test_reasoner_consult_keeps_later_tools_aligned_with_their_observations():
    # The spine records ask_reasoner in `tools` but folds its advice into the failing
    # tool's observation, so `observations` is one shorter. Positional pairing gave
    # the reasoner the NEXT tool's output and that tool an empty (failed) observation.
    pairs = turn_memory._pairs(
        ["disk_probe", "disk_probe", "ask_reasoner", "disk_free"],
        ["(tool error: denied)",
         "(tool error: denied)\n\nREASONER (a bigger model you consulted): try disk_free",
         OBS_FACT],
    )
    assert [n for n, _ in pairs] == ["disk_probe", "disk_probe", "ask_reasoner", "disk_free"]
    assert pairs[1][1] == "(tool error: denied)"
    assert "try disk_free" in pairs[2][1]
    assert pairs[3] == ("disk_free", OBS_FACT)


def test_reasoner_consult_does_not_hide_the_next_tools_fact():
    crystal = FakeCrystal()
    agent = SimpleNamespace(crystal=crystal)
    _run(turn_memory.after_turn(
        agent, message="how much disk is free", answer="41 GB",
        tools=["disk_probe", "ask_reasoner", "disk_free"],
        observations=["(tool error: denied)\n\nREASONER (x): try disk_free", OBS_FACT],
        sid=None))
    assert crystal.summaries and "disk_free: " + OBS_FACT in crystal.summaries[0]
    assert "try disk_free" not in crystal.summaries[0]


def test_tool_digest_folds_into_the_answer_row_on_replay(tmp_path):
    # Two assistant rows in a row (answer, then the digest) reach the model as two
    # consecutive assistant turns; strict chat templates refuse that.
    from adk.memory import Memory

    mem = Memory(db_path=tmp_path / "m.db")
    agent = SimpleNamespace(memory=mem)

    async def go():
        await mem.add_message("s1", "user", "how much disk is free")
        await mem.add_message("s1", "assistant", "41 GB")
        await turn_memory.after_turn(agent, message="how much disk is free",
                                     answer="41 GB", tools=["disk_free"],
                                     observations=[OBS_FACT], sid="s1")
        await mem.add_message("s1", "user", "and on D:?")
        return await mem.get_history("s1")

    hist = _run(go())
    assert [h["role"] for h in hist] == ["user", "assistant", "user"]
    assert hist[1]["content"].startswith("41 GB")
    assert "disk_free ->" in hist[1]["content"]


def test_reasoner_with_its_own_observation_stays_aligned():
    pairs = turn_memory._pairs(
        ["disk_probe", "ask_reasoner", "disk_free"],
        ["(tool error: denied)\n\nREASONER (x): try disk_free", "try disk_free", OBS_FACT])
    assert pairs[2] == ("disk_free", OBS_FACT)


def test_credentials_and_secret_tools_are_never_crystallized():
    agent = _agent()
    fake = "sk-" + "live-" + "x" * 20  # assembled so no scanner sees a literal key
    keyed = f"config loaded api_key={fake} for the billing worker"
    _run(turn_memory.after_turn(
        agent, message="check the worker config and fetch the vault entry",
        answer="done", tools=["read_config", "get_secret"],
        observations=[keyed + "\nworker billing runs on node seven of the cluster",
                      "VALUE: hunter2-very-private-password-string"], sid="s1"))
    written = "\n".join(agent.crystal.summaries)
    assert fake not in written
    assert "hunter2" not in written
    assert "node seven" in written
    digest = "\n".join(str(r) for r in agent.memory.rows) if hasattr(agent.memory, "rows") else ""
    assert "hunter2" not in digest


def test_web_results_are_not_crystallized():
    # A web result is a live snapshot: recalled later as a fact, it answered "anything
    # exciting about tanium?" from this morning's search with no new look (2026-10-14).
    agent = _agent()
    _run(turn_memory.after_turn(
        agent, message="what is in the news and how full is the disk",
        answer="done", tools=["web_search", "disk_usage"],
        observations=["Ravens news: the new overtime rules were cracked this week",
                      OBS_FACT], sid="s1"))
    written = "\n".join(agent.crystal.summaries)
    assert "Ravens" not in written
    assert "41 GB free" in written


def test_json_fragments_and_compaction_markers_are_not_facts():
    # call_tool results were stored one JSON line at a time (2026-10-14).
    facts = turn_memory._observation_facts([(
        "call_tool",
        '[compacted by the decision door: kept 244 of 1016 lines]\n'
        '"uptime_seconds": 15536.558002233505\n'
        '"title": "Tanium adds endpoint behavior detection and AI threat hunting",\n'
        "... (12 similar lines dropped)\n"
        "{\n"
        "the orchestrator lane served 41 requests in the last hour on node seven",
    )])
    assert "uptime_seconds" not in facts and "Tanium" not in facts
    assert "compacted" not in facts and "similar lines" not in facts
    assert "node seven" in facts
