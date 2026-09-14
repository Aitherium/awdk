"""LivenessProbe — a single-budget, non-hanging capability probe.

Phase 0. Everything runs inside ONE ~1.5s wall-clock budget: per-target work is
wrapped in ``asyncio.wait_for(..., ~1.2s)`` and fanned out with ``asyncio.gather``.
EVERY probe swallows its exceptions into a status enum and NEVER raises out, so a
dead DGX / unreachable gateway produces an honest UNREACHABLE/MISSING row instead
of a hang or a traceback.

Endpoints come ONLY from the router's already-resolved backends and the
documented env vars (AITHER_STRUCTURED_ML_URL, AITHER_GENESIS_URL, AITHER_MCP_URL).
No new endpoint table, no capability_domains.yaml, no port re-scanning of our own.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import time

from .report import (
    CapabilityReport,
    SlotHealth,
    STATUS_OK,
    STATUS_MISSING,
    STATUS_UNREACHABLE,
    STATUS_AUTH,
    STATUS_TIER_DENIED,
    STATUS_UNSUPPORTED,
    SOURCE_LOCAL,
    SOURCE_HOSTED,
    SOURCE_OFFLINE,
    SOURCE_NONE,
)

# Overall wall-clock budget and the per-target timeout inside it.
_TOTAL_BUDGET_S = 1.5
_PER_TARGET_S = 1.2

# Hosts we treat as "local" network inference (LAN / loopback), everything else
# reachable over the public internet is "hosted".
_LOCAL_HINTS = ("localhost", "127.0.0.1", "0.0.0.0", "192.168.", "10.", "::1", "host.docker.internal")


def _source_for_url(url: str) -> str:
    if not url:
        return SOURCE_NONE
    u = url.lower()
    if any(h in u for h in _LOCAL_HINTS):
        return SOURCE_LOCAL
    return SOURCE_HOSTED


def _provider_bits(provider) -> tuple[str, str, str]:
    """Best-effort (base_url, default_model, class-name) for a resolved provider."""
    base = getattr(provider, "base_url", "") or getattr(provider, "host", "") or ""
    model = getattr(provider, "default_model", "") or getattr(provider, "_model", "") or ""
    cls = type(provider).__name__
    return base, model, cls


class LivenessProbe:
    """Probe the capabilities an agent can actually reach, fast and honestly."""

    def __init__(self, per_target_s: float = _PER_TARGET_S, total_budget_s: float = _TOTAL_BUDGET_S):
        self.per_target_s = per_target_s
        self.total_budget_s = total_budget_s

    async def run(self, agent) -> CapabilityReport:
        report = CapabilityReport()
        report.machine = _machine_info()
        report.entitlements = _entitlements()

        # Resolve the router's provider OBJECTS (references only, no network)
        # WITHOUT re-scanning ports ourselves. The health_check network calls
        # happen inside the budgeted tasks below, never here.
        router = getattr(agent, "llm", None)
        primary_provider = getattr(router, "_provider", None) if router else None
        if primary_provider is None and router is not None:
            # Not resolved yet — let the router auto-detect, but keep it bounded
            # (uncancellable DNS is handled by the outer asyncio.wait budget).
            try:
                primary_provider = await asyncio.wait_for(
                    router.get_provider(), timeout=self.per_target_s
                )
            except Exception:
                primary_provider = getattr(router, "_provider", None)
        reasoning_provider = (
            getattr(router, "_reasoning_provider", None) if router else None
        ) or primary_provider
        collapsed = (
            primary_provider is not None and primary_provider is reasoning_provider
        )

        # Build the probe tasks, tagged by slot so a task that overruns the
        # SHARED wall-clock budget can be synthesized as UNREACHABLE without
        # awaiting its (possibly uncancellable) DNS/connect thread.
        specs = {
            "primary": self._probe_llm_slot("primary", primary_provider),
            "reasoning": self._probe_llm_slot("reasoning", reasoning_provider),
            "embeddings": self._probe_embeddings(),
            "mcp": self._probe_mcp(),
            "ml_teach": self._probe_ml_teach(),
            "voice": self._probe_voice(agent),
            "vision": self._probe_vision(primary_provider),
        }
        tasks = {asyncio.ensure_future(coro): slot for slot, coro in specs.items()}

        done, pending = await asyncio.wait(
            list(tasks.keys()), timeout=self.total_budget_s
        )
        for t in done:
            slot = tasks[t]
            try:
                res = t.result()
            except Exception:  # noqa: BLE001 - a probe should never raise, but be safe
                res = SlotHealth(slot=slot, status=STATUS_UNREACHABLE, note="probe error")
            if isinstance(res, SlotHealth):
                report.slots[res.slot] = res
        for t in pending:
            slot = tasks[t]
            t.cancel()  # fire-and-forget; do NOT await (DNS thread may be stuck)
            report.slots[slot] = SlotHealth(
                slot=slot,
                status=STATUS_UNREACHABLE,
                latency_ms=int(self.total_budget_s * 1000),
                note="exceeded shared preflight budget",
            )

        p = report.slots.get("primary")
        r = report.slots.get("reasoning")
        if p and r and collapsed:
            extra = "identical to primary (collapsed to one backend)"
            r.note = (r.note + "; " + extra) if r.note else extra

        # offline iff no primary inference slot is OK.
        report.offline = not (p is not None and p.status == STATUS_OK)

        # degraded = OK slots not on their preferred source.
        report.degraded = _compute_degraded(report.slots)
        return report

    # ── individual probes (each swallows all exceptions) ──────────────────

    async def _probe_llm_slot(self, slot: str, provider) -> SlotHealth:
        sh = SlotHealth(slot=slot)
        if provider is None:
            sh.status = STATUS_MISSING
            sh.note = "no backend resolved by the router"
            return sh
        base, model, cls = _provider_bits(provider)
        sh.provider = cls
        sh.model = model
        sh.base_url = base
        sh.source = _source_for_url(base)
        t0 = time.monotonic()
        try:
            healthy = False
            hc = getattr(provider, "health_check", None)
            if hc is not None:
                healthy = await asyncio.wait_for(hc(), timeout=self.per_target_s)
            else:
                healthy = True  # provider has no probe; assume configured
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            if not healthy:
                sh.status = STATUS_UNREACHABLE
                sh.note = "health_check failed within budget"
                return sh
            # Reachable — try to name a real model (best-effort, still bounded).
            try:
                lm = getattr(provider, "list_models", None)
                if lm is not None:
                    models = await asyncio.wait_for(lm(), timeout=self.per_target_s)
                    if models:
                        if not sh.model or sh.model not in models:
                            sh.model = models[0]
            except Exception:
                pass
            sh.status = STATUS_OK
            if sh.source == SOURCE_HOSTED:
                sh.note = "reachable; spend/tier unverified until first call"
        except asyncio.TimeoutError:
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            sh.status = STATUS_UNREACHABLE
            sh.note = "timed out within probe budget"
        except Exception as e:  # noqa: BLE001 - never raise out of a probe
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            sh.status = STATUS_UNREACHABLE
            sh.note = f"{type(e).__name__}"
        return sh

    async def _probe_embeddings(self) -> SlotHealth:
        sh = SlotHealth(slot="embeddings")
        t0 = time.monotonic()
        try:
            from adk.embeddings import get_default_embedder, get_provider

            embedder = get_default_embedder()
            # One call resolves + reports the winner; do not re-resolve.
            await asyncio.wait_for(embedder("preflight"), timeout=self.per_target_s)
            desc = get_provider().describe()
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            backend = desc.get("backend", "unresolved")
            sh.provider = backend
            sh.model = desc.get("model", "")
            sh.base_url = desc.get("url", "")
            degraded = bool(desc.get("degraded"))
            if backend in ("vllm", "ollama"):
                sh.source = _source_for_url(sh.base_url) or SOURCE_LOCAL
                sh.source = sh.source if sh.source != SOURCE_NONE else SOURCE_LOCAL
            elif backend == "gateway":
                sh.source = SOURCE_HOSTED
            else:  # cpu / hash / unresolved
                sh.source = SOURCE_OFFLINE
            if backend in ("unresolved", "hash"):
                sh.status = STATUS_MISSING
                sh.note = "no real embeddings backend (degraded)"
            else:
                sh.status = STATUS_OK
                dim = desc.get("dim", 0)
                sh.note = f"dim={dim}" + ("; DEGRADED" if degraded else "")
                if sh.source == SOURCE_HOSTED:
                    sh.note += "; reachable; spend/tier unverified until first call"
        except asyncio.TimeoutError:
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            sh.status = STATUS_UNREACHABLE
            sh.note = "embed probe timed out within budget"
        except Exception as e:  # noqa: BLE001
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            sh.status = STATUS_MISSING
            sh.note = f"{type(e).__name__}"
        return sh

    async def _probe_mcp(self) -> SlotHealth:
        sh = SlotHealth(slot="mcp")
        t0 = time.monotonic()
        try:
            from adk.mcp import MCPBridge, MCPAuthError, MCPBalanceError

            bridge = MCPBridge()  # reads AITHER_MCP_URL / default gateway
            sh.base_url = getattr(bridge, "mcp_url", "")
            sh.source = _source_for_url(sh.base_url) or SOURCE_HOSTED
            sh.provider = "mcp-gateway"
            try:
                tools = await asyncio.wait_for(
                    bridge.list_tools(), timeout=self.per_target_s
                )
                sh.latency_ms = int((time.monotonic() - t0) * 1000)
                sh.status = STATUS_OK
                note = f"{len(tools)} tools"
                tier = (_entitlements() or {}).get("tier")
                if tier and tier != "unknown":
                    note += f"; tier={tier}"
                if sh.source == SOURCE_HOSTED:
                    note += "; reachable; spend/tier unverified until first call"
                sh.note = note
            except MCPAuthError:
                sh.latency_ms = int((time.monotonic() - t0) * 1000)
                sh.status = STATUS_AUTH
                sh.note = "MCPAuthError - authentication required"
            except MCPBalanceError:
                sh.latency_ms = int((time.monotonic() - t0) * 1000)
                sh.status = STATUS_TIER_DENIED
                sh.note = "MCPBalanceError - insufficient balance/tier"
        except asyncio.TimeoutError:
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            sh.status = STATUS_UNREACHABLE
            sh.note = "list_tools timed out within budget"
        except Exception as e:  # noqa: BLE001
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            sh.status = STATUS_UNREACHABLE
            sh.note = f"{type(e).__name__}"
        return sh

    async def _probe_ml_teach(self) -> SlotHealth:
        """OPTIONAL. Probes structuredml + genesis /ml health endpoints."""
        sh = SlotHealth(slot="ml_teach")
        sml = os.getenv("AITHER_STRUCTURED_ML_URL", "http://localhost:8192").rstrip("/")
        gen = os.getenv("AITHER_GENESIS_URL", "http://localhost:8001").rstrip("/")
        targets = [f"{sml}/health", f"{gen}/ml/health"]
        sh.base_url = sml
        sh.source = _source_for_url(sml) or SOURCE_LOCAL
        t0 = time.monotonic()
        try:
            import httpx

            async def _get(url: str) -> bool:
                try:
                    async with httpx.AsyncClient(timeout=self.per_target_s) as c:
                        r = await c.get(url)
                        return r.status_code < 500
                except Exception:
                    return False

            oks = await asyncio.wait_for(
                asyncio.gather(*[_get(u) for u in targets]),
                timeout=self.per_target_s,
            )
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            reached = [t for t, ok in zip(targets, oks) if ok]
            if reached:
                sh.status = STATUS_OK
                sh.provider = "ml-teach"
                sh.note = "reached: " + ", ".join(reached)
            else:
                sh.status = STATUS_MISSING
                sh.note = "no ml-teach endpoint answered (optional)"
        except asyncio.TimeoutError:
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            sh.status = STATUS_MISSING
            sh.note = "ml-teach probe timed out (optional)"
        except Exception as e:  # noqa: BLE001
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            sh.status = STATUS_MISSING
            sh.note = f"{type(e).__name__} (optional)"
        return sh

    async def _probe_voice(self, agent) -> SlotHealth:
        """OPTIONAL. MISSING unless a voice backend resolves. Phase 0 has no
        voice send path wired, so this reports MISSING honestly."""
        sh = SlotHealth(slot="voice", status=STATUS_MISSING, source=SOURCE_NONE)
        try:
            url = os.getenv("AITHER_VOICE_URL", "").strip()
            if url:
                import httpx

                async def _get() -> bool:
                    try:
                        async with httpx.AsyncClient(timeout=self.per_target_s) as c:
                            r = await c.get(url.rstrip("/") + "/health")
                            return r.status_code < 500
                    except Exception:
                        return False

                ok = await asyncio.wait_for(_get(), timeout=self.per_target_s)
                if ok:
                    sh.status = STATUS_OK
                    sh.provider = "voice"
                    sh.base_url = url
                    sh.source = _source_for_url(url)
                    sh.note = "voice endpoint reachable"
                    return sh
            sh.note = "no voice backend resolved (optional)"
        except Exception as e:  # noqa: BLE001
            sh.status = STATUS_MISSING
            sh.note = f"{type(e).__name__} (optional)"
        return sh

    # A DISCRIMINABLE probe: a solid red 8x8 PNG. The probe asks the model to
    # name the color and only passes on "red" — a 1x1 pixel + "reply ok" would
    # false-OK a blind/text-only model that just echoes the word. This is the
    # honest test that the model can actually SEE the image.
    _PROBE_IMG = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSnc"
        "AAAAEUlEQVR42mO4IyKCFTEMLQkAmD9BAeEqE6gAAAAASUVORK5CYII="
    )

    async def _probe_vision(self, provider) -> SlotHealth:
        """Really probe vision: send a red-square image and require the model to
        NAME the color. adk now has a multimodal send path (adk.llm.multimodal),
        so the old unconditional UNSUPPORTED is gone. Only the OpenAI-compatible
        family (gateway / vLLM / OpenAI) forwards image content parts, so a
        non-openai-compat or missing provider is UNSUPPORTED, not a false OK."""
        sh = SlotHealth(slot="vision")
        if provider is None or not hasattr(provider, "chat") or not hasattr(provider, "base_url"):
            sh.status = STATUS_UNSUPPORTED
            sh.source = SOURCE_NONE
            sh.note = "no openai-compatible provider resolved for image input"
            return sh
        base = getattr(provider, "base_url", "") or ""
        sh.base_url = base
        sh.source = _source_for_url(base)
        # Vision model: explicit env, else the gateway's logical vision model, else
        # the provider default (a local vLLM would serve e.g. gemma4-12b).
        vmodel = os.environ.get("AITHER_VISION_MODEL", "").strip()
        if not vmodel:
            vmodel = "aither-vision" if "aitherium" in base else getattr(provider, "default_model", "")
        sh.model = vmodel
        t0 = time.monotonic()
        try:
            from adk.llm.multimodal import image_message

            msg = image_message(
                "What color is the shape in this image? Answer with one word.",
                self._PROBE_IMG,
            )
            resp = await asyncio.wait_for(
                provider.chat([msg], model=vmodel, max_tokens=8, temperature=0.0),
                timeout=self.per_target_s,
            )
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            answer = (getattr(resp, "content", None) or "").lower() if resp else ""
            if "red" in answer:
                sh.status = STATUS_OK
                sh.note = "vision round-trip OK (named the color)"
                if sh.source == SOURCE_HOSTED:
                    sh.note += "; spend/tier unverified until first call"
            elif answer:
                # Responded but did not see the image — blind/text-only model.
                sh.status = STATUS_UNSUPPORTED
                sh.note = f"responded but did not identify the image (got {answer[:16]!r})"
            else:
                sh.status = STATUS_UNREACHABLE
                sh.note = "empty response to probe image"
        except asyncio.TimeoutError:
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            sh.status = STATUS_UNREACHABLE
            sh.note = "timed out within probe budget"
        except Exception as e:  # noqa: BLE001 - never raise out of a probe
            sh.latency_ms = int((time.monotonic() - t0) * 1000)
            msg = str(e).lower()
            if any(x in msg for x in ("401", "403", "unauthor", "forbidden", "invalid api key")):
                sh.status = STATUS_AUTH
                sh.note = "auth/entitlement rejected the vision call"
            else:
                sh.status = STATUS_UNREACHABLE
                sh.note = f"{type(e).__name__}"
        return sh


# ── module helpers ───────────────────────────────────────────────────────

# Preferred source per slot (for the degraded calculation).
_PREFERRED_SOURCE = {
    "primary": SOURCE_LOCAL,
    "reasoning": SOURCE_LOCAL,
    "embeddings": SOURCE_LOCAL,
    "mcp": SOURCE_HOSTED,
    "ml_teach": SOURCE_LOCAL,
    "voice": SOURCE_LOCAL,
}


def _compute_degraded(slots: dict) -> list:
    out: list = []
    for name, sh in slots.items():
        if sh.status != STATUS_OK:
            continue
        pref = _PREFERRED_SOURCE.get(name)
        if pref and sh.source and sh.source != pref:
            out.append(name)
    return out


def _entitlements() -> dict:
    """resolve_os_license() if importable, else {"tier": "unknown"}."""
    try:
        from lib.licensing.entitlements import resolve_os_license  # type: ignore

        envelope = os.getenv("AITHER_LICENSE_KEY", "")
        if not envelope:
            return {"tier": "unknown"}
        ok, payload = resolve_os_license(envelope)
        if ok and isinstance(payload, dict):
            ent = dict(payload)
            ent.setdefault("tier", payload.get("tier", "unknown"))
            return ent
        return {"tier": "unknown"}
    except Exception:
        return {"tier": "unknown"}


def _machine_info() -> dict:
    """Best-effort OS/RAM/GPU + ollama presence. Never blocks; degrades to a
    minimal platform.* dict if the richer probes are unavailable."""
    info: dict = {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu": platform.processor() or "unknown",
    }
    # RAM (best-effort).
    try:
        import psutil  # type: ignore

        info["ram_gb"] = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        try:
            pages = os.sysconf("SC_PHYS_PAGES")  # type: ignore[attr-defined]
            page = os.sysconf("SC_PAGE_SIZE")    # type: ignore[attr-defined]
            info["ram_gb"] = round(pages * page / (1024 ** 3), 1)
        except Exception:
            pass
    # GPU — NON-BLOCKING best-effort: check for a driver binary rather than
    # SHELLING OUT to nvidia-smi (a 5s subprocess that would blow the budget).
    if shutil.which("nvidia-smi"):
        info["gpu"] = "nvidia (driver present)"
    elif shutil.which("rocminfo"):
        info["gpu"] = "amd (driver present)"
    elif platform.system() == "Darwin":
        info["gpu"] = "apple"
    else:
        info["gpu"] = "unknown"
    # Ollama present?
    info["ollama"] = "yes" if shutil.which("ollama") else "no"
    return info
