"""Home device tools for the local Hearth (``adk home serve``): Home Assistant, gated.

Registered only when a Home Assistant URL is configured (``AITHER_HA_URL``). The token
is read BY NAME at call time: the env var named by ``AITHER_HA_TOKEN_SECRET`` (default
``HA_TOKEN``), else the vault entry of that name (:func:`adk.vault_lockbox.get_secret`).

The local Hearth has ONE owner, so the person is always the owner (a guardian) and a
quorum is at most that one person. The policy (:mod:`adk.home.devices.policy`) is the
household's: ``<home>/devices.json``.

* ``home_status``  -- the devices and their state (a private read; see hearth.py).
* ``home_act``     -- free and untainted: acts. Otherwise NOT done: it returns a
  ``suggest`` that Hearth turns into an owner card for ``home_act_confirmed`` (confirm)
  or ``home_approve`` (guarded). Both are in ``life_tools.ALWAYS_ASK``, so the model
  can never run either without the owner's yes on exactly those arguments.
* ``home_scene``   -- ``home_act`` for ``scene.turn_on``.

Taint: a session that read mail, the web or other untrusted text (``<home>/taint.json``,
written by :class:`adk.home.hearth.HearthCore`) never acts without the owner; an
unreadable taint file counts as tainted. Reading device state alone does not count.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import home_dir
from .devices import approvals as ap
from .devices import policy as pol
from .devices.gate import DeviceGate
from .devices.ha import HAClient

URL_ENV = "AITHER_HA_URL"
TOKEN_NAME_ENV = "AITHER_HA_TOKEN_SECRET"
DEFAULT_TOKEN_NAME = "HA_TOKEN"
POLICY_NAME = "devices.json"
APPROVALS_NAME = "device_approvals.json"
TAINT_NAME = "taint.json"
#: Taint sources that do not block device actions (the household's own state).
DEVICE_SAFE_SOURCES = frozenset({"home_status"})
OWNER = {"pid": "owner", "name": "owner", "role": pol.OWNER}

DEVICE_PROMPT = """\
## The home's devices
home_status shows the lights, heating, locks and other devices you may use. home_act
changes one (domain, action, target entity id, params); home_scene turns a scene on.
Some actions run at once; others come back NOT done and wait for the owner's yes --
then say you asked, never that it is done. Device state is private: never put it in a
web lookup or a message.
"""


def ha_url() -> str:
    return (os.environ.get(URL_ENV) or "").strip().rstrip("/")


def token_name() -> str:
    return (os.environ.get(TOKEN_NAME_ENV) or "").strip() or DEFAULT_TOKEN_NAME


def read_token() -> str:
    """The Home Assistant token, by name: env first, then the vault."""
    name = token_name()
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    try:
        from adk.vault_lockbox import get_secret

        return str(get_secret(name) or "").strip()
    except Exception:  # noqa: BLE001 -- no vault: "no token" is the honest answer
        return ""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def load_policy(root: Optional[Path] = None) -> Dict[str, Any]:
    raw = _read_json((root or home_dir()) / POLICY_NAME)
    return pol.validate_config(raw) if raw is not None else pol.default_config()


def device_tainted(root: Optional[Path] = None) -> bool:
    """True when any Hearth session holds untrusted text (fails closed)."""
    try:
        data = _read_json((root or home_dir()) / TAINT_NAME)
    except (OSError, ValueError):
        return True
    sessions = (data or {}).get("sessions") if isinstance(data, dict) else None
    if data is not None and not isinstance(sessions, dict):
        return True
    for entry in (sessions or {}).values():
        sources = (entry or {}).get("sources") if isinstance(entry, dict) else ["unknown"]
        if any(s not in DEVICE_SAFE_SOURCES for s in sources or []):
            return True
    return False


def build_device_tools(receipts_file: Optional[Path] = None, root: Optional[Path] = None,
                       client: Optional[HAClient] = None,
                       tainted: Optional[Callable[[], bool]] = None
                       ) -> List[Callable[..., Any]]:
    """The four device tools, or ``[]`` when no Home Assistant is configured."""
    if client is None:
        if not ha_url():
            return []
        client = HAClient(ha_url(), read_token)
    home = root or home_dir()
    is_tainted = tainted or (lambda: device_tainted(home))

    def receipt(kind: str, name: str, args: Any, result: Any, approval: str) -> None:
        from adk import receipts

        path = receipts_file or home / "actions.jsonl"
        receipts.append(kind, name, args=args, result=result, approval=approval, path=path)

    gate = DeviceGate(client, household="local", receipt=receipt)
    store_path = home / APPROVALS_NAME

    def load_store() -> Dict[str, Any]:
        data = _read_json(store_path)
        return data if isinstance(data, dict) else {"approvals": {}}

    def save_store(store: Dict[str, Any]) -> None:
        from adk._private_file import write_private_text

        write_private_text(store_path, json.dumps(store, default=str))

    def dumps(obj: Any) -> str:
        return json.dumps(obj, default=str)

    async def home_status(domain: str = "") -> str:
        """The home's devices you may use, with their state and risk class.

        domain: optional filter, like light, climate or lock
        """
        return dumps(await gate.status(OWNER, load_policy(home), domain))

    async def home_act(domain: str, action: str, target: str, params: str = "") -> str:
        """Change one device. Free actions run now; others wait for the owner's yes.

        domain: the device kind, like light, climate, cover or lock
        action: the Home Assistant service, like turn_on, turn_off, set_temperature
        target: one entity id from home_status, like light.kitchen
        params: optional JSON object, like {"brightness_pct": 40} or {"temperature": 21}
        """
        store = load_store()
        out = await gate.request(OWNER, load_policy(home), store, domain=domain,
                                 action=action, target=target, params=params or None,
                                 tainted=is_tainted(), guardians=[OWNER["pid"]])
        if out.get("status") == "waiting_for_you":
            act = out["action"]
            out = {"error": f"not done yet: {out['summary']} waits for the owner's yes",
                   "summary": out["summary"], "policy": out["policy"],
                   "suggest": {"tool": "home_act_confirmed", "args": {
                       "domain": act["domain"], "action": act["action"],
                       "target": act["target"], "params": json.dumps(act["params"]),
                       "digest": out["digest"]}}}
        elif out.get("status") in ("waiting_for_guardians", "asked_a_parent"):
            save_store(store)
            row = out["approval"]
            out = {"error": f"not done yet: {out['summary']} is guarded and waits for "
                            "the owner's yes", "summary": out["summary"],
                   "policy": out["policy"], "approval": row["id"],
                   "suggest": {"tool": "home_approve", "args": {
                       "approval": row["id"], "summary": row["summary"]}}}
        return dumps(out)

    async def home_scene(scene: str) -> str:
        """Turn on one scene (an entity id like scene.bedtime, from home_status).

        scene: the scene's entity id
        """
        target = scene if str(scene).startswith("scene.") else f"scene.{scene}"
        return await home_act("scene", "turn_on", target)

    async def home_act_confirmed(domain: str, action: str, target: str, params: str,
                                 digest: str) -> str:
        """Run a device action the owner said yes to (always asks the owner first).

        domain: as in home_act
        action: as in home_act
        target: as in home_act
        params: the JSON object from home_act
        digest: the action digest home_act returned
        """
        try:
            p = json.loads(params) if params else {}
        except ValueError:
            return dumps({"error": "params must be a JSON object"})
        return dumps(await gate.run_confirmed(
            OWNER, load_policy(home),
            {"domain": domain, "action": action, "target": target, "params": p},
            digest, tainted=is_tainted()))

    async def home_approve(approval: str, summary: str) -> str:
        """Approve a guarded device request (always asks the owner first).

        approval: the request id home_act returned
        summary: the request's summary, exactly as home_act returned it
        """
        store = load_store()
        row = ap.get(store, approval)
        if row is None or row.get("summary") != summary:
            return dumps({"error": "refused: no such request with that summary; "
                                   "nothing ran"})
        out = await gate.vote(OWNER, load_policy(home), store, approval, True,
                              guardians=[OWNER["pid"]])
        save_store(store)
        if out.get("ran"):
            out["summary"] = row["summary"]
        return dumps(out)

    return [home_status, home_act, home_scene, home_act_confirmed, home_approve]


__all__ = ["build_device_tools", "DEVICE_PROMPT", "device_tainted", "load_policy",
           "read_token", "ha_url"]
