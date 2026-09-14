"""arc-brainpack tools — self-contained, guarded client for the AitherWorldModel
Contribution Gateway, plus a VENDORED random-policy ARC player.

This module is fully self-contained: `pip install awdk[arc]` gives a working
contribution experience with NO clone of any other repo. The gateway client
(register / observe / status / leaderboard) needs only httpx (a core dep); the
ARC play path lazily imports `arcengine` (the [arc] extra) for ACTION/STATE
semantics and drives the public ARC-AGI-3 REST API (three.arcprize.org) directly.

Design contract:
  * GUARDED. Every tool returns a string / JSON-string the agent can relay and
    NEVER raises into the caller's loop. A dead gateway, a missing token, or a
    missing arcengine extra produces a readable message, not a crash.
  * Gateway HTTPS verifies via adk._tls.tls_verify(): trusts the fleet
    internal CA bundle when present (or plain HTTP on a solo box). ARC-AGI-3
    (a public site) is verified normally.
  * Bearer auth on gateway calls. The token is minted by arc_register (ACTA
    wallet -> POST /v1/register), persisted to ~/.aitheros/arc_contrib_token.json,
    and mirrored into WM_CONTRIB_TOKEN for this session.

Env:
  WM_GATEWAY_URL    gateway base URL   (default https://arc.aitherium.com/contribute)
  WM_CONTRIB_TOKEN  Bearer token       (else read from the persisted token file)
  ARC_API_KEY       required by arc_contribute to play real ARC games
  ARC_BASE_URL      ARC-AGI-3 API base (default https://three.arcprize.org)
  ARC_ACTA_URL      ACTA wallet mint/exchange base (optional; skipped if unset)
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.request
from typing import Any, List, Optional
from pathlib import Path

# The public Contribution Gateway is a PATH on the playground host, not a host of its
# own: the Cloudflare tunnel's ingress is remotely managed, and a third-level name
# (contribute.arc.aitherium.com) is outside Universal SSL's one-label *.aitherium.com
# wildcard, so it would have no valid certificate. arc.aitherium.com/contribute/* is
# already routed and already trusted, and proxies straight through to the gateway.
DEFAULT_GATEWAY = "https://arc.aitherium.com/contribute"
# The public ARC-AGI-3 game API. Override with ARC_BASE_URL for a mirror.
DEFAULT_ARC_BASE = "https://three.arcprize.org"
TOKEN_FILE = Path.home() / ".aitheros" / "arc_contrib_token.json"
_USER_AGENT = "arc-brainpack/1.0 (+https://arc.aitherium.com)"


# --------------------------------------------------------------------------- #
# config / persistence
# --------------------------------------------------------------------------- #
def _gateway() -> str:
    return (os.environ.get("WM_GATEWAY_URL") or DEFAULT_GATEWAY).rstrip("/")


def _wallet_origin() -> str:
    """Origin that serves POST /api/wallet/register (the free-wallet mint).

    The gateway is a PATH under the playground ("https://host/contribute"), and the
    wallet mint is a sibling on the same origin ("https://host/api/wallet/register") --
    so strip the trailing /contribute rather than assuming a separate host. For an
    own-stack contributor pointing straight at their local gateway there is no
    playground alongside it, so fall back to the public origin, which is where a free
    wallet actually comes from."""
    gw = _gateway()
    if gw.endswith("/contribute"):
        return gw[: -len("/contribute")]
    if "127.0.0.1" in gw or "localhost" in gw:
        return "https://arc.aitherium.com"
    return gw


def _load_persisted() -> dict:
    try:
        if TOKEN_FILE.is_file():
            return json.loads(TOKEN_FILE.read_text(encoding="utf-8") or "{}") or {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _token() -> str:
    """Effective contributor token: WM_CONTRIB_TOKEN wins, else the persisted one."""
    tok = (os.environ.get("WM_CONTRIB_TOKEN") or "").strip()
    if tok:
        return tok
    return str(_load_persisted().get("token") or "").strip()


def _persist(record: dict) -> None:
    """Persist the token record and mirror it into the process env (WM_CONTRIB_TOKEN)
    so the play path picks it up this session without a re-register."""
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    tok = str(record.get("token") or "").strip()
    if tok:
        os.environ["WM_CONTRIB_TOKEN"] = tok


# --------------------------------------------------------------------------- #
# guarded HTTP — httpx if present (tls_verify), else stdlib urllib fallback
# --------------------------------------------------------------------------- #
def _request(method: str, url: str, *, body: Optional[dict] = None,
             headers: Optional[dict] = None, timeout: float = 20.0) -> dict:
    """Return {"ok", "status", "json"|"text"|"error"}. Never raises."""
    # The User-Agent is NOT cosmetic. The public gateway rides in behind Cloudflare,
    # whose bot rules hard-block the default stdlib signature ("Python-urllib/3.x")
    # with a 403/1010 before the request ever reaches us — so the urllib fallback
    # below would fail for every self-service contributor on the internet. Naming
    # ourselves gets us through (httpx/requests UAs pass; Python-urllib does not).
    hdrs = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    # Preferred path: httpx. TLS verify routes through adk._tls.tls_verify():
    # verified by default, trusts the AitherNet internal CA bundle when present
    # (fleet-internal gateways), and only AITHER_TLS_VERIFY=false disables it.
    try:
        import httpx
        from adk._tls import tls_verify
        with httpx.Client(verify=tls_verify(), timeout=timeout) as c:
            r = c.request(method, url, json=body, headers=hdrs)
            out: dict = {"ok": 200 <= r.status_code < 300, "status": r.status_code}
            try:
                out["json"] = r.json()
            except Exception:  # noqa: BLE001
                out["text"] = (r.text or "")[:2000]
            return out
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 0, "error": str(e)}
    # Fallback: stdlib urllib. Same TLS policy as the httpx path: verified by
    # default, internal CA bundle when present, disabled ONLY via the explicit
    # AITHER_TLS_VERIFY=false escape hatch (mirrors adk._tls.tls_verify()).
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        from adk._tls import tls_verify
        _v = tls_verify()
        if _v is False:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif isinstance(_v, str):
            ctx = ssl.create_default_context(cafile=_v)
        else:
            ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", "replace")
            out = {"ok": True, "status": getattr(resp, "status", 200)}
            try:
                out["json"] = json.loads(raw or "{}")
            except Exception:  # noqa: BLE001
                out["text"] = raw[:2000]
            return out
    except urllib.error.HTTPError as e:  # noqa: PERF203
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "status": e.code, "text": raw[:2000]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 0, "error": str(e)}


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
def arc_register(handle: str = "") -> str:
    """Enroll as a world-model contributor: mint/exchange an ACTA wallet (if
    ARC_ACTA_URL is set), then POST it to the Contribution Gateway /v1/register to
    receive a contributor Bearer token, and PERSIST that token locally
    (~/.aitheros/arc_contrib_token.json + WM_CONTRIB_TOKEN) so every later
    arc_contribute / arc_status call is authenticated. Idempotent-ish: if a token
    is already persisted it is returned without re-registering.

    Args:
        handle: optional public display name for the leaderboard.
    """
    existing = _token()
    if existing:
        rec = _load_persisted()
        return json.dumps({
            "already_enrolled": True,
            "contributor_id": rec.get("contributor_id"),
            "gateway": _gateway(),
            "token_file": str(TOKEN_FILE),
        })

    # 1) The AitherOS wallet key. It is the IDENTITY ANCHOR the gateway verifies before it
    #    mints anything, so this step is REQUIRED, not best-effort — without a wallet the
    #    gateway 401s. Order: an env key the user already has, else self-mint a free one.
    #    The mint is the SAME public endpoint the playground's own wallet button calls
    #    (POST /api/wallet/register {name,email} -> {api_key, tokens:1000}); it is NOT
    #    "<acta>/v1/wallet", which does not exist.
    wallet_key = (os.environ.get("AITHER_API_KEY") or "").strip()
    minted = False
    if not wallet_key:
        base = (os.environ.get("ARC_ACTA_URL") or "").strip().rstrip("/") or _wallet_origin()
        who = handle or f"arc-brainpack-{os.urandom(3).hex()}"
        w = _request("POST", f"{base}/api/wallet/register",
                     body={"name": who, "email": f"{who}@contributors.arc.aitherium.com"})
        wj = w.get("json") if isinstance(w.get("json"), dict) else {}
        wallet_key = str(wj.get("api_key") or "").strip()
        minted = bool(wallet_key)
        if not wallet_key:
            return json.dumps({
                "registered": False,
                "step": "wallet",
                "error": wj.get("error") or w.get("error") or w.get("text")
                or "could not mint a free AitherOS wallet; set AITHER_API_KEY to an "
                   "existing aither_sk_live_* key and retry",
            })

    # 2) exchange the wallet for a contributor token (idempotent server-side: the same
    #    wallet always gets the same token back)
    r = _request("POST", f"{_gateway()}/v1/register",
                 body={"wallet": wallet_key, "handle": handle or None})
    if not r.get("ok"):
        return json.dumps({
            "registered": False,
            "gateway": _gateway(),
            "status": r.get("status"),
            "error": r.get("error") or r.get("text")
            or "gateway did not accept /v1/register",
        })
    d = r.get("json") or {}
    token = str(d.get("token") or d.get("contrib_token") or "").strip()
    if not token:
        return json.dumps({"registered": False, "gateway": _gateway(),
                           "error": "register returned no token", "raw": d})
    record = {
        "token": token,
        "contributor_id": d.get("contributor_id") or d.get("id") or handle or None,
        "gateway": _gateway(),
        # the wallet KEY is a credential: persist it only so a re-register is idempotent,
        # never echo it back to the agent/chat.
        "wallet": wallet_key,
    }
    _persist(record)
    return json.dumps({
        "registered": True,
        "contributor_id": record["contributor_id"],
        "gateway": _gateway(),
        "wallet_minted": minted,
        "existing": bool(d.get("existing")),
        "daily_quota": d.get("daily_quota"),
        "token_file": str(TOKEN_FILE),
        "note": "token persisted + exported as WM_CONTRIB_TOKEN for this session. "
                "Next: arc_contribute(games='ls20', n=200)",
    })


def arc_contribute(games: str, n: int = 200) -> str:
    """Play real ARC-AGI-3 games with a random policy and submit every
    (state, action, next_state) transition to the Contribution Gateway. Requires a
    contributor token (run arc_register first) and ARC_API_KEY to reach the ARC API.

    Args:
        games: comma-separated ARC game ids or prefixes (e.g. "ls20,vc33").
        n: max actions per game (default 200).
    """
    if not _token():
        return json.dumps({"ok": False,
                           "error": "not enrolled — run arc_register first (or set "
                                    "WM_CONTRIB_TOKEN)."})
    if not (os.environ.get("ARC_API_KEY") or "").strip():
        return json.dumps({"ok": False,
                           "error": "ARC_API_KEY is unset — needed to play real ARC "
                                    "games. Set it and retry."})
    game_ids = [g.strip() for g in str(games or "").split(",") if g.strip()]
    if not game_ids:
        return json.dumps({"ok": False, "error": "no game ids given"})

    try:
        res = contribute_random(game_ids, n=int(n), base_url=_gateway(),
                                token=_token())
        return json.dumps(res)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"contribute_random failed: {e}"})


def arc_enroll(game_id: str, episodes: int = 6, budget: int = 30,
               epsilon: float = 0.3, submit: bool = True, name: str = "") -> str:
    """Enroll a real ARC game through env_enroll (B1, BYO-cognition program).

    Validates the ArcGatewayAdapter, runs the learn-safely explore loop
    (surprise-driven, hard budget cap), records every transition into the LOCAL
    world model (in-process when AITHER_OFFLINE=1), AND submits each transition to
    the gateway — so a BYO agent plays a real game with its own policy, teaches
    the shared model, and appears on the leaderboard, in one call. Writes a
    sandbox proof (env_enroll) when the world model demonstrably learns the game.

    Requires ARC_API_KEY to reach the real game API. Contribution additionally
    wants a token (arc_register first); without one the game is still played and
    learned locally — only the gateway half is skipped.

    Args:
        game_id: ARC game id or prefix (e.g. "ls20", "vc33").
        episodes: exploration episodes (each starts a fresh game instance).
        budget: hard step cap per episode — the safety budget.
        epsilon: exploration probability for the epsilon-greedy loop.
        submit: when False, play + learn locally without contributing.
        name: proof registry key (default "arc:<game_id>").
    """
    if not (os.environ.get("ARC_API_KEY") or "").strip():
        return json.dumps({"ok": False,
                           "error": "ARC_API_KEY is unset — needed to reach a real "
                                    "ARC game. Set it and retry."})
    try:
        from adk.packs.world_model.env_enroll import env_enroll as _enroll
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"env_enroll unavailable: {exc}"})
    try:
        out = _enroll(
            "adk.packs.arc_world.adapter:ArcGatewayAdapter",
            adapter_kwargs={"game_id": str(game_id), "submit": bool(submit)},
            episodes=int(episodes), budget=int(budget), epsilon=float(epsilon),
            name=name or f"arc:{game_id}",
        )
        return json.dumps(out)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"arc_enroll failed: {exc}"})


def arc_status() -> str:
    """This contributor's server-side status at the gateway (accepted count,
    quarantine path) as a JSON string. Reports 'not enrolled' if no token."""
    tok = _token()
    if not tok:
        return json.dumps({"enrolled": False,
                           "hint": "run arc_register (or set WM_CONTRIB_TOKEN)."})
    r = _request("GET", f"{_gateway()}/v1/status",
                 headers={"Authorization": f"Bearer {tok}"})
    if not r.get("ok"):
        return json.dumps({"enrolled": True, "gateway": _gateway(),
                           "status": r.get("status"),
                           "error": r.get("error") or r.get("text") or "gateway error"})
    return json.dumps({"enrolled": True, "gateway": _gateway(), **(r.get("json") or {})})


def arc_leaderboard(limit: int = 20) -> str:
    """Top world-model contributors (accepted-transition ranking) from the gateway
    /v1/leaderboard, as a JSON string.

    Args:
        limit: max rows to return (default 20).
    """
    tok = _token()
    hdrs = {"Authorization": f"Bearer {tok}"} if tok else None
    r = _request("GET", f"{_gateway()}/v1/leaderboard?limit={int(limit)}", headers=hdrs)
    if not r.get("ok"):
        return json.dumps({"gateway": _gateway(), "status": r.get("status"),
                           "error": r.get("error") or r.get("text")
                           or "leaderboard unavailable (endpoint may be staging-only)"})
    j = r.get("json")
    return json.dumps({"gateway": _gateway(), "leaderboard": j})


def arc_solo() -> str:
    """Print the ONE-COMMAND bootstrap to run your OWN world-model + contribution
    gateway stack locally (no fleet, no TLS, no secrets), using the committed
    docker-compose.standalone.yml. Everything binds to loopback only. Point
    arc_contribute at it with WM_GATEWAY_URL=http://127.0.0.1:8199."""
    return (
        "Run your OWN ARC world model + contribution gateway (solo, loopback-only):\n"
        "\n  # from the ARC world-model project (which ships the standalone stack):\n"
        "  docker compose -f docker-compose.standalone.yml up --build\n"
        "\nEndpoints (plain HTTP, loopback only):\n"
        "  world model : http://127.0.0.1:8197/health\n"
        "  gateway     : http://127.0.0.1:8199/health\n"
        "\nThen contribute to your own stack instead of the public one:\n"
        "  export WM_GATEWAY_URL=http://127.0.0.1:8199\n"
        "  # drop a token into <compose dir>/standalone-data/contrib/contrib_tokens.json\n"
        '  #   { \"<your-token>\": \"<your-name>\" }\n'
        "  export WM_CONTRIB_TOKEN=<your-token>\n"
        "  # then call arc_contribute(games=\"ls20\", n=200)\n"
        "\nNo GPU required (AITHER_WM_DEVICE=cpu by default). Nothing leaves your box."
    )


# --------------------------------------------------------------------------- #
# VENDORED ARC player — play real ARC-AGI-3 games with a uniform-random policy
# and submit every (grid, action, next_grid) transition to the gateway. This is
# self-contained: it drives the public ARC-AGI-3 REST API directly and depends on
# `arcengine` (the [arc] extra) only for ACTION/STATE semantics. No external repo.
# --------------------------------------------------------------------------- #
def _arc_base() -> str:
    return (os.environ.get("ARC_BASE_URL") or DEFAULT_ARC_BASE).rstrip("/")


class _ArcHttp:
    """Minimal persistent HTTP session for the ARC-AGI-3 REST API.

    Keeps cookies across reset/step: the ARC edge is sticky by cookie, so a fresh
    connection per call could land on a different backend and lose the session.
    Prefers httpx (a core dep); falls back to urllib with a shared cookie jar.
    ARC-AGI-3 is a public site, so TLS is verified normally (unlike the gateway,
    which may ride the fleet internal CA)."""

    def __init__(self, api_key: str, timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._hx = None
        self._opener = None
        try:
            import httpx
            self._hx = httpx.Client(timeout=timeout,
                                    headers={"User-Agent": _USER_AGENT})
        except Exception:  # noqa: BLE001 — httpx absent -> urllib + cookie jar
            import http.cookiejar
            jar = http.cookiejar.CookieJar()
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar))

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key, "Accept": "application/json",
                "Content-Type": "application/json", "User-Agent": _USER_AGENT}

    def post(self, url: str, body: dict) -> dict:
        if self._hx is not None:
            r = self._hx.post(url, json=body, headers=self._headers())
            r.raise_for_status()
            return r.json() or {}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(),
                                     method="POST")
        with self._opener.open(req, timeout=self.timeout) as resp:  # type: ignore[union-attr]
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")

    def get(self, url: str) -> Any:
        if self._hx is not None:
            r = self._hx.get(url, headers=self._headers())
            r.raise_for_status()
            return r.json()
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        with self._opener.open(req, timeout=self.timeout) as resp:  # type: ignore[union-attr]
            return json.loads(resp.read().decode("utf-8", "replace") or "null")

    def close(self) -> None:
        try:
            if self._hx is not None:
                self._hx.close()
        except Exception:  # noqa: BLE001
            pass


def _arc_list_games(http: "_ArcHttp", root: str) -> List[str]:
    """GET /api/games -> concrete game ids (as returned by the ARC API)."""
    data = http.get(f"{root}/api/games") or []
    out: List[str] = []
    for g in data:
        gid = g.get("game_id") if isinstance(g, dict) else g
        if gid:
            out.append(str(gid))
    return out


def _arc_open_scorecard(http: "_ArcHttp", root: str,
                        tags: Optional[List[str]]) -> Optional[str]:
    """POST /api/scorecard/open -> card_id. Best-effort: returns None on failure,
    since some deployments accept RESET without a card."""
    try:
        d = http.post(f"{root}/api/scorecard/open", {"tags": list(tags or ["arc-brainpack"])})
        cid = (d or {}).get("card_id")
        return str(cid) if cid else None
    except Exception:  # noqa: BLE001
        return None


def _submit_observe(base: str, tok: str, grid: Any, action_str: str,
                    next_grid: Any, game: str) -> tuple:
    """POST {base}/v1/observe with Bearer auth. Returns (submitted:bool, accepted:bool).
    Uses the pack's guarded _request helper (httpx tls_verify / urllib fallback)."""
    r = _request("POST", f"{base}/v1/observe",
                 body={"grid": grid, "action": action_str,
                       "next_grid": next_grid, "game": str(game)},
                 headers={"Authorization": f"Bearer {tok}"})
    j = r.get("json") if isinstance(r.get("json"), dict) else {}
    return True, bool(r.get("ok") and j.get("quarantined"))


def _play_one(http: "_ArcHttp", root: str, base: str, tok: str, game: str,
              card_id: Optional[str], n: int, GameAction, GameState, rng) -> tuple:
    """Play one game up to `n` actions with a uniform-random policy, submitting
    every non-RESET transition. Returns (submitted, accepted)."""
    submitted = accepted = 0
    reset_body: dict = {"game_id": game}
    if card_id:
        reset_body["card_id"] = card_id
    frame = http.post(f"{root}/api/cmd/RESET", reset_body)
    guid = frame.get("guid")

    for _ in range(max(1, n)):
        state = frame.get("state")
        grids = frame.get("frame") or []
        pre = grids[-1] if grids else None

        # A not-started / dead episode needs a RESET (a discontinuity we do NOT
        # submit as a transition). Otherwise pick a uniform-random legal action.
        if state in (GameState.NOT_PLAYED, GameState.GAME_OVER) or pre is None:
            aid, x, y = 0, None, None
        else:
            avail = [a for a in (frame.get("available_actions") or [])
                     if isinstance(a, int) and a != 0]
            if not avail:
                avail = [1, 2, 3, 4, 5, 6, 7]
            aid = rng.choice(avail)
            if GameAction.from_id(aid).is_complex():   # ACTION6 = click at (x,y)
                x, y = rng.randint(0, 63), rng.randint(0, 63)
            else:
                x = y = None

        action_name = "RESET" if aid == 0 else f"ACTION{aid}"
        step_body: dict = {"game_id": game, "guid": guid}
        if x is not None:
            step_body["x"] = x
        if y is not None:
            step_body["y"] = y
        nxt = http.post(f"{root}/api/cmd/{action_name}", step_body)
        guid = nxt.get("guid") or guid

        ngrids = nxt.get("frame") or []
        post = ngrids[-1] if ngrids else None
        # Submit only real transitions: skip RESET and env discontinuities so the
        # model never learns a bogus (pre -> post) pairing across a reset boundary.
        if aid != 0 and pre is not None and post is not None and not nxt.get("full_reset"):
            action_str = f"ACTION{aid}({x},{y})" if x is not None else f"ACTION{aid}"
            sub, acc = _submit_observe(base, tok, pre, action_str, post, game)
            submitted += 1 if sub else 0
            accepted += 1 if acc else 0

        frame = nxt
        if nxt.get("state") == GameState.WIN:   # episode solved -> stop early
            break

    return submitted, accepted


def contribute_random(game_ids: List[str], n: int = 200,
                      base_url: Optional[str] = None, token: Optional[str] = None,
                      tags: Optional[List[str]] = None) -> dict:
    """Play each game in `game_ids` (exact ids or prefixes) for up to `n` actions
    with a uniform-random policy and submit every (grid, action, next_grid)
    transition to the Contribution Gateway's /v1/observe.

    Returns {"ok", "games", "submitted", "accepted", ...}. Bails early (submitting
    nothing) if arcengine is missing, the token is unset/invalid, or ARC_API_KEY is
    absent — so a bad token never wastes ARC quota.
    """
    # arcengine drives ACTION/STATE semantics; it is the [arc] extra, imported lazily
    # so `register()` and the gateway tools work in a minimal (httpx-only) env.
    try:
        from arcengine import GameAction, GameState  # type: ignore
    except Exception:  # noqa: BLE001
        return {"ok": False, "submitted": 0, "accepted": 0, "games": [],
                "error": "ARC gameplay needs the arc extra: pip install 'awdk[arc]'"}

    base = (base_url or _gateway()).rstrip("/")
    tok = (token or _token()).strip()
    if not tok:
        return {"ok": False, "submitted": 0, "accepted": 0, "games": [],
                "error": "not enrolled — run arc_register first (or set WM_CONTRIB_TOKEN)."}
    api_key = (os.environ.get("ARC_API_KEY") or "").strip()
    if not api_key:
        return {"ok": False, "submitted": 0, "accepted": 0, "games": [],
                "error": "ARC_API_KEY is unset — needed to play real ARC games."}

    # Prove the token before spending ARC quota (/v1/status is auth'd, not rate-limited).
    st = _request("GET", f"{base}/v1/status", headers={"Authorization": f"Bearer {tok}"})
    if not st.get("ok"):
        return {"ok": False, "submitted": 0, "accepted": 0, "games": [],
                "gateway": base, "status": st.get("status"),
                "error": "gateway rejected the contributor token — run arc_register "
                         "(or set WM_CONTRIB_TOKEN)."}

    import random
    rng = random.Random()
    root = _arc_base()
    http = _ArcHttp(api_key)
    submitted = accepted = 0
    played: List[str] = []
    card_id: Optional[str] = None
    try:
        try:
            all_games = _arc_list_games(http, root)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "submitted": 0, "accepted": 0, "games": [],
                    "error": f"could not list ARC games (check ARC_API_KEY): {e}"}
        want = [str(g).strip() for g in (game_ids or []) if str(g).strip()]
        games = [g for g in all_games
                 if g in want or any(g.startswith(p) for p in want)]
        if not games:
            return {"ok": True, "submitted": 0, "accepted": 0, "games": [],
                    "note": f"none of {want} matched the {len(all_games)} available games"}

        card_id = _arc_open_scorecard(http, root, tags)
        for game in games:
            try:
                sub, acc = _play_one(http, root, base, tok, game, card_id, int(n),
                                     GameAction, GameState, rng)
                submitted += sub
                accepted += acc
                played.append(game)
            except Exception:  # noqa: BLE001 — one bad game must not end the run
                continue
        if card_id:
            try:
                http.post(f"{root}/api/scorecard/close", {"card_id": card_id})
            except Exception:  # noqa: BLE001
                pass
    finally:
        http.close()

    return {"ok": True, "games": played, "submitted": submitted,
            "accepted": accepted, "gateway": base, "scorecard": card_id}
