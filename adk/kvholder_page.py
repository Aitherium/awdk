"""The browser KV holder: a page a phone opens, no app to install.

``webui/kvholder/holder.js`` is the PATN v3 state machine (same behaviour as
``adk.kvholder.KVHolder``) with two engines: WebGPU (WGSL online-softmax attention,
keys split across workgroups, an exact log-sum-exp merge) and a CPU fallback. It runs
unchanged in a browser and in Node (the tests drive the CPU engine from Node against the
Python relay). ``webui/kvholder/index.html`` is the page the relay serves at ``/``;
``webui/kvholder/swarm.html`` is the owner's view of every attached holder, at ``/swarm``
(this machine only).
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent / "webui" / "kvholder"


def _read(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8")


def __getattr__(name: str) -> str:
    if name == "HOLDER_JS":
        return _read("holder.js")
    if name == "PAGE_HTML":
        return _read("index.html")
    if name == "SWARM_HTML":
        return _read("swarm.html")
    raise AttributeError(name)
