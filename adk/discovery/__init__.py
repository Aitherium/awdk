"""Session discovery index: what tools, skills, packs and agents THIS caller can use.

One local index, built in the background and read at session start, answers "what do I
have for <task>?" across every place a capability can live:

* the platform gateway's entitled tool list (``/tools/manifest``) and its live
  ToolGraph search (``/v1/tools/search``),
* skills and slash commands under ``<cwd>/.claude``, ``~/.claude`` and installed
  Claude Code plugins, plus a checkout's ``awskills/skills``,
* aw* command-line tools on PATH (checked without spawning them),
* packs: awpack shelves, awdk toolpacks, brain packs and the pack catalog,
* agents: ``.claude/agents/*.md`` and the gateway's agent roster.

The index is two files under ``~/.aither/discovery/``: ``user-<fp>.json`` (sources that
do not depend on the working directory, one per caller: fp is a hash of the bearer, so a
tenant session never reads the owner's gateway list) and ``cwd-<hash>.json`` (one per
directory), so sessions in different worktrees never overwrite each other's skills.
An hourly awrise wake rebuilds both; no session hook builds.

Every card says where it runs (``hosting``: local, fleet or hosted) and every card has
passed the scope filter in :mod:`adk.discovery.scope` -- a card the filter cannot
judge is dropped, never shown.

    adk discover build [--detach] [--force] [--all-cwds]
    adk discover query "segment objects in video masks" [--k 8] [--json]
    adk discover check [--max-age 2h] [--probe "<task>" --expect-domains tools,skills]
"""

from __future__ import annotations

from adk.discovery.check import check_index
from adk.discovery.index import build, index_dir, load_merged, part_path
from adk.discovery.rank import query

__all__ = ["build", "check_index", "index_dir", "load_merged", "part_path", "query"]
