"""``adk handoff`` — the host half of the v0.4 HANDOFF lifecycle.

The fleet half (v0.4a) turned the sticky note into a task: pending →
picked_up (first ack wins, the ack is STAMPED onto the blob) → done. The
MCP tools live on the gateway (pickup_handoff / resolve_handoff /
list_pending_handoffs, in mcp_context.py); the LEASE half must run on the
HOST, because a fleet container cannot see ``~/.aither/awgit``, and this
host's commits are lease-gated by the vcs pre-commit hook. So:

  list  — pending handoffs via the gateway.
  pick  — pickup_handoff (first ack wins) → for every file_to_lease the
          host acquires an awgit lease (subprocess to the real awgit CLI —
          same binary, same store the commit gate reads). A pickup someone
          else already owns is reported, never leased.
  done  — resolve_handoff marks the task finished.

``--dry-run`` on pick reports the lease plan without acquiring anything.

Lease spelling: the commit gate's expected path spelling has flipped between
AwNode/awnode on this case-insensitive FS (measured 2026-08-25), so
``lease_path_variants`` leases BOTH spellings of any awnode segment — cheap,
always passes. Gateway transport: host loopback ``http://127.0.0.1:8182``
(never ``localhost`` — the ::1 refusal tax) with the session bearer from
``~/.aither/session-bearer``, via adk.client._gateway_mcp.GatewayMCPClient.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

GATEWAY_URL = "http://127.0.0.1:8182"
BEARER_FILE = Path.home() / ".aither" / "session-bearer"
LEASE_TTL = 900
_AGENT_DEFAULT = "claude"


# ── pure plan logic (unit-tested) ───────────────────────────────────────────

def parse_tool_json(text: str) -> dict:
    """The handoff tools return JSON strings; parse or name the failure."""
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"error": "non-object JSON"}
    except ValueError as exc:
        return {"error": f"tool returned non-JSON: {exc}"}


def lease_path_variants(paths: list[str]) -> list[str]:
    """Lease-safe spellings: any awnode segment also gets its case sibling.

    The vcs lease gate's expected spelling of the AwNode/awnode tree has
    flipped between runs (case-insensitive FS, measured 2026-08-25); a lease
    held under one spelling was rejected under the other. Leasing both is
    cheap and always passes.
    """
    variants: list[str] = []
    for path in paths:
        norm = path.replace("\\", "/")
        variants.append(norm)
        segs = norm.split("/")
        for i, seg in enumerate(segs):
            if seg.lower() == "awnode" and seg != "awnode":
                sibling = "awnode"
            elif seg == "awnode":
                sibling = "AwNode"
            else:
                continue
            alt = "/".join(segs[:i] + [sibling] + segs[i + 1:])
            if alt != norm:
                variants.append(alt)
    return variants


def plan_leases(pickup: dict, our_agent: str) -> tuple[list[str], list[str]]:
    """(files to lease, notes) from a pickup_handoff response.

    Leasing follows OWNERSHIP, never the mere presence of files: a handoff
    someone else's ack already won, or one already done, must not hand this
    session leases it cannot back up.
    """
    notes: list[str] = []
    if not pickup.get("has_handoff"):
        notes.append("no pending handoff found — nothing to lease")
        return [], notes
    status = pickup.get("status", "")
    owner = str(pickup.get("picked_up_by") or "").strip()
    if status == "done":
        notes.append("handoff already resolved — nothing to lease")
        return [], notes
    if status == "picked_up" and owner and owner != our_agent:
        notes.append(f"already picked up by {owner} — no leases taken")
        return [], notes
    files = pickup.get("files_to_lease") or []
    if not files:
        notes.append("handoff carries no files — no leases needed")
        return [], notes
    return list(files), notes


def lease_argv(files: list[str], ttl: int = LEASE_TTL) -> list[str]:
    """The exact lease command the host runs for a file set."""
    return ["awgit", "lease", "acquire", "--ttl", str(ttl), *files]


# ── gateway I/O ─────────────────────────────────────────────────────────────

def _bearer() -> str:
    if not BEARER_FILE.is_file():
        raise RuntimeError(
            f"no session bearer at {BEARER_FILE} — mint one with "
            "AitherOS/dev/tools/mint_session_bearer.py")
    return BEARER_FILE.read_text(encoding="utf-8").strip()


def _call_tool(name: str, **arguments: Any) -> dict:
    """One tools/call through the gateway; every failure is a dict, not a raise."""
    try:
        from adk.client._gateway_mcp import GatewayMCPClient
    except ImportError as exc:
        return {"error": "import_failed", "message": str(exc)}
    try:
        bearer = _bearer()  # sync disk read stays in sync context
    except RuntimeError as exc:
        return {"error": "no_bearer", "message": str(exc)}

    async def _go() -> dict:
        client = GatewayMCPClient(gateway_url=GATEWAY_URL, api_key=bearer)
        # ping(), NOT connect(): connect only GETs /health, which this
        # gateway answers 200 while /mcp is unresponsive — ping
        # performs the real initialize handshake the tools/call needs.
        if not await client.ping():
            return {"error": "gateway_unreachable",
                    "message": f"could not reach {GATEWAY_URL}/mcp"}
        result = await client.call_tool(name, arguments)
        if result.get("error"):
            return result
        return parse_tool_json(result.get("text", ""))

    try:
        return asyncio.run(_go())
    except RuntimeError as exc:
        return {"error": "call_failed", "message": str(exc)}


def list_handoffs(target_agent: str = "") -> dict:
    return _call_tool("list_pending_handoffs", target_agent=target_agent)


def pick_handoff(handoff_id: str, target_agent: str) -> dict:
    return _call_tool("pickup_handoff", target_agent=target_agent,
                      handoff_id=handoff_id)


def resolve_handoff(handoff_id: str, target_agent: str) -> dict:
    return _call_tool("resolve_handoff", handoff_id=handoff_id,
                      target_agent=target_agent)


def _acquire_leases(files: list[str]) -> tuple[list[str], str]:
    """Run the real awgit lease acquire; report, never guess."""
    if not files:
        return [], ""
    argv = lease_argv(lease_path_variants(files))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"lease acquire failed: {exc}"
    if proc.returncode != 0:
        return [], f"lease acquire exited {proc.returncode}: " \
                   f"{(proc.stderr or proc.stdout).strip()[:200]}"
    return lease_path_variants(files), proc.stdout.strip()


# ── the command ─────────────────────────────────────────────────────────────

def cmd_handoff(args: Any) -> int:
    agent = getattr(args, "agent", "") or _AGENT_DEFAULT
    sub = getattr(args, "handoff_sub", "list")

    if sub == "list":
        data = list_handoffs(getattr(args, "target_agent", ""))
        if data.get("error"):
            print(f"handoff list failed: {data.get('error')}: "
                  f"{data.get('message', '')}".rstrip(": "))
            return 1
        handoffs = data.get("handoffs", [])
        if not handoffs:
            print("no pending handoffs")
            return 0
        for h in handoffs:
            if not isinstance(h, dict):
                continue
            status = h.get("status", "pending")
            stale = " (STALE)" if h.get("stale") else ""
            print(f"{h.get('handoff_id')}  {status}{stale}  → "
                  f"{h.get('target_agent', '?')}  {h.get('summary', '')[:80]}")
        return 0

    if sub == "pick":
        handoff_id = getattr(args, "handoff_id", "")
        if not handoff_id:
            print("pick requires a handoff id — `adk handoff list` shows them")
            return 2
        pickup = pick_handoff(handoff_id, agent)
        if pickup.get("error"):
            print(f"pickup failed: {pickup.get('error')}: "
                  f"{pickup.get('message', '')}".rstrip(": "))
            return 1
        files, notes = plan_leases(pickup, agent)
        for note in notes:
            print(f"  {note}")
        if not files:
            if pickup.get("has_handoff"):
                print(f"handoff {handoff_id}: {pickup.get('status')} "
                      f"(ack_written={pickup.get('ack_written')})")
            return 0 if pickup.get("has_handoff") else 1
        if getattr(args, "dry_run", False):
            print(f"dry-run — would lease {len(files)} file(s):")
            for f in lease_path_variants(files):
                print(f"    {f}")
            return 0
        acquired, detail = _acquire_leases(files)
        if detail:
            print(f"  lease failed: {detail}")
            return 1
        print(f"handoff {handoff_id} picked up ({pickup.get('status')}, "
              f"ack_written={pickup.get('ack_written')})")
        print(f"leased {len(acquired)} path(s), TTL {LEASE_TTL}s: "
              f"{' '.join(acquired)}")
        return 0

    if sub == "done":
        handoff_id = getattr(args, "handoff_id", "")
        if not handoff_id:
            print("done requires a handoff id — `adk handoff list` shows them")
            return 2
        done = resolve_handoff(handoff_id, agent)
        if done.get("success"):
            print(f"handoff {handoff_id} resolved ({done.get('status')})")
            return 0
        print(f"resolve failed: {done.get('error', 'unknown')}")
        return 1

    print(f"unknown handoff subcommand: {sub}")
    return 2
