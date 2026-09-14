"""Document management CLI commands.

Commands:
    adk doc upload <path> [--type <doc_type>]  — Upload and encrypt a document
    adk doc list                                 — List your documents (metadata)
    adk doc get <doc_id> -o <output_path>      — Download and decrypt a document
    adk doc delete <doc_id>                     — Delete a document

All commands enforce tenant/user authentication via server-side caller context.
Documents are encrypted at rest. Metadata is plaintext.
"""

from __future__ import annotations
from adk._tls import tls_verify

import asyncio
import os
from pathlib import Path
from typing import Optional

from adk.config import load_saved_config


async def _get_client_with_auth(
    gateway_url: str,
    api_key: Optional[str] = None,
) -> tuple:
    """
    Return an httpx.AsyncClient and authentication headers.

    Args:
        gateway_url: Genesis/Gateway base URL (e.g., http://localhost:8001)
        api_key: Optional API key (AITHER_API_KEY or ACTA token)

    Returns:
        (client, headers) tuple
    """
    import httpx

    headers = {}

    # Resolve API key: explicit arg > env var > saved config
    if not api_key:
        api_key = os.environ.get("AITHER_API_KEY", "")
    if not api_key:
        saved = load_saved_config()
        api_key = saved.get("api_key", "")

    # Add Authorization header if key exists
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    client = httpx.AsyncClient(
        timeout=30.0,
        verify=tls_verify(),  # Trust internal CA for local deployments
    )

    return client, headers


def _print_auth_error():
    """Print helpful message when authentication fails."""
    print()
    print("  [!!] Authentication required but not found.")
    print()
    print("  To authenticate, run:")
    print("    adk login                    — Browser OAuth flow")
    print("    adk login --api-key <key>   — Direct API key")
    print()
    print("  Or set the environment variable:")
    print("    export AITHER_API_KEY=<your-key>")
    print()


async def _cmd_doc_upload(args) -> int:
    """Handle 'adk doc upload <path> [--type <doc_type>]'."""
    file_path = Path(args.path)

    # Validate file exists and is readable
    if not file_path.exists():
        print(f"  [!!] File not found: {file_path}")
        return 1

    if not file_path.is_file():
        print(f"  [!!] Not a file: {file_path}")
        return 1

    # Get gateway URL
    gateway_url = getattr(args, "gateway", None) or os.environ.get(
        "AITHER_CORE_URL", "http://localhost:8001"
    )
    gateway_url = gateway_url.rstrip("/")

    # Get API key
    api_key = getattr(args, "api_key", None)

    print()
    print("  Document Upload")
    print("  ===============")
    print()
    print(f"  File:       {file_path}")
    print(f"  Gateway:    {gateway_url}")
    if getattr(args, "type", None):
        print(f"  Type:       {args.type}")

    client, headers = await _get_client_with_auth(gateway_url, api_key)

    try:
        # Read file
        with open(file_path, "rb") as f:
            file_bytes = f.read()

        if not file_bytes:
            print("  [!!] File is empty")
            return 1

        file_size = len(file_bytes)
        print(f"  Size:       {file_size} bytes")
        print()

        # Prepare multipart upload
        files = {
            "file": (file_path.name, file_bytes, getattr(args, "type", None) or "application/octet-stream"),
        }

        # POST to /v1/documents/upload
        resp = await client.post(
            f"{gateway_url}/v1/documents/upload",
            files=files,
            headers=headers,
        )

        if resp.status_code == 401:
            _print_auth_error()
            return 1
        elif resp.status_code == 403:
            print(f"  [!!] Access denied: {resp.json().get('detail', 'Unknown error')}")
            return 1
        elif resp.status_code in (200, 201):
            data = resp.json()
            doc_id = data.get("doc_id", "")
            sha256 = data.get("sha256", "")[:16]

            print("  [OK] Document uploaded and encrypted!")
            print()
            print(f"  Doc ID:     {doc_id}")
            print(f"  SHA-256:    {sha256}...")
            print(f"  Created:    {data.get('created_at', '?')}")
            print()
            print("  Next steps:")
            print("    adk doc list                  — List all your documents")
            print(f"    adk doc get {doc_id[:8]}... -o output.bin  — Download this document")
            print(f"    adk doc delete {doc_id[:8]}...     — Delete this document")
            print()

            return 0
        else:
            print(f"  [!!] Upload failed: HTTP {resp.status_code}")
            try:
                err = resp.json()
                print(f"       Error: {err.get('error', '?')}")
                print(f"       Detail: {err.get('detail', '?')}")
            except Exception:
                print(f"       Response: {resp.text[:200]}")
            return 1

    except FileNotFoundError as e:
        print(f"  [!!] File read error: {e}")
        return 1
    except Exception as e:
        print(f"  [!!] Error: {e}")
        return 1
    finally:
        await client.aclose()


async def _cmd_doc_list(args) -> int:
    """Handle 'adk doc list'."""
    gateway_url = getattr(args, "gateway", None) or os.environ.get(
        "AITHER_CORE_URL", "http://localhost:8001"
    )
    gateway_url = gateway_url.rstrip("/")

    api_key = getattr(args, "api_key", None)

    print()
    print("  Documents")
    print("  =========")
    print()

    client, headers = await _get_client_with_auth(gateway_url, api_key)

    try:
        resp = await client.get(
            f"{gateway_url}/v1/documents",
            headers=headers,
        )

        if resp.status_code == 401:
            _print_auth_error()
            return 1
        elif resp.status_code == 403:
            print(f"  [!!] Access denied: {resp.json().get('detail', 'Unknown error')}")
            return 1
        elif resp.status_code == 200:
            data = resp.json()
            documents = data.get("documents", [])
            total = data.get("total", 0)

            if not documents:
                print("  No documents found.")
                print()
                print("  Upload your first document:")
                print("    adk doc upload <path>")
                print()
                return 0

            # Print table header
            print(f"{'Doc ID (first 16 chars)':<24} {'Filename':<30} {'Size':<12} {'Created':<20}")
            print("-" * 86)

            for doc in documents:
                doc_id = doc.get("doc_id", "")[:16]
                filename = doc.get("filename", "?")[:30]
                size = doc.get("size", 0)
                created = doc.get("created_at", "?")[:19]  # ISO date, no time

                print(f"{doc_id:<24} {filename:<30} {size:<12} {created:<20}")

            print()
            print(f"  Total: {total} document(s)")
            print()

            return 0
        else:
            print(f"  [!!] List failed: HTTP {resp.status_code}")
            try:
                err = resp.json()
                print(f"       Error: {err.get('error', '?')}")
            except Exception:
                print(f"       Response: {resp.text[:200]}")
            return 1

    except Exception as e:
        print(f"  [!!] Error: {e}")
        return 1
    finally:
        await client.aclose()


async def _cmd_doc_get(args) -> int:
    """Handle 'adk doc get <doc_id> -o <output_path>'."""
    doc_id = args.doc_id
    output_path = getattr(args, "output", None) or getattr(args, "o", None)

    if not output_path:
        print("  [!!] Output path required: use -o <path>")
        return 1

    output_path = Path(output_path)

    gateway_url = getattr(args, "gateway", None) or os.environ.get(
        "AITHER_CORE_URL", "http://localhost:8001"
    )
    gateway_url = gateway_url.rstrip("/")

    api_key = getattr(args, "api_key", None)

    print()
    print("  Document Download")
    print("  =================")
    print()
    print(f"  Doc ID:        {doc_id}")
    print(f"  Output path:   {output_path}")
    print(f"  Gateway:       {gateway_url}")
    print()

    client, headers = await _get_client_with_auth(gateway_url, api_key)

    try:
        resp = await client.get(
            f"{gateway_url}/v1/documents/{doc_id}",
            headers=headers,
        )

        if resp.status_code == 401:
            _print_auth_error()
            return 1
        elif resp.status_code == 403:
            print(f"  [!!] Access denied: {resp.json().get('detail', 'Not the document owner')}")
            return 1
        elif resp.status_code == 404:
            print(f"  [!!] Document not found: {doc_id}")
            return 1
        elif resp.status_code == 200:
            # Write decrypted bytes to output path
            with open(output_path, "wb") as f:
                f.write(resp.content)

            size = len(resp.content)
            print(f"  [OK] Downloaded {size} bytes")
            print(f"       {output_path}")
            print()

            return 0
        else:
            print(f"  [!!] Download failed: HTTP {resp.status_code}")
            try:
                err = resp.json()
                print(f"       Error: {err.get('error', '?')}")
                print(f"       Detail: {err.get('detail', '?')}")
            except Exception:
                print(f"       Response: {resp.text[:200]}")
            return 1

    except FileNotFoundError as e:
        print(f"  [!!] Output path error: {e}")
        return 1
    except Exception as e:
        print(f"  [!!] Error: {e}")
        return 1
    finally:
        await client.aclose()


async def _cmd_doc_delete(args) -> int:
    """Handle 'adk doc delete <doc_id>'."""
    doc_id = args.doc_id

    gateway_url = getattr(args, "gateway", None) or os.environ.get(
        "AITHER_CORE_URL", "http://localhost:8001"
    )
    gateway_url = gateway_url.rstrip("/")

    api_key = getattr(args, "api_key", None)

    print()
    print("  Document Delete")
    print("  ===============")
    print()
    print(f"  Doc ID:        {doc_id}")
    print()

    # Confirm deletion
    confirm = input("  Delete this document? (type 'yes' to confirm): ").strip().lower()
    if confirm != "yes":
        print("  Cancelled.")
        return 0

    print()

    client, headers = await _get_client_with_auth(gateway_url, api_key)

    try:
        resp = await client.delete(
            f"{gateway_url}/v1/documents/{doc_id}",
            headers=headers,
        )

        if resp.status_code == 401:
            _print_auth_error()
            return 1
        elif resp.status_code == 403:
            print(f"  [!!] Access denied: {resp.json().get('detail', 'Not the document owner')}")
            return 1
        elif resp.status_code == 404:
            print(f"  [!!] Document not found: {doc_id}")
            return 1
        elif resp.status_code == 204:
            print(f"  [OK] Document deleted: {doc_id}")
            print()
            return 0
        else:
            print(f"  [!!] Delete failed: HTTP {resp.status_code}")
            try:
                err = resp.json()
                print(f"       Error: {err.get('error', '?')}")
            except Exception:
                print(f"       Response: {resp.text[:200]}")
            return 1

    except Exception as e:
        print(f"  [!!] Error: {e}")
        return 1
    finally:
        await client.aclose()


def cmd_doc(args) -> int:
    """Main 'adk doc' dispatcher."""
    doc_cmd = getattr(args, "doc_command", None)

    if doc_cmd == "upload":
        return asyncio.run(_cmd_doc_upload(args))
    elif doc_cmd == "list":
        return asyncio.run(_cmd_doc_list(args))
    elif doc_cmd == "get":
        return asyncio.run(_cmd_doc_get(args))
    elif doc_cmd == "delete":
        return asyncio.run(_cmd_doc_delete(args))
    else:
        print("  Usage: adk doc [upload|list|get|delete]")
        print()
        print("  upload <path> [--type <type>]    Upload and encrypt a document")
        print("  list                              List your documents (metadata)")
        print("  get <doc_id> -o <path>           Download and decrypt a document")
        print("  delete <doc_id>                   Delete a document")
        print()
        print("  Options:")
        print("    --gateway <url>   Genesis/Gateway URL (default: http://localhost:8001)")
        print("    --api-key <key>   API key (or set AITHER_API_KEY env var)")
        print()
        return 1
