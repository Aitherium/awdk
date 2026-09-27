"""Shared TLS verification policy for adk HTTP clients.

The adk is a public SDK that talks to BOTH public-CA portals and self-signed
LOCAL AitherOS deployments. Historically dozens of HTTP calls used
``verify=False``, which silently disables certificate verification and exposes
auth tokens / secrets to man-in-the-middle. This module is the single source of
truth for the ``verify=`` value, defaulting to *verify* and trusting the
AitherNet internal CA when its bundle is installed.

Policy (``tls_verify()``):
  * ``AITHER_TLS_VERIFY`` in {false,0,no,off}  -> ``False`` (disable checks;
    isolated dev box only — never for auth/secret traffic in production).
  * else, if ``AITHER_CA_BUNDLE`` names a file -> exactly that file.
  * else, if the AitherNet CA bundle is present -> a file holding the public
    roots PLUS that bundle, so internal AND public-CA hosts both verify.
  * else -> ``True`` (verify against the system trust store).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Union

_log = logging.getLogger("aither.adk.tls")
_warned_disabled = False


def _ca_bundle_path() -> str | None:
    """Locate the AitherNet CA bundle installed by ``adk setup`` / mcp_setup.

    Searches in order:
      1. $AITHER_CA_BUNDLE env var (explicit override)
      2. $HOME/.aither/aithernet-ca-bundle.pem (user install)
      3. $AITHER_HOME/aithernet-ca-bundle.pem (adk home)
      4. /app/AitherOS/Library/Data/tls/combined-ca-bundle.pem (container co-located)
      5. /certs/ca-chain.pem (container generic mount)

    Not cached: setup may install the bundle after the first HTTP call. Never
    raises — TLS policy must not crash an HTTP call (e.g. ``Path.home()`` raises
    when no home dir is determinable, such as a stripped test/CI environment).
    """
    try:
        explicit = os.getenv("AITHER_CA_BUNDLE", "").strip()
        if explicit and Path(explicit).is_file():
            return explicit
        home = (
            os.environ.get("AITHER_HOME")
            or os.environ.get("HOME")
            or os.environ.get("USERPROFILE")
        )
        if not home:
            home = ""

        # Search order: home-relative, then container mounts
        candidates = []
        if home:
            base = Path(home)
            candidates.extend([
                base / "aithernet-ca-bundle.pem",
                base / ".aither" / "aithernet-ca-bundle.pem",
            ])
        # Container-native paths (bind-mounted from AitherOS or generic /certs)
        candidates.extend([
            Path("/app/AitherOS/Library/Data/tls/combined-ca-bundle.pem"),
            Path("/certs/ca-chain.pem"),
        ])
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    except Exception:
        return None
    return None


def tls_verify() -> Union[bool, str]:
    """Return the ``verify=`` value for an httpx/requests client.

    Defaults to certificate verification. Returns the AitherNet CA bundle path
    when available so self-signed internal certs are trusted *with* verification.
    Only ``AITHER_TLS_VERIFY=false`` (or 0/no/off) disables verification.
    """
    flag = os.getenv("AITHER_TLS_VERIFY", "true").strip().lower()
    if flag in ("false", "0", "no", "off"):
        global _warned_disabled
        if not _warned_disabled:
            _warned_disabled = True
            _log.warning(
                "TLS certificate verification is DISABLED (AITHER_TLS_VERIFY=%s). "
                "Traffic — including auth/secret calls — is exposed to MITM. "
                "Use this only on an isolated dev box, never in production.",
                flag,
            )
        return False
    explicit = os.getenv("AITHER_CA_BUNDLE", "").strip()
    bundle = _ca_bundle_path()
    if not bundle:
        return True
    if explicit and bundle == explicit:
        return bundle  # an explicit override is the caller's whole trust decision
    return _merged_with_public_roots(bundle)


def _merged_with_public_roots(bundle: str) -> str:
    """Return a CA file trusting the public roots AND the internal bundle.

    The discovered AitherNet bundle holds only the internal CA. Handing it to
    httpx/requests as ``verify=`` REPLACES the public trust store, so every call
    to a public-CA host (idp.aitherium.com, tunnel.aitherium.com behind
    Cloudflare) failed CERTIFICATE_VERIFY_FAILED -- `adk devices status` and
    `adk rc` could not reach the control plane from any box that ran setup.
    The merged file is content-addressed, so a changed bundle yields a new file.
    Never raises: on any failure the bare bundle is returned (old behaviour).
    """
    try:
        import hashlib
        import tempfile

        import certifi  # httpx depends on it, so it is always importable

        public = Path(certifi.where()).read_bytes()
        internal = Path(bundle).read_bytes()
        digest = hashlib.sha256(public + b"|" + internal).hexdigest()[:16]
        out = Path(tempfile.gettempdir()) / ("adk-ca-merged-" + digest + ".pem")
        if not out.is_file():
            tmp = out.with_suffix(".tmp" + str(os.getpid()))
            tmp.write_bytes(public.rstrip(b"\n") + b"\n" + internal)
            os.replace(tmp, out)
        return str(out)
    except Exception:
        return bundle
