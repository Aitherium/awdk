"""forkd_client — the M5 Tier-2 fan-out adapter (aithersandbox scale-up).

Forks warm Claude-Code sandbox children from a snapshot (wizzense/forkd —
Firecracker fork-from-warm over KVM), each child KVM-isolated and pinned to a
chosen account's quota via the M1 Tier-1 multi-account layer. The runner's
"spawn a fresh claude -p" becomes "fork a warm aithersandbox child".

forkd needs Linux >= 5.7 + KVM; the owner host is Windows, so LIVE fan-out runs
on a Linux mesh node (OptiPlex / DGX). This module is the CLIENT/adapter over the
forkd daemon REST API + a DEGRADE path — it does NOT implement Firecracker. When
no forkd daemon is reachable (everywhere today), ``fanout`` degrades to bounded
sequential spawns so the fan-out still completes.

The REST contract below is the daemon's OWN router, taken from
``crates/forkd-controller/src/http.rs`` and cross-read against the published
Python SDK (``forkd`` 0.5.3, ``controller.py``) — not from this repo's older
design text, which described routes the daemon never served:

    GET    /healthz                      liveness (auth bypassed)
    GET    /v1/snapshots                 list warm parents
    POST   /v1/sandboxes                 {snapshot_tag, n, per_child_netns, ...}
    GET    /v1/sandboxes/{id}            one sandbox
    DELETE /v1/sandboxes/{id}            terminate it
    POST   /v1/sandboxes/{id}/exec       {args: [...], timeout_secs} -> {stdout,
                                         stderr, exit_code}
    POST   /v1/sandboxes/{id}/branch     pause + snapshot + resume

Base URL ``$FORKD_URL`` or ``http://127.0.0.1:8889``; bearer ``$FORKD_TOKEN``
sent as ``Authorization: Bearer`` when set.

Fail-closed, by the rule that a gate denies on every error path:
  * A child's requested tools must all lie inside the warm parent snapshot's
    scope — a fork can never ESCALATE privilege. The daemon's exec surface takes
    argv, not tool names, so a wider request is DENIED rather than silently
    narrowed: nothing downstream could enforce a narrowing.
  * A child that requested an account which cannot be pinned (throttled/absent)
    is DENIED, not run on an arbitrary account.
  * Secrets never travel in a child/snapshot definition (task + metadata are
    scanned; a match DENIES the child rather than shipping the secret into a VM).
  * Transport is injectable; the default never uses verify=False. An internal-CA
    mesh (https) caller injects an ``http_fn`` that trusts the internal CA.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

_log = logging.getLogger("forkd_client")

DEFAULT_FORKD_URL = "http://127.0.0.1:8889"

# Secret patterns that must never be embedded in a child/snapshot definition.
_SECRET_RES = [
    re.compile(p)
    for p in (
        r"sk-ant-[A-Za-z0-9_\-]{8,}",
        r"sk-[A-Za-z0-9_\-]{16,}",
        r"ghp_[A-Za-z0-9]{16,}",
        r"ghs_[A-Za-z0-9]{16,}",
        r"AKIA[0-9A-Z]{12,}",
        r"pk_live_[A-Za-z0-9]{8,}",
        r"sk_live_[A-Za-z0-9]{8,}",
        r"xox[bp]-[A-Za-z0-9\-]{8,}",
        r"aither_sk_live_[A-Za-z0-9_\-]{8,}",
        r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*\S{8,}",
    )
]


def _has_secret(text: str) -> bool:
    return any(rx.search(text) for rx in _SECRET_RES)


class ChildState(str, Enum):
    FORKED = "forked"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


class ForkdError(Exception):
    """forkd daemon transport/protocol error."""


# Injected transport: (method, url, json) -> (status_code, body dict).
HttpFn = Callable[[str, str, Optional[Dict[str, Any]]], Awaitable[Tuple[int, Dict[str, Any]]]]
# Injected account selector: (candidate_profiles | None) -> chosen profile ('' if none).
AccountSelectFn = Callable[[Optional[List[str]]], str]
# Injected degrade spawner: (ChildSpec, chosen_account) -> ForkResult.
DegradeSpawnFn = Callable[["ChildSpec", str], Awaitable["ForkResult"]]


@dataclass
class ChildSpec:
    task: str
    allowed_tools: List[str]
    account_profile: str = ""  # '' = auto-select via the M1 layer
    # NB: MCP config is NOT a per-child field — all children reuse the WARM
    # PARENT snapshot's live MCP connections (that is the whole point of
    # fork-from-warm), so it lives on ForkdParentSnapshot, not here.
    timeout_sec: int = 300
    metadata: Dict[str, Any] = field(default_factory=dict)
    goal_id: str = ""
    child_id: str = ""

    def __post_init__(self) -> None:
        if not self.child_id:
            self.child_id = f"child_{uuid.uuid4().hex[:10]}"

    def validate(self) -> None:
        if not str(self.task or "").strip():
            raise ForkdError("child task must be non-empty")
        if not self.allowed_tools or not all(
            isinstance(t, str) and t.strip() for t in self.allowed_tools
        ):
            raise ForkdError("child allowed_tools must be a non-empty list")
        # Secrets must not be embedded in the child definition (persisted to a VM).
        if _has_secret(" ".join([self.task, str(self.metadata)])):
            raise ForkdError("child definition contains a secret; refusing")


@dataclass
class ForkdParentSnapshot:
    snapshot_id: str
    allowed_tools: List[str]
    mcp_config: Optional[Dict[str, Any]] = None
    parent_account_profile: str = ""
    ttl_sec: int = 300


@dataclass
class ForkResult:
    child_id: str
    state: str  # ChildState value
    account_profile: str = ""
    exit_code: Optional[int] = None
    result_text: str = ""
    error: str = ""
    session_id: str = ""
    degraded: bool = False

    @property
    def ok(self) -> bool:
        return self.state == ChildState.COMPLETED.value


class ForkdClient:
    """Client + degrade adapter for the forkd fork-from-warm daemon."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        http_fn: Optional[HttpFn] = None,
        account_select: Optional[AccountSelectFn] = None,
        degrade_spawn: Optional[DegradeSpawnFn] = None,
        snapshot_tag: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 30.0,
        degrade_on_error: bool = True,
    ) -> None:
        self.base_url = (base_url or os.environ.get("FORKD_URL") or DEFAULT_FORKD_URL).rstrip("/")
        self._http_fn = http_fn
        self._account_select = account_select
        self._degrade_spawn = degrade_spawn
        # The warm parent a fan-out forks from. There is no REST call that makes a
        # warm parent out of a tool scope: the daemon snapshots a BOOTED VM
        # (``forkd snapshot`` CLI, or POST /v1/snapshots with kernel+rootfs on the
        # daemon host). So a client resolves an existing tag and refuses to invent one.
        self.snapshot_tag = snapshot_tag if snapshot_tag is not None else os.environ.get(
            "FORKD_SNAPSHOT_TAG", "")
        self.token = token if token is not None else os.environ.get("FORKD_TOKEN", "")
        self.timeout = timeout
        self.degrade_on_error = degrade_on_error

    # ── transport ──────────────────────────────────────────────────────────

    async def _http(self, method: str, path: str,
                    body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        if self._http_fn is not None:
            try:
                return await self._http_fn(method, f"{self.base_url}{path}", body)
            except Exception as exc:  # noqa: BLE001 — normalize to ForkdError so the
                # finally-reclaim and gather paths handle it uniformly.
                raise ForkdError(f"forkd transport error: {exc}") from exc
        import httpx

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            # Default transport: public-CA verify (never verify=False). For an
            # internal-CA mesh (https), inject an http_fn that trusts the CA.
            async with httpx.AsyncClient(timeout=self.timeout, verify=True) as c:
                resp = await c.request(method, f"{self.base_url}{path}", json=body,
                                       headers=headers)
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                data = {}
            return resp.status_code, data
        except Exception as exc:  # noqa: BLE001
            raise ForkdError(f"forkd transport error: {exc}") from exc

    async def health_check(self) -> bool:
        try:
            status, _ = await self._http("GET", "/healthz")
            return status == 200
        except ForkdError:
            return False

    async def list_snapshots(self) -> List[Dict[str, Any]]:
        status, data = await self._http("GET", "/v1/snapshots")
        if status != 200:
            raise ForkdError(f"snapshot list failed ({status}): {data}")
        rows = data if isinstance(data, list) else data.get("snapshots", [])
        return [r for r in rows if isinstance(r, dict)]

    async def resolve_snapshot(
        self,
        tag: str,
        *,
        allowed_tools: List[str],
        mcp_config: Optional[Dict[str, Any]] = None,
        account_profile: str = "",
        ttl_sec: int = 300,
    ) -> ForkdParentSnapshot:
        """Bind this client's policy envelope to a warm parent that ALREADY exists.

        Refuses a tag the daemon does not list, and one it lists as not bootable —
        forking from either would fail at spawn with a less useful error.
        """
        rows = await self.list_snapshots()
        match = next((r for r in rows if str(r.get("tag") or r.get("snapshot_tag") or "") == tag),
                     None)
        if match is None:
            known = sorted(str(r.get("tag") or r.get("snapshot_tag") or "?") for r in rows)
            raise ForkdError(f"snapshot {tag!r} not known to the daemon; have {known}")
        if match.get("bootable") is False or str(match.get("status", "")).lower() == "failed":
            raise ForkdError(f"snapshot {tag!r} is not bootable ({match.get('status')})")
        return ForkdParentSnapshot(
            snapshot_id=tag,
            allowed_tools=list(allowed_tools),
            mcp_config=mcp_config,
            parent_account_profile=account_profile,
            ttl_sec=ttl_sec,
        )

    async def spawn_sandboxes(self, snapshot_tag: str, n: int = 1,
                              per_child_netns: bool = True) -> List[Dict[str, Any]]:
        """`POST /v1/sandboxes` — fork n children; returns their SandboxInfo dicts."""
        status, data = await self._http(
            "POST", "/v1/sandboxes",
            {"snapshot_tag": snapshot_tag, "n": n, "per_child_netns": per_child_netns},
        )
        rows = data if isinstance(data, list) else data.get("sandboxes", [])
        if status not in (200, 201) or not rows:
            raise ForkdError(f"sandbox spawn failed ({status}): {data}")
        return [r for r in rows if isinstance(r, dict)]

    async def exec_command(self, sandbox_id: str, args: List[str],
                           timeout_secs: int = 30) -> Dict[str, Any]:
        """`POST /v1/sandboxes/{id}/exec` — `{stdout, stderr, exit_code}`."""
        status, data = await self._http(
            "POST", f"/v1/sandboxes/{sandbox_id}/exec",
            {"args": list(args), "timeout_secs": int(timeout_secs)},
        )
        if status != 200:
            raise ForkdError(f"exec failed ({status}): {data}")
        return data

    async def kill_sandbox(self, sandbox_id: str) -> None:
        """`DELETE /v1/sandboxes/{id}` — terminate one sandbox."""
        await self._http("DELETE", f"/v1/sandboxes/{sandbox_id}")

    # ── account + scope helpers (fail-closed) ──────────────────────────────

    def _select_account(self, requested: str) -> str:
        """Pin an account via the M1 selector. A REQUESTED account is validated
        (passed as the sole candidate) so a throttled/absent request returns ''
        (=> the caller denies it), never silently runs on another account."""
        if self._account_select is not None:
            try:
                return self._account_select([requested] if requested else None) or ""
            except Exception as exc:  # noqa: BLE001 — no account => deny (fail closed)
                _log.warning("account select failed: %s", exc)
                return ""
        return requested  # no selector wired -> honor requested (or '' = default login)

    @staticmethod
    def _intersect_tools(child: List[str], parent: List[str]) -> List[str]:
        """Child scope = child ∩ parent — a fork can never ESCALATE privilege."""
        pset = set(parent)
        return [t for t in child if t in pset]

    # ── fan-out ────────────────────────────────────────────────────────────

    async def fanout(
        self,
        children: List[ChildSpec],
        *,
        snapshot: Optional[ForkdParentSnapshot] = None,
        parallel_limit: int = 10,
    ) -> List[ForkResult]:
        """Fork + exec each child from the warm snapshot; degrade if forkd is down."""
        if not children:
            return []
        for c in children:
            c.validate()

        # Parent envelope — the tool ceiling children may NOT exceed (fork or
        # degrade). An explicit snapshot's scope, else the union of children
        # (self-bounding: a child can never exceed its own request either way).
        envelope = (
            list(snapshot.allowed_tools)
            if snapshot is not None
            else sorted({t for c in children for t in c.allowed_tools})
        )

        def _degraded_or_failed(reason: str) -> List[ForkResult]:
            if not self.degrade_on_error:
                return [
                    ForkResult(child_id=c.child_id, state=ChildState.FAILED.value, error=reason)
                    for c in children
                ]
            _log.info("%s — degrading to sequential spawns (%d children)", reason, len(children))
            return None  # signal: proceed to degrade

        alive = await self.health_check()
        if not alive:
            failed = _degraded_or_failed("forkd daemon unavailable")
            return failed if failed is not None else await self._degrade_all(children, envelope)

        if snapshot is None:
            if not self.snapshot_tag:
                failed = _degraded_or_failed(
                    "no warm snapshot tag configured ($FORKD_SNAPSHOT_TAG or snapshot_tag=)")
                return failed if failed is not None else await self._degrade_all(children, envelope)
            try:
                snapshot = await self.resolve_snapshot(
                    self.snapshot_tag, allowed_tools=envelope)
            except ForkdError as exc:
                # Post-health transport failure must degrade too (not raise).
                failed = _degraded_or_failed(f"snapshot resolve failed: {exc}")
                return failed if failed is not None else await self._degrade_all(children, envelope)

        sem = asyncio.Semaphore(max(1, int(parallel_limit)))

        async def _one(child: ChildSpec) -> ForkResult:
            async with sem:
                return await self._fork_one(child, snapshot)

        results = await asyncio.gather(*[_one(c) for c in children], return_exceptions=True)
        out: List[ForkResult] = []
        for child, r in zip(children, results):
            if isinstance(r, ForkResult):
                out.append(r)
            else:
                out.append(ForkResult(child_id=child.child_id, state=ChildState.FAILED.value,
                                      error=str(r)))
        return out

    async def _fork_one(self, child: ChildSpec, snapshot: ForkdParentSnapshot) -> ForkResult:
        # The daemon execs argv, not a named tool set, so scope is enforced as a
        # CEILING: a child asking for anything the parent scope lacks is denied
        # outright. Narrowing it silently would leave a promise nothing enforces.
        outside = sorted(set(child.allowed_tools) - set(snapshot.allowed_tools))
        if outside:
            return ForkResult(child_id=child.child_id, state=ChildState.DENIED.value,
                              error="requests tools outside the parent scope: "
                                    + ", ".join(outside))
        account = self._select_account(child.account_profile)
        if not account and child.account_profile:
            return ForkResult(child_id=child.child_id, state=ChildState.DENIED.value,
                              error="requested account unavailable/throttled")
        sandbox_id = ""
        try:
            [sbx] = await self.spawn_sandboxes(snapshot.snapshot_id, n=1)
            sandbox_id = str(sbx.get("id") or "")
            if not sandbox_id:
                return ForkResult(child_id=child.child_id, state=ChildState.FAILED.value,
                                  account_profile=account,
                                  error=f"spawn returned no id: {sbx}")
            ex = await self.exec_command(sandbox_id, ["sh", "-lc", child.task],
                                         timeout_secs=child.timeout_sec)
            ec = ex.get("exit_code", 1)
            ec = 1 if ec is None else int(ec)  # 0 is a VALID success — don't `or 1` it
            done = ec == 0
            return ForkResult(
                child_id=child.child_id,
                state=ChildState.COMPLETED.value if done else ChildState.FAILED.value,
                account_profile=account,
                exit_code=ex.get("exit_code"),
                result_text=str(ex.get("stdout") or ""),
                error="" if done else str(ex.get("stderr") or "exec failed"),
                session_id=sandbox_id,
            )
        except ForkdError as exc:
            return ForkResult(child_id=child.child_id, state=ChildState.FAILED.value,
                              account_profile=account, error=str(exc))
        finally:
            # Reclaim the VM (best-effort; never fail the result on reclaim error).
            if sandbox_id:
                try:
                    await self.kill_sandbox(sandbox_id)
                except ForkdError as exc:
                    # The result is already decided; a leaked VM must still be visible.
                    _log.warning("reclaim of sandbox %s failed: %s", sandbox_id, exc)

    # ── degrade path (forkd absent -> normal spawns) ───────────────────────

    async def _degrade_all(
        self, children: List[ChildSpec], envelope: List[str]
    ) -> List[ForkResult]:
        """Sequential fallback. Still fail-closed: intersect each child's tools
        against the parent envelope (no escalation) and deny throttled accounts —
        the degrade path must not be a hole in the fork path's guarantees."""
        results: List[ForkResult] = []
        for child in children:  # bounded (sequential) — no microVM parallelism here
            account = self._select_account(child.account_profile)
            if not account and child.account_profile:
                results.append(ForkResult(
                    child_id=child.child_id, state=ChildState.DENIED.value,
                    degraded=True, error="requested account unavailable/throttled"))
                continue
            tools = self._intersect_tools(child.allowed_tools, envelope)
            if not tools:
                results.append(ForkResult(
                    child_id=child.child_id, state=ChildState.DENIED.value,
                    account_profile=account, degraded=True,
                    error="empty tool scope after parent intersection"))
                continue
            if self._degrade_spawn is None:
                results.append(ForkResult(
                    child_id=child.child_id, state=ChildState.FAILED.value,
                    account_profile=account, degraded=True,
                    error="forkd down and no degrade spawner wired",
                ))
                continue
            bounded = replace(child, allowed_tools=tools)  # intersected, no escalation
            try:
                r = await self._degrade_spawn(bounded, account)
                r.degraded = True
                results.append(r)
            except Exception as exc:  # noqa: BLE001
                results.append(ForkResult(
                    child_id=child.child_id, state=ChildState.FAILED.value,
                    account_profile=account, degraded=True, error=str(exc),
                ))
        return results
