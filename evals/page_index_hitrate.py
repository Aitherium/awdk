"""Hit-rate of page_index.tree_search (lexical, or --router) vs flat 2 KB windows.

A question HITS when a returned section (tree) / window (flat) overlaps the expected
section's own lines. Questions are paraphrases written by hand from the section text.
Usage: python evals/page_index_hitrate.py <awdk dir> [--router]   (from awdk/)
"""
import asyncio
import sys
from pathlib import Path

from adk.graph_rag.page_index import (
    _own_ranges,
    build_tree,
    lexical_score,
    summarize_tree,
    tree_search,
)

Q = [
    ("SELF_HOSTING_RUNBOOK.md",
     "where is the working-set memory stored locally and why",
     "Local Tier: Qdrant (Working Set)"),
    ("SELF_HOSTING_RUNBOOK.md",
     "what storage backs the durable cloud memory used for training",
     "Cloud Tier: Nexus (Durable RAG)"),
    ("SELF_HOSTING_RUNBOOK.md",
     "what does provisioning qdrant do with the generated api key",
     "What Happens@108"),
    ("SELF_HOSTING_RUNBOOK.md",
     "what happens when I join the mesh, does it make a wireguard key",
     "What Happens@159"),
    ("SELF_HOSTING_RUNBOOK.md",
     "how often does graph memory push new nodes to the cloud",
     "How It Works"),
    ("SELF_HOSTING_RUNBOOK.md",
     "the conductor is unreachable and joining fails, how do I debug",
     "Mesh Join Fails"),
    ("SELF_HOSTING_RUNBOOK.md", "how do I back up the local sqlite memory", "Maintenance"),
    ("SELF_HOSTING_RUNBOOK.md", "can nexus be used without qdrant at all", "FAQ"),
    ("SELF_HOSTING_RUNBOOK.md",
     "should api keys be committed and is tls verified in the fleet",
     "Security Notes"),
    ("KVHOLDER.md",
     "a phone opens a browser page and dials out to the relay over usb",
     "Phones: no app to install"),
    ("KVHOLDER.md",
     "run a holder from a pixel linux terminal with pip",
     "A Python holder (Pixel Linux terminal, a laptop, a server)"),
    ("KVHOLDER.md",
     "two sessions with the same system prompt reuse keys already published",
     "Shared pool: sessions reuse a prefix already on the holders"),
    ("KVHOLDER.md", "how many old tokens fit in 8 GB of free memory", "Plan and check"),
    ("KVHOLDER.md",
     "holder discovers the relay on a lan without a token and waits for approval",
     "Mesh: holders that find the relay themselves"),
    ("KVHOLDER.md",
     "which port does the llama.cpp engine connect to on the relay",
     "The engine side"),
    ("AGENT_DEV_GUIDE.md",
     "do self hosted keys get token caps on reasoning effort",
     "✅ Self-hosted / BYO-key is uncapped by default"),
    ("AGENT_DEV_GUIDE.md",
     "mount and unmount a tool pack on a running agent",
     "Switch tool packs on and off in a live agent"),
    ("AGENT_DEV_GUIDE.md",
     "streaming returns no usage so how do I count tokens",
     "5. Token accounting (used vs. saved)"),
    ("AGENT_DEV_GUIDE.md",
     "fastapi cannot resolve a locally imported Request annotation",
     "🚨 FastAPI + `from __future__ import annotations`"),
    ("AGENT_DEV_GUIDE.md",
     "let the agent author its own packs as tools",
     "Creator mode: the agent builds its own packs"),
    ("WORLD_MODEL.md",
     "which mode lets the world model reorder tool candidates and what are the risks",
     "STEER Mode Is Dangerous"),
    ("WORLD_MODEL.md",
     "what is the default goal weighting for success recall errors latency",
     "AITHER_AGENT_WM_GOAL"),
    ("WORLD_MODEL.md", "force a refit of the model now for manual testing", "adk wm train <agent>"),
    ("WORLD_MODEL.md",
     "what does one line of the transitions jsonl file contain",
     "Transition Buffer Format"),
    ("WORLD_MODEL.md",
     "register my own backend factory instead of the builtin model",
     "Custom Backends"),
]


def find(root, title):
    want_line = None
    if "@" in title:
        title, at = title.rsplit("@", 1)
        want_line = int(at)
    for n in root.walk():
        if n.title == title and (want_line is None or n.start_line == want_line):
            return n
    raise KeyError(title)


def overlap(a0, a1, ranges):
    return any(a0 <= b1 and b0 <= a1 for b0, b1 in ranges)


def flat_windows(lines, size=2000):
    out, start, buf = [], 1, 0
    for i, line in enumerate(lines, 1):
        buf += len(line) + 1
        if buf >= size:
            out.append((start, i))
            start, buf = i + 1, 0
    if start <= len(lines):
        out.append((start, len(lines)))
    return out


async def main(awdk: Path, use_router: bool):
    router = None
    trees = {}
    if use_router:
        from adk.llm import LLMRouter
        router = LLMRouter()
    tree_hit = {1: 0, 3: 0}
    flat_hit = {1: 0, 3: 0}
    tree_chars = flat_chars = 0
    for doc, q, title in Q:
        md = (awdk / "docs" / doc).read_text(encoding="utf-8")
        lines = md.splitlines()
        if doc not in trees:
            trees[doc] = build_tree(md)
            if router is not None:
                await summarize_tree(trees[doc], md, router=router)
        root = trees[doc]
        target = _own_ranges(find(root, title))
        hits = await tree_search(root, md, q, router=router, top_k=3)
        wins = sorted(flat_windows(lines),
                      key=lambda w: lexical_score(q, "\n".join(lines[w[0] - 1:w[1]])),
                      reverse=True)
        tree_chars += sum(len(h["text"]) for h in hits[:3])
        flat_chars += sum(len(chr(10).join(lines[a - 1:b])) for a, b in wins[:3])
        for k in (1, 3):
            tree_hit[k] += any(
                any(overlap(a, b, target) for a, b in _own_ranges(root.find(h["node_id"])))
                for h in hits[:k])
            flat_hit[k] += any(overlap(a, b, target) for a, b in wins[:k])
    n = len(Q)
    print(f"{n} questions over 4 awdk docs ({'router' if router else 'lexical, no model'})")
    print(f"  mean chars returned in top-3: tree {tree_chars // n}  flat {flat_chars // n}")
    for k in (1, 3):
        print(f"  hit@{k}: tree {tree_hit[k]}/{n} = {tree_hit[k]/n:.2f}   "
              f"flat 2KB windows {flat_hit[k]}/{n} = {flat_hit[k]/n:.2f}")


asyncio.run(main(Path(sys.argv[1]), '--router' in sys.argv))
