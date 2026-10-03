"""``/kvholder/*`` on the harness daemon: the KV relay on this host, for the MCP gateway.

The relay's owner routes are loopback + master token, so a caller in a container (the MCP
gateway) cannot reach them. These routes run in the daemon, on the relay host, and call
:mod:`adk.kvholder_mesh` there; the master token never leaves this machine.

Who may do what:

* ``GET /kvholder/status`` -- any principal the daemon authenticates and scopes here.
* ``POST /kvholder/{pending,join,elastic}`` -- the OWNER only: the daemon's own owner
  principal (the root bearer, a local caller), or a request carrying the gateway's
  Ed25519 owner assertion (:mod:`adk.harnesses.owner_steer`, target ``kvholder``) over the
  exact action text. A scoped ``agent`` token alone is a transport, never authority.

No response carries a token: a mesh holder collects its join token from the relay, and
``elastic`` hands tokens straight to the launcher.
"""

import json

from adk.harnesses.owner_steer import OWNER_ASSERTION_FIELD

#: The assertion's ``to``: a kvholder assertion can never steer a session, and back.
TARGET = "kvholder"
ACTIONS = ("pending", "join", "elastic")


def parse_action(body, action, principal_plan, verifier):
    """``(args, error)``: the action's arguments if the caller may run it, else an error.

    ``body`` is ``{"id", "actor", "payload": {"text": json, "owner_assertion"?}}``; the
    text is ``{"action": ..., **args}`` and its action must equal the route's.
    """
    if not isinstance(body, dict):
        return None, "body must be an object"
    payload = body.get("payload")
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        return None, "payload.text must be the action as JSON"
    if principal_plan != "owner":
        if verifier is None:
            return None, "owner only (this daemon pins no owner key)"
        if not isinstance(payload.get(OWNER_ASSERTION_FIELD), dict):
            return None, "owner only"
        actor = body.get("actor") or {}
        if not (isinstance(actor, dict) and actor.get("kind") == "human"):
            return None, "owner only"
        ok, why = verifier.verify(body, TARGET)
        if not ok:
            return None, f"owner only ({why})"
    try:
        args = json.loads(text)
    except ValueError:
        return None, "payload.text is not JSON"
    if not isinstance(args, dict) or args.get("action") != action:
        return None, "the signed action does not match the route"
    return args, ""


def run_action(action, args):
    from adk import kvholder_mesh as mesh

    if action == "pending":
        return {"pending": mesh.pending()}
    if action == "join":
        return mesh.decide(str(args.get("code") or ""), args.get("decision") != "deny")
    return mesh.elastic(
        count=int(args.get("count") or 1),
        minutes=int(args.get("minutes") or 30),
        max_mb=int(args.get("max_mb") or 4096),
        workflow=str(args.get("workflow") or "kvholder-runner.yml"),
        ref=str(args.get("ref") or "develop"),
        dry_run=args.get("dry_run", True) is not False,
    )


def mount(app, auth, verifier) -> None:
    """Add the routes to the daemon's FastAPI ``app`` (``auth`` = its bearer dependency)."""
    from fastapi import Body, Depends, HTTPException

    @app.get("/kvholder/status")
    def kvholder_status(principal=Depends(auth)):
        from adk import kvholder_mesh as mesh

        try:
            return mesh.status()
        except (OSError, RuntimeError) as e:
            return {"error": str(e)}

    @app.post("/kvholder/{action}")
    def kvholder_action(action: str, body: dict = Body(...), principal=Depends(auth)):
        if action not in ACTIONS:
            raise HTTPException(status_code=404, detail="no such kvholder action")
        args, error = parse_action(body, action, getattr(principal, "plan", ""), verifier)
        if error:
            raise HTTPException(status_code=403, detail=error)
        try:
            return run_action(action, args)
        except (OSError, RuntimeError, ValueError) as e:
            return {"error": str(e)}
