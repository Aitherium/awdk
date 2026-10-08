"""Hearth's hands in the physical home: a risk policy, a quorum approval store, a Home
Assistant client and the gate that joins them.

Shared by the local Hearth (:mod:`adk.home.device_tools`) and the hosted one
(the platform's home-devices service). Nothing here knows who the caller is: the
host resolves the person (``{"pid", "name", "role"}``), the household policy and
whether the session is tainted, and hands them in.

* :mod:`.policy` -- pure: entity -> risk class, (person, action, taint) -> decision.
* :mod:`.approvals` -- pure: N-of-M approvals bound to one action digest, with expiry.
* :mod:`.ha` -- Home Assistant: MCP (``/api/mcp``) first, REST (``/api/...``) fallback.
* :mod:`.gate` -- runs a decision: act, ask, or refuse, with a receipt for each.
* :mod:`.notify` -- the ONE seam for "an approval is waiting" (no-op until wired).
"""

from .notify import notify_approval, set_approval_notifier  # noqa: F401
