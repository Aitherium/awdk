"""adk volunteer — volunteer embedding compute commands.

Volunteer CLI for DGG community embedding compute:
- enroll: mesh onboard + consent + trust request
- serve: download model + start llama-server
- start: claim batches, embed, submit results
- status: show reputation, tokens, batches
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("adk.volunteer")

# Constants from the plan
EMBED_MODEL_URL = "https://artifact.aitherium.com/aither-code-embed-v1/aither-code-embed.q8_0.gguf"
EMBED_MODEL_BYTE_COUNT = 639145920  # Must match exactly
EMBED_LISTEN_HOST = "127.0.0.1"
EMBED_LISTEN_PORT = 8229
HEARTBEAT_INTERVAL = 300  # 5 minutes
HEARTBEAT_TTL = 1800  # 30 minutes


def _get_auth_token() -> str | None:
    """Resolve auth token from environment or config."""
    token = os.getenv("AITHER_AUTH_TOKEN", "").strip()
    if token:
        return token

    try:
        config_file = Path.home() / ".aither" / "config.json"
        if config_file.exists():
            config = json.loads(config_file.read_text(encoding="utf-8"))
            return config.get("auth_token", "").strip() or None
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read auth config: %s", e)

    return None


async def _resolve_peer_id() -> str:
    """Get the peer_id from node's onboarded state."""
    peer_id = os.getenv("AITHER_PEER_ID", "").strip()
    if peer_id:
        return peer_id

    try:
        node_auth_file = Path.home() / ".aither" / "node_auth.json"
        if node_auth_file.exists():
            auth_data = json.loads(node_auth_file.read_text(encoding="utf-8"))
            peer_id = auth_data.get("peer_id", "").strip()
            if peer_id:
                return peer_id
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read peer_id from node_auth: %s", e)

    raise RuntimeError(
        "Cannot resolve peer_id. Run 'adk mesh onboard' first, or set AITHER_PEER_ID."
    )


def _saved_tenant() -> str:
    """The tenant `adk login` saved -- the same field `adk whoami` prints."""
    try:
        from adk.config import load_saved_config
        return str(load_saved_config().get("tenant_id") or "").strip()
    except Exception:  # noqa: BLE001 - no config = no tenant
        return ""


async def enroll(args: Any) -> None:
    """Enroll as a volunteer: mesh onboard if needed, grant consent, request trust.

    Usage: adk volunteer enroll [--tenant <tenant id>]

    The tenant is the peer's OWNER tenant as Strata will see it (server-derived
    from the authenticated caller at mesh onboard, e.g. ``tnt_dgg`` for a DGG
    member). It defaults to the tenant saved by ``adk login``; ``--tenant`` is
    an override for a caller who legitimately holds more than one. A guessed
    slug here (``dgg``) keys consent under a tenant the peer record does not
    carry, and every claim is refused as cross-tenant.
    """
    tenant = (getattr(args, "tenant", None) or "").strip() or _saved_tenant()
    if not tenant:
        print("✗ No tenant: run 'adk login' first (or pass --tenant <tenant id>)")
        return

    try:
        # Step 1: Try to get existing peer_id
        try:
            peer_id = await _resolve_peer_id()
            print(f"✓ Already onboarded: {peer_id}")
        except RuntimeError:
            # Step 2: If not onboarded, run mesh onboard
            print("Detecting hardware and registering mesh peer...")
            try:
                # Call adk mesh onboard
                proc = await asyncio.create_subprocess_exec(
                    "adk", "mesh", "onboard",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
                if proc.returncode != 0:
                    print(f"✗ Mesh onboard failed: {stderr.decode('utf-8', errors='replace')}")
                    sys.exit(1)
                print(stdout.decode("utf-8", errors="replace"))
                peer_id = await _resolve_peer_id()
                print(f"✓ Mesh peer registered: {peer_id}")
            except asyncio.TimeoutError:
                print("✗ Mesh onboard timeout")
                sys.exit(1)
            except FileNotFoundError:
                print("✗ adk command not found")
                sys.exit(1)

        # Step 3: Grant community inference consent
        print("Granting community inference consent...")
        try:
            from adk.mesh_provider import grant_consent
            result = await grant_consent(peer_id=peer_id, tenant_id=tenant, consent=True)
            if result.get("ok"):
                print("✓ Community inference consent granted")
            else:
                print(f"⚠ Consent already granted: {result.get('error')}")
        except ImportError:
            print("⚠ Consent endpoint unavailable (mesh_provider import failed)")
        except Exception as e:
            print(f"⚠ Consent error (non-blocking): {e}")

        # Step 4: Request trust tier
        print("Requesting trust tier...")
        try:
            from adk.mesh_provider import request_trust
            result = await request_trust(peer_id=peer_id)
            if result.get("ok"):
                status = result.get("trust_status", "pending")
                print(f"✓ Trust request: {status}")
                if status == "auto_granted":
                    print("✓ Trust tier 1 auto-granted")
            else:
                print(f"✗ Trust request failed: {result.get('error')}")
        except Exception as e:
            print(f"⚠ Trust request error: {e}")

        # Final status
        print("\n✓ Enrollment complete")
        print(f"  Peer ID: {peer_id}")
        print(f"  Tenant: {tenant}")
        print("  Next: run 'adk volunteer serve' to download the model")

    except Exception as e:
        print(f"✗ Enrollment failed: {e}")
        sys.exit(1)


async def serve(args: Any) -> None:
    """Download embedding model and start llama-server.

    Usage: adk volunteer serve [--model MODEL] [--device auto|cpu|gpu]
    """
    model = getattr(args, "model", "aither-code-embed-0.6b")
    device = getattr(args, "device", "auto")

    try:
        # Step 1: Resolve model cache path
        model_dir = Path.home() / ".cache" / "aither-models"
        model_dir.mkdir(parents=True, exist_ok=True)
        model_path = model_dir / "aither-code-embed.q8_0.gguf"

        # Step 2: Download model if needed
        if not model_path.exists():
            print(f"Downloading {model} ({EMBED_MODEL_BYTE_COUNT / 1e9:.1f} GB)...")
            try:
                async with httpx.AsyncClient(timeout=300.0) as client:
                    async with client.stream("GET", EMBED_MODEL_URL) as response:
                        response.raise_for_status()
                        total = int(response.headers.get("content-length", 0))
                        downloaded = 0
                        async for chunk in response.aiter_bytes(chunk_size=1024*1024):
                            model_path.write_bytes(chunk, mode="ab")
                            downloaded += len(chunk)
                            if total:
                                pct = int(100 * downloaded / total)
                                print(f"\r  [{pct}%] {downloaded/1e9:.1f}GB", end="", flush=True)
                        print()
            except Exception as e:
                print(f"✗ Download failed: {e}")
                sys.exit(1)

        # Step 3: Verify byte count
        actual_size = model_path.stat().st_size
        if actual_size != EMBED_MODEL_BYTE_COUNT:
            print(f"✗ Model size mismatch: expected {EMBED_MODEL_BYTE_COUNT}, got {actual_size}")
            sys.exit(1)
        print(f"✓ Model verified ({EMBED_MODEL_BYTE_COUNT} bytes)")

        # Step 4: Start llama-server
        print(f"Starting llama-server on {EMBED_LISTEN_HOST}:{EMBED_LISTEN_PORT}...")

        cmd = [
            "llama-server",
            "--embedding",
            "-m", str(model_path),
            "-ngl", "33" if device in ("auto", "gpu") else "0",
            "--host", EMBED_LISTEN_HOST,
            "-p", str(EMBED_LISTEN_PORT),
            "--n-threads", "4",
        ]

        # Try to start llama-server
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            # Wait for server to be ready
            health_url = f"http://{EMBED_LISTEN_HOST}:{EMBED_LISTEN_PORT}/v1/models"
            for i in range(30):  # Try for 30s
                try:
                    async with httpx.AsyncClient() as c:
                        r = await c.get(health_url, timeout=1.0)
                        if r.status_code == 200:
                            print("✓ llama-server ready")
                            break
                except Exception:
                    await asyncio.sleep(1)
            else:
                print("⚠ Server may still be starting (timeout after 30s)")

            # Keep server running (don't exit)
            print("\nServer running. Press Ctrl+C to stop.")
            await asyncio.sleep(3600)  # Run for 1 hour, then suggest status check
            print("\n✓ Server stable. Check status with: adk volunteer status")

        except FileNotFoundError:
            msg = "✗ llama-server not found. Install with: pip install llama-cpp-python[server]"
            print(msg)
            sys.exit(1)
        except Exception as e:
            print(f"✗ Server startup failed: {e}")
            sys.exit(1)

    except Exception as e:
        print(f"✗ Serve failed: {e}")
        sys.exit(1)


async def start(args: Any) -> None:
    """Claim batches, embed, and submit results in a loop.

    Usage: adk volunteer start [--batch-size 64]
    """
    batch_size = getattr(args, "batch_size", 64)
    genesis_url = os.getenv("AITHER_GENESIS_URL", "https://api.aitherium.com")

    try:
        # Get peer_id and auth token
        peer_id = await _resolve_peer_id()
        auth_token = _get_auth_token()
        if not auth_token:
            print("✗ Not authenticated. Run: adk login")
            sys.exit(1)

        # Check embedding server is running
        server_url = f"http://{EMBED_LISTEN_HOST}:{EMBED_LISTEN_PORT}"
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(f"{server_url}/v1/models")
                r.raise_for_status()
                print("✓ Embedding server ready")
        except Exception:
            print(f"✗ Embedding server not running on {server_url}")
            print("  Run: adk volunteer serve")
            sys.exit(1)

        # Load embedding prefixes
        try:
            from AitherOS.packages.awembed.awembed.evaluate import (
                QUERY_PREFIX,  # noqa: N811
            )
            query_prefix = QUERY_PREFIX
        except ImportError:
            query_prefix = (
                "Instruct: Given a code search question, retrieve the directory "
                "summary that answers it\nQuery: "
            )

        # Main loop
        print(f"Starting volunteer loop (batch_size={batch_size})...")
        print("Press Ctrl+C to exit gracefully\n")

        headers = {"Authorization": f"Bearer {auth_token}"}
        consecutive_errors = 0
        last_heartbeat = time.time()

        while True:
            try:
                # Step 1: Claim a job
                print(f"[{_timestamp()}] Claiming batch...")
                claim_url = f"{genesis_url}/volunteer/jobs/claim"
                async with httpx.AsyncClient() as c:
                    r = await c.post(
                        claim_url,
                        json={"batch_size": batch_size, "peer_id": peer_id},
                        headers=headers,
                        timeout=10.0,
                    )
                    if r.status_code == 404 and "pending" in r.text.lower():
                        print("  Queue empty; waiting 60s")
                        consecutive_errors = 0
                        await asyncio.sleep(60)
                        continue
                    if r.status_code != 200:
                        print(f"  Claim failed ({r.status_code}): {r.text[:100]}")
                        consecutive_errors += 1
                        if consecutive_errors >= 5:
                            print("✗ Too many errors, exiting")
                            sys.exit(1)
                        await asyncio.sleep(30)
                        continue

                    job_data = r.json()
                    job_id = job_data.get("job_id")
                    tasks = job_data.get("tasks", [])
                    expires_at = job_data.get("expires_at")

                    print(f"  ✓ Claimed {len(tasks)} tasks (job_id={job_id[:12]}...)")
                    consecutive_errors = 0

                # Step 2: Embed each task
                print(f"  Embedding {len(tasks)} texts...")
                for i, task in enumerate(tasks):
                    task_id = task.get("task_id")
                    text = task.get("text", "")

                    # Embed with DOC prefix (no prefix for documents, just QUERY_PREFIX for queries)
                    try:
                        embedding = await _embed_text(server_url, text)

                        # L2-normalize
                        import numpy as np
                        vec = np.array(embedding, dtype=np.float32)
                        vec = vec / np.linalg.norm(vec)

                        # Truncate to 256 dimensions
                        vec_256 = vec[:256].tolist()

                        # Submit result
                        result_url = f"{genesis_url}/volunteer/result"
                        async with httpx.AsyncClient() as c:
                            r = await c.post(
                                result_url,
                                json={
                                    "peer_id": peer_id,
                                    "job_id": job_id,
                                    "task_id": task_id,
                                    "vectors_256d": vec_256,
                                },
                                headers=headers,
                                timeout=10.0,
                            )
                            if r.status_code == 200:
                                result = r.json()
                                status = result.get("status")
                                print(f"    [{i+1}/{len(tasks)}] ✓ {status}")
                            else:
                                print(f"    [{i+1}/{len(tasks)}] ✗ Submit failed: {r.status_code}")
                    except Exception as e:
                        print(f"    [{i+1}/{len(tasks)}] ✗ Embed error: {e}")

                # Step 3: Heartbeat every 5 minutes
                now = time.time()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    try:
                        hb_url = f"{genesis_url}/volunteer/jobs/{job_id}/heartbeat"
                        async with httpx.AsyncClient() as c:
                            r = await c.post(
                                hb_url,
                                json={"peer_id": peer_id},
                                headers=headers,
                                timeout=5.0,
                            )
                            if r.status_code == 200:
                                print("  [heartbeat] ✓ Lease extended")
                            else:
                                print(f"  [heartbeat] ✗ Failed: {r.status_code}")
                        last_heartbeat = now
                    except Exception as e:
                        print(f"  [heartbeat] ✗ Error: {e}")

                # Small delay before next claim
                await asyncio.sleep(5)

            except KeyboardInterrupt:
                print(f"\n[{_timestamp()}] Shutting down gracefully...")
                try:
                    # Try to release the lease
                    if 'job_id' in locals():
                        release_url = f"{genesis_url}/volunteer/jobs/{job_id}/release"
                        async with httpx.AsyncClient() as c:
                            await c.post(
                                release_url,
                                json={"peer_id": peer_id},
                                headers=headers,
                                timeout=5.0,
                            )
                    print("✓ Lease released")
                except Exception as e:
                    logger.debug("Error releasing lease: %s", e)
                sys.exit(0)
            except Exception as e:
                print(f"  Error: {e}")
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    print("✗ Too many errors, exiting")
                    sys.exit(1)
                await asyncio.sleep(30)

    except Exception as e:
        print(f"✗ Start failed: {e}")
        sys.exit(1)


async def _embed_text(server_url: str, text: str) -> list[float]:
    """Call llama-server /v1/embeddings to embed text."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                f"{server_url}/v1/embeddings",
                json={
                    "input": text,
                    "model": "aither-code-embed",
                },
            )
            r.raise_for_status()
            data = r.json()
            embedding = data["data"][0]["embedding"]
            return embedding
    except Exception as e:
        raise RuntimeError(f"Embedding failed: {e}")


async def status(args: Any) -> None:
    """Show volunteer status: reputation, tokens, batches.

    Usage: adk volunteer status [--peer-id PEER_ID]
    """
    peer_id = getattr(args, "peer_id", None)
    genesis_url = os.getenv("AITHER_GENESIS_URL", "https://api.aitherium.com")

    try:
        if not peer_id:
            peer_id = await _resolve_peer_id()

        auth_token = _get_auth_token()
        if not auth_token:
            print("✗ Not authenticated. Run: adk login")
            sys.exit(1)

        # Query status endpoint
        headers = {"Authorization": f"Bearer {auth_token}"}
        status_url = f"{genesis_url}/volunteer/status/{peer_id}"

        async with httpx.AsyncClient() as c:
            r = await c.get(status_url, headers=headers, timeout=10.0)
            if r.status_code == 200:
                vol_status = r.json()
                print("\nVolunteer Status")
                print("================")
                print(f"Peer ID:           {peer_id}")
                print(f"Status:            {vol_status.get('status')}")
                print(f"Reputation:        {vol_status.get('reputation', 0)}")
                print(f"Tokens Earned:     {vol_status.get('tokens_earned', 0)}")
                print(f"Batches Verified:  {vol_status.get('batches_verified', 0)}")
                vr = vol_status.get('verification_rate', 0)
                print(f"Verification Rate: {vr:.1%}")
                print()
            else:
                print(f"✗ Status query failed ({r.status_code}): {r.text}")
                sys.exit(1)

    except Exception as e:
        print(f"✗ Status failed: {e}")
        sys.exit(1)


def _timestamp() -> str:
    """Return a simple timestamp string."""
    return time.strftime("%H:%M:%S")
