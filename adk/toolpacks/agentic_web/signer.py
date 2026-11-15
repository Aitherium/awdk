"""RFC 9421 HTTP Message Signatures with Ed25519, shaped for Web Bot Auth.

``sign_request`` signs ``("@authority" "signature-agent")`` with
``tag="web-bot-auth"`` and returns the three headers an outbound agent request
carries: ``Signature``, ``Signature-Input`` and ``Signature-Agent``. The keyid is
the RFC 7638 JWK thumbprint, so a verifier finds the key in the signer's
``/.well-known/http-message-signatures-directory``.

The private key is a JWK dict the CALLER resolved (for example from a vault
secret by name); it is never logged and never placed in a returned header.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

WEB_BOT_AUTH_TAG = "web-bot-auth"
DIRECTORY_TAG = "http-message-signatures-directory"
DEFAULT_COMPONENTS = ("@authority", "signature-agent")
DEFAULT_LABEL = "sig1"
DEFAULT_TTL = 300


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------- keys
def generate_ed25519_jwk() -> Dict[str, str]:
    """A fresh private OKP/Ed25519 JWK (with ``d``) and its thumbprint as ``kid``."""
    sk = Ed25519PrivateKey.generate()
    d = sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    x = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": _b64u(x), "d": _b64u(d)}
    jwk["kid"] = jwk_thumbprint(jwk)
    return jwk


def public_jwk(jwk: Mapping[str, Any]) -> Dict[str, str]:
    pub = {"kty": "OKP", "crv": "Ed25519", "x": str(jwk["x"])}
    pub["kid"] = jwk_thumbprint(pub)
    return pub


def jwk_thumbprint(jwk: Mapping[str, Any]) -> str:
    """RFC 7638 thumbprint (SHA-256, base64url) over the required OKP members."""
    if jwk.get("kty") != "OKP":
        raise ValueError("only OKP keys are supported")
    canon = json.dumps({"crv": jwk["crv"], "kty": "OKP", "x": jwk["x"]},
                       separators=(",", ":"), sort_keys=True)
    return _b64u(hashlib.sha256(canon.encode("utf-8")).digest())


def _private_key(jwk: Mapping[str, Any]) -> Ed25519PrivateKey:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or not jwk.get("d"):
        raise ValueError("private key must be an OKP/Ed25519 JWK with 'd'")
    return Ed25519PrivateKey.from_private_bytes(_b64u_dec(str(jwk["d"])))


def _public_key(jwk: Mapping[str, Any]) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(_b64u_dec(str(jwk["x"])))


# ---------------------------------------------------------------- base
def _hget(headers: Mapping[str, str], name: str) -> Optional[str]:
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return v
    return None


def _component_value(comp: str, method: str, url: str, headers: Mapping[str, str],
                     request_authority: str = "") -> str:
    name = comp.split(";", 1)[0]
    u = urlsplit(url)
    if name == "@method":
        return method.upper()
    if name == "@authority":
        auth = request_authority or u.netloc or (_hget(headers, "host") or "")
        auth = auth.lower()
        if u.scheme == "https" and auth.endswith(":443"):
            auth = auth[:-4]
        if u.scheme == "http" and auth.endswith(":80"):
            auth = auth[:-3]
        return auth
    if name == "@path":
        return u.path or "/"
    if name == "@query":
        return "?" + (u.query or "")
    if name == "@scheme":
        return u.scheme.lower()
    if name == "@target-uri":
        return url
    if name.startswith("@"):
        raise ValueError(f"unsupported derived component {name}")
    v = _hget(headers, name)
    if v is None:
        raise ValueError(f"covered header {name!r} is missing")
    return v.strip()


def _params_string(components: Sequence[str], params: Sequence[Tuple[str, Any]]) -> str:
    def comp(c: str) -> str:
        n, _, rest = c.partition(";")
        return f'"{n}"' + (";" + rest if rest else "")

    inner = "(" + " ".join(comp(c) for c in components) + ")"
    for k, v in params:
        if v is None:
            continue
        inner += f";{k}=" + (str(v) if isinstance(v, int) else f'"{v}"')
    return inner


def signature_base(components: Sequence[str], method: str, url: str,
                   headers: Mapping[str, str], params_str: str,
                   request_authority: str = "") -> str:
    """The RFC 9421 section 2.5 signature base."""
    lines = []
    for c in components:
        n, _, rest = c.partition(";")
        label = f'"{n}"' + (";" + rest if rest else "")
        lines.append(f"{label}: {_component_value(c, method, url, headers, request_authority)}")
    lines.append(f'"@signature-params": {params_str}')
    return "\n".join(lines)


def sign_base(base: str, private_jwk: Mapping[str, Any]) -> str:
    return base64.b64encode(_private_key(private_jwk).sign(base.encode("utf-8"))).decode("ascii")


# ---------------------------------------------------------------- sign / verify
def _sf_string(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sign_request(method: str, url: str, headers: Optional[Mapping[str, str]],
                 private_jwk: Mapping[str, Any], signature_agent: str,
                 created: Optional[int] = None, expires: Optional[int] = None,
                 nonce: Optional[str] = None, label: str = DEFAULT_LABEL
                 ) -> Dict[str, str]:
    """Sign an outbound request for Web Bot Auth. Returns only the headers to add."""
    created = int(time.time()) if created is None else int(created)
    expires = created + DEFAULT_TTL if expires is None else int(expires)
    if expires <= created:
        raise ValueError("expires must be after created")
    nonce = nonce or _b64u(os.urandom(32))
    agent_value = _sf_string(signature_agent)
    hdrs = dict(headers or {})
    hdrs["Signature-Agent"] = agent_value
    keyid = jwk_thumbprint(private_jwk)
    params = _params_string(DEFAULT_COMPONENTS, [
        ("created", created), ("keyid", keyid), ("alg", "ed25519"),
        ("expires", expires), ("nonce", nonce), ("tag", WEB_BOT_AUTH_TAG)])
    base = signature_base(DEFAULT_COMPONENTS, method, url, hdrs, params)
    sig = sign_base(base, private_jwk)
    return {"Signature": f"{label}=:{sig}:", "Signature-Input": f"{label}={params}",
            "Signature-Agent": agent_value}


_INPUT_RE = re.compile(r'^\s*([a-z0-9_.*-]+)=(\((?:[^()]*)\)(?:;.*)?)\s*$')


def parse_signature_input(value: str) -> Dict[str, Dict[str, Any]]:
    """Parse ``label=("a" "b");k=v;...`` (one or more, comma-separated)."""
    out: Dict[str, Dict[str, Any]] = {}
    for part in _split_top(value):
        m = _INPUT_RE.match(part)
        if not m:
            raise ValueError(f"malformed Signature-Input member {part!r}")
        label, rest = m.group(1), m.group(2)
        close = rest.index(")")
        comps = re.findall(r'"([^"]+)"((?:;[a-z]+)*)', rest[1:close])
        components = [n + suffix for n, suffix in comps]
        params: Dict[str, Any] = {}
        for k, v in re.findall(r';([a-z]+)=("(?:[^"\\]|\\.)*"|-?\d+)', rest[close + 1:]):
            params[k] = v[1:-1] if v.startswith('"') else int(v)
        out[label] = {"components": components, "params": params, "raw": rest}
    return out


def _split_top(value: str) -> List[str]:
    parts, depth, cur, q = [], 0, "", False
    for ch in value:
        if ch == '"' and not cur.endswith("\\"):
            q = not q
        if not q:
            depth += ch == "("
            depth -= ch == ")"
            if ch == "," and depth == 0:
                parts.append(cur)
                cur = ""
                continue
        cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _parse_signature(value: str) -> Dict[str, bytes]:
    out = {}
    for label, b64 in re.findall(r'([a-z0-9_.*-]+)=:([A-Za-z0-9+/=]*):', value):
        out[label] = base64.b64decode(b64)
    return out


def _keys(jwks: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(jwks, Mapping) and "keys" in jwks:
        return jwks["keys"]
    if isinstance(jwks, Mapping):
        return [jwks]
    return list(jwks or [])


def verify_request(method: str, url: str, headers: Mapping[str, str], jwks: Any,
                   now: Optional[int] = None, required_tag: Optional[str] = WEB_BOT_AUTH_TAG,
                   request_authority: str = "") -> bool:
    """True when some signature on the request verifies against a key in ``jwks``."""
    si, sv = _hget(headers, "signature-input"), _hget(headers, "signature")
    if not si or not sv:
        return False
    try:
        inputs, sigs = parse_signature_input(si), _parse_signature(sv)
    except ValueError:
        return False
    now = int(time.time()) if now is None else int(now)
    by_kid: Dict[str, Mapping[str, Any]] = {}
    for k in _keys(jwks):
        by_kid[jwk_thumbprint(k)] = k
        if k.get("kid"):
            by_kid[str(k["kid"])] = k
    for label, spec in inputs.items():
        p = spec["params"]
        if required_tag and p.get("tag") != required_tag:
            continue
        if "expires" in p and int(p["expires"]) < now:
            continue
        if "created" in p and int(p["created"]) > now + 60:
            continue
        key = by_kid.get(str(p.get("keyid", "")))
        if key is None and "keyid" not in p:
            ks = list(_keys(jwks))
            key = ks[0] if len(ks) == 1 else None
        if key is None or label not in sigs:
            continue
        try:
            base = signature_base(spec["components"], method, url, headers, spec["raw"],
                                  request_authority)
            _public_key(key).verify(sigs[label], base.encode("utf-8"))
            return True
        except (InvalidSignature, ValueError):
            continue
    return False


def sign_directory_response(authority: str, private_jwk: Mapping[str, Any],
                            created: Optional[int] = None, expires: Optional[int] = None,
                            nonce: Optional[str] = None, label: str = DEFAULT_LABEL
                            ) -> Dict[str, str]:
    """Headers for the key-directory response: signs ``"@authority";req``."""
    created = int(time.time()) if created is None else int(created)
    expires = created + DEFAULT_TTL if expires is None else int(expires)
    nonce = nonce or _b64u(os.urandom(32))
    comps = ("@authority;req",)
    params = _params_string(comps, [
        ("alg", "ed25519"), ("keyid", jwk_thumbprint(private_jwk)), ("nonce", nonce),
        ("tag", DIRECTORY_TAG), ("created", created), ("expires", expires)])
    base = signature_base(comps, "GET", f"https://{authority}/", {}, params,
                          request_authority=authority)
    return {"Signature": f"{label}=:{sign_base(base, private_jwk)}:",
            "Signature-Input": f"{label}={params}"}
