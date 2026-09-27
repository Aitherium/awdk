"""adk.evalharness.arc_agi3.live: the Theater sink never raises, drops on overflow,
coalesces frames, and maps loop records onto turn/hypothesis events."""

import threading
import time

from adk.evalharness.arc_agi3 import live


def _sink(**kw):
    got = []
    gate = kw.pop("gate", None)

    def opener(url, body, headers, timeout):
        if gate is not None:
            gate.wait(5)
        import json

        got.append((url, headers, json.loads(body)))
        return 200

    return live.LiveSink("http://theater.test", "tok", opener=opener, **kw), got


def test_posts_batches_with_bearer_and_ingest_path():
    s, got = _sink()
    s.post({"type": "run_start", "model": "m"})
    st = s.close()
    assert st["sent"] == 1 and st["failed"] == 0
    url, headers, body = got[0]
    assert url == "http://theater.test/api/ingest"
    assert headers["Authorization"] == "Bearer tok"
    assert body["events"][0]["type"] == "run_start"
    # Cloudflare in front of arc.aitherium.com refuses urllib's default
    # "Python-urllib/3.x" with 403: every public post failed (sent=0, failed=4).
    assert headers.get("User-Agent", "").startswith("awdk-arc-live/")


def test_overflow_drops_and_never_blocks():
    gate = threading.Event()
    s, _ = _sink(gate=gate, queue_max=4)
    t0 = time.monotonic()
    for i in range(50):
        s.post({"type": "step", "actions": i})
    assert time.monotonic() - t0 < 0.5
    assert s.dropped >= 40
    gate.set()
    s.close()


def test_failures_are_counted_not_raised():
    def boom(*a):
        raise OSError("refused")

    s = live.LiveSink("http://dead.test", "tok", opener=boom, backoff_s=60)
    for i in range(10):
        s.post({"type": "step"})
        time.sleep(0.01)
    st = s.close()
    assert st["sent"] == 0 and st["failed"] + st["dropped"] == 10
    assert "refused" in st["last_error"]


def test_only_newest_step_in_a_batch_keeps_its_frame():
    s = live.LiveSink.__new__(live.LiveSink)
    s.batch_max = 10
    import queue

    s._q = queue.Queue()
    for i in range(3):
        s._q.put({"type": "step", "frame": [[i]]})
    batch = s._drain({"type": "step", "frame": [[9]]})
    frames = [e.get("frame") for e in batch]
    assert frames[:-1] == [None, None, None] and frames[-1] == [[2]]


class _Env:
    levels, actions, state_name = 0, 0, "NOT_FINISHED"

    def act(self, action, source="model"):
        self.actions += 1
        return type("O", (), {"state": [[1, 2], [3, 4]], "level": 0})()

    def primer(self):
        return "hook"


def test_live_env_posts_steps_and_delegates_hooks():
    posted = []
    sink = type("S", (), {"post": lambda self, e: posted.append(e)})()
    env = live.LiveEnv(_Env(), sink, "ls20")
    env.act((6, 3, 4))
    assert posted[0]["action"] == "ACTION6" and posted[0]["xy"] == [3, 4]
    assert posted[0]["frame"] == [[1, 2], [3, 4]] and env.primer() == "hook"


def test_loop_tee_maps_turn_and_result():
    posted = []
    sink = type("S", (), {"post": lambda self, e: posted.append(e)})()
    tee = live.LoopTee(sink, "ls20")
    tee(
        {
            "event": "turn",
            "turn": 1,
            "intent": "probe",
            "strategy": "explore",
            "content": "SITUATION: a wall\nANALYSIS: blocked\nSYNTHESIS: go left\nEXECUTION:\n"
            "```python\nact(3, expect={'moved': True})\n```",
        }
    )
    tee(
        {
            "event": "result",
            "turn": 1,
            "success": False,
            "verified_now": ["h_ok"],
            "result": "REFUTED by new evidence: h_bad\nRecent prediction misses: moved != True",
        }
    )
    turn = posted[0]
    assert turn["sase"]["situation"] == "a wall" and "act(3" in turn["code"]
    assert "moved" in turn["prediction"]
    assert posted[1]["hit"] is False
    hyps = {e["name"]: e["status"] for e in posted if e["type"] == "hypothesis"}
    assert hyps == {"h_ok": "verified", "h_bad": "refuted"}
    tee({"event": "turn", "content": None, "turn": object()})  # never raises


def test_disabled_without_token(monkeypatch):
    for n in live.TOKEN_ENVS:
        monkeypatch.delenv(n, raising=False)
    msgs = []
    assert live.from_args("http://x", say=msgs.append) is None
    assert live.from_args("off") is None
