"""DGG research tools — the actor/evidence doors, as agent tools.

These call the elif_2026 actor endpoint rather than reimplementing any of it.
That is deliberate and it is the whole point of the split: the RESOLUTION
POLICY -- which role may auto-link a fuzzy match, at what score, with what
margin -- lives in the server's database where an owner can change it without
touching an agent, a deploy, or this file. An agent that scored its own matches
would be a second authority for the one question that must have exactly one.

So every tool here is a thin, honest client. It reports the server's verdict
verbatim, including refusals, and it never retries a refusal with a different
spelling.

Endpoint base comes from DGG_API_BASE (default http://localhost:8000). The
caller's role comes from DGG_ACTOR_ROLE and selects a POLICY, never a
permission -- it grants nothing; the server's own auth still sits in front of
every write door.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://localhost:8000"
TIMEOUT = 30


def _base() -> str:
    return os.environ.get("DGG_API_BASE", DEFAULT_BASE).rstrip("/")


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    role = os.environ.get("DGG_ACTOR_ROLE", "").strip()
    user = os.environ.get("DGG_ACTOR_USER", "").strip()
    if role:
        h["X-Actor-Role"] = role
    if user:
        h["X-Actor-User"] = user
    return h


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    """One HTTP call. A refusal is a RESULT, not an exception.

    A 400/403/404 from this API carries the reason and the fix in its body, and
    turning that into a raised error would throw away the most useful part --
    the agent then sees "tool failed" and retries blindly, which is precisely
    the behaviour the server's refusals exist to prevent.
    """
    url = _base() + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            body = {"error": f"HTTP {e.code}"}
        body["http_status"] = e.code
        return body
    except (urllib.error.URLError, TimeoutError) as e:
        # Could-not-reach is NOT "no such actor". Conflating them is how an
        # agent reports a name as new because the server was down.
        return {"error": f"could not reach {url}: {e}",
                "unreachable": True,
                "why_it_matters": "this is not the same as the actor being absent"}


# --------------------------------------------------------------------- tools

def actor_resolve(name: str) -> dict:
    """Is this person already in the record? Never writes.

    Returns every candidate with a score, the margin over the runner-up, and
    what THIS caller's policy would do about it. Call this before asserting
    anything about a person.
    """
    q = urllib.parse.urlencode({"q": name})
    out = _call("GET", f"/api/actors/resolve/?{q}")
    if out.get("verdict") in ("park", "propose"):
        out["read_this"] = (
            "This is a correct outcome, not a retry signal. Do NOT re-send with "
            "a different spelling to force a link.")
    return out


def actor_references(actor_id: str) -> dict:
    """Everywhere an actor appears: frames as actor, push actor and target,
    plus the evidence linked to them. `actor_id` is the stable public id --
    quote that, never the name or the slug."""
    return _call("GET", f"/api/actors/{urllib.parse.quote(actor_id)}/")


def evidence_push(evidence_id: str, actor: str, action: str, summary: str,
                  occurred_date: str = "", push_actor: str = "",
                  target: str = "", is_omission: bool = False,
                  window_start: str = "", window_end: str = "",
                  claim_type: str = "fact", locator: str = "") -> dict:
    """Record an action by an actor, against the artifact it came from.

    `evidence_id` is required and is checked: an action with no artifact behind
    it is an assertion, not evidence. Every act needs `occurred_date`; only an
    omission may lack one, and then it needs a window.

    A 202 with `created: false` means a name did not resolve. Nothing is lost --
    the mention is already recorded and carries an id a person can land later.
    """
    body = {"evidence_id": evidence_id, "actor": actor, "action": action,
            "summary": summary, "claim_type": claim_type}
    for k, v in (("occurred_date", occurred_date), ("push_actor", push_actor),
                 ("target", target), ("window_start", window_start),
                 ("window_end", window_end), ("locator", locator)):
        if v:
            body[k] = v
    if is_omission:
        body["is_omission"] = True
    return _call("POST", "/api/actions/push/", body)


def mention_verify(mention_id: str, decision: str, actor_id: str = "",
                   reason: str = "") -> dict:
    """A PERSON confirms or rejects a proposed match.

    Exposed as a tool so an agent can carry a human's decision, never so it can
    make one. The server refuses self-verification wherever the policy says so,
    and that refusal is the point.
    """
    body = {"decision": decision}
    if actor_id:
        body["actor_id"] = actor_id
    if reason:
        body["reason"] = reason
    return _call("PATCH", f"/api/mentions/{urllib.parse.quote(mention_id)}/", body)


def corpus_null(corpus: str, query: str, window: str, control_query: str = "",
                control_hits: int = -1) -> dict:
    """Record a NULL with the boundaries that make it a finding.

    "I found nothing" is a statement about method, not about the world, so this
    refuses a null that does not say what was searched and over what period --
    and asks for the control query that proves the instrument was working. A
    zero from a broken tool and a real zero are indistinguishable without it.

    Returns the structured null for the caller to file; it writes nothing.
    """
    missing = [n for n, v in (("corpus", corpus), ("query", query),
                              ("window", window)) if not str(v).strip()]
    if missing:
        return {"error": "a null needs its boundaries: " + ", ".join(missing),
                "why": ("without the corpus and the period, a null cannot be "
                        "checked, reproduced, or disagreed with")}
    out = {"kind": "null_result", "corpus": corpus, "query": query,
           "window": window}
    if control_query and control_hits >= 0:
        out["control"] = {"query": control_query, "hits": control_hits}
        out["instrument_proven"] = control_hits > 0
        if control_hits == 0:
            out["read_this"] = (
                "The CONTROL returned nothing, so this null is unsafe: you have "
                "not shown the instrument works. Do not report the zero yet.")
    else:
        out["read_this"] = (
            "No control query recorded. Run one you KNOW should return "
            "something before trusting this zero.")
    return out


TOOLS = (actor_resolve, actor_references, evidence_push, mention_verify,
         corpus_null)


def register(registry) -> int:
    """Register these tools on a tool REGISTRY. Returns how many landed.

    The loader hands over `agent._tools` / `agent.tools` / `agent.tool_registry`
    -- a registry, not the agent -- and calls `registry.register(fn)`. Taking an
    agent here instead is silent: the loader logs one line and registers ZERO
    tools, so the pack discovers, licenses and activates cleanly while the agent
    behaves exactly as though the feature were configured off. Caught by driving
    the real loader rather than by reading it.

    One bad tool never sinks the pack; each failure is logged and skipped.
    """
    n = 0
    for fn in TOOLS:
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:      # noqa: BLE001 - one tool must not sink the pack
            logging.getLogger(__name__).warning(
                "dgg_research: could not register %s: %s", fn.__name__, exc)
    return n
