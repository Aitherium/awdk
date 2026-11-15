"""awsh end to end against a daemon and a kit started from THIS checkout.

Not a pytest test (it needs a real local model and about four minutes): run it
by hand. Temporary home, free ports; it never touches a running daemon.

    python awdk/tests/live_awsh_durable_e2e.py [--keep]

The model is the local spine's ``bonsai2-27b-5090`` at
``https://127.0.0.1:8150/v1``. A small model does not always make the tool call
it is asked for; when a step prints ``started.txt exists=False`` the model did
not run the slow command and the kill landed on nothing -- run it again.

  kit   : python -m adk run            (worktree code, project agent with slow tools)
  awsh  : python -m adk.harnesses.daemon (worktree code, AITHER_ADK_URL -> that kit)

Steps: spawn an awdk session with a cwd -> send a turn -> interrupt check ->
kill the kit mid-tool -> restart the kit -> the awsh session's events carry the
resumed answer -> awsh_resumable lists the kit session -> resume it and prompt it.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

AWDK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AWDK / "tests"))
sys.path.insert(0, str(AWDK))
import test_turn_resume_kill as k  # noqa: E402

TOKEN = "e2e-owner-token-for-a-temp-daemon"


def log(line):
    print(line, flush=True)


def call(method, url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as reply:
            return reply.status, json.loads(reply.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode()[:500]}


def main():
    root = Path(tempfile.mkdtemp(prefix="adk-awsh-e2e-"))
    work = root / "work"
    work.mkdir()
    (work / "marker.txt").write_text("the marker file in the session cwd\n", encoding="utf-8")
    kit = k.Kit(root, real=True, base_url="https://127.0.0.1:8150/v1",
                model="bonsai2-27b-5090", log=log)
    daemon = None
    port = k.free_port()
    base = f"http://127.0.0.1:{port}"
    try:
        kit.start(slow_s=600)
        env = kit.env(600)
        env.update({"AITHER_ADK_URL": kit.url, "AITHER_HARNESS_TOKEN": TOKEN,
                    "AITHER_HARNESS_ROOT": str(root / "harness-sessions"),
                    "AITHER_RELAY_RESUME_WAIT_S": "240", "AITHER_HOOKD": "0"})
        out = open(root / "daemon.log", "ab")
        daemon = subprocess.Popen(
            [sys.executable, "-m", "adk.harnesses.daemon", "--host", "127.0.0.1",
             "--port", str(port)],
            cwd=str(root), env=env, stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out.close()
        log(f"$ python -m adk.harnesses.daemon --port {port}   (pid {daemon.pid}, "
            f"AITHER_ADK_URL={kit.url})")
        for _ in range(120):
            try:
                if call("GET", base + "/health", timeout=3)[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("daemon never answered /health")

        code, info = call("POST", base + "/sessions",
                          {"harness": "awdk", "cwd": str(work), "title": "e2e"})
        log(f"POST /sessions awdk cwd={work.name} -> {code} id={info.get('id')} "
            f"cwd={info.get('cwd')}")
        sid = info["id"]

        # 1. cwd: a relative path resolves in the spawn cwd
        code, sent = call("POST", f"{base}/sessions/{sid}/input", {
            "text": "Run the shell command `pwd` with the shell tool and reply with "
                    "only the directory it printed."})
        log(f"POST /sessions/{sid}/input (pwd) -> {code}")
        wait = call("GET", f"{base}/sessions/{sid}/wait?until=idle&timeout=180", timeout=210)
        log(f"wait idle -> {wait[0]} {str(wait[1])[:160]}")
        events = call("GET", f"{base}/sessions/{sid}/events?after=0")[1].get("events", [])
        text = "".join(e.get("text") or "" for e in events if e.get("kind") == "text.delta")
        log(f"turn 1 answer: {text[-300:]!r}")

        log(f"CWD HONOURED: {work.name in text}")

        # 2. a slow turn in a FRESH session: state while streaming, then the kill
        first_sid = sid
        code, info = call("POST", base + "/sessions",
                          {"harness": "awdk", "cwd": str(work), "title": "e2e-kill"})
        sid = info["id"]
        log(f"POST /sessions awdk (kill session) -> {code} id={sid}")
        seq0 = 0
        slow = ("New task, unrelated to the earlier one. Call shell_exec exactly once with "
                "timeout=120 and this exact command, copied character for character: "
                "Add-Content started.txt go; Start-Sleep -Seconds %d; Add-Content done.txt ok"
                " -- do not run any other command. Then tell me in one sentence what happened.")
        code, _ = call("POST", f"{base}/sessions/{sid}/input", {"text": slow % 90})
        deadline = time.time() + 240
        while time.time() < deadline and not (work / "started.txt").exists():
            time.sleep(0.5)
        log(f"shell step in flight: started.txt exists={(work / 'started.txt').exists()}")
        state = call("GET", f"{base}/sessions/{sid}")[1].get("state")
        log(f"session state while the turn streams: {state}")
        kit.kill()
        kit.start(slow_s=0.2)
        log("kit restarted; waiting for the awsh session to receive the resumed answer")
        wait = call("GET", f"{base}/sessions/{sid}/wait?until=idle&timeout=280", timeout=310)
        events = call("GET", f"{base}/sessions/{sid}/events?after={seq0}")[1].get("events", [])
        for e in events:
            if e.get("kind") in ("error", "turn.completed") or (
                    e.get("kind") == "notice" and "pipeline" not in str(e.get("text"))):
                log(f"  event {e.get('kind')}: {str(e.get('text'))[:200]!r} "
                    f"{json.dumps(e.get('data'))[:200]}")
        answer = "".join(e.get("text") or "" for e in events if e.get("kind") == "text.delta")
        log(f"resumed answer in the awsh session: {answer[-400:]!r}")
        started = ((work / "started.txt").read_text("utf-8", "replace").split()
                   if (work / "started.txt").exists() else [])
        log(f"started.txt lines after resume: {len(started)} (1 = the shell step was not re-run); "
            f"done.txt exists={(work / 'done.txt').exists()}")
        status = kit.get(f"/sessions/{sid}/turns?agent=aither", timeout=180)
        for t in status["turns"]:
            log(f"  kit turn: outcome={t['outcome']} attempts={t['attempts']} "
                f"steps={[(x['tool'], x['status']) for x in t['steps']]} "
                f"notes={[(n['kind'], n['tool']) for n in t['notes']]}")

        # 3. interrupt a running turn, in another fresh session
        code, info = call("POST", base + "/sessions",
                          {"harness": "awdk", "cwd": str(work), "title": "e2e-interrupt"})
        sid = info["id"]
        log(f"POST /sessions awdk (interrupt session) -> {code} id={sid}")
        call("POST", f"{base}/sessions/{sid}/input", {
            "text": "Without using any tool, print every integer from 1 to 4000, one "
                    "per line, with nothing else."})
        deadline = time.time() + 120
        while time.time() < deadline:
            ev = call("GET", f"{base}/sessions/{sid}/events?after=0")[1].get("events", [])
            if sum(1 for e in ev if e.get("kind") == "text.delta") >= 3:
                break
            time.sleep(0.5)
        log(f"state before interrupt: {call('GET', f'{base}/sessions/{sid}')[1].get('state')}")
        code, out = call("POST", f"{base}/sessions/{sid}/interrupt", {})
        log(f"POST /sessions/{sid}/interrupt -> {code} {out}")
        wait = call("GET", f"{base}/sessions/{sid}/wait?until=idle&timeout=60", timeout=90)
        log(f"state after interrupt: {call('GET', f'{base}/sessions/{sid}')[1].get('state')}")
        status = kit.get(f"/sessions/{sid}/turns?agent=aither", timeout=180)
        log("kit turn outcomes: " + str([(t["outcome"], t["reason"][:40])
                                           for t in status["turns"]]))

        # 4. awsh_resumable + resume + prompt (the MCP tool functions, this daemon)
        os.environ.update({"HOME": str(kit.home), "USERPROFILE": str(kit.home),
                           "AITHER_HARNESS_URL": base, "AITHER_HARNESS_TOKEN": TOKEN})
        from adk.harnesses import mcp_stdio as m
        m._unsearched_resumable = lambda searched: []   # never dial another daemon
        listing = m._resumable({"harness": "awdk"})
        log("awsh_resumable(harness=awdk) -> count=%s sessions=%s" % (
            listing.get("count"),
            [(r["session_id"], r["messages"], r["open_turn"])
             for r in listing.get("sessions", [])]))
        resumed = call("POST", base + "/sessions", {
            "harness": "awdk", "cwd": str(work), "resume_session_id": first_sid})
        rid = resumed[1].get("id")
        log(f"POST /sessions resume_session_id={first_sid} -> {resumed[0]} new id {rid}")
        call("POST", f"{base}/sessions/{rid}/input", {
            "text": "In one short sentence, from this conversation only and without "
                    "any tool: what directory did pwd print at the start?"})
        call("GET", f"{base}/sessions/{rid}/wait?until=idle&timeout=180", timeout=210)
        ev = call("GET", f"{base}/sessions/{rid}/events?after=0")[1].get("events", [])
        said = "".join(e.get("text") or "" for e in ev if e.get("kind") == "text.delta")
        log(f"resumed session answered: {said[-300:]!r}")
        log(f"RESUMED SESSION REMEMBERS: {work.name in said}")
    finally:
        kit.stop()
        if daemon is not None and daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=30)
        if "--keep" in sys.argv:
            log(f"kept {root}")
        else:
            for _ in range(10):
                shutil.rmtree(root, ignore_errors=True)
                if not root.exists():
                    break
                time.sleep(0.5)


if __name__ == "__main__":
    main()
