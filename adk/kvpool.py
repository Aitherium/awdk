"""adk kvpool — engine sessions share KV already resident on holders.

Two engine sessions that start from the same prompt prefix (a system prompt, a repository,
a document) produce byte-identical keys and values for that prefix. Without a pool each one
ships its own copy to its own holders. With it the first session PUBLISHES the prefix as
shared blocks and every later session ATTACHES to them: it appends only its own tail.

Keying. A block is ``BLOCK_TOKENS`` positions (256 by default). Its key is

    (model fingerprint, layer layout, chain hash of every token up to the block's end)

The chain hash is ``h_j = sha256(h_{j-1} || tokens of block j as uint32 LE)``, seeded with
the model fingerprint, so a block key names the whole prefix before it and its position.
The layer layout is the holder's own CONFIG (layers, KV heads, dims, row format) plus its
store, so blocks never cross models or formats.

Copy-on-write. Shared blocks are read-only. A session's own keys go to its private range
after the shared prefix; attention runs over the shared blocks then the private tail in one
online softmax, so the exact log-sum-exp merge across holders still holds. A TRUNCATE below
the shared boundary copies the kept part of the straddling block into the private range and
drops the session's references to the rest.

Refcounts and eviction. Each session holds one reference per shared block. A block nobody
references stays resident (a warm prefix for the next session) until a holder needs room:
then the least recently used unreferenced blocks are evicted by bytes. A referenced block is
never evicted; when there is no room the APPEND is refused, exactly as before.

Protocol (PATN pool extension, adk): four message types a v3/v4 holder does not know. Such a
holder answers ERR "unknown message", which every caller here treats as "no pool": the
engine appends the whole prefix, as it always did.

    SESSION       <Q sid>                         bind this link to session sid (0 = default)
    POOL_ATTACH   <32s fp><I block><I n> + n*32   reply OK <I tokens attached>
    POOL_PUBLISH  same                            reply OK <I blocks shared>
    POOL_STATUS   (empty)                         reply OK <json>

The platform's Workspace Shared-Prefix Directory (Nexus ``/kv-cache/register-prefix``,
``/kv-cache/prefix-directory``) answers "which nodes hold prefix H warm". ``NexusPrefixDirectory``
is a thin client for it: a holder registers each published chain head and releases it on
eviction, so a router can send a session to the relay that already holds its prefix.
"""

from __future__ import annotations

import hashlib
import json
import logging
import ssl
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field

log = logging.getLogger("adk.kvpool")

SESSION, POOL_ATTACH, POOL_PUBLISH, POOL_STATUS = 20, 21, 22, 23
POOL_TYPES = (SESSION, POOL_ATTACH, POOL_PUBLISH, POOL_STATUS)
BLOCK_TOKENS = 256
MAX_SESSIONS = 256
POOL_REQ = struct.Struct("<32sII")
SESSION_REQ = struct.Struct("<Q")


# ---------------------------------------------------------------- keys


def model_fingerprint(*parts: str | bytes) -> bytes:
    """32 bytes naming a model: pass its name, weights digest, tokenizer, anything that changes KV."""
    h = hashlib.sha256(b"adk-kvpool-model\0")
    for p in parts:
        h.update(p if isinstance(p, bytes) else p.encode())
        h.update(b"\0")
    return h.digest()


def chain_hashes(tokens, model_fp: bytes, block: int = BLOCK_TOKENS) -> list[bytes]:
    """One 32-byte key per FULL block of ``tokens``; a trailing partial block is not shareable."""
    out, prev = [], model_fp
    toks = list(tokens)
    for j in range(len(toks) // block):
        seg = toks[j * block : (j + 1) * block]
        prev = hashlib.sha256(prev + struct.pack(f"<{block}I", *seg)).digest()
        out.append(prev)
    return out


def pack_pool_req(model_fp: bytes, block: int, hashes: list[bytes]) -> bytes:
    if len(model_fp) != 32 or any(len(h) != 32 for h in hashes):
        raise ValueError("fingerprint and hashes are 32 bytes each")
    return POOL_REQ.pack(model_fp, block, len(hashes)) + b"".join(hashes)


def unpack_pool_req(p: bytes) -> tuple[bytes, int, list[bytes]]:
    if len(p) < POOL_REQ.size:
        raise ValueError("short pool request")
    fp, block, n = POOL_REQ.unpack_from(p)
    if not 0 < block <= 1 << 20 or len(p) != POOL_REQ.size + 32 * n:
        raise ValueError("bad pool request")
    o = POOL_REQ.size
    return fp, block, [p[o + 32 * i : o + 32 * (i + 1)] for i in range(n)]


# ---------------------------------------------------------------- the catalog


@dataclass
class Block:
    """``tokens`` positions of every layer, read-only. ``layers[l]`` is one chunk tuple."""

    ns: tuple  # (model fp, layout fp, block tokens)
    key: bytes
    index: int  # block number in its chain (position = index * tokens)
    tokens: int
    layers: list
    nbytes: int
    refs: int = 0
    last_used: float = field(default_factory=time.monotonic)


class KVPool:
    """Content-addressed shared blocks on one holder: refcounts, LRU eviction by bytes.

    Not thread-safe on its own: the holder calls it under its state lock.
    """

    def __init__(self) -> None:
        self.blocks: OrderedDict[tuple, Block] = OrderedDict()  # LRU order: oldest first
        self.bytes = 0
        self.evicted = 0
        self.evicted_bytes = 0
        self.attached_tokens = 0  # tokens sessions did NOT have to append
        self.listeners: list = []  # objects with on_publish(block) / on_evict(block)

    def get(self, ns: tuple, key: bytes) -> Block | None:
        return self.blocks.get((ns, key))

    def chain(self, ns: tuple, keys: list[bytes]) -> list[Block]:
        """The longest resident prefix of ``keys``."""
        out = []
        for k in keys:
            b = self.blocks.get((ns, k))
            if b is None:
                break
            out.append(b)
        return out

    def insert(self, blk: Block) -> Block:
        """Add ``blk`` or return the resident block with the same key (dedup)."""
        have = self.blocks.get((blk.ns, blk.key))
        if have is not None:
            return have
        self.blocks[(blk.ns, blk.key)] = blk
        self.bytes += blk.nbytes
        self._notify("on_publish", blk)
        return blk

    def acquire(self, blocks: list[Block]) -> None:
        now = time.monotonic()
        for b in blocks:
            b.refs += 1
            b.last_used = now
            self.blocks.move_to_end((b.ns, b.key))

    def release(self, blocks: list[Block]) -> None:
        now = time.monotonic()
        for b in blocks:
            b.refs = max(0, b.refs - 1)
            b.last_used = now

    def evict(self, need: int) -> int:
        """Free at least ``need`` bytes from unreferenced blocks, oldest first. Returns freed.

        Within one release (blocks a session let go together) the deepest block goes first: a
        chain is only reachable from its head, so evicting the head first would strand the rest.
        """
        freed = 0
        cold = sorted((b for b in self.blocks.values() if not b.refs), key=_evict_order)
        for b in cold:
            if freed >= need:
                break
            del self.blocks[(b.ns, b.key)]
            self.bytes -= b.nbytes
            freed += b.nbytes
            self.evicted += 1
            self.evicted_bytes += b.nbytes
            self._notify("on_evict", b)
        return freed

    def _notify(self, what: str, blk: Block) -> None:
        for lst in self.listeners:
            fn = getattr(lst, what, None)
            if fn is None:
                continue
            try:
                fn(blk)
            except Exception as e:  # a directory listener never breaks the holder
                log.warning("kvpool: listener %s failed: %s", what, e)

    def status(self) -> dict:
        refd = [b for b in self.blocks.values() if b.refs]
        return {
            "blocks": len(self.blocks),
            "bytes": self.bytes,
            "referenced_blocks": len(refd),
            "referenced_bytes": sum(b.nbytes for b in refd),
            "evicted": self.evicted,
            "evicted_bytes": self.evicted_bytes,
            "attached_tokens": self.attached_tokens,
            "chains": sorted({b.ns[0].hex()[:12] for b in self.blocks.values()}),
        }


def _evict_order(b: Block) -> tuple:
    return (b.last_used, -b.index)


def layout_fingerprint(cfg_bytes: bytes, store: str) -> bytes:
    return hashlib.sha256(b"adk-kvpool-layout\0" + cfg_bytes + store.encode()).digest()


# ---------------------------------------------------------------- engine side (any PATN client)


NO_POOL = "no pool"  # a relay's ERR when one of its holders lacks the extension


def _unknown(e: Exception) -> bool:
    return "unknown message" in str(e) or str(e).startswith(NO_POOL)


def open_session(client, sid: int) -> bool:
    """Bind ``client`` (a KVHolderClient) to session ``sid``. False: the holder has no pool."""
    try:
        client._call(SESSION, SESSION_REQ.pack(sid))
        return True
    except RuntimeError as e:
        if _unknown(e):
            return False
        raise


def attach(client, model_fp: bytes, hashes: list[bytes], block: int = BLOCK_TOKENS) -> int:
    """Attach the resident part of a prefix right after CONFIG; returns tokens attached.

    The engine then appends from that position on. 0 when the holder has no pool.
    """
    try:
        rep = client._call(POOL_ATTACH, pack_pool_req(model_fp, block, hashes))
    except RuntimeError as e:
        if _unknown(e):
            return 0
        raise
    return struct.unpack_from("<I", rep)[0]


def publish(client, model_fp: bytes, hashes: list[bytes], block: int = BLOCK_TOKENS) -> int:
    """Offer the session's first ``len(hashes)`` blocks for sharing; returns blocks shared."""
    try:
        rep = client._call(POOL_PUBLISH, pack_pool_req(model_fp, block, hashes))
    except RuntimeError as e:
        if _unknown(e):
            return 0
        raise
    return struct.unpack_from("<I", rep)[0]


def status(client) -> dict | None:
    try:
        return json.loads(client._call(POOL_STATUS).decode())
    except RuntimeError as e:
        if _unknown(e):
            return None
        raise


# ---------------------------------------------------------------- relay side


@dataclass
class _RelaySessions:
    lock: threading.Lock = field(default_factory=threading.Lock)
    sid: int = 0
    saved: dict = field(default_factory=dict)  # sid -> (cfg, n, broken)
    linked: dict = field(default_factory=dict)  # id(holder) -> sid its link is bound to
    used: bool = False  # a non-default session was ever opened


_RELAYS: dict[int, _RelaySessions] = {}
_RELAYS_LOCK = threading.Lock()


def _sessions(relay) -> _RelaySessions:
    with _RELAYS_LOCK:
        s = _RELAYS.get(id(relay))
        if s is None or getattr(relay, "_kvpool_sessions", None) is not s:
            s = _RELAYS[id(relay)] = _RelaySessions()
            relay._kvpool_sessions = s
        return s


class EngineConn:
    """Per engine connection on the relay: which session its messages belong to."""

    def __init__(self) -> None:
        self.sid = 0


def relay_call(relay, conn: EngineConn, msg: bytes, expect_reply: bool = True) -> bytes:
    """``Relay.call`` with sessions and the pool messages. Without them it IS ``Relay.call``."""
    from adk import kvholder as kv

    _, mtype, n = kv.HDR.unpack_from(msg)
    p = msg[kv.HDR.size : kv.HDR.size + n]
    rs = _sessions(relay)
    with rs.lock:
        if mtype == SESSION:
            if len(p) != SESSION_REQ.size:
                return _rmsg(kv.ERR, b"bad SESSION")
            sid = SESSION_REQ.unpack(p)[0]
            prev, used = rs.sid, rs.used
            err = _switch(relay, rs, sid)
            if err:  # put the relay back as it was: this connection stays on its session
                _switch(relay, rs, max(prev, 0))
                rs.used = used
                return _rmsg(kv.ERR, err.encode())
            conn.sid = sid
            return _rmsg(kv.OK, b"")
        if rs.used:
            err = _switch(relay, rs, conn.sid)
            if err:
                return _rmsg(kv.ERR, err.encode())
        if mtype in (POOL_ATTACH, POOL_PUBLISH, POOL_STATUS):
            with relay.lock:
                if not relay.holders:
                    return _rmsg(kv.ERR, b"no holder attached")
                try:
                    return _relay_pool(relay, mtype, p)
                except kv_lost() as e:
                    return _rmsg(kv.ERR, (relay.broken or str(e)).encode())
        rep = relay.call(msg, expect_reply=expect_reply)
        if mtype == kv.BYE and conn.sid:
            # every holder dropped that session and fell back to its default link binding
            rs.saved.pop(conn.sid, None)
            relay.cfg, relay.n, relay.broken = None, [], ""
            rs.sid = -1  # nothing loaded: the next call loads its session
            rs.linked = {k: 0 for k in rs.linked}
        return rep


def kv_lost():
    from adk.kvholder_net import HolderLostError

    return HolderLostError


def _rmsg(mtype: int, payload: bytes) -> bytes:
    from adk import kvholder as kv

    return kv.HDR.pack(kv.MAGIC, mtype, len(payload)) + payload


def _switch(relay, rs: _RelaySessions, sid: int) -> str:
    """Load session ``sid`` into the relay and bind every holder link to it. '' when done."""
    from adk import kvholder as kv

    with relay.lock:
        if sid != rs.sid:
            if rs.sid >= 0:
                rs.saved[rs.sid] = (relay.cfg, relay.n, relay.broken)
            relay.cfg, relay.n, relay.broken = rs.saved.pop(sid, (None, [], ""))
            rs.sid = sid
            if relay.cfg is not None:
                for h in relay.holders:
                    relay._place(h)
        if sid:
            rs.used = True
        if not rs.used:
            return ""
        live = {id(h) for h in relay.holders}
        rs.linked = {k: v for k, v in rs.linked.items() if k in live}
        for h in list(relay.holders):
            bound = rs.linked.get(id(h), 0)
            if bound == sid:
                continue
            try:
                rep = relay._call(h, _rmsg(SESSION, SESSION_REQ.pack(sid)))
                if _rtype(rep) == kv.OK and relay.cfg is not None and id(h) not in rs.linked:
                    # a holder that joined while another session was loaded: give it this
                    # session's CONFIG (its range is empty: it is the newest)
                    rep = relay._call(h, _rmsg(kv.CONFIG, relay.cfg.pack()))
            except kv_lost() as e:
                return str(e)
            if _rtype(rep) != kv.OK:
                return f"{NO_POOL}: holder {h.device} does not know SESSION"
            rs.linked[id(h)] = sid
        return ""


def _rtype(rep: bytes) -> int:
    from adk import kvholder as kv

    return kv.HDR.unpack_from(rep)[1]


def _relay_pool(relay, mtype: int, p: bytes) -> bytes:
    """POOL_* through a relay: each holder gets the blocks inside its own key range."""
    from adk import kvholder as kv

    if mtype == POOL_STATUS:
        out = []
        for h in list(relay.holders):
            rep = relay._call(h, _rmsg(POOL_STATUS, b""))
            body = rep[kv.HDR.size :]
            st = json.loads(body.decode()) if _rtype(rep) == kv.OK else None
            out.append({"device": h.device, "off": h.off, "cap": h.cap, "status": st})
        return _rmsg(kv.OK, json.dumps({"relay": True, "holders": out}).encode())
    if relay.cfg is None:
        return _rmsg(kv.ERR, b"pool: CONFIG first")
    try:
        fp, block, hashes = unpack_pool_req(p)
    except ValueError as e:
        return _rmsg(kv.ERR, str(e).encode())
    if mtype == POOL_ATTACH and any(relay.n):
        return _rmsg(kv.ERR, b"POOL_ATTACH needs an empty session (right after CONFIG)")
    held = min(relay.n) if relay.n else 0
    total = 0
    for h in sorted(relay.holders, key=lambda x: x.off):
        if h.off != total or h.off % block:
            break  # sharing is contiguous from position 0 and block-aligned per holder
        j0 = h.off // block
        nb = len(hashes) - j0 if h.cap is None else min(len(hashes) - j0, h.cap // block)
        if mtype == POOL_PUBLISH:
            nb = min(nb, (held - h.off) // block)
        if nb <= 0:
            break
        rep = relay._call(h, _rmsg(mtype, pack_pool_req(fp, block, hashes[j0 : j0 + nb])))
        if _rtype(rep) != kv.OK:
            break  # a holder without the pool: what is shared so far stands
        got = struct.unpack_from("<I", rep, kv.HDR.size)[0]
        if mtype == POOL_ATTACH:
            total += got
            if got < nb * block or (h.cap is not None and got < h.cap):
                break
        else:
            total += got * block
            if got < nb:
                break
    if mtype == POOL_ATTACH:
        relay.n = [total] * relay.cfg.n_layer
        return _rmsg(kv.OK, struct.pack("<I", total))
    return _rmsg(kv.OK, struct.pack("<I", total // block))


# ---------------------------------------------------------------- platform directory (Nexus WSPD)


class NexusPrefixDirectory:
    """Thin client for the platform's Workspace Shared-Prefix Directory.

    ``POST {base}/kv-cache/register-prefix`` and ``GET {base}/kv-cache/prefix-directory``.
    The directory is a routing hint ("which nodes hold prefix H warm"), never the data: KV
    bytes stay on the holders. Attach it to a holder's pool with ``holder.pool.listeners``;
    every published chain block is registered and every evicted one released, off the
    holder's lock and never fatal. The caller supplies the credential header; this client
    reads no secret itself.
    """

    def __init__(
        self,
        base_url: str,
        tenant_slug: str,
        workspace_id: str,
        node_id: str,
        headers: dict | None = None,
        share_scope: str = "workspace",
        entitlement_fingerprint: tuple = (),
        timeout: float = 2.0,
        ssl_context: ssl.SSLContext | None = None,
        background: bool = True,
    ):
        self.base = base_url.rstrip("/")
        self.tenant, self.workspace, self.node = tenant_slug, workspace_id, node_id
        self.headers = dict(headers or {})
        self.scope = share_scope
        self.fp = list(entitlement_fingerprint)
        self.timeout = timeout
        self.ctx = ssl_context
        self.background = background
        self.errors = 0

    def _post(self, body: dict) -> bool:
        req = urllib.request.Request(
            f"{self.base}/kv-cache/register-prefix",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **self.headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                return r.status == 200
        except OSError as e:
            self.errors += 1
            log.warning("kvpool: prefix directory unreachable: %s", e)
            return False

    def register(self, content_hash: str, block_count: int, token_count: int, released=False):
        body = {
            "tenant_slug": self.tenant,
            "workspace_id": self.workspace,
            "content_hash": content_hash,
            "node_id": self.node,
            "tier": "kvholder",
            "block_count": block_count,
            "token_count": token_count,
            "share_scope": self.scope,
            "entitlement_fingerprint": self.fp,
            "released": released,
        }
        return self._post(body)

    def lookup(self, content_hash: str) -> list[str]:
        """Node ids holding ``content_hash`` warm for this workspace ([] when none or down)."""
        q = urllib.parse.urlencode(
            {
                "tenant_slug": self.tenant,
                "workspace_id": self.workspace,
                "content_hash": content_hash,
            }
        )
        req = urllib.request.Request(
            f"{self.base}/kv-cache/prefix-directory?{q}", headers=self.headers
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                data = json.loads(r.read())
        except (OSError, ValueError) as e:
            self.errors += 1
            log.warning("kvpool: prefix directory unreachable: %s", e)
            return []
        for e in data.get("entries", []):
            if e.get("content_hash") == content_hash:
                return [n for n in e.get("node_ids") or [] if n]
        return []

    def _later(self, fn, *a, **kw) -> None:
        if self.background:
            threading.Thread(target=fn, args=a, kwargs=kw, daemon=True).start()
        else:
            fn(*a, **kw)

    # pool listener
    def on_publish(self, blk: Block) -> None:
        self._later(self.register, blk.key.hex(), blk.index + 1, (blk.index + 1) * blk.tokens)

    def on_evict(self, blk: Block) -> None:
        self._later(self.register, blk.key.hex(), blk.index + 1, (blk.index + 1) * blk.tokens, True)


# ---------------------------------------------------------------- CLI


def run_pool(args) -> int:
    """``adk kvholder pool status [--target HOST:PORT]``."""
    from adk import kvholder as kv

    if getattr(args, "pool_action", None) != "status":
        print("usage: adk kvholder pool status [--target HOST:PORT]", file=sys.stderr)
        return 2
    host, _, port = args.target.partition(":")
    try:
        c = kv.KVHolderClient(host, int(port or kv.DEFAULT_PORT), timeout=10)
        st = status(c)
        c.close()
    except (OSError, RuntimeError, ConnectionError, ValueError) as e:
        print(f"kvholder pool {args.target}: {e}", file=sys.stderr)
        return 1
    if st is None:
        print(f"{args.target}: no shared pool (a plain PATN v3/v4 holder)")
        return 1
    print(json.dumps(st, indent=1))
    return 0


__all__ = [
    "BLOCK_TOKENS",
    "Block",
    "EngineConn",
    "KVPool",
    "NexusPrefixDirectory",
    "POOL_ATTACH",
    "POOL_PUBLISH",
    "POOL_STATUS",
    "SESSION",
    "attach",
    "chain_hashes",
    "model_fingerprint",
    "open_session",
    "publish",
    "relay_call",
    "status",
]
