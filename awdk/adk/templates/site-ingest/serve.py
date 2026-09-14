"""Site Ingest Studio — one-command local server.

Runs awdk's AitherAgent with the `site-ingest` pack applied, the
operator's own LLM key (Anthropic / OpenAI / Codex-OAuth / local Ollama), and a
live "tokens used vs. saved" meter. No sign-in, no AitherOS stack required.

    python serve.py                 # auto-detects key from env / .env, opens browser
    python serve.py --port 8131 --no-open

The agent IS awdk (ReAct loop, knowledge graph, memory, metering). This
file only: (1) applies the pack, (2) injects the BYO LLM via a metering router,
(3) registers the curated tool set, and (4) serves the chat UI + SSE + ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# Path setup — frozen-aware so a PyInstaller one-file build works:
#   HERE   = writable location (next to the .exe / the script) for .env, .data, artifacts
#   ASSETS = read-only bundled assets (web/, pack/) — _MEIPASS when frozen
if getattr(sys, "frozen", False):
    HERE = Path(sys.executable).resolve().parent
    ASSETS = Path(getattr(sys, "_MEIPASS", HERE))
else:
    HERE = Path(__file__).resolve().parent
    ASSETS = HERE
if str(ASSETS) not in sys.path:
    sys.path.insert(0, str(ASSETS))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("site_ingest.serve")


# ─────────────────────────────────────────────────────────────────────────────
# Environment + provider resolution
# ─────────────────────────────────────────────────────────────────────────────
def _env_path() -> Path:
    """The .env file path (overridable via AITHER_ENV_FILE — used by tests)."""
    return Path(os.getenv("AITHER_ENV_FILE", str(HERE / ".env")))


def load_env() -> None:
    """Load .env (KEY=VALUE) from the deliverable folder without overriding env."""
    env_path = _env_path()
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def write_env(updates: dict) -> None:
    """Create/replace KEY=VALUE lines in .env and apply them to the running process.
    Used by the in-UI settings screen so the operator never edits files by hand."""
    env_path = _env_path()
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    seen, out = set(), []
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() in updates:
            k = s.split("=", 1)[0].strip()
            out.append(f"{k}={updates[k]}")
            seen.add(k)
        else:
            out.append(ln)
    for k in (set(updates) - seen):
        out.append(f"{k}={updates[k]}")
    env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    for k, v in updates.items():
        os.environ[k] = str(v)


def _ollama_reachable() -> bool:
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    try:
        import httpx
        r = httpx.get(f"{host}/api/tags", timeout=2.0)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def resolve_provider() -> tuple[str, str, str | None]:
    """Return (provider, api_key, model). Order: explicit -> Anthropic -> OpenAI
    -> DeepSeek -> Ollama. DeepSeek/Groq/Together are OpenAI-compatible and handled
    natively by adk's LLMRouter (correct base_url + default model per provider)."""
    model = os.getenv("SITE_INGEST_MODEL") or None
    explicit = (os.getenv("SITE_INGEST_PROVIDER") or "").strip().lower()

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    openai_key = os.getenv("OPENAI_API_KEY", "")
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")

    if explicit == "anthropic" or (not explicit and anthropic_key):
        return "anthropic", anthropic_key, model
    if explicit == "openai" or (not explicit and openai_key):
        return "openai", openai_key, model
    if explicit == "deepseek" or (not explicit and deepseek_key):
        return "deepseek", deepseek_key, model
    if explicit in ("groq", "together"):
        return explicit, os.getenv(f"{explicit.upper()}_API_KEY", ""), model
    if explicit == "ollama" or (not explicit and _ollama_reachable()):
        return "ollama", "", model
    # Codex / ChatGPT OAuth (experimental) — if a saved token exists, use it as OpenAI bearer.
    try:
        from engine import codex_oauth
        creds = codex_oauth.load_saved()
        if creds and creds.valid:
            return "openai", creds.access_token, model
    except Exception:  # noqa: BLE001
        pass
    if explicit:
        # User asked for a provider but we have no key — surface clearly.
        return explicit, anthropic_key or openai_key or deepseek_key, model
    return "", "", model


# ─────────────────────────────────────────────────────────────────────────────
# Load the pack (no key needed) + build the pack-applied agent (needs a key)
# ─────────────────────────────────────────────────────────────────────────────
def load_pack():
    """Load the brain pack — persona/system_prompt, UI labels, tool whitelist. No
    LLM key required, so the app can serve the UI (and the key-config screen) before
    a provider is configured. Also makes a local awdk checkout importable."""
    adk_path = os.getenv("AITHER_ADK_PATH", "")  # dev/monorepo escape hatch
    if adk_path and adk_path not in sys.path and Path(adk_path).is_dir():
        sys.path.insert(0, adk_path)
    pack_dir = ASSETS / "pack" / "site-ingest"
    os.environ.setdefault("AGENT_BRAIN_PACK", str(pack_dir / "brain_pack.yaml"))
    os.environ.setdefault("AITHER_PACKS_DIR", str(ASSETS / "pack"))
    try:
        from adk.pack_discovery import discover_brain_pack
        bp_path = discover_brain_pack() or (pack_dir / "brain_pack.yaml")
    except Exception:  # noqa: BLE001
        bp_path = pack_dir / "brain_pack.yaml"
    bp = yaml.safe_load(Path(bp_path).read_text(encoding="utf-8")) or {}
    sys_prompt = (bp.get("system_prompt") or "").rstrip()
    skills_dir = Path(bp_path).parent / "skills"
    if skills_dir.is_dir():
        for sk in sorted(skills_dir.glob("*.md")):
            sys_prompt += "\n\n" + sk.read_text(encoding="utf-8").strip()
    return bp, sys_prompt


def build_agent():
    """Construct the AitherAgent with the site-ingest pack applied. Returns
    (agent, session, brain_pack_dict, provider, model). Raises SystemExit
    when no LLM provider/key is set — the server catches this and starts in
    'unconfigured' mode so the operator can add a key in the web UI."""
    bp, sys_prompt = load_pack()

    # BYO-key deliverable: the operator runs entirely on their OWN inference, so
    # adk's community-tier monthly-token cap must NOT apply. Force gating OFF.
    os.environ["AITHER_LICENSE_ENFORCE"] = "0"

    provider, api_key, model = resolve_provider()
    if not provider:
        raise SystemExit("no LLM configured")

    # Data lives under the deliverable folder (self-contained, easy to wipe).
    data_dir = Path(os.getenv("AITHER_DATA_DIR", str(HERE / ".data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["AITHER_DATA_DIR"] = str(data_dir)
    log.info("Brain pack applied; provider=%s model=%s", provider, model or "(default)")

    from adk.agent import AitherAgent
    from adk.builtin_tools import register_self_tools
    from adk.identity import Identity
    from adk.memory import Memory
    from engine.tools import SiteIngestSession, build_ingest_tools

    llm = _create_llm_router(provider, api_key, model)

    identity = Identity(
        name="site-analyst",
        description="Website analyst — discovers pages, extracts brand tokens, produces SiteSpec JSON.",
        system_prompt=sys_prompt,
        skills=["site_ingest"],
    )
    # Keep conversation memory INSIDE the deliverable's data dir
    (data_dir / "memory").mkdir(parents=True, exist_ok=True)
    mem = Memory(
        db_path=str(data_dir / "memory" / "site-analyst.db"),
        agent_name="site-analyst",
    )
    agent = AitherAgent(
        name="site-analyst",
        identity=identity,
        llm=llm,
        memory=mem,
        builtin_tools=False,  # default-deny: only the curated tools below
        system_prompt=sys_prompt,
    )

    # Guaranteed kill-switch for the community-tier quota (BYO key = unmetered).
    try:
        q = agent.meter._quota
        q.monthly_limit = q.daily_limit = q.hourly_limit = 0
        q.cost_limit_usd = 0
    except Exception as exc:  # noqa: BLE001
        log.debug("meter quota reset skipped: %s", exc)

    # Workspace for artifacts
    workspace = data_dir / "site_foundry" / "default"
    workspace.mkdir(parents=True, exist_ok=True)
    session = SiteIngestSession(workspace_dir=workspace, session_id="ingest")

    for fn in build_ingest_tools(session):
        agent._tools.register(fn)
    try:
        register_self_tools(agent)  # honest introspection tools
    except Exception as exc:  # noqa: BLE001
        log.debug("register_self_tools failed: %s", exc)

    # Enforce the whitelist (default-deny): keep only pack tools + self_* introspection.
    allowed = set(bp.get("tools") or [])
    for name in list(agent._tools._tools.keys()):
        if name not in allowed and not name.startswith("self_"):
            del agent._tools._tools[name]
    log.info(
        "Tools enabled (%d): %s",
        len(agent._tools._tools),
        ", ".join(sorted(agent._tools._tools)),
    )

    return agent, session, bp, provider, model


def _create_llm_router(provider: str, api_key: str, model: str | None):
    """Create the LLM router (metering + provider selection)."""
    from adk.llm.router import LLMRouter
    return LLMRouter(provider=provider, api_key=api_key, model=model)


# ─────────────────────────────────────────────────────────────────────────────
# One ingest turn (shared by the HTTP route AND the tests — identical logic)
# ─────────────────────────────────────────────────────────────────────────────
async def ingest_turn(agent, session, message: str, sid: str, on_event=None):
    """Run the site ingest pipeline: fetch -> analyze -> produce SiteSpec.
    Returns (answer, tools_used)."""
    resp = await agent.stream_chat(
        message, on_event=on_event, session_id=sid, token_delay=0.012
    )
    return (resp.content or "").strip(), resp.tool_calls_made


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
PROVIDERS = [
    {
        "id": "anthropic",
        "label": "Anthropic (Claude)",
        "key_env": "ANTHROPIC_API_KEY",
        "needs_key": True,
        "hint": "sk-ant-...",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "key_env": "OPENAI_API_KEY",
        "needs_key": True,
        "hint": "sk-...",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "key_env": "DEEPSEEK_API_KEY",
        "needs_key": True,
        "hint": "sk-...",
    },
    {
        "id": "ollama",
        "label": "Local Ollama (no key)",
        "key_env": "",
        "needs_key": False,
        "hint": "runs offline",
    },
]
_KEY_ENV = {p["id"]: p["key_env"] for p in PROVIDERS}


def create_app():
    bp, _sys_prompt = load_pack()
    state: dict = {"agent": None, "session": None, "provider": None, "model": None, "error": "not_configured"}

    def rebuild() -> bool:
        try:
            a, se, _bp, prov, mdl = build_agent()
            state.update(agent=a, session=se, provider=prov, model=mdl, error=None)
            return True
        except SystemExit:
            state.update(agent=None, error="not_configured")
            return False
        except Exception as exc:  # noqa: BLE001
            log.exception("agent build failed")
            state.update(agent=None, error=str(exc))
            return False

    rebuild()  # works immediately if a key is already in .env / the environment

    app = FastAPI(title="Site Ingest Studio")

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

    @app.get("/")
    async def index():
        return FileResponse(str(ASSETS / "web" / "index.html"))

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "configured": state["agent"] is not None,
            "provider": state["provider"],
            "model": state["model"] or "(default)",
        }

    skills_dir = ASSETS / "pack" / "site-ingest" / "skills"
    pack_skills = (
        sorted(p.stem for p in skills_dir.glob("*.md")) if skills_dir.is_dir() else []
    )

    @app.get("/api/config")
    async def config():
        agent = state["agent"]
        tool_names = (
            sorted(agent._tools._tools)
            if agent
            else (bp.get("tools") or [])
        )
        tools = [t for t in tool_names if not t.startswith("self_")]
        return {
            "labels": bp.get("ui_labels", {}),
            "welcome": bp.get("welcome_message", ""),
            "configured": agent is not None,
            "provider": state["provider"],
            "model": state["model"] or "(provider default)",
            "providers": PROVIDERS,
            "samples": bp.get("sample_prompts", []),
            "pack": bp.get("app_name", "Site Ingest"),
            "tools": tools,
            "skills": pack_skills,
        }

    @app.post("/api/settings")
    async def settings(request: Request):
        body = await request.json()
        prov = (body.get("provider") or "").strip().lower()
        key = (body.get("api_key") or "").strip()
        mdl = (body.get("model") or "").strip()
        if prov not in _KEY_ENV:
            return JSONResponse(
                {"ok": False, "error": "unknown provider"}, status_code=400
            )
        if _KEY_ENV[prov] and not key:
            return JSONResponse(
                {"ok": False, "error": "API key required"}, status_code=400
            )
        updates = {"SITE_INGEST_PROVIDER": prov}
        if mdl:
            updates["SITE_INGEST_MODEL"] = mdl
        if _KEY_ENV[prov]:
            updates[_KEY_ENV[prov]] = key
        write_env(updates)
        ok = rebuild()
        return {
            "ok": ok,
            "configured": state["agent"] is not None,
            "provider": state["provider"],
            "error": state["error"],
        }

    @app.post("/api/ingest")
    async def ingest(request: Request):
        body = await request.json()
        message = (body.get("message") or "").strip()
        sid = body.get("session_id") or "ingest"
        if not message:
            return JSONResponse({"error": "empty message"}, status_code=400)
        agent, session = state["agent"], state["session"]

        async def gen():
            if agent is None:
                yield sse(
                    "error",
                    {
                        "error": "Not configured — add your LLM API key in "
                        "Settings (⚙) to start ingesting."
                    },
                )
                yield sse("complete", {})
                return
            q: asyncio.Queue = asyncio.Queue()
            sentinel = object()

            def on_event(ev):
                try:
                    q.put_nowait(ev)
                except Exception:  # noqa: BLE001
                    pass

            async def run():
                try:
                    content, tools = await ingest_turn(
                        agent, session, message, sid, on_event
                    )
                    q.put_nowait({"type": "_done", "content": content, "tools": tools})
                except Exception as exc:  # noqa: BLE001
                    log.exception("ingest turn failed")
                    q.put_nowait({"type": "error", "error": str(exc)})
                finally:
                    q.put_nowait(sentinel)

            asyncio.create_task(run())
            while True:
                ev = await q.get()
                if ev is sentinel:
                    break
                t = ev.get("type", "event")
                if t == "_done":
                    yield sse(
                        "answer",
                        {"content": ev.get("content", ""), "tools": ev.get("tools", [])},
                    )
                    continue
                yield sse(t, ev)
            yield sse("complete", {})

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def main():
    parser = argparse.ArgumentParser(description="Site Ingest Studio")
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("PORT", "8131"))
    )
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--no-open", action="store_true", help="Don't open a browser")
    args = parser.parse_args()

    load_env()
    app = create_app()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Site Ingest Studio → {url}\n  (Ctrl+C to stop)\n")
    if not args.no_open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
