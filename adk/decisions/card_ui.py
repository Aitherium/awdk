"""Generated UI for a decision card: an A2UI v0.9 surface the owner can answer from.

A card's ``ui`` is OPTIONAL presentation, never authority. The answer is still an
option KEY recorded by :meth:`adk.decisions.store.DecisionStore.answer`; a ``ui``
only decides how the question is drawn. So the one property this module exists to
hold is that a generated interface cannot ask a different question than the card:

* every Button's event is ``answer:<key>`` for a key the card actually offers
  (:func:`validate_bindings`) -- a button can only report a choice the store
  would accept anyway, never invent one or carry a payload;
* only the subset of the Basic catalog a card needs (Column, Row, Text, Button),
  at most :data:`MAX_COMPONENTS` components, and no URL in any component -- nothing loads,
  nothing links out, nothing runs;
* :func:`card_hash` fingerprints what the card SAYS (title, summary, facts,
  option keys and labels). A ``ui`` records the hash it was compiled against, so
  a surface can tell a ``ui`` written for an older revision of the card and fall
  back to :func:`compile_default` instead of drawing stale text
  (:func:`is_current`), and :meth:`DecisionStore.set_ui` refuses a write whose
  expected hash no longer matches (compare-and-set).

Shape of ``ui``::

    {"a2ui": "v0.9", "card_hash": "<sha256 hex>",
     "messages": [<createSurface>, <updateComponents>]}

``messages`` are A2UI v0.9 server messages as JSON objects; joined one per line
they pass the platform's ``a2ui.py`` ``validate`` unchanged. This module does NOT
import that file: awdk is published on its own (gate 1zi,
``check_adk_publishable.py``), so the subset check is implemented here and kept
compatible with that schema -- same version string, same catalog id, same
envelope keys.

:func:`to_text` degrades a ``ui`` to plain text for a surface that cannot draw it
(Discord, a phone lock screen, a terminal).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Optional, Sequence

UI_VERSION = "v0.9"
#: The Basic catalog id -- identical to ``lib/a2ui.py`` BASIC_CATALOG_ID.
BASIC_CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
#: The Basic-catalog components a card may use. Inputs (TextField, ChoicePicker,
#: ...) are absent on purpose: a card is answered by an option key, not by a form.
CARD_COMPONENTS = ("Column", "Row", "Text", "Button")
#: Properties a component may carry. Anything else (``url``, ``src``,
#: ``functionCall``...) is refused rather than passed through to a renderer.
_ALLOWED_PROPS = frozenset({
    "id", "component", "children", "text", "variant", "action", "align",
    "justify", "weight",
})
MAX_COMPONENTS = 30
ANSWER_PREFIX = "answer:"
ROOT_ID = "root"

_VERSION_RE = re.compile(r"^v?0\.9(\.|$)")
_URL_RE = re.compile(
    r"(?i)\b[a-z][a-z0-9+.\-]*://|\bjavascript:|\bvbscript:|\bdata:[a-z]+/|\bwww\.")
_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


class CardUIError(ValueError):
    """A ``ui`` that does not bind to its card. The message names every reason."""


# ── reading a card that may be a DecisionCard or a raise body ──────────────────


def _get(card: Any, name: str, default: Any = None) -> Any:
    if isinstance(card, dict):
        return card.get(name, default)
    return getattr(card, name, default)


def _options(card: Any) -> list[tuple[str, str, bool]]:
    """(key, label, recommended) for every option, from objects or dicts."""
    out: list[tuple[str, str, bool]] = []
    for opt in _get(card, "options", None) or []:
        key = str(_get(opt, "key", "") or "").strip()
        label = str(_get(opt, "label", "") or "").strip()
        out.append((key, label, bool(_get(opt, "recommended", False))))
    return out


def _default_key(card: Any) -> str:
    return str(_get(card, "default_key", "") or _get(card, "default", "") or "").strip()


# ── identity ────────────────────────────────────────────────────────────────────


def card_hash(card: Any) -> str:
    """sha256 over what the card SAYS: title | summary | facts | option keys+labels.

    Encoded as one JSON array so no choice of separator can make two different
    cards collide ("a|b" + "c" vs "a" + "b|c"). Status, notes and answers are
    excluded: they change without the question changing.
    """
    payload = [
        str(_get(card, "title", "") or "").strip(),
        str(_get(card, "summary", "") or "").strip(),
        [str(f).strip() for f in (_get(card, "facts", None) or [])],
        [[key, label] for key, label, _rec in _options(card)],
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_current(card: Any) -> bool:
    """True when the card carries a ``ui`` compiled against its present text."""
    ui = _get(card, "ui", None)
    return isinstance(ui, dict) and ui.get("card_hash") == card_hash(card)


# ── compile ─────────────────────────────────────────────────────────────────────


def _surface_id(card: Any, digest: str) -> str:
    cid = _ID_SAFE_RE.sub("-", str(_get(card, "id", "") or "")).strip("-")
    return f"card-{cid or digest[:12]}"


def compile_default(card: Any, *,
                    rows: Optional[Sequence[tuple[str, str]]] = None) -> dict[str, Any]:
    """The default A2UI surface for ``card``: title, facts, one Button per option.

    * ``title`` is a heading Text, ``summary`` a body Text;
    * every fact is a Row holding one Text (the WHOLE fact -- nothing is cut);
    * ``rows`` are extra ``(label, value)`` Rows drawn after the facts, for a
      producer that has a number worth putting side by side (spend vs revenue);
    * every option is a Button sitting directly in the root Column, which
      stretches its children: full width, so a long label WRAPS instead of being
      clipped. Its text is the full label and its event is ``answer:<key>``; the
      recommended (else default) option is the ``primary`` variant.

    Raises :class:`CardUIError` when the result would not pass
    :func:`validate_bindings` (more than :data:`MAX_COMPONENTS` components, a URL
    in the card text) -- the caller then raises the card without a ``ui``.
    """
    digest = card_hash(card)
    sid = _surface_id(card, digest)
    comps: list[dict[str, Any]] = []
    children: list[str] = []

    def add(comp: dict[str, Any]) -> str:
        comps.append(comp)
        return comp["id"]

    children.append(add({"id": "title", "component": "Text", "variant": "h3",
                         "text": str(_get(card, "title", "") or "").strip()}))
    summary = str(_get(card, "summary", "") or "").strip()
    if summary:
        children.append(add({"id": "summary", "component": "Text", "variant": "body",
                             "text": summary}))
    for i, fact in enumerate(_get(card, "facts", None) or []):
        text = str(fact).strip()
        if not text:
            continue
        cell = add({"id": f"fact{i}-t", "component": "Text", "variant": "caption",
                    "text": text})
        children.append(add({"id": f"fact{i}", "component": "Row", "children": [cell]}))
    for i, (label, value) in enumerate(rows or []):
        left = add({"id": f"row{i}-k", "component": "Text", "variant": "caption",
                    "text": str(label)})
        right = add({"id": f"row{i}-v", "component": "Text", "variant": "body",
                     "text": str(value)})
        children.append(add({"id": f"row{i}", "component": "Row",
                             "justify": "spaceBetween", "children": [left, right]}))
    options = _options(card)
    primary = next((k for k, _l, rec in options if rec), "") or _default_key(card)
    for i, (key, label, _rec) in enumerate(options):
        children.append(add({
            "id": f"opt{i}", "component": "Button",
            "variant": "primary" if key == primary else "borderless",
            "text": label or key,
            "action": {"event": {"name": f"{ANSWER_PREFIX}{key}"}},
        }))
    root = {"id": ROOT_ID, "component": "Column", "align": "stretch",
            "children": children}
    ui = {
        "a2ui": UI_VERSION,
        "card_hash": digest,
        "messages": [
            {"version": UI_VERSION,
             "createSurface": {"surfaceId": sid, "catalogId": BASIC_CATALOG_ID}},
            {"version": UI_VERSION,
             "updateComponents": {"surfaceId": sid, "components": [root, *comps]}},
        ],
    }
    errors = validate_ui(ui, [k for k, _l, _r in options])
    if errors:
        raise CardUIError("; ".join(errors))
    return ui


# ── validate ────────────────────────────────────────────────────────────────────


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _check_button(cid: str, comp: dict[str, Any], keys: Sequence[str],
                  errors: list[str]) -> None:
    action = comp.get("action")
    event = action.get("event") if isinstance(action, dict) else None
    if not isinstance(action, dict) or set(action) != {"event"} or not isinstance(event, dict):
        errors.append(f"button {cid}: action must be exactly {{event: {{name}}}}")
        return
    if set(event) - {"name"}:
        errors.append(f"button {cid}: an answer carries no context "
                      f"({', '.join(sorted(set(event) - {'name'}))})")
    name = event.get("name")
    if not isinstance(name, str) or not name.startswith(ANSWER_PREFIX):
        errors.append(f"button {cid}: event {name!r} is not answer:<option key>")
        return
    key = name[len(ANSWER_PREFIX):]
    if key not in keys:
        errors.append(f"button {cid}: event {name!r} names no option of this card "
                      f"(options: {', '.join(keys) or 'none'})")


def validate_ui(ui: Any, option_keys: Sequence[str]) -> list[str]:
    """Every reason ``ui`` does not bind to a card offering ``option_keys``. [] = valid."""
    if ui is None:
        return []
    if not isinstance(ui, dict):
        return ["ui must be an object"]
    errors: list[str] = []
    if set(ui) - {"a2ui", "card_hash", "messages"}:
        errors.append(f"ui has unknown keys {sorted(set(ui) - {'a2ui', 'card_hash', 'messages'})}")
    if not _VERSION_RE.match(str(ui.get("a2ui", UI_VERSION))):
        errors.append(f"ui.a2ui must be {UI_VERSION}")
    msgs = ui.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return errors + ["ui.messages must be a non-empty list"]
    surface: Optional[str] = None
    comps: dict[str, dict[str, Any]] = {}
    count = 0
    for i, m in enumerate(msgs):
        at = f"messages[{i}]"
        if not isinstance(m, dict):
            errors.append(f"{at} must be an object")
            continue
        kinds = [k for k in ("createSurface", "updateComponents") if k in m]
        if set(m) - {"version", "createSurface", "updateComponents"} or len(kinds) != 1:
            errors.append(f"{at} must be exactly one of createSurface/updateComponents")
            continue
        if not _VERSION_RE.match(str(m.get("version", ""))):
            errors.append(f"{at}.version must be {UI_VERSION}")
            continue
        body = m[kinds[0]]
        sid = body.get("surfaceId") if isinstance(body, dict) else None
        if not isinstance(sid, str) or not sid:
            errors.append(f"{at}.surfaceId is required")
            continue
        if kinds[0] == "createSurface":
            if surface is not None:
                errors.append(f"{at}: a card has exactly one surface")
            if body.get("catalogId") != BASIC_CATALOG_ID:
                errors.append(f"{at}: only the Basic catalog is allowed")
            surface = sid
            continue
        if sid != surface:
            errors.append(f"{at}: surface {sid} was never created")
            continue
        items = body.get("components")
        if not isinstance(items, list):
            errors.append(f"{at}.components must be a list")
            continue
        for comp in items:
            count += 1
            cid = comp.get("id") if isinstance(comp, dict) else None
            if not isinstance(cid, str) or not cid:
                errors.append(f"{at}: a component needs a string id")
                continue
            kind = comp.get("component")
            if kind not in CARD_COMPONENTS:
                errors.append(f"component {cid}: {kind!r} is not one of "
                              f"{', '.join(CARD_COMPONENTS)}")
                continue
            url = next((t for t in _strings(comp) if _URL_RE.search(t)), None)
            if url is not None:
                errors.append(f"component {cid}: carries a URL ({url[:60]!r}); "
                              f"a card links nowhere")
            extra = set(comp) - _ALLOWED_PROPS
            if extra:
                errors.append(f"component {cid}: unsupported properties {sorted(extra)}")
            if kind in ("Text", "Button") and not isinstance(comp.get("text"), str):
                errors.append(f"component {cid}: text must be a literal string")
            if kind == "Button":
                _check_button(cid, comp, option_keys, errors)
            elif "action" in comp:
                errors.append(f"component {cid}: only a Button has an action")
            comps[cid] = comp
    if count > MAX_COMPONENTS:
        errors.append(f"ui has {count} components (max {MAX_COMPONENTS})")
    if surface is None:
        errors.append("ui never creates its surface")
    elif ROOT_ID not in comps:
        errors.append('no component has id "root"')
    for cid, comp in comps.items():
        ch = comp.get("children")
        if ch is None:
            continue
        if not isinstance(ch, list) or not all(isinstance(r, str) for r in ch):
            errors.append(f"component {cid}: children must be a list of ids")
            continue
        for ref in ch:
            if ref not in comps:
                errors.append(f"component {cid} references missing component {ref}")
    return errors


def validate_bindings(card: Any) -> list[str]:
    """Every reason ``card.ui`` would let the owner answer something the card does not ask.

    ``card`` is a :class:`~adk.decisions.store.DecisionCard` or a raise body dict
    (``options`` as dicts). No ``ui`` is valid. Refused: any Button whose event is
    not ``answer:<an existing option key>`` (or carries context), a component
    outside Column/Row/Text/Button, more than :data:`MAX_COMPONENTS`, any catalog
    but Basic, and any URL in a component.
    """
    return validate_ui(_get(card, "ui", None), [k for k, _l, _r in _options(card)])


# ── degrade ─────────────────────────────────────────────────────────────────────


def to_text(ui: Any) -> str:
    """Plain text for a surface that cannot draw A2UI (Discord, phone, terminal).

    Walks the root depth-first: a Text is a line, a Row joins its texts with
    `` -- ``, a Button becomes ``[key] label`` (the key is what to reply with).
    An invalid or empty ``ui`` yields ``""`` -- the caller renders the card itself.
    """
    if not isinstance(ui, dict) or not isinstance(ui.get("messages"), list):
        return ""
    comps: dict[str, dict[str, Any]] = {}
    for m in ui["messages"]:
        body = m.get("updateComponents") if isinstance(m, dict) else None
        if isinstance(body, dict) and isinstance(body.get("components"), list):
            for c in body["components"]:
                if isinstance(c, dict) and isinstance(c.get("id"), str):
                    comps[c["id"]] = c

    def inline(cid: str, seen: set[str]) -> str:
        c = comps.get(cid)
        if c is None or cid in seen:
            return ""
        seen.add(cid)
        if c.get("component") == "Row":
            parts = [inline(ch, seen) for ch in c.get("children") or []]
            return " -- ".join(p for p in parts if p)
        if c.get("component") == "Button":
            name = str(((c.get("action") or {}).get("event") or {}).get("name") or "")
            key = name[len(ANSWER_PREFIX):] if name.startswith(ANSWER_PREFIX) else name
            return f"[{key}] {c.get('text') or ''}".strip()
        if c.get("component") == "Column":
            return "\n".join(p for p in (inline(ch, seen)
                                         for ch in c.get("children") or []) if p)
        return str(c.get("text") or "")

    return inline(ROOT_ID, set())


# ── self-test (DC006: this module can prove it still fails) ─────────────────────


def _self_test() -> int:
    card = {"id": "d-abcd", "title": "Ship it?", "summary": "", "facts": ["tests pass"],
            "options": [{"key": "yes", "label": "Ship " + "x" * 80, "recommended": True},
                        {"key": "no", "label": "Hold"}], "default": "no"}
    ui = compile_default(card)
    card["ui"] = ui
    assert validate_bindings(card) == [], validate_bindings(card)
    assert is_current(card)
    bad = json.loads(json.dumps(ui))
    bad["messages"][1]["updateComponents"]["components"][-1]["action"]["event"]["name"] = \
        "answer:rm-rf"
    assert validate_bindings({**card, "ui": bad}), "an unknown event must be refused"
    assert "[yes] Ship" in to_text(ui)
    changed = {**card, "title": "Ship it now?"}
    assert card_hash(changed) != card_hash(card) and not is_current(changed)
    print("card_ui self-test: ok")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_self_test())
