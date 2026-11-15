"""Kill a real kit server mid-tool, restart it, and read the resumed turn.

This starts ``python -m adk run`` (the worktree's code, a temporary home, a free
port) on a project whose ``agent.py`` registers three tools that write every real
execution to an effects file:

* ``record_receipt`` -- finishes before the kill;
* ``slow_job`` -- not declared repeat-safe, in flight when the process is killed;
* ``slow_lookup`` -- declared ``repeat_safe=True``, in flight in a second session.

The server process is killed hard (``TerminateProcess`` / ``SIGKILL``) while both
slow tools are running, then started again. What must hold afterwards:

* ``record_receipt`` ran ONCE (its recorded result is replayed);
* ``slow_job`` was NOT run again and the model was told ``outcome_unknown``;
* ``slow_lookup`` WAS run again (it said it is safe to repeat);
* both turns reach the terminal outcome ``resumed``;
* the client that sends the same request again receives the answer (before the
  journal: 0 bytes), and the answer is in the session's history.

As a pytest test the model is a deterministic stub: a small OpenAI-compatible
HTTP server in this process, so the kit runs its real provider code against it.
Run this file directly for a transcript, and with ``--real`` for a real local
model::

    python awdk/tests/test_turn_resume_kill.py                 # stub model
    python awdk/tests/test_turn_resume_kill.py --real \\
        --base-url https://127.0.0.1:8150/v1 --model bonsai2-27b-5090
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

AWDK = Path(__file__).resolve().parents[1]

AGENT_PY = r'''
"""Project agent for the kill-and-resume test (written by test_turn_resume_kill.py)."""
import asyncio
import json
import os
from pathlib import Path

from adk.agent import AitherAgent

EFFECTS = Path(os.environ["DURABLE_EFFECTS"])
SLOW_S = float(os.environ.get("DURABLE_SLOW_S", "60"))


def effect(line: str) -> None:
    with open(EFFECTS, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


agent = AitherAgent("durable", builtin_tools=False)
agent._graph = None


@agent.tool(name="record_receipt", description="Record receipt number n. Call this FIRST.")
async def record_receipt(n: int = 1) -> str:
    effect("record_receipt ran")
    return json.dumps({"ok": True, "receipt": "R-%s" % n})


@agent.tool(name="slow_job", description="Run the slow job for an amount. Takes a while.")
async def slow_job(amount: int = 5) -> str:
    effect("slow_job start")
    await asyncio.sleep(SLOW_S)
    effect("slow_job end")
    return json.dumps({"ok": True, "job": "done", "amount": amount})


@agent.tool(name="slow_lookup", description="Look up the slow value for an amount.",
            repeat_safe=True)
async def slow_lookup(amount: int = 5) -> str:
    effect("slow_lookup start")
    await asyncio.sleep(SLOW_S)
    effect("slow_lookup end")
    return json.dumps({"ok": True, "value": 42, "amount": amount})
'''

JOB_PROMPT = ("Do exactly two tool calls, in this order. First: record_receipt with n=1. "
              "Second, after it returns: slow_job with amount=5 (the job tool; this task "
              "has nothing to look up). Then report both tool results in one sentence.")
LOOKUP_PROMPT = ("Do exactly two tool calls, in this order. First: record_receipt with "
                 "n=1. Second, after it returns: slow_lookup with amount=5 (the lookup "
                 "tool; this task runs no job). Then report both tool results in one "
                 "sentence.")


class StubModel:
    """A deterministic OpenAI-compatible model: receipt first, then the slow tool,
    then a report of exactly what the tool results in front of it say."""

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        stub = self
        self.requests = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):     # quiet
                return

            def _send(self, code, payload, content_type="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    self._send(200, {"object": "list", "data": [
                        {"id": "stub-model", "object": "model"}]})
                else:
                    self._send(200, {"status": "ok"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    request = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    request = {}
                stub.requests += 1
                message = stub.reply(request)
                finish = "tool_calls" if message.get("tool_calls") else "stop"
                if request.get("stream"):
                    delta = dict(message)
                    for i, call in enumerate(delta.get("tool_calls") or []):
                        call["index"] = i
                    chunk = {"id": "stub", "object": "chat.completion.chunk",
                             "model": "stub-model",
                             "choices": [{"index": 0, "delta": delta,
                                          "finish_reason": finish}]}
                    self._send(200, ("data: %s\n\ndata: [DONE]\n\n" % json.dumps(chunk)
                                     ).encode(), "text/event-stream")
                    return
                self._send(200, {
                    "id": "stub", "object": "chat.completion", "model": "stub-model",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                              "total_tokens": 15}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @staticmethod
    def _text(content) -> str:
        if isinstance(content, list):
            return " ".join(str(part.get("text", "")) for part in content
                            if isinstance(part, dict))
        return str(content or "")

    def reply(self, request: dict) -> dict:
        messages = request.get("messages") or []
        if not request.get("tools"):
            return {"role": "assistant", "content": "ok"}
        users = [self._text(m.get("content")) for m in messages if m.get("role") == "user"]
        tools = [self._text(m.get("content")) for m in messages if m.get("role") == "tool"]
        slow = "slow_lookup" if "slow_lookup" in (users[-1] if users else "") else "slow_job"

        def call(call_id, name, args):
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}

        if len(tools) == 0:
            return call("c1", "record_receipt", {"n": 1})
        if len(tools) == 1:
            return call("c2", slow, {"amount": 5})
        unknown = "outcome_unknown" in tools[1]
        return {"role": "assistant", "content": "REPORT receipt=%s slow=%s" % (
            tools[0], "OUTCOME-UNKNOWN (not re-run)" if unknown else tools[1])}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Kit:
    """One kit server process on a temporary home and a temporary project."""

    def __init__(self, root: Path, *, real: bool = False, base_url: str = "",
                 model: str = "", log=print):
        self.root = root
        self.home = root / "home"
        self.project = root / "project"
        self.effects = root / "effects.log"
        self.server_log = root / "server.log"
        self.port = free_port()
        self.real, self.base_url, self.model = real, base_url, model
        self.proc: subprocess.Popen | None = None
        self.log = log
        self.home.mkdir(parents=True, exist_ok=True)
        self.project.mkdir(parents=True, exist_ok=True)
        (self.project / "agent.py").write_text(AGENT_PY, encoding="utf-8")
        self.effects.write_text("", encoding="utf-8")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def env(self, slow_s: float) -> dict:
        env = dict(os.environ)
        for name in [k for k in env if k.startswith(("AITHER_", "ADK_"))]:
            env.pop(name)
        env.update({
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "PYTHONPATH": str(AWDK), "PYTHONIOENCODING": "utf-8",
            "AITHER_OFFLINE": "1", "AITHER_PHONEHOME": "0", "AITHER_SPIRIT_BRIDGE": "false",
            "DURABLE_EFFECTS": str(self.effects), "DURABLE_SLOW_S": str(slow_s),
        })
        env["AITHER_LLM_BASE_URL"] = self.base_url
        if self.real:
            ca = Path(os.environ.get("AITHER_CA_BUNDLE")
                      or Path.home() / ".aither" / "aithernet-ca-bundle.pem")
            if ca.is_file():
                env["AITHER_CA_BUNDLE"] = str(ca)
        return env

    def start(self, slow_s: float) -> None:
        argv = [sys.executable, "-m", "adk", "run", "--port", str(self.port),
                "--host", "127.0.0.1", "--backend", "vllm", "--model", self.model]
        out = open(self.server_log, "ab")
        self.proc = subprocess.Popen(
            argv, cwd=str(self.project), env=self.env(slow_s), stdout=out,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out.close()
        self.log(f"$ {' '.join(argv[1:])}   (cwd=project, pid {self.proc.pid})")
        deadline = time.time() + 180
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"kit server exited {self.proc.returncode}:\n"
                                   + self.server_log.read_text("utf-8", "replace")[-3000:])
            try:
                health = self.get("/health", timeout=5)
                if health.get("status"):
                    return
            except (OSError, ValueError):
                time.sleep(0.5)
        raise RuntimeError("kit server never answered /health")

    def kill(self) -> None:
        assert self.proc is not None
        pid = self.proc.pid
        self.proc.kill()            # TerminateProcess on Windows, SIGKILL elsewhere
        self.proc.wait(timeout=30)
        self.log(f"KILLED pid {pid} (exit {self.proc.returncode})")
        self.proc = None

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)
        self.proc = None

    def headers(self) -> dict:
        """The owner's local credential: an offline kit refuses an anonymous
        caller (adk.local_auth). It is minted in the temporary home at start."""
        token_file = self.home / ".aither" / "daemon-token"
        token = token_file.read_text("utf-8").strip() if token_file.is_file() else ""
        return {"X-Aither-Local-Token": token} if token else {}

    def get(self, path: str, timeout: float = 30):
        request = urllib.request.Request(self.url + path, headers=self.headers())
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))

    def post(self, path: str, body: dict, timeout: float = 300):
        request = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **self.headers()})
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))

    def effect_lines(self) -> list[str]:
        return [ln for ln in self.effects.read_text("utf-8").splitlines() if ln]


def _post_in_thread(kit: Kit, body: dict, box: dict):
    import threading

    def run():
        try:
            reply = kit.post("/chat", body, timeout=600)
            box["bytes"] = len(json.dumps(reply))
            box["reply"] = reply
        except Exception as exc:  # noqa: BLE001 -- the kill lands here
            box["bytes"] = 0
            box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def run_scenario(root: Path, *, real: bool = False, base_url: str = "", model: str = "",
                 log=print) -> dict:
    """The whole kill-and-resume run. Returns what was observed."""
    stub = None if real else StubModel()
    if stub is not None:
        base_url, model = stub.base_url, "stub-model"
    kit = Kit(root, real=real, base_url=base_url, model=model, log=log)
    seen: dict = {"model": model if real else "stub (deterministic OpenAI-compatible)"}
    log(f"model: {seen['model']} at {base_url}")
    try:
        kit.start(slow_s=600)
        seen["journal_enabled"] = kit.get("/health")["turns"]["enabled"]
        log(f"GET /health .turns.enabled = {seen['journal_enabled']}")

        job, lookup = {}, {}
        t_job = _post_in_thread(kit, {"message": JOB_PROMPT, "session_id": "kill-job"}, job)
        t_lookup = _post_in_thread(
            kit, {"message": LOOKUP_PROMPT, "session_id": "kill-lookup"}, lookup)
        log('POST /chat {session_id: "kill-job"}     -> record_receipt, slow_job')
        log('POST /chat {session_id: "kill-lookup"}  -> record_receipt, slow_lookup')

        deadline = time.time() + (240 if real else 120)
        while time.time() < deadline:
            lines = kit.effect_lines()
            if "slow_job start" in lines and "slow_lookup start" in lines:
                break
            if not (t_job.is_alive() or t_lookup.is_alive()):
                break
            time.sleep(0.25)
        seen["effects_before_kill"] = kit.effect_lines()
        log(f"effects before kill: {seen['effects_before_kill']}")
        if not ("slow_job start" in seen["effects_before_kill"]
                and "slow_lookup start" in seen["effects_before_kill"]):
            seen["error"] = ("the model never got both slow tools in flight: "
                             f"job={job} lookup={lookup}")
            return seen

        kit.kill()
        t_job.join(timeout=60)
        t_lookup.join(timeout=60)
        seen["client_bytes_at_kill"] = [job.get("bytes"), lookup.get("bytes")]
        log(f"clients at kill: job -> {job.get('bytes')} bytes ({job.get('error')}), "
            f"lookup -> {lookup.get('bytes')} bytes")

        kit.start(slow_s=0.2)            # restart: same home, same project, same port
        startup = None
        deadline = time.time() + 60
        while time.time() < deadline and not startup:
            startup = kit.get("/health")["turns"].get("startup_resume")
            time.sleep(0.25)
        seen["startup_resume"] = startup
        log(f"restarted; /health .turns.startup_resume = {startup}")

        # the reconnecting client: the same request, the same session id
        again = kit.post("/chat", {"message": JOB_PROMPT, "session_id": "kill-job"},
                         timeout=600 if real else 120)
        seen["reconnect_job"] = again.get("response", "")
        log(f'reconnect POST /chat "kill-job" -> {len(json.dumps(again))} bytes: '
            f'{again.get("response", "")[:300]!r}')

        # the client that never reconnects: the answer is in the session's status
        status_lookup = kit.get("/sessions/kill-lookup/turns?wait=120",
                                timeout=600 if real else 150)
        status_job = kit.get("/sessions/kill-job/turns")
        seen["job_turns"] = status_job["turns"]
        seen["lookup_turns"] = status_lookup["turns"]
        for name, status in (("kill-job", status_job), ("kill-lookup", status_lookup)):
            turn = status["turns"][0]
            log(f"GET /sessions/{name}/turns -> outcome={turn['outcome']} "
                f"attempts={turn['attempts']} steps="
                f"{[(s['tool'], s['status']) for s in turn['steps']]} "
                f"notes={[(n['kind'], n['tool']) for n in turn['notes']]}")
            log(f"    result: {str(turn.get('result', ''))[:300]!r}")
        seen["effects_after"] = kit.effect_lines()
        log(f"effects after resume: {seen['effects_after']}")
        seen["health_turns"] = kit.get("/health")["turns"]
        log("GET /health .turns = " + json.dumps(
            {k: seen["health_turns"].get(k) for k in (
                "open", "resumed", "abandoned", "failed", "replayed_steps",
                "unknown_steps", "rerun_steps")}))
        return seen
    finally:
        kit.stop()
        if stub is not None:
            stub.close()


def check(seen: dict) -> list[str]:
    """Every claim the run has to support. Empty list = all held."""
    bad: list[str] = []
    if seen.get("error"):
        return [seen["error"]]
    after = seen["effects_after"]
    if after.count("record_receipt ran") != 2:      # once per session, never again
        bad.append(f"record_receipt ran {after.count('record_receipt ran')}x, want 2 "
                   "(once in each session; a resumed turn must not re-run it)")
    if after.count("slow_job start") != 1:
        bad.append(f"slow_job started {after.count('slow_job start')}x: the in-flight "
                   "step that is not repeat-safe was re-run")
    # The lookup session's in-flight slow_lookup is repeat-safe: it ran again, to
    # the end. (A real model told "outcome unknown" in the OTHER session may also
    # choose to call a read to check; that is its decision, so no upper bound.)
    lookup_steps = {s["tool"]: s["status"] for s in seen["lookup_turns"][0]["steps"]}
    if (after.count("slow_lookup start") < 2 or after.count("slow_lookup end") < 1
            or lookup_steps.get("slow_lookup") != "finished"):
        bad.append("slow_lookup (repeat_safe) was not re-run to completion: "
                   f"{after} steps={lookup_steps}")
    if seen["client_bytes_at_kill"] != [0, 0]:
        bad.append(f"a client got bytes from a killed server: {seen['client_bytes_at_kill']}")
    if not seen["reconnect_job"].strip():
        bad.append("the reconnecting client received an empty answer")
    for name in ("job_turns", "lookup_turns"):
        turn = seen[name][0]
        if turn["outcome"] != "resumed" or turn["attempts"] != 1:
            bad.append(f"{name}: outcome={turn['outcome']} attempts={turn['attempts']}, "
                       "want resumed after 1 resume")
        if not str(turn.get("result") or "").strip():
            bad.append(f"{name}: no result recorded")
    job_notes = [(n["kind"], n["tool"]) for n in seen["job_turns"][0]["notes"]]
    if ("tool_unknown", "slow_job") not in job_notes:
        bad.append(f"slow_job was not reported outcome-unknown: {job_notes}")
    if len(seen["job_turns"]) != 1:
        bad.append(f"the reconnect started a second turn: {len(seen['job_turns'])} turns")
    return bad


def test_kill_mid_tool_restart_and_resume(tmp_path):
    """Stub model, real process, real kill."""
    lines: list[str] = []
    seen = run_scenario(tmp_path, log=lines.append)
    problems = check(seen)
    assert not problems, "\n".join(problems + ["--- transcript ---"] + lines)
    # stub-only exact claims: the model's own report says what it was told
    assert "OUTCOME-UNKNOWN (not re-run)" in seen["reconnect_job"]
    assert "R-1" in seen["reconnect_job"]                       # the replayed receipt
    assert '"value": 42' in seen["lookup_turns"][0]["result"]   # the re-run lookup
    assert seen["effects_after"].count("slow_lookup start") == 2
    assert seen["effects_after"].count("slow_lookup end") == 1
    replayed = [n for n in seen["job_turns"][0]["notes"] if n["kind"] == "tool_replayed"]
    assert [n["tool"] for n in replayed] == ["record_receipt"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--real", action="store_true", help="use a real local model")
    parser.add_argument("--base-url", default="https://127.0.0.1:8150/v1")
    parser.add_argument("--model", default="bonsai2-27b-5090")
    parser.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="adk-kill-"))
    try:
        seen = run_scenario(root, real=args.real, base_url=args.base_url, model=args.model,
                            log=lambda line: print(line, flush=True))
        problems = check(seen)
        print("\nRESULT:", "PASS" if not problems else "FAIL")
        for problem in problems:
            print("  -", problem)
        if problems:
            print("--- server log tail ---")
            log = root / "server.log"
            if log.is_file():
                print(log.read_text("utf-8", "replace")[-4000:]
                      .encode("ascii", "replace").decode("ascii"))
        return 0 if not problems else 1
    finally:
        if not args.keep:
            for _ in range(10):       # Windows releases the killed server's log late
                shutil.rmtree(root, ignore_errors=True)
                if not root.exists():
                    break
                time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
