"""Illustrate the campaign through Media Forge, without widening who-knows-what.

`campaign_illustrate` turns a character, or a scene drawn from campaign memory,
into images — and keeps three promises the rest of this pack already keeps:

1. **The picture only shows what the viewpoint knows.** The prompt is built
   from campaign memory read at the viewpoint's scope — the character's own
   notes plus world state, exactly what `campaign_recall` returns — and from
   NOTHING else. The caller's `scene` is a QUERY that selects among those
   notes; it is never pasted into the prompt. So a scene that is "about"
   another character's secret either matches nothing the viewpoint knows (and
   is refused, before any request leaves the machine) or is drawn from the
   facts the viewpoint does know. The boundary is structural, not a filter
   applied to a flat list.

2. **One lane: the curated one.** Media Forge serves two surfaces. `/ops` and
   `/op/{name}` are the AitherSafety-tiered twins — restricted ops filtered,
   refused at execution, prompts sanitized. `/api/*` is the owner's own,
   ungated. This module only ever builds `<base>/op/<name>`, refuses a base
   that would turn that into `/api/op/<name>`, and resolves the base from the
   SAME consent record the local Studio tab's remote lane uses
   (`~/.aither/studio-remote.json`, written only by the explicit consent route
   in `adk.local_routes`). No consent, no request. The curated `POST
   /op/{name}` is synchronous (`{ok, head_ids, images, node_outputs}`); a
   refusal is HTTP 200 `{ok: false, error}` and is surfaced in Media Forge's
   own words, never re-wrapped as success.

3. **The character stays the same character.** The first illustration of a
   character renders a portrait (`txt2img`) and stores its gallery id ON THE
   CHARACTER'S CARD — a `card-ref` record at the character's own scope, which
   `campaign_export_card` hands back and `campaign_import_card` restores. Every
   later call composes from that reference (`compose`, `face_ref` = the stored
   id) instead of re-rolling the character from prose, which is the drift the
   character-forge skill measured. `portrait_transfer` (identity swap) and
   `identity_score` are deliberately NOT used: the first is refused on the
   customer path by name, the second is not priced there, and a tool that only
   works on the owner's box is not the customer lane.

Every outcome is a dict; nothing raises into the chat loop. An illustration
that exists is recorded as a campaign note at the VIEWPOINT's scope — a
picture of a secret is itself a secret.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

from adk.packs.gobbonet.campaign_memory import WORLD, _norm_character

#: The kind and key the reference record is stored under, at the character's
#: own scope. A fixed key means choosing a new reference REPLACES the old one.
REF_KIND = "card-ref"
REF_KEY = "card-ref"
_REF_PREFIX = "card-ref: "

#: Media Forge op names are one lowercase path segment. Anything else could
#: walk out of /op/ onto the owner's surface.
_OP_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")

#: `compose` caps count at 4; asking for more is a refusal we can avoid.
_MAX_COUNT = 4

#: The curated op is synchronous and a GPU render is minutes on a shared card.
_TIMEOUT_S = 900.0

#: The consent record the Studio tab's remote lane writes (adk.local_routes).
_CONSENT_FILE = "studio-remote.json"

#: Notes that are card bookkeeping, not facts about the world. Never prompt text.
_BOOKKEEPING_PREFIXES = ("card-meta: ", _REF_PREFIX)

#: Card prose fields that describe how a character LOOKS, in priority order.
_APPEARANCE_FIELDS = ("description", "appearance")


# ── the lane ──────────────────────────────────────────────────────────────────

def _consented_base() -> Optional[str]:
    """The Media Forge URL the person consented to, or None. Same file, same
    validity rule as `adk.local_routes._studio_consent` — absent, unreadable or
    malformed is OFF."""
    home = Path(os.environ.get("AITHER_HOME") or (Path.home() / ".aither"))
    try:
        rec = json.loads((home / _CONSENT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    url = str(rec.get("url") or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")) or not urlsplit(url).hostname:
        return None
    return url


class StudioLane:
    """Media Forge's CURATED op surface. Never raises; every answer is a dict.

    `base` is the curated root — a Media Forge (`http://127.0.0.1:8200`) or a
    host proxy that forwards to its curated twins (`https://host/api/studio`).
    Requests go to `<base>/op/<name>` and nowhere else.
    """

    def __init__(self, base: Optional[str] = None, *, key: Optional[str] = None,
                 timeout: float = _TIMEOUT_S) -> None:
        self.base = (base if base is not None else _consented_base() or "").rstrip("/")
        self.key = key if key is not None else os.environ.get("AITHER_STUDIO_KEY", "")
        self.timeout = timeout

    def unavailable(self) -> Optional[Dict[str, Any]]:
        """None when the lane can be used, else the refusal envelope."""
        if not self.base:
            return {"ok": False, "error_code": "not_connected",
                    "error": "no Media Forge is connected for illustration",
                    "fix": "connect your own Media Forge in the Studio tab and confirm "
                           "what will be sent (POST /api/local/studio/remote/consent)"}
        path = urlsplit(self.base).path.rstrip("/")
        if path == "/api" or path.endswith("/api"):
            # <base>/op/<name> would be /api/op/<name>: the owner's ungated surface.
            return {"ok": False, "error_code": "owner_surface",
                    "error": f"{self.base} is Media Forge's owner-private /api surface; "
                             "illustration only uses the curated /op/{name} twins"}
        return None

    def media_url(self, path: str) -> str:
        """A result's `/media/...` path, made reachable through the same base."""
        if not path:
            return ""
        if re.match(r"^(https?:)?//", path) or path.startswith("data:"):
            return path
        return f"{self.base}/{path.lstrip('/')}"

    def run_op(self, name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """POST <base>/op/<name>. Media Forge's own result, or a classified error:

        not_connected / owner_surface  the lane is not usable (nothing was sent)
        unreachable                    the request did not complete
        unknown_op                     404 — this Media Forge does not serve it
        refused                        200 {ok:false} or 401/403 — its words, verbatim
        bad_response                   a body that is not a JSON object
        """
        blocked = self.unavailable()
        if blocked:
            return blocked
        if not _OP_NAME.fullmatch(name or ""):
            return {"ok": False, "error_code": "unknown_op", "error": f"invalid op name {name!r}"}
        import httpx

        headers = {"content-type": "application/json"}
        if self.key:
            headers["authorization"] = f"Bearer {self.key}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(f"{self.base}/op/{name}", json=params, headers=headers)
        except httpx.HTTPError as exc:
            return {"ok": False, "error_code": "unreachable",
                    "error": f"Media Forge at {self.base} did not answer "
                             f"({type(exc).__name__}); nothing was generated"}
        if r.status_code == 404:
            return {"ok": False, "error_code": "unknown_op",
                    "error": f"Media Forge does not serve op {name!r}"}
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code in (401, 403):
            detail = data.get("error") if isinstance(data, dict) else ""
            return {"ok": False, "error_code": "refused",
                    "error": f"Media Forge refused {name}: {detail or f'HTTP {r.status_code}'}"}
        if r.status_code >= 400:
            return {"ok": False, "error_code": "unreachable",
                    "error": f"Media Forge answered HTTP {r.status_code} for {name}"}
        if not isinstance(data, dict):
            return {"ok": False, "error_code": "bad_response",
                    "error": f"Media Forge answered {name} with a body that is not an object"}
        if data.get("ok") is False:
            return {"ok": False, "error_code": "refused",
                    "error": f"Media Forge refused {name}: {data.get('error') or 'no reason given'}"}
        return data


# ── the reference on the card ─────────────────────────────────────────────────

def get_reference(memory: Any, character: str) -> Optional[Dict[str, Any]]:
    """The reference stored at THIS character's own scope, or None.

    Exact-scope only: recall includes ancestors, and a reference inherited from
    the world scope would make every character wear the same face.
    """
    who = _norm_character(character)
    if who == WORLD or not memory.available():
        return None
    own = f"{memory.campaign}:{who}:*"
    for n in memory.notes_for(who, limit=200):
        if n.get("scope") == own and n.get("key") == REF_KEY:
            meta = n.get("meta") or {}
            if isinstance(meta, dict) and meta.get("media_id") not in (None, ""):
                return dict(meta)
    return None


def set_reference(memory: Any, character: str, ref: Dict[str, Any]) -> Dict[str, Any]:
    """Store `ref` as the character's reference — on their card, at their scope."""
    who = _norm_character(character)
    if who == WORLD:
        return {"ok": False, "error": "the world has no face — name a character"}
    if not memory.available():
        return {"ok": False, "error": memory.unavailable_reason()}
    clean = {k: ref[k] for k in ("media_id", "url", "op", "style") if ref.get(k) not in (None, "")}
    if clean.get("media_id") in (None, ""):
        return {"ok": False, "error": "a reference needs a media id"}
    mid = clean["media_id"]
    if isinstance(mid, str) and mid.strip().isdigit():
        clean["media_id"] = int(mid.strip())    # gallery ids are ints on the wire
    import awm  # available() proved it imports

    scope = awm.Scope(memory.campaign, who, WORLD)
    text = _REF_PREFIX + json.dumps(clean, sort_keys=True, separators=(",", ":"))
    memory._store.remember(scope, REF_KEY, text, kind=REF_KIND, meta=clean)  # noqa: SLF001
    return {"ok": True, "scope": str(scope), "reference": clean}


# ── who, and what they know ───────────────────────────────────────────────────

def resolve_character(memory: Any, ident: str) -> Optional[str]:
    """A character by scope name, card name or card `persona_id`."""
    raw = (ident or "").strip()
    if not raw:
        return None
    norm = _norm_character(raw)
    known = memory.known_characters()
    if norm in known:
        return norm
    for who in known:
        for n in memory.notes_for(who, limit=200):
            v = str(n.get("value") or "")
            if not v.startswith("card-meta: "):
                continue
            try:
                meta = json.loads(v[len("card-meta: "):])
            except ValueError:
                continue
            if isinstance(meta, dict) and raw.lower() in {
                    str(meta.get("persona_id") or "").lower(),
                    str(meta.get("name") or "").lower()}:
                return who
    return None


def _facts(notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [n for n in notes if n.get("kind") != REF_KIND
            and not str(n.get("value") or "").startswith(_BOOKKEEPING_PREFIXES)]


def _appearance(notes: List[Dict[str, Any]], own_scope: str) -> str:
    for field in _APPEARANCE_FIELDS:
        for n in notes:
            v = str(n.get("value") or "")
            if n.get("scope") == own_scope and v.startswith(field + ": "):
                return v[len(field) + 2:].strip()
    return ""


def _display_name(memory: Any, who: str) -> str:
    for n in memory.notes_for(who, limit=200):
        v = str(n.get("value") or "")
        if v.startswith("card-meta: "):
            try:
                name = json.loads(v[len("card-meta: "):]).get("name")
            except (ValueError, AttributeError):
                name = None
            if name:
                return str(name)
    return who


# ── the tool ──────────────────────────────────────────────────────────────────

def illustrate(memory: Any, lane: StudioLane, *, character: str = "", scene: str = "",
               style: str = "anime", count: int = 1, reference_media_id: Any = None,
               max_facts: int = 6) -> Dict[str, Any]:
    """Illustrate a character, or a scene from campaign memory, at one viewpoint."""
    if not memory or not memory.available():
        reason = getattr(memory, "unavailable_reason", lambda: "no campaign memory")()
        return {"ok": False, "error_code": "no_memory", "error": reason}
    if not (character or "").strip() and not (scene or "").strip():
        return {"ok": False, "error_code": "bad_request",
                "error": "name a character, or describe a scene to draw from campaign memory"}
    count = max(1, min(int(count or 1), _MAX_COUNT))
    style = (style or "anime").strip()

    # 1. WHO is looking. A named character must exist; the world is the default.
    who = WORLD
    if (character or "").strip():
        who = resolve_character(memory, character)
        if who is None:
            return {"ok": False, "error_code": "unknown_character",
                    "error": f"no character {character!r} in this campaign — import their "
                             "card or record a note known to them first"}
    own_scope = f"{memory.campaign}:{who}:*"

    # 2. WHAT they know: exactly campaign_recall's scope (theirs + world).
    visible = memory.notes_for(who, limit=200)
    facts = _facts(visible)
    chosen: List[Dict[str, Any]] = []
    if (scene or "").strip():
        from adk.packs.gobbonet.retrieval import rank, tokenize

        q = set(tokenize(scene))
        matching = [n for n in facts if q & set(tokenize(str(n.get("value") or "")))]
        if not matching:
            # Refused BEFORE any request: nothing this viewpoint knows is about
            # that scene, so drawing it would mean drawing what they don't know.
            holder = "the world" if who == WORLD else who
            return {"ok": False, "error_code": "not_known",
                    "error": f"nothing {holder} knows matches that scene — it cannot be "
                             "illustrated from their viewpoint. Record the fact as known "
                             "to them (campaign_note) first."}
        chosen = rank(matching, scene, limit=max_facts)

    # 3. The prompt: appearance + chosen facts. The raw scene text is NOT in it.
    name = _display_name(memory, who) if who != WORLD else ""
    look = _appearance(visible, own_scope) if who != WORLD else ""
    parts: List[str] = []
    if name:
        parts.append(f"{name}" + (f", {look}" if look else ""))
    for n in chosen:
        parts.append(str(n.get("value")).strip())
    prompt = ". ".join(p.rstrip(".") for p in parts if p) or name

    # 4. The reference: an explicit choice is stored first, then reused.
    ref: Optional[Dict[str, Any]] = None
    ref_created = False
    if who != WORLD:
        if reference_media_id not in (None, "", 0):
            stored = set_reference(memory, who, {"media_id": reference_media_id,
                                                 "op": "chosen", "style": style})
            if not stored.get("ok"):
                return {"ok": False, "error_code": "bad_request", "error": stored.get("error")}
        ref = get_reference(memory, who)

    calls: List[str] = []
    if who != WORLD and ref is None:
        # First sight of this character: a portrait that becomes their reference.
        portrait = f"portrait of {name}" + (f", {look}" if look else "")
        res = lane.run_op("txt2img", {"prompt": portrait, "style": style, "count": 1})
        calls.append("txt2img")
        if not res.get("ok"):
            return _fail(res, calls)
        ids = list(res.get("head_ids") or [])
        if not ids:
            return {"ok": False, "error_code": "bad_response", "calls": calls,
                    "error": "Media Forge rendered the portrait but returned no media id"}
        imgs = list(res.get("images") or [])
        ref = {"media_id": ids[0], "url": lane.media_url(imgs[0]) if imgs else "",
               "op": "txt2img", "style": style}
        stored = set_reference(memory, who, ref)
        if not stored.get("ok"):
            return {"ok": False, "error_code": "no_memory", "calls": calls,
                    "error": stored.get("error")}
        ref_created = True
        if not chosen and count == 1:
            # A bare "draw this character" — the portrait IS the illustration.
            return _done(memory, lane, who, res, calls, ref, ref_created, prompt, chosen)

    if ref is not None:
        op, params = "compose", {"face_ref": ref["media_id"], "prompt": prompt,
                                 "style": style, "count": count}
    else:
        op, params = "txt2img", {"prompt": prompt, "style": style, "count": count}
    res = lane.run_op(op, params)
    calls.append(op)
    if not res.get("ok"):
        out = _fail(res, calls)
        if ref_created:
            out["reference"] = dict(ref, created=True)
        return out
    return _done(memory, lane, who, res, calls, ref, ref_created, prompt, chosen)


def _fail(res: Dict[str, Any], calls: List[str]) -> Dict[str, Any]:
    out = {"ok": False, "error_code": res.get("error_code") or "refused",
           "error": res.get("error") or "Media Forge did not generate anything", "calls": calls}
    if res.get("fix"):
        out["fix"] = res["fix"]
    return out


def _done(memory: Any, lane: StudioLane, who: str, res: Dict[str, Any], calls: List[str],
          ref: Optional[Dict[str, Any]], ref_created: bool, prompt: str,
          chosen: List[Dict[str, Any]]) -> Dict[str, Any]:
    ids = list(res.get("head_ids") or [])
    urls = [lane.media_url(p) for p in (res.get("images") or [])]
    subject = "the world" if who == WORLD else who
    about = "; ".join(str(n.get("value")).strip()[:60] for n in chosen[:2]) or "portrait"
    noted = memory.note(f"Illustration of {subject} ({about}) exists: media "
                        f"{', '.join(str(i) for i in ids) or '-'}", known_by=who)
    out: Dict[str, Any] = {
        "ok": True, "character": who, "media_ids": ids, "urls": urls, "calls": calls,
        "prompt": prompt, "facts_used": len(chosen),
        "note": {"ok": bool(noted.get("ok")), "scope": noted.get("scope")},
    }
    if ref is not None:
        out["reference"] = dict(ref, created=ref_created)
    return out


def register_illustrate_tools(agent: Any, memory: Any,
                              lane_factory: Optional[Callable[[], StudioLane]] = None) -> int:
    """Give the agent the brush, beside the pen and the card pair."""
    make_lane = lane_factory or StudioLane

    def campaign_illustrate(character: str = "", scene: str = "", style: str = "anime",
                            count: int = 1, reference_media_id: str = "") -> dict:
        """Illustrate a character (by card name or persona id) or a scene drawn from
        campaign memory, through the person's connected Media Forge (curated ops only).
        The picture uses only what that character knows (their notes plus world
        state) — `scene` selects among those facts and is never drawn verbatim. The
        first illustration of a character stores a reference image on their card;
        later ones reuse it so the character stays the same. Pass
        `reference_media_id` to choose a different reference. Returns media ids and
        urls, and records a campaign note that the illustration exists."""
        return illustrate(memory, make_lane(), character=character, scene=scene,
                          style=style, count=count,
                          reference_media_id=reference_media_id or None)

    try:
        agent.tools.register(campaign_illustrate, name="campaign_illustrate",
                             description=(campaign_illustrate.__doc__ or "").strip())
        return 1
    except Exception:  # noqa: BLE001 - a registry refusal must not kill chat
        return 0
