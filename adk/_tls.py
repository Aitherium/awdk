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

        # Memoized on both files' mtimes: tls_verify() runs per request (a /v1/models
        # poll every few seconds) and re-read + re-hashed both CA files ON the event loop
        # every time (py-spy, 2026-10-14).
        memo_key = (bundle, os.path.getmtime(bundle), os.path.getmtime(certifi.where()))
        hit = _MERGED_MEMO.get(memo_key)
        if hit and os.path.isfile(hit):
            return hit
        public = Path(certifi.where()).read_bytes()
        internal = Path(bundle).read_bytes()
        digest = hashlib.sha256(public + b"|" + internal).hexdigest()[:16]
        out = Path(tempfile.gettempdir()) / ("adk-ca-merged-" + digest + ".pem")
        if not out.is_file():
            tmp = out.with_suffix(".tmp" + str(os.getpid()))
            tmp.write_bytes(public.rstrip(b"\n") + b"\n" + internal)
            os.replace(tmp, out)
        if len(_MERGED_MEMO) > 8:
            _MERGED_MEMO.clear()
        _MERGED_MEMO[memo_key] = str(out)
        return str(out)
    except Exception:
        return bundle


_MERGED_MEMO: dict = {}
_CTX_CACHE: dict = {}
_HTTPX_CTX: dict = {}


def install_httpx_ssl_cache() -> bool:
    """Make httpx reuse one ``SSLContext`` per trust decision, process-wide.

    httpx builds a NEW context (loading the whole CA file) for every client constructed
    with a True or CA-path verify value, and adk constructs clients per call in hundreds
    of places. py-spy on the :9001 daemon, 2026-10-14: 27% of the event loop's time in a
    turn was ``ssl.create_default_context`` under the crystal's embedding client, enough
    that every recall timer fired late together and the turn got no memory at all.
    ``ssl_context()`` fixed the call sites that use it; this covers every other one.
    A client certificate, an explicit ``SSLContext`` or verification turned off is passed
    through untouched; a CA file that changes on disk gets a new context. Idempotent;
    False when this httpx has no such hook (then nothing changes).
    """
    try:
        import ssl

        import httpx._transports.default as _default  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 -- no httpx: nothing to cache
        return False
    orig = getattr(_default, "create_ssl_context", None)
    if orig is None:
        return False
    if getattr(orig, "_adk_cached", False):
        return True

    def cached(verify=True, cert=None, trust_env=True):  # httpx 0.28 signature
        if cert is not None or not verify or isinstance(verify, ssl.SSLContext):
            return orig(verify=verify, cert=cert, trust_env=trust_env)
        env = ((os.environ.get("SSL_CERT_FILE"), os.environ.get("SSL_CERT_DIR"))
               if trust_env else (None, None))
        try:
            stamp = os.path.getmtime(verify) if isinstance(verify, str) else 0.0
        except OSError:
            stamp = -1.0
        key = (verify, trust_env, env, stamp)
        ctx = _HTTPX_CTX.get(key)
        if ctx is None:
            ctx = orig(verify=verify, cert=cert, trust_env=trust_env)
            if len(_HTTPX_CTX) > 16:
                _HTTPX_CTX.clear()
            _HTTPX_CTX[key] = ctx
        return ctx

    cached._adk_cached = True  # type: ignore[attr-defined]
    cached._adk_orig = orig  # type: ignore[attr-defined]
    _default.create_ssl_context = cached
    return True


def _env_trust_store() -> tuple:
    """``(cafile, capath)`` for ``verify=True``, matching httpx's ``trust_env`` lookup.

    ``SSL_CERT_FILE`` (an existing file) wins, then ``SSL_CERT_DIR`` (an existing
    directory), else certifi's bundle.
    """
    cert_file = os.environ.get("SSL_CERT_FILE", "").strip()
    if cert_file and os.path.isfile(cert_file):
        return cert_file, None
    cert_dir = os.environ.get("SSL_CERT_DIR", "").strip()
    if cert_dir and os.path.isdir(cert_dir):
        return None, cert_dir
    import certifi

    return certifi.where(), None


def ssl_context(verify: Union[bool, str, None] = None) -> Union[bool, "object"]:
    """A cached ``ssl.SSLContext`` for an httpx ``verify=`` value (default: ``tls_verify()``).

    httpx builds a NEW context for every client it constructs from ``True`` or a CA
    path, loading the whole CA file each time. Measured 2026-10-14 on the adk daemon:
    those builds were ~60 % of the event loop's busy time during a turn (gateway ping,
    LLM client, embeddings, service probe, spirit recall), each one blocking the loop
    long enough that every per-stage recall timer fired late. One context per trust
    decision is shared instead; a CA file that changes on disk gets a new one.
    ``False`` stays ``False``. For ``requests``, keep passing ``tls_verify()``.
    """
    if verify is None:
        verify = tls_verify()
    if verify is False:
        return False
    try:
        import ssl

        capath = None
        if verify is True:
            # httpx's own verify=True honours SSL_CERT_FILE / SSL_CERT_DIR (trust_env)
            # before certifi. A container that points SSL_CERT_FILE at a bundle holding
            # an internal CA relies on that; certifi alone fails every internal host
            # with CERTIFICATE_VERIFY_FAILED.
            path, capath = _env_trust_store()
        else:
            path = str(verify)
        stamp = os.path.getmtime(path) if path else os.path.getmtime(capath)
        key = (path, capath, stamp)
        ctx = _CTX_CACHE.get(key)
        if ctx is None:
            ctx = ssl.create_default_context(cafile=path, capath=capath)
            if len(_CTX_CACHE) > 8:  # a CA file rewritten many times: drop old ones
                _CTX_CACHE.clear()
            _CTX_CACHE[key] = ctx
        return ctx
    except Exception:  # noqa: BLE001 -- TLS policy must never crash an HTTP call
        return verify
