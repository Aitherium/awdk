"""The household device risk policy: pure, no I/O, table-tested.

Three classes, decided per ENTITY (and, for a few domains, per action):

``free``     lights, fans, scenes, media (outside quiet hours), a climate set point
             inside the household band. Acts at once for anyone it is exposed to.
``confirm``  climate outside the band (or a mode change), blinds/shades and other
             covers, speakers that would make sound during quiet hours, vacuums,
             scripts, switches and anything unknown. One yes from the asker (an
             adult) on a card bound to the exact arguments.
``guarded``  locks, garage doors / gates / doors, alarm panels, valves, sirens, water
             heaters, anything tagged ``dangerous``, and every camera action. A
             request that resolves only when ``approvals_required`` of the allowed
             approvers (default: one guardian) say yes, before it expires.

Who may do what (``role`` is the family roster role):

=================  ==================  ===========================================
role               sees                may trigger
=================  ==================  ===========================================
owner/co_guardian  every exposed       free acts; confirm asks them; guarded asks
adult              every exposed       the guardians' quorum
teen, child        only their list     free (on their list) acts; anything else
                                       becomes "ask a parent" (a guardian request)
guest              nothing             nothing
=================  ==================  ===========================================

A TAINTED session (it read mail, the web or other untrusted text) never acts without
a human: free and confirm become confirm (asker's card; a child's become ask-parent),
guarded stays guarded.

Device STATE is private household data, but it is written by the household, not by
strangers: reading it makes egress (web/mail) ask first, yet it does not by itself
block device actions. A camera SUMMARY is model text about a picture, so it taints
fully (see :data:`TAINTING_ACTIONS`).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Dict, Iterable, List, Mapping, Optional

FREE, CONFIRM, GUARDED = "free", "confirm", "guarded"
CLASSES = (FREE, CONFIRM, GUARDED)
#: Verdicts :func:`decide` returns.
ACT, ASK_ASKER, ASK_GUARDIANS, ASK_PARENT, REFUSE = (
    "act", "confirm", "guarded", "ask_parent", "refuse")

OWNER, CO_GUARDIAN, ADULT, TEEN, CHILD, GUEST = (
    "owner", "co_guardian", "adult", "teen", "child", "guest")
GUARDIAN_ROLES = frozenset({OWNER, CO_GUARDIAN})
ADULT_ROLES = frozenset({OWNER, CO_GUARDIAN, ADULT})
MINOR_ROLES = frozenset({TEEN, CHILD})
ANY_GUARDIAN = "any_guardian"

#: domain -> class when nothing more specific applies.
DOMAIN_CLASS: Dict[str, str] = {
    "light": FREE, "fan": FREE, "scene": FREE, "media_player": FREE,
    "climate": CONFIRM, "cover": CONFIRM, "vacuum": CONFIRM, "lawn_mower": CONFIRM,
    "humidifier": CONFIRM, "switch": CONFIRM, "input_boolean": CONFIRM,
    "script": CONFIRM, "automation": CONFIRM, "button": CONFIRM, "number": CONFIRM,
    "select": CONFIRM,
    "lock": GUARDED, "alarm_control_panel": GUARDED, "camera": GUARDED,
    "valve": GUARDED, "siren": GUARDED, "water_heater": GUARDED,
}
#: (domain, device_class) -> class, over :data:`DOMAIN_CLASS`.
DEVICE_CLASS_CLASS: Dict[tuple, str] = {
    ("cover", "garage"): GUARDED, ("cover", "gate"): GUARDED, ("cover", "door"): GUARDED,
    ("cover", "blind"): CONFIRM, ("cover", "shade"): CONFIRM, ("cover", "curtain"): CONFIRM,
    ("cover", "shutter"): CONFIRM, ("cover", "awning"): CONFIRM, ("cover", "window"): CONFIRM,
    ("switch", "outlet"): CONFIRM,
}
#: Unknown domains are never free.
DEFAULT_CLASS = CONFIRM

#: media_player actions that make (or raise) sound: confirm during quiet hours.
LOUD_MEDIA_ACTIONS = frozenset({"turn_on", "media_play", "media_play_pause", "play_media",
                                "volume_set", "volume_up", "toggle", "media_next_track",
                                "media_previous_track", "select_source"})
#: climate actions that only move the set point (free inside the band).
CLIMATE_SETPOINT = "set_temperature"
CLIMATE_TEMP_KEYS = ("temperature", "target_temp_high", "target_temp_low")
#: camera actions; all guarded except a summary, which asks the asker (it reads a picture).
CAMERA_SUMMARY = "summarize"
#: Actions whose RESULT is model text about the world: the session is tainted after.
TAINTING_ACTIONS = frozenset({("camera", CAMERA_SUMMARY)})

#: Params that would re-target a service call: never accepted from a caller.
TARGET_KEYS = frozenset({"entity_id", "device_id", "area_id", "floor_id", "label_id",
                         "target"})
_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
_ENTITY_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}\.[a-z0-9_]{1,128}$")

DEFAULT_RULE = {"approvals_required": 1, "approvers": ANY_GUARDIAN, "expires_s": 900}
MAX_EXPIRES_S = 24 * 3600


def default_config() -> Dict[str, Any]:
    """A household's policy before a guardian changed anything."""
    return {
        "version": 1,
        "quiet_hours": {"start": "21:00", "end": "07:00", "timezone": "UTC"},
        "climate_band": {"min": 18.0, "max": 24.0},
        "classes": {GUARDED: dict(DEFAULT_RULE)},
        "devices": {},
        "hidden": [],
        "exposure": {"people": {}, GUEST: []},
    }


# ── validation (what a guardian may save) ─────────────────────────────────────

class PolicyError(ValueError):
    """A policy (or an action) that cannot be accepted."""


def _rule(raw: Any, where: str) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise PolicyError(f"{where}: a rule is an object")
    out: Dict[str, Any] = {}
    if "approvals_required" in raw:
        n = raw["approvals_required"]
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 9:
            raise PolicyError(f"{where}: approvals_required is a whole number 1-9")
        out["approvals_required"] = n
    if "approvers" in raw:
        a = raw["approvers"]
        if a == ANY_GUARDIAN:
            out["approvers"] = ANY_GUARDIAN
        elif isinstance(a, list) and a and all(isinstance(p, str) and 0 < len(p) <= 64
                                                for p in a):
            out["approvers"] = sorted(set(a))
        else:
            raise PolicyError(f"{where}: approvers is \"any_guardian\" or a list of people")
    if "expires_s" in raw:
        e = raw["expires_s"]
        if not isinstance(e, int) or isinstance(e, bool) or not 60 <= e <= MAX_EXPIRES_S:
            raise PolicyError(f"{where}: expires_s is 60-{MAX_EXPIRES_S} seconds")
        out["expires_s"] = e
    if isinstance(out.get("approvers"), list) and \
            out.get("approvals_required", 1) > len(out["approvers"]):
        raise PolicyError(f"{where}: quorum asks for more approvals than approvers")
    return out


def validate_config(raw: Any) -> Dict[str, Any]:
    """A clean copy of ``raw`` merged over the defaults; :class:`PolicyError` on anything
    malformed. Unknown keys are dropped."""
    if not isinstance(raw, Mapping):
        raise PolicyError("policy is an object")
    cfg = default_config()
    qh = raw.get("quiet_hours")
    if qh is not None:
        if not isinstance(qh, Mapping):
            raise PolicyError("quiet_hours is {start, end, timezone}")
        start, end = str(qh.get("start", "")), str(qh.get("end", ""))
        _hhmm(start), _hhmm(end)
        tz = str(qh.get("timezone") or "UTC")
        _zone(tz)
        cfg["quiet_hours"] = {"start": start, "end": end, "timezone": tz}
    band = raw.get("climate_band")
    if band is not None:
        try:
            lo, hi = float(band["min"]), float(band["max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyError("climate_band is {min, max}") from exc
        if not lo < hi:
            raise PolicyError("climate_band: min must be below max")
        cfg["climate_band"] = {"min": lo, "max": hi}
    classes = raw.get("classes") or {}
    if not isinstance(classes, Mapping):
        raise PolicyError("classes is an object")
    for cls, rule in classes.items():
        if cls not in CLASSES:
            raise PolicyError(f"unknown class {cls!r}")
        cfg["classes"][cls] = {**cfg["classes"].get(cls, {}), **_rule(rule, cls)}
    devices = raw.get("devices") or {}
    if not isinstance(devices, Mapping):
        raise PolicyError("devices is an object")
    for eid, d in devices.items():
        if not _ENTITY_RE.match(str(eid)) or not isinstance(d, Mapping):
            raise PolicyError(f"device {eid!r}: not an entity id with an object")
        row = _rule({k: v for k, v in d.items() if k in DEFAULT_RULE}, str(eid))
        if "class" in d:
            if d["class"] not in CLASSES:
                raise PolicyError(f"device {eid}: unknown class {d['class']!r}")
            row["class"] = d["class"]
        if d.get("dangerous"):
            row["dangerous"] = True
        cfg["devices"][str(eid)] = row
    hidden = raw.get("hidden") or []
    cfg["hidden"] = sorted({str(e) for e in hidden if _ENTITY_RE.match(str(e))})
    exp = raw.get("exposure") or {}
    if not isinstance(exp, Mapping):
        raise PolicyError("exposure is an object")
    people = exp.get("people") or {}
    if not isinstance(people, Mapping):
        raise PolicyError("exposure.people is {person: [entity ids]}")
    for pid, lst in people.items():
        if not isinstance(lst, list) or not all(_ENTITY_RE.match(str(e)) for e in lst):
            raise PolicyError(f"exposure for {pid!r}: a list of entity ids")
        cfg["exposure"]["people"][str(pid)[:64]] = sorted({str(e) for e in lst})
    guests = exp.get(GUEST) or []
    if not isinstance(guests, list) or not all(_ENTITY_RE.match(str(e)) for e in guests):
        raise PolicyError("exposure.guest: a list of entity ids")
    cfg["exposure"][GUEST] = sorted({str(e) for e in guests})
    return cfg


def _hhmm(text: str) -> dtime:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text.strip())
    if not m:
        raise PolicyError(f"time must be HH:MM, got {text!r}")
    return dtime(int(m.group(1)), int(m.group(2)))


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001 -- unknown zone or no tz database
        raise PolicyError(f"unknown timezone {name!r}") from exc


def in_quiet_hours(cfg: Mapping[str, Any], now: datetime) -> bool:
    """Is the aware ``now`` inside the household's quiet window (midnight-safe)?"""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    qh = cfg.get("quiet_hours") or {}
    try:
        start, end = _hhmm(str(qh.get("start", ""))), _hhmm(str(qh.get("end", "")))
        local = now.astimezone(_zone(str(qh.get("timezone") or "UTC"))).time()
    except PolicyError:
        return True          # a window that cannot be read is treated as quiet
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


# ── classification ────────────────────────────────────────────────────────────

def domain_of(entity_id: str) -> str:
    return str(entity_id).split(".", 1)[0]


def entity_class(entity_id: str, device_class: Optional[str] = None,
                 cfg: Optional[Mapping[str, Any]] = None) -> str:
    """The class of an ENTITY: an override, else ``dangerous``, else device class, else
    domain, else :data:`DEFAULT_CLASS`."""
    dev = ((cfg or {}).get("devices") or {}).get(entity_id) or {}
    if dev.get("class") in CLASSES:
        return dev["class"]
    if dev.get("dangerous"):
        return GUARDED
    dom = domain_of(entity_id)
    if device_class and (dom, str(device_class)) in DEVICE_CLASS_CLASS:
        return DEVICE_CLASS_CLASS[(dom, str(device_class))]
    return DOMAIN_CLASS.get(dom, DEFAULT_CLASS)


def action_class(entity_id: str, action: str, params: Mapping[str, Any],
                 cfg: Mapping[str, Any], now: datetime,
                 device_class: Optional[str] = None) -> str:
    """The class of THIS action on that entity (an entity's class, raised or lowered by
    what the action does: a set point in the band, a loud speaker at night)."""
    base = entity_class(entity_id, device_class, cfg)
    overridden = bool(((cfg.get("devices") or {}).get(entity_id) or {}).get("class"))
    dom = domain_of(entity_id)
    if overridden or base == GUARDED and dom != "camera":
        return base
    if dom == "camera":
        return CONFIRM if action == CAMERA_SUMMARY else GUARDED
    if dom == "climate":
        if action != CLIMATE_SETPOINT:
            return CONFIRM
        band = cfg.get("climate_band") or {}
        temps = [params[k] for k in CLIMATE_TEMP_KEYS if k in params]
        try:
            lo, hi = float(band["min"]), float(band["max"])
            ok = bool(temps) and all(lo <= float(t) <= hi for t in temps)
        except (KeyError, TypeError, ValueError):
            ok = False
        return FREE if ok else CONFIRM
    if dom == "media_player" and base == FREE:
        if action in LOUD_MEDIA_ACTIONS and in_quiet_hours(cfg, now):
            return CONFIRM
    return base


# ── exposure ──────────────────────────────────────────────────────────────────

def _first_name(person: Mapping[str, Any]) -> str:
    words = re.findall(r"[a-z0-9]+", str(person.get("name") or "").lower())
    return words[0] if words else ""


def default_minor_exposure(person: Mapping[str, Any], entity_ids: Iterable[str]) -> List[str]:
    """A child's list before a guardian sets one: lights named for them (their room) and
    any bedtime scene."""
    first = _first_name(person)
    out = []
    for eid in entity_ids:
        dom, _, obj = str(eid).partition(".")
        if dom == "scene" and "bedtime" in obj:
            out.append(eid)
        elif dom == "light" and first and len(first) >= 2 and first in obj.split("_"):
            out.append(eid)
    return sorted(out)


def exposed_to(person: Mapping[str, Any], cfg: Mapping[str, Any],
               entity_ids: Iterable[str]) -> List[str]:
    """The entity ids this person may see (and, if their class allows, act on)."""
    ids = [e for e in entity_ids if _ENTITY_RE.match(str(e))]
    hidden = set(cfg.get("hidden") or [])
    role = str(person.get("role") or GUEST)
    exp = cfg.get("exposure") or {}
    if role in ADULT_ROLES:
        return sorted(e for e in ids if e not in hidden)
    if role in MINOR_ROLES:
        listed = (exp.get("people") or {}).get(str(person.get("pid") or ""))
        allowed = set(listed) if isinstance(listed, list) else \
            set(default_minor_exposure(person, ids))
        return sorted(e for e in ids if e in allowed and e not in hidden)
    allowed = set(exp.get(GUEST) or [])
    return sorted(e for e in ids if e in allowed and e not in hidden)


# ── the decision ──────────────────────────────────────────────────────────────

@dataclass
class Decision:
    verdict: str                       # act | confirm | guarded | ask_parent | refuse
    cls: str                           # free | confirm | guarded ("" when refused early)
    reason: str
    role: str
    tainted: bool
    approvals_required: int = 0
    approvers: Any = None              # "any_guardian" | [pid, ...] | None
    expires_s: int = 0
    rule_source: str = ""              # device | class | default

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def rule_for(entity_id: str, cls: str, cfg: Mapping[str, Any]) -> tuple:
    """(rule, source): the device's own rule over its class's, over the default."""
    rule = dict(DEFAULT_RULE)
    source = "default"
    cls_rule = ((cfg.get("classes") or {}).get(cls)) or {}
    if cls_rule:
        rule.update({k: v for k, v in cls_rule.items() if k in DEFAULT_RULE})
        source = "class"
    dev = ((cfg.get("devices") or {}).get(entity_id)) or {}
    dev_rule = {k: v for k, v in dev.items() if k in DEFAULT_RULE}
    if dev_rule:
        rule.update(dev_rule)
        source = "device"
    return rule, source


def decide(*, cfg: Mapping[str, Any], person: Mapping[str, Any], entity_id: str,
           action: str, params: Mapping[str, Any], tainted: bool, now: datetime,
           exposed: Iterable[str], device_class: Optional[str] = None) -> Decision:
    """What happens when ``person`` asks for ``action`` on ``entity_id``.

    ``exposed`` is :func:`exposed_to` for this person (the host computes it from the
    live entity list). Pure: same inputs, same decision."""
    role = str(person.get("role") or GUEST)
    t = bool(tainted)
    if role not in ADULT_ROLES | MINOR_ROLES:
        return Decision(REFUSE, "", "guests cannot use the home's devices", role, t)
    if entity_id not in set(exposed):
        why = ("that device is not on your list -- ask a parent to add it"
               if role in MINOR_ROLES else "that device is not shared with Hearth")
        return Decision(REFUSE, "", why, role, t)
    cls = action_class(entity_id, action, params, cfg, now, device_class)
    rule, source = rule_for(entity_id, cls, cfg)

    def needs_guardians(verdict: str, reason: str) -> Decision:
        return Decision(verdict, cls, reason, role, t,
                        approvals_required=int(rule["approvals_required"]),
                        approvers=copy.deepcopy(rule["approvers"]),
                        expires_s=int(rule["expires_s"]), rule_source=source)

    if role in MINOR_ROLES:
        if cls == FREE and not t:
            return Decision(ACT, cls, "free, on your list", role, t)
        why = {FREE: "this conversation read outside text, so a parent says yes first",
               CONFIRM: "a parent says yes to this first",
               GUARDED: "only a parent can allow this"}[cls]
        return needs_guardians(ASK_PARENT, why)
    if cls == GUARDED:
        return needs_guardians(ASK_GUARDIANS, "guarded: the guardians' quorum decides")
    if cls == CONFIRM:
        return Decision(ASK_ASKER, cls, "confirm: your yes on the exact action first",
                        role, t, approvals_required=1, approvers=[str(person.get("pid"))],
                        expires_s=int(rule["expires_s"]), rule_source="asker")
    if t:
        return Decision(ASK_ASKER, cls, "this conversation read outside text, so you "
                        "say yes to the exact action first", role, t, approvals_required=1,
                        approvers=[str(person.get("pid"))],
                        expires_s=int(rule["expires_s"]), rule_source="asker")
    return Decision(ACT, cls, "free", role, t)


# ── the action itself ─────────────────────────────────────────────────────────

def normalize_action(domain: Any, action: Any, target: Any,
                     params: Any = None) -> Dict[str, Any]:
    """``{"domain", "action", "target", "params"}`` checked and canonical, or
    :class:`PolicyError`. A param that would re-target the call is refused, not dropped:
    the person must see exactly what runs."""
    dom, act, tgt = str(domain or "").strip().lower(), str(action or "").strip().lower(), \
        str(target or "").strip().lower()
    if not _NAME_RE.match(dom) or not _NAME_RE.match(act):
        raise PolicyError("domain and action are lower-case names like light / turn_on")
    if not _ENTITY_RE.match(tgt):
        raise PolicyError("target is one entity id, like light.kitchen")
    if domain_of(tgt) != dom:
        raise PolicyError(f"{tgt} is not a {dom}")
    if params is None:
        params = {}
    if isinstance(params, str):
        try:
            params = json.loads(params) if params.strip() else {}
        except ValueError as exc:
            raise PolicyError("params must be a JSON object") from exc
    if not isinstance(params, Mapping):
        raise PolicyError("params must be an object")
    bad = sorted(k for k in params if str(k).lower() in TARGET_KEYS)
    if bad:
        raise PolicyError(f"params may not set {', '.join(bad)}: the target is one entity")
    clean = {}
    for k, v in params.items():
        if not _NAME_RE.match(str(k)):
            raise PolicyError(f"param {k!r} is not a plain name")
        if not isinstance(v, (str, int, float, bool, list)) or \
                (isinstance(v, str) and len(v) > 200) or \
                (isinstance(v, list) and (len(v) > 4 or not all(
                    isinstance(x, (int, float)) for x in v))):
            raise PolicyError(f"param {k} is not a simple value")
        clean[str(k)] = v
    return {"domain": dom, "action": act, "target": tgt, "params": clean}


def action_digest(action: Mapping[str, Any], household: str = "") -> str:
    """sha256 of the canonical action (and household): what an approval is bound to."""
    body = {"household": household, "domain": action["domain"], "action": action["action"],
            "target": action["target"], "params": action.get("params") or {}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def summary(action: Mapping[str, Any], name: str = "") -> str:
    """One line a person approves: what, on which device, with which values."""
    p = action.get("params") or {}
    vals = ", ".join(f"{k}={v}" for k, v in sorted(p.items()))
    who = f"{name} ({action['target']})" if name else action["target"]
    return f"{action['action'].replace('_', ' ')} {who}" + (f" [{vals}]" if vals else "")


__all__ = [n for n in dir() if not n.startswith("_") and n not in (
    "annotations", "asdict", "copy", "dataclass", "datetime", "dtime", "field", "hashlib",
    "json", "re", "Any", "Dict", "Iterable", "List", "Mapping", "Optional")]
