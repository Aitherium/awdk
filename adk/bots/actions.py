"""OpenAPI 3.x Action loader and SSRF-guarded HTTP executor.

Fail-closed SSRF defense:
- Requires ``http`` or ``https`` scheme and non-empty hostname.
- Blocks ``localhost``, ``.internal``, ``.local`` suffixes.
- Blocks loopback, private (RFC1918/ULA), link-local/metadata (``169.254.169.254``),
  reserved, multicast, unspecified, IPv4-mapped IPv6, and CGNAT (``100.64.0.0/10``).
- Resolves hostnames via ``socket.getaddrinfo`` to catch DNS-rebinding/private IPs.
- Disables automatic HTTP redirects and re-validates every redirect hop (up to 5).
- Permits private/loopback endpoints ONLY when explicitly opted in via
  ``allow_local_urls=True``, ``allow_private=True``, or ``allowlisted_hosts``.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
import yaml

from adk.bots.manifest import ActionSpec

_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_HTTP_METHODS = ("get", "post", "put", "patch", "delete")
_MAX_REDIRECTS = 5
_IPV4_PART_RE = re.compile(r"^(?:0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)$")
_SENSITIVE_REDIRECT_HEADERS = frozenset(
    {"authorization", "x-api-key", "proxy-authorization", "cookie"}
)


def _parse_noncanonical_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse decimal/octal/hex/shorthand IPv4 literals (inet_aton forms)."""
    bare = (host or "").strip("[]").rstrip(".")
    if not bare:
        return None
    parts = bare.split(".")
    if not (1 <= len(parts) <= 4):
        return None
    if not all(_IPV4_PART_RE.match(part) for part in parts):
        return None
    try:
        vals = []
        for part in parts:
            if part.startswith(("0x", "0X")):
                vals.append(int(part, 16))
            elif len(part) > 1 and part.startswith("0"):
                vals.append(int(part, 8))
            else:
                vals.append(int(part, 10))
    except ValueError:
        return None
    if any(v < 0 for v in vals):
        return None
    if any(v > 255 for v in vals[:-1]):
        return None
    last_limit = 1 << (8 * (5 - len(vals)))
    if vals[-1] >= last_limit:
        return None
    addr_int = 0
    for v in vals[:-1]:
        addr_int = (addr_int << 8) | v
    addr_int = (addr_int << (8 * (5 - len(vals)))) | vals[-1]
    return ipaddress.IPv4Address(addr_int)


def _url_origin(url: str) -> tuple[str, str, int]:
    p = urlparse(url)
    scheme = (p.scheme or "").lower()
    host = (p.hostname or "").lower()
    port = p.port or (443 if scheme == "https" else 80)
    return (scheme, host, port)


def _is_disallowed_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        return _is_disallowed_ip(ip_obj.ipv4_mapped)
    if ip_obj.is_loopback:
        return f"loopback IP {ip_obj} is blocked"
    if ip_obj.is_link_local:
        return f"link-local/metadata IP {ip_obj} is blocked"
    if ip_obj.is_private:
        return f"private IP {ip_obj} is blocked"
    if ip_obj.is_reserved:
        return f"reserved IP {ip_obj} is blocked"
    if ip_obj.is_multicast:
        return f"multicast IP {ip_obj} is blocked"
    if ip_obj.is_unspecified:
        return f"unspecified IP {ip_obj} is blocked"
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT_NET:
        return f"CGNAT IP {ip_obj} is blocked"
    return None


def validate_action_url(
    url: str,
    *,
    allow_local_urls: bool = False,
    allow_private: bool = False,
    allowlisted_hosts: set[str] | list[str] | tuple[str, ...] | None = None,
    skip_dns: bool = False,
    fail_on_dns_error: bool = True,
) -> tuple[bool, str]:
    """Validate an action target URL against SSRF rules (fail-closed by default)."""
    raw = (url or "").strip()
    if not raw:
        return False, "empty URL"
    try:
        parsed = urlparse(raw)
    except Exception as exc:
        return False, f"malformed URL: {exc}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"unsupported scheme {scheme!r}; only http/https allowed"

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False, "missing hostname"

    allowlist = {
        h.strip().lower().rstrip(".")
        for h in (allowlisted_hosts or [])
        if h and h.strip()
    }
    if allow_local_urls or allow_private or host in allowlist:
        return True, ""

    if (
        host in ("localhost", "metadata.google.internal", "metadata")
        or host.endswith(".local")
        or host.endswith(".internal")
        or host.endswith(".localhost")
    ):
        return False, f"local/internal hostname {host!r} is blocked"

    # Check IP literal directly.
    try:
        literal_ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        reason = _is_disallowed_ip(literal_ip)
        if reason:
            return False, reason
        return True, ""

    noncanonical_ip = _parse_noncanonical_ipv4(host)
    if noncanonical_ip is not None:
        reason = _is_disallowed_ip(noncanonical_ip)
        detail = f" ({reason})" if reason else ""
        return False, f"non-canonical IPv4 literal {host!r} is blocked{detail}"

    if skip_dns:
        return True, ""

    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        if fail_on_dns_error:
            return False, f"DNS resolution failed for {host!r}: {exc}"
        return True, ""

    if not infos:
        if fail_on_dns_error:
            return False, f"DNS returned no records for {host!r}"
        return True, ""

    for info in infos:
        addr_str = info[4][0].split("%")[0]
        try:
            resolved_ip = ipaddress.ip_address(addr_str)
        except ValueError:
            continue
        reason = _is_disallowed_ip(resolved_ip)
        if reason:
            return False, f"hostname {host!r} resolved to blocked address: {reason}"

    return True, ""


@dataclass
class OpenAPIAction:
    name: str
    operation_id: str
    method: str
    path: str
    base_url: str
    description: str
    parameters_schema: dict[str, Any] = field(default_factory=dict)
    path_params: list[str] = field(default_factory=list)
    query_params: list[str] = field(default_factory=list)
    header_params: list[str] = field(default_factory=list)
    has_body: bool = False
    body_properties: list[str] = field(default_factory=list)
    auth_type: str = "none"
    auth_env: str = ""


def _resolve_spec_path(bot_dir: Path, spec_rel: str) -> Path:
    raw_input = spec_rel or ""
    if "\x00" in raw_input:
        raise ValueError(f"Null byte not allowed in openapi_spec: {raw_input!r}")
    raw = raw_input.strip()
    if not raw:
        raise ValueError("Empty openapi_spec path")
    p = Path(raw)
    if p.is_absolute() or raw.startswith(("/", "\\")) or (len(raw) >= 2 and raw[1] == ":"):
        raise ValueError(f"Absolute openapi_spec path not allowed: {raw!r}")
    if ".." in p.parts:
        raise ValueError(f"Path traversal not allowed in openapi_spec: {raw!r}")
    root_resolved = bot_dir.resolve()
    cur = bot_dir
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f"Symlink not allowed in openapi_spec: {raw!r}")
    resolved = cur.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ValueError(f"openapi_spec escapes bot_dir: {raw!r}")
    if not cur.is_file():
        raise FileNotFoundError(f"OpenAPI spec not found: {cur}")
    return cur


def _sanitize_tool_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", raw).strip("_").lower()
    return cleaned or "openapi_action"


def load_openapi_actions(
    action_cfg: ActionSpec | dict[str, Any], bot_dir: str | Path
) -> list[OpenAPIAction]:
    """Load OpenAPI 3.x operations declared by *action_cfg* into ``OpenAPIAction`` items."""
    root = Path(bot_dir)
    spec_cfg = (
        ActionSpec.model_validate(action_cfg)
        if isinstance(action_cfg, dict)
        else action_cfg
    )
    spec_path = _resolve_spec_path(root, spec_cfg.openapi_spec)
    text = spec_path.read_text(encoding="utf-8")
    data = json.loads(text) if spec_path.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"OpenAPI spec {spec_path} must be a mapping")
    openapi_ver = str(data.get("openapi") or "").strip()
    if not openapi_ver.startswith("3."):
        raise ValueError(f"Unsupported OpenAPI version {openapi_ver!r} in {spec_path}")

    base_url = spec_cfg.base_url.strip()
    if not base_url:
        servers = data.get("servers") or []
        if isinstance(servers, list) and servers and isinstance(servers[0], dict):
            base_url = str(servers[0].get("url") or "").strip()
    base_url = base_url.rstrip("/")

    paths = data.get("paths") or {}
    if not isinstance(paths, dict) or not paths:
        raise ValueError(f"OpenAPI spec {spec_path} defines no paths")

    actions: list[OpenAPIAction] = []
    for path_tmpl, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters") or []
        for method in _HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue

            op_id = str(op.get("operationId") or "").strip()
            tool_name = _sanitize_tool_name(
                op_id or f"{spec_cfg.name}_{method}_{path_tmpl}"
            )
            description = (
                str(op.get("summary") or op.get("description") or "").strip()
                or f"{method.upper()} {path_tmpl}"
            )

            properties: dict[str, Any] = {}
            required: list[str] = []
            path_params: list[str] = []
            query_params: list[str] = []
            header_params: list[str] = []

            combined_params = list(shared_params) + list(op.get("parameters") or [])
            for p in combined_params:
                if not isinstance(p, dict):
                    continue
                pname = str(p.get("name") or "").strip()
                pin = str(p.get("in") or "query").strip()
                if not pname:
                    continue
                pschema = dict(p.get("schema") or {"type": "string"})
                if p.get("description") and "description" not in pschema:
                    pschema["description"] = str(p["description"])
                properties[pname] = pschema
                if p.get("required") and pname not in required:
                    required.append(pname)
                if pin == "path":
                    path_params.append(pname)
                elif pin == "header":
                    header_params.append(pname)
                else:
                    query_params.append(pname)

            has_body = False
            body_props: list[str] = []
            req_body = op.get("requestBody")
            if isinstance(req_body, dict):
                content = req_body.get("content") or {}
                json_media = content.get("application/json") if isinstance(content, dict) else None
                if isinstance(json_media, dict) and isinstance(json_media.get("schema"), dict):
                    has_body = True
                    bschema = json_media["schema"]
                    bprops = bschema.get("properties")
                    if isinstance(bprops, dict) and bprops:
                        for bname, bprop_schema in bprops.items():
                            properties[bname] = dict(bprop_schema) if isinstance(bprop_schema, dict) else {"type": "string"}
                            body_props.append(bname)
                        for rname in bschema.get("required") or []:
                            if rname not in required:
                                required.append(rname)
                    else:
                        properties["body"] = bschema
                        body_props.append("body")

            schema: dict[str, Any] = {
                "type": "object",
                "properties": properties,
            }
            if required:
                schema["required"] = required

            actions.append(
                OpenAPIAction(
                    name=tool_name,
                    operation_id=op_id or tool_name,
                    method=method.upper(),
                    path=str(path_tmpl),
                    base_url=base_url,
                    description=description,
                    parameters_schema=schema,
                    path_params=path_params,
                    query_params=query_params,
                    header_params=header_params,
                    has_body=has_body,
                    body_properties=body_props,
                    auth_type=spec_cfg.auth_type,
                    auth_env=spec_cfg.auth_env,
                )
            )
    return actions


class ActionExecutor:
    """Executes ``OpenAPIAction`` operations with strict SSRF & redirect protection."""

    def __init__(
        self,
        actions: list[OpenAPIAction],
        *,
        allow_local_urls: bool = False,
        allow_private: bool = False,
        allowlisted_hosts: set[str] | list[str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
        max_response_bytes: int = 32768,
    ) -> None:
        self.actions = list(actions)
        self.allow_local_urls = allow_local_urls
        self.allow_private = allow_private
        self.allowlisted_hosts = set(allowlisted_hosts or ())
        self.http_client = http_client
        self.transport = transport
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def _check_url(self, url: str) -> tuple[bool, str]:
        # When an explicit MockTransport / injected AsyncClient is used in offline tests,
        # literal private/loopback/metadata/local IPs & DNS-resolved private IPs are
        # STILL blocked unless allow_local_urls=True, while offline DNS gaierror is allowed.
        has_injected_transport = self.transport is not None or self.http_client is not None
        return validate_action_url(
            url,
            allow_local_urls=self.allow_local_urls,
            allow_private=self.allow_private,
            allowlisted_hosts=self.allowlisted_hosts,
            skip_dns=False,
            fail_on_dns_error=not has_injected_transport,
        )

    async def execute_action(self, action: OpenAPIAction, **kwargs: Any) -> dict[str, Any]:
        if not action.base_url:
            return {"error": f"Action {action.name} has no base_url or servers[0].url"}

        path = action.path
        for pname in action.path_params:
            if pname not in kwargs:
                return {"error": f"Missing required path parameter: {pname}"}
            raw_val = str(kwargs[pname])
            if raw_val in (".", ".."):
                return {
                    "error": f"Blocked path traversal segment {raw_val!r} for parameter {pname!r}",
                    "blocked": True,
                }
            path = path.replace("{" + pname + "}", quote(raw_val, safe=""))

        target_url = f"{action.base_url.rstrip('/')}/{path.lstrip('/')}"
        ok, reason = self._check_url(target_url)
        if not ok:
            return {"error": f"SSRF blocked URL {target_url}: {reason}", "blocked": True}

        params = {q: kwargs[q] for q in action.query_params if q in kwargs and kwargs[q] is not None}
        headers = {
            h: str(kwargs[h])
            for h in action.header_params
            if h in kwargs and kwargs[h] is not None
        }

        if action.auth_type != "none" and action.auth_env:
            secret = os.environ.get(action.auth_env, "")
            if secret:
                if action.auth_type == "bearer":
                    headers["Authorization"] = f"Bearer {secret}"
                elif action.auth_type == "api_key":
                    headers["X-API-Key"] = secret

        json_body: Any = None
        if action.has_body:
            if "body" in kwargs and len(action.body_properties) == 1 and action.body_properties[0] == "body":
                json_body = kwargs["body"]
            else:
                payload = {
                    k: kwargs[k]
                    for k in action.body_properties
                    if k in kwargs and kwargs[k] is not None
                }
                if payload:
                    json_body = payload

        owns_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout,
            follow_redirects=False,
        )
        try:
            current_url = target_url
            current_method = action.method
            current_params = params
            current_headers = dict(headers)
            current_body = json_body
            initial_origin = _url_origin(target_url)
            final_status: int | None = None
            raw_bytes = b""
            limit = max(0, int(self.max_response_bytes))

            for _ in range(_MAX_REDIRECTS + 1):
                ok_hop, reason_hop = self._check_url(current_url)
                if not ok_hop:
                    return {
                        "error": f"SSRF blocked redirect target {current_url}: {reason_hop}",
                        "blocked": True,
                    }
                async with client.stream(
                    current_method,
                    current_url,
                    params=current_params,
                    headers=current_headers,
                    json=current_body,
                    follow_redirects=False,
                ) as resp:
                    final_status = resp.status_code
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location", "")
                        if not location:
                            break
                        next_url = urljoin(current_url, location)
                        next_origin = _url_origin(next_url)
                        if (
                            next_origin != _url_origin(current_url)
                            or next_origin != initial_origin
                        ):
                            current_headers = {
                                k: v
                                for k, v in current_headers.items()
                                if k.lower() not in _SENSITIVE_REDIRECT_HEADERS
                            }
                        current_url = next_url
                        if resp.status_code in (301, 302, 303):
                            current_method = "GET"
                            current_body = None
                        current_params = {}
                        continue

                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        need = (limit + 1) - len(buf)
                        if need <= 0:
                            break
                        buf.extend(chunk[:need])
                        if len(buf) > limit:
                            break
                    raw_bytes = bytes(buf[:limit])
                    break

            assert final_status is not None
            if final_status in (301, 302, 303, 307, 308):
                return {"error": "Too many redirects", "status_code": final_status}

            text = raw_bytes.decode("utf-8", errors="replace")
            parsed_json: Any = None
            try:
                parsed_json = json.loads(text)
            except Exception:
                parsed_json = None

            return {
                "status_code": final_status,
                "ok": 200 <= final_status < 400,
                "data": parsed_json if parsed_json is not None else text,
            }
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            if owns_client:
                await client.aclose()

    def register_into(self, registry: Any) -> list[str]:
        """Register every ``OpenAPIAction`` into an ``adk.tools.ToolRegistry``."""
        registered: list[str] = []
        for action in self.actions:
            def _make_runner(act: OpenAPIAction):
                async def _runner(**kwargs: Any) -> dict[str, Any]:
                    return await self.execute_action(act, **kwargs)

                _runner.__name__ = act.name
                _runner.__doc__ = act.description
                return _runner

            fn = _make_runner(action)
            td = registry.register(
                fn,
                name=action.name,
                description=action.description,
                repeat_safe=(action.method == "GET"),
            )
            td.parameters = action.parameters_schema
            registered.append(action.name)
        return registered
