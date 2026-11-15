"""node_join_path -- the kit's copy of the ONE way a machine joins the fleet.

The platform keeps its own copy, ``node_join_path.py`` in its mesh library (awdk cannot
import it). The platform's ``test_node_join_path.py`` fails when the commands or steps
here differ from it, so ``adk`` help, ``adk volunteer`` errors and the MCP tools
quote one sequence.

Also here, because both were the reason ``adk volunteer`` dead-ended (measured
2026-11-12):

* :func:`remember_peer_id` -- the Conductor's onboard answer names this machine's
  mesh peer (``strata_peer``). Nothing kept it, so after a good join
  ``adk volunteer status`` still said "Cannot resolve peer_id".
* :func:`peer_id_problem` -- that error now says what was looked at and the
  commands that produce a peer id, instead of naming ``adk mesh onboard`` (the
  operator path, which needs a conductor, a tenant and a capability token a
  signed-in person does not have).

Stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger("adk.node_join_path")

OWNER_JOIN_COMMAND = "awrun submit --kind node-op --node <node> --action fleet-join"
DRY_RUN_COMMAND = OWNER_JOIN_COMMAND + " --node-dry-run"
STATUS_COMMAND = "awrun submit --kind node-op --node <node> --action fleet-status"
LEAVE_COMMAND = "awrun submit --kind node-op --node <node> --action fleet-leave"
#: What the machine itself runs first (this kit).
MACHINE_COMMAND = "adk login && adk mesh join"

HUMAN_STEP_LINE = (
    "The one human step: the platform owner runs `%s` on the fleet host from their own "
    "shell (an awiam session in AITHER_SESSION_BEARER whose subject is listed in "
    "AWRUN_NODE_OP_OPERATORS; an agent session has neither); add --node-dry-run to "
    "see the plan first. Everything after it (token, install, approval, read-back) "
    "is automatic." % OWNER_JOIN_COMMAND)

JOIN_STEPS: List[Dict[str, Any]] = [
    {"n": 1, "who": "anyone", "human": False,
     "do": "On the machine: sign in and join the tailnet, so the fleet host can reach "
           "it over ssh.",
     "command": MACHINE_COMMAND},
    {"n": 2, "who": "anyone", "human": False,
     "do": "Declare the machine in your fleet's `nodes.yaml` (`config/nodes.yaml` on "
           "the fleet host) under an id (`<node>`): its tailnet address, a bash ssh "
           "shell, the vault secret for its key.",
     "command": ""},
    {"n": 3, "who": "owner", "human": True,
     "do": "Join it, from the owner's own shell on the fleet host (awiam session in "
           "AITHER_SESSION_BEARER, subject listed in AWRUN_NODE_OP_OPERATORS). Add "
           "--node-dry-run to see the plan and change nothing.",
     "command": OWNER_JOIN_COMMAND},
    {"n": 4, "who": "automatic", "human": False,
     "do": "awrun mints a 600 s token bound to `<node>`, pushes node-install.sh over "
           "ssh, enrolls the node with the placement controller, approves it and "
           "reads it back. The token is never printed.",
     "command": ""},
    {"n": 5, "who": "owner", "human": False,
     "do": "Check it (same shell): status `approved`, and the node is listed by "
           "`fleet_placement_nodes` / `list_cluster_nodes`.",
     "command": STATUS_COMMAND},
]


def join_path_text(indent: str = "  ") -> str:
    """The join path as numbered lines, for help and error text."""
    lines = []
    for s in JOIN_STEPS:
        tag = " [HUMAN: owner]" if s["human"] else (
            " [automatic]" if s["who"] == "automatic" else "")
        lines.append("%s%d.%s %s" % (indent, s["n"], tag, s["do"]))
        if s["command"]:
            lines.append("%s     %s" % (indent, s["command"]))
    return "\n".join(lines)


def join_help() -> str:
    """The block ``adk mesh`` / ``adk volunteer`` print under their own usage."""
    return ("Adding this machine to the fleet (one path):\n" + join_path_text()
            + "\n  " + HUMAN_STEP_LINE)


# ── this machine's mesh peer id ─────────────────────────────────────────────

def node_auth_path() -> Path:
    return Path.home() / ".aither" / "node_auth.json"


def _read_node_auth(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The file's object, ``{}`` when absent, None when unreadable."""
    p = path or node_auth_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def usable_peer_id(value: Any) -> str:
    """``value`` when it is a real Strata peer id (``peer-<hex>``), else ``""``.

    The Conductor answers ``peer-<tenant>-<node>-deferred`` when Strata did not
    register the peer; that is a placeholder no claim will be accepted for."""
    peer = str(value or "").strip()
    if not peer.startswith("peer-") or peer.endswith("-deferred"):
        return ""
    return peer


def remember_peer_id(onboard_response: Mapping[str, Any],
                     path: Optional[Path] = None) -> str:
    """Keep the peer id the Conductor's onboard answer names, in node_auth.json.

    Returns the id that was saved, or ``""`` when the answer carried none (or only
    a deferred placeholder). Other fields of the file are left as they are. Never
    raises: bookkeeping must not fail a join."""
    try:
        peer = usable_peer_id(onboard_response.get("strata_peer")
                              or onboard_response.get("peer_id"))
        if not peer:
            return ""
        p = path or node_auth_path()
        data = _read_node_auth(p)
        if data is None:
            logger.warning("node_auth.json is unreadable; peer id %s not saved", peer)
            return ""
        if data.get("peer_id") == peer:
            return peer
        data["peer_id"] = peer
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return peer
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not save the mesh peer id: %s", exc)
        return ""


def peer_id_problem(path: Optional[Path] = None) -> str:
    """Why no peer id resolves on this machine, what was looked at, and the fix.

    Starts with ``Cannot resolve peer_id`` (callers and tests match on it)."""
    p = path or node_auth_path()
    data = _read_node_auth(p)
    if data is None:
        saw = "%s is unreadable" % p
    elif not data:
        saw = "%s does not exist (this machine never registered)" % p
    elif data.get("node_id"):
        saw = ("%s has node_id %s but no peer_id (registered with Identity, not yet a "
               "mesh peer)" % (p, data.get("node_id")))
    else:
        saw = "%s has no peer_id" % p
    env = "set but empty" if "AITHER_PEER_ID" in os.environ else "unset"
    return (
        "Cannot resolve peer_id: this machine has no mesh peer id yet. Looked at "
        "AITHER_PEER_ID (%s) and found that %s. Get one with `%s` (the join saves the "
        "peer id the mesh registry returns), then run this again; or set AITHER_PEER_ID "
        "to a known id (peer-<12 hex>). Making this machine a FLEET node is a separate "
        "step only the owner can take: `%s` on the fleet host."
        % (env, saw, MACHINE_COMMAND, OWNER_JOIN_COMMAND))
