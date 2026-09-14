"""AitherNet Chat — Lightweight IRC-compatible chat relay for awnodes.

Every ADK node running `aither-serve` becomes a chat relay node.
Supports WebSocket real-time + REST API + IRC protocol bridge (RFC 2812 subset).
Federation across nodes via the AitherNet relay mesh.

Usage:
    from adk.chat import ChatRelay, get_chat_relay

    relay = get_chat_relay()
    relay.join("#general", "alice")
    relay.post("#general", "alice", "Hello world!")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("adk.chat")

# ── Data Models ──────────────────────────────────────────────────────────

_DEFAULT_CHANNELS = ["#general", "#agents", "#dev"]
_MAX_HISTORY = 500
_MAX_MESSAGE_LEN = 4000
_NICK_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_\-\.]{0,31}$")
_CHANNEL_RE = re.compile(r"^#[a-zA-Z0-9_\-]{1,48}$")


@dataclass
class ChatMessage:
    msg_id: str
    channel: str
    nick: str
    content: str
    msg_type: str = "message"  # message, action, system, join, part
    timestamp: float = 0.0
    thread_id: str = ""
    node_id: str = ""  # Origin node for federated messages
    user_id: str = ""  # User ID of the message sender
    session_id: str = ""  # Session ID for conversation tracking

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.msg_id:
            self.msg_id = uuid.uuid4().hex[:12]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChatUser:
    nick: str
    display_name: str = ""
    is_agent: bool = False
    channels: list[str] = field(default_factory=list)
    joined_at: float = 0.0
    status: str = "online"  # online, away, offline
    node_id: str = ""  # Which node this user is on

    def __post_init__(self):
        if not self.joined_at:
            self.joined_at = time.time()
        if not self.display_name:
            self.display_name = self.nick


@dataclass
class Channel:
    name: str
    topic: str = ""
    mode: str = "public"  # public, private, agent-only
    created_by: str = "system"
    created_at: float = 0.0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


# ── Chat Relay ───────────────────────────────────────────────────────────


class ChatRelay:
    """Lightweight IRC-compatible chat relay with SQLite persistence."""

    def __init__(self, data_dir: str | Path | None = None, node_id: str = ""):
        self.node_id = node_id
        self._data_dir = Path(data_dir) if data_dir else Path.home() / ".aither"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._data_dir / "chat.db"

        # In-memory state
        self._channels: dict[str, Channel] = {}
        self._users: dict[str, ChatUser] = {}
        self._ws_connections: dict[str, Any] = {}  # nick -> WebSocket
        self._handlers: dict[str, list[Callable]] = {}  # event -> [callbacks]
        self._mention_handlers: dict[str, Callable] = {}  # agent_nick -> handler
        # Outbound relay subscribers: name -> handler(msg). Called on every post()
        # so an external integration (e.g. a channel gateway) can relay a room
        # message back out to its origin channel. Fire-and-forget; never blocks post().
        self._outbound_handlers: dict[str, Callable] = {}

        # Setup
        self._init_db()
        self._load_channels()
        self._ensure_defaults()

    def _schedule(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        loop.create_task(coro)

    # ── Database ─────────────────────────────────────────────────────

    def _init_db(self):
        db = sqlite3.connect(str(self._db_path))
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                name        TEXT PRIMARY KEY,
                topic       TEXT NOT NULL DEFAULT '',
                mode        TEXT NOT NULL DEFAULT 'public',
                created_by  TEXT NOT NULL DEFAULT 'system',
                created_at  REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                msg_id      TEXT PRIMARY KEY,
                channel     TEXT NOT NULL,
                nick        TEXT NOT NULL,
                content     TEXT NOT NULL,
                msg_type    TEXT NOT NULL DEFAULT 'message',
                timestamp   REAL NOT NULL,
                thread_id   TEXT NOT NULL DEFAULT '',
                node_id     TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_msg_channel ON messages(channel, timestamp);
            CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id);
            CREATE TABLE IF NOT EXISTS reactions (
                msg_id      TEXT NOT NULL,
                emoji       TEXT NOT NULL,
                nick        TEXT NOT NULL,
                timestamp   REAL NOT NULL,
                PRIMARY KEY (msg_id, emoji, nick)
            );
            CREATE INDEX IF NOT EXISTS idx_react_msg ON reactions(msg_id);
        """)
        db.close()

    def _get_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self._db_path))
        db.row_factory = sqlite3.Row
        return db

    def _load_channels(self):
        db = self._get_db()
        try:
            for row in db.execute("SELECT * FROM channels"):
                ch = Channel(
                    name=row["name"], topic=row["topic"],
                    mode=row["mode"], created_by=row["created_by"],
                    created_at=row["created_at"],
                )
                self._channels[ch.name] = ch
        finally:
            db.close()

    def _ensure_defaults(self):
        for name in _DEFAULT_CHANNELS:
            if name not in self._channels:
                self.create_channel(name, topic=f"Default channel {name}")

    # ── Channel Management ───────────────────────────────────────────

    def create_channel(self, name: str, topic: str = "", mode: str = "public",
                       created_by: str = "system") -> Channel:
        if not _CHANNEL_RE.match(name):
            raise ValueError(f"Invalid channel name: {name}")
        if name in self._channels:
            return self._channels[name]

        ch = Channel(name=name, topic=topic, mode=mode, created_by=created_by)
        self._channels[name] = ch

        db = self._get_db()
        try:
            db.execute(
                "INSERT OR IGNORE INTO channels (name, topic, mode, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ch.name, ch.topic, ch.mode, ch.created_by, ch.created_at),
            )
            db.commit()
        finally:
            db.close()

        return ch

    def list_channels(self) -> list[dict]:
        result = []
        for ch in self._channels.values():
            users = [n for n, u in self._users.items() if ch.name in u.channels]
            result.append({
                "name": ch.name, "topic": ch.topic, "mode": ch.mode,
                "users": len(users), "created_by": ch.created_by,
            })
        return result

    def set_topic(self, channel: str, topic: str, nick: str = "system"):
        if channel not in self._channels:
            return
        self._channels[channel].topic = topic
        db = self._get_db()
        try:
            db.execute("UPDATE channels SET topic = ? WHERE name = ?", (topic, channel))
            db.commit()
        finally:
            db.close()
        self._emit_sync("topic", {"channel": channel, "topic": topic, "nick": nick})

    # ── User Management ──────────────────────────────────────────────

    def join(self, channel: str, nick: str, is_agent: bool = False,
             node_id: str = "") -> bool:
        if not _NICK_RE.match(nick):
            return False
        if channel not in self._channels:
            self.create_channel(channel)

        if nick not in self._users:
            self._users[nick] = ChatUser(
                nick=nick, is_agent=is_agent,
                node_id=node_id or self.node_id,
            )

        user = self._users[nick]
        if channel not in user.channels:
            user.channels.append(channel)

        # System message
        self._store_message(ChatMessage(
            msg_id="", channel=channel, nick=nick,
            content=f"{nick} has joined {channel}",
            msg_type="join", node_id=self.node_id,
        ))
        self._emit_sync("join", {"channel": channel, "nick": nick})
        return True

    def part(self, channel: str, nick: str):
        user = self._users.get(nick)
        if user and channel in user.channels:
            user.channels.remove(channel)
            self._store_message(ChatMessage(
                msg_id="", channel=channel, nick=nick,
                content=f"{nick} has left {channel}",
                msg_type="part", node_id=self.node_id,
            ))
            self._emit_sync("part", {"channel": channel, "nick": nick})
            if not user.channels:
                self._users.pop(nick, None)

    def who(self, channel: str) -> list[dict]:
        return [
            {"nick": u.nick, "display_name": u.display_name,
             "is_agent": u.is_agent, "status": u.status, "node_id": u.node_id}
            for u in self._users.values()
            if channel in u.channels
        ]

    def online_users(self) -> list[dict]:
        return [
            {"nick": u.nick, "is_agent": u.is_agent, "channels": u.channels,
             "status": u.status, "node_id": u.node_id}
            for u in self._users.values()
            if u.status != "offline"
        ]

    def register_agent(self, nick: str, channels: list[str] | None = None,
                       mention_handler: Callable | None = None):
        """Register an agent as a chat participant."""
        target_channels = channels or ["#general", "#agents"]
        for ch in target_channels:
            self.join(ch, nick, is_agent=True)
        if mention_handler:
            self._mention_handlers[nick.lower()] = mention_handler

    def register_outbound_handler(self, name: str, handler: Callable) -> None:
        """Register an outbound relay subscriber.

        ``handler`` is called as ``handler(msg: ChatMessage)`` on every ``post()``
        (and federated/action posts). It may be sync or async. Use this to relay
        room messages back out to an external channel (e.g. Telegram/WhatsApp).
        The handler decides whether a given message should be relayed; the relay
        itself never inspects message intent.
        """
        self._outbound_handlers[name] = handler

    def unregister_outbound_handler(self, name: str) -> None:
        self._outbound_handlers.pop(name, None)

    # ── Messaging ────────────────────────────────────────────────────

    def post(self, channel: str, nick: str, content: str,
             thread_id: str = "", node_id: str = "") -> ChatMessage | None:
        if channel not in self._channels:
            return None
        if len(content) > _MAX_MESSAGE_LEN:
            content = content[:_MAX_MESSAGE_LEN]

        msg = ChatMessage(
            msg_id="", channel=channel, nick=nick, content=content,
            msg_type="message", thread_id=thread_id,
            node_id=node_id or self.node_id,
        )
        self._store_message(msg)

        # Dispatch to WebSocket listeners
        self._schedule(self._broadcast_ws(channel, msg.to_dict()))

        # Check for @mentions
        self._check_mentions(msg)

        # Relay outward to any external subscribers (e.g. channel gateways)
        self._dispatch_outbound(msg)

        # Emit event
        self._emit_sync("message", msg.to_dict())
        return msg

    def post_action(self, channel: str, nick: str, action: str) -> ChatMessage | None:
        """IRC /me action."""
        if channel not in self._channels:
            return None
        msg = ChatMessage(
            msg_id="", channel=channel, nick=nick,
            content=f"* {nick} {action}", msg_type="action",
            node_id=self.node_id,
        )
        self._store_message(msg)
        self._schedule(self._broadcast_ws(channel, msg.to_dict()))
        self._emit_sync("action", msg.to_dict())
        return msg

    def post_dm(self, from_nick: str, to_nick: str, content: str) -> ChatMessage | None:
        """Direct message between two users."""
        dm_channel = self._dm_key(from_nick, to_nick)
        msg = ChatMessage(
            msg_id="", channel=dm_channel, nick=from_nick,
            content=content, msg_type="message", node_id=self.node_id,
        )
        self._store_message(msg)
        # Send to recipient if connected
        ws = self._ws_connections.get(to_nick)
        if ws:
            self._schedule(self._send_ws(ws, msg.to_dict()))
        # Also send back to sender
        ws_from = self._ws_connections.get(from_nick)
        if ws_from:
            self._schedule(self._send_ws(ws_from, msg.to_dict()))
        self._emit_sync("dm", msg.to_dict())
        return msg

    def history(self, channel: str, limit: int = 50,
                before: float = 0, thread_id: str = "") -> list[dict]:
        db = self._get_db()
        try:
            if thread_id:
                rows = db.execute(
                    "SELECT * FROM messages WHERE thread_id = ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (thread_id, limit),
                ).fetchall()
            elif before:
                rows = db.execute(
                    "SELECT * FROM messages WHERE channel = ? AND timestamp < ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (channel, before, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM messages WHERE channel = ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (channel, limit),
                ).fetchall()
            out = [dict(r) for r in reversed(rows)]
            # attach reactions so chips re-render on reload (one extra query per message)
            for m in out:
                rx = {}
                for rr in db.execute("SELECT emoji, nick FROM reactions WHERE msg_id=? ORDER BY timestamp",
                                     (m.get("msg_id"),)):
                    rx.setdefault(rr["emoji"], []).append(rr["nick"])
                if rx:
                    m["reactions"] = rx
            return out
        finally:
            db.close()

    # ── WebSocket Management ─────────────────────────────────────────

    def connect_ws(self, nick: str, websocket: Any):
        self._ws_connections[nick] = websocket

    def disconnect_ws(self, nick: str):
        self._ws_connections.pop(nick, None)
        user = self._users.get(nick)
        if user:
            user.status = "offline"

    async def handle_ws_message(self, nick: str, data: dict):
        """Handle incoming WebSocket message from a client."""
        msg_type = data.get("type", "message")

        if msg_type == "message":
            channel = data.get("channel", "#general")
            content = data.get("content", "")
            thread_id = data.get("thread_id", "")
            if content:
                self.post(channel, nick, content, thread_id=thread_id)

        elif msg_type == "join":
            channel = data.get("channel", "#general")
            self.join(channel, nick)

        elif msg_type == "part":
            channel = data.get("channel", "#general")
            self.part(channel, nick)

        elif msg_type == "dm":
            to_nick = data.get("to", "")
            content = data.get("content", "")
            if to_nick and content:
                self.post_dm(nick, to_nick, content)

        elif msg_type == "action":
            channel = data.get("channel", "#general")
            action = data.get("action", "")
            if action:
                self.post_action(channel, nick, action)

        elif msg_type == "who":
            channel = data.get("channel", "#general")
            users = self.who(channel)
            ws = self._ws_connections.get(nick)
            if ws:
                await self._send_ws(ws, {"type": "who_reply", "channel": channel, "users": users})

        elif msg_type == "list":
            channels = self.list_channels()
            ws = self._ws_connections.get(nick)
            if ws:
                await self._send_ws(ws, {"type": "list_reply", "channels": channels})

    async def _broadcast_ws(self, channel: str, message: dict):
        dead = []
        for nick, ws in list(self._ws_connections.items()):
            user = self._users.get(nick)
            if user and channel in user.channels:
                try:
                    await self._send_ws(ws, message)
                except Exception:
                    dead.append(nick)
        for nick in dead:
            self.disconnect_ws(nick)

    async def _send_ws(self, ws: Any, data: dict):
        try:
            await ws.send_json(data)
        except Exception:
            pass

    # ── Federation (AitherNet Relay Mesh) ────────────────────────────

    async def federate_message(self, msg: ChatMessage):
        """Send a message to all federated nodes via the relay mesh."""
        try:
            from adk.relay import get_relay
            relay = get_relay()
            if relay and relay.is_registered:
                await relay.broadcast("chat", {
                    "msg_id": msg.msg_id,
                    "channel": msg.channel,
                    "nick": f"{msg.nick}@{self.node_id[:8]}",
                    "content": msg.content,
                    "msg_type": msg.msg_type,
                    "timestamp": msg.timestamp,
                    "thread_id": msg.thread_id,
                    "origin_node": self.node_id,
                })
        except Exception as e:
            logger.debug("Federation broadcast failed: %s", e)

    def handle_federated_message(self, data: dict):
        """Handle incoming federated message from another node."""
        origin = data.get("origin_node", "")
        if origin == self.node_id:
            return  # Loop prevention

        channel = data.get("channel", "#general")
        if channel not in self._channels:
            self.create_channel(channel)

        msg = ChatMessage(
            msg_id=data.get("msg_id", ""),
            channel=channel,
            nick=data.get("nick", "unknown"),
            content=data.get("content", ""),
            msg_type=data.get("msg_type", "message"),
            timestamp=data.get("timestamp", time.time()),
            thread_id=data.get("thread_id", ""),
            node_id=origin,
        )
        self._store_message(msg)
        self._schedule(self._broadcast_ws(channel, msg.to_dict()))

    # ── IRC Protocol Bridge ──────────────────────────────────────────

    def parse_irc_command(self, nick: str, raw: str) -> dict | None:
        """Parse IRC-style commands. Returns action dict or None."""
        raw = raw.strip()
        if not raw.startswith("/"):
            return None

        parts = raw.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "/join":
            channel = args.strip() if args else "#general"
            if not channel.startswith("#"):
                channel = f"#{channel}"
            self.join(channel, nick)
            return {"action": "join", "channel": channel}

        elif cmd == "/part":
            channel = args.strip() if args else "#general"
            self.part(channel, nick)
            return {"action": "part", "channel": channel}

        elif cmd == "/nick":
            new_nick = args.strip()
            if new_nick and _NICK_RE.match(new_nick):
                self._rename_user(nick, new_nick)
                return {"action": "nick", "old": nick, "new": new_nick}

        elif cmd == "/who":
            channel = args.strip() if args else "#general"
            return {"action": "who", "channel": channel, "users": self.who(channel)}

        elif cmd == "/list":
            return {"action": "list", "channels": self.list_channels()}

        elif cmd == "/me":
            return {"action": "action", "text": args}

        elif cmd == "/msg":
            target_parts = args.split(None, 1)
            if len(target_parts) == 2:
                to_nick, content = target_parts
                self.post_dm(nick, to_nick, content)
                return {"action": "dm", "to": to_nick}

        elif cmd == "/topic":
            topic_parts = args.split(None, 1)
            if len(topic_parts) == 2:
                channel, topic = topic_parts
                self.set_topic(channel, topic, nick)
                return {"action": "topic", "channel": channel, "topic": topic}

        elif cmd == "/help":
            return {"action": "help", "commands": [
                "/join #channel", "/part #channel", "/nick <name>",
                "/who [#channel]", "/list", "/me <action>",
                "/msg <nick> <message>", "/topic #channel <topic>",
            ]}

        return None

    # ── Event Handlers ───────────────────────────────────────────────

    def on(self, event: str, handler: Callable):
        """Register event handler: on("message", callback)"""
        self._handlers.setdefault(event, []).append(handler)

    def _emit_sync(self, event: str, data: dict):
        for handler in self._handlers.get(event, []):
            try:
                result = handler(data)
                if asyncio.iscoroutine(result):
                    self._schedule(result)
            except Exception as e:
                logger.debug("Chat event handler error (%s): %s", event, e)

    # ── Internal Helpers ─────────────────────────────────────────────

    def _store_message(self, msg: ChatMessage):
        db = self._get_db()
        try:
            db.execute(
                "INSERT OR IGNORE INTO messages "
                "(msg_id, channel, nick, content, msg_type, timestamp, thread_id, node_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (msg.msg_id, msg.channel, msg.nick, msg.content,
                 msg.msg_type, msg.timestamp, msg.thread_id, msg.node_id),
            )
            # Trim old messages
            count = db.execute(
                "SELECT COUNT(*) FROM messages WHERE channel = ?", (msg.channel,)
            ).fetchone()[0]
            if count > _MAX_HISTORY * 2:
                db.execute(
                    "DELETE FROM messages WHERE channel = ? AND msg_id IN "
                    "(SELECT msg_id FROM messages WHERE channel = ? "
                    "ORDER BY timestamp ASC LIMIT ?)",
                    (msg.channel, msg.channel, count - _MAX_HISTORY),
                )
            db.commit()
        finally:
            db.close()

    def clear_channel(self, channel: str) -> int:
        """Delete all stored messages for a channel (e.g. a demo reset). Returns rows removed.
        In-memory channel/user state is left intact; only the message log is cleared."""
        db = self._get_db()
        try:
            cur = db.execute("DELETE FROM messages WHERE channel = ?", (channel,))
            db.execute("DELETE FROM reactions")   # reactions are demo-scoped; clear them too
            db.commit()
            return cur.rowcount
        finally:
            db.close()

    # ── Reactions ────────────────────────────────────────────────────
    # Lightweight per-message emoji reactions. Sync (DB only); callers broadcast the update
    # over their WS channel. A reaction can carry a signal to an agent (e.g. ✅ = approve).

    def add_reaction(self, msg_id: str, emoji: str, nick: str) -> None:
        db = self._get_db()
        try:
            db.execute("INSERT OR IGNORE INTO reactions (msg_id, emoji, nick, timestamp) "
                       "VALUES (?, ?, ?, ?)", (msg_id, emoji, nick, time.time()))
            db.commit()
        finally:
            db.close()

    def remove_reaction(self, msg_id: str, emoji: str, nick: str) -> None:
        db = self._get_db()
        try:
            db.execute("DELETE FROM reactions WHERE msg_id=? AND emoji=? AND nick=?",
                       (msg_id, emoji, nick))
            db.commit()
        finally:
            db.close()

    def reactions_for(self, msg_id: str) -> dict[str, list[str]]:
        """Return {emoji: [nicks]} for a message (for rendering counts + who reacted)."""
        db = self._get_db()
        try:
            out: dict[str, list[str]] = {}
            for row in db.execute("SELECT emoji, nick FROM reactions WHERE msg_id=? "
                                  "ORDER BY timestamp", (msg_id,)):
                out.setdefault(row["emoji"], []).append(row["nick"])
            return out
        finally:
            db.close()

    def message_channel(self, msg_id: str) -> str:
        """Channel a message was posted in (so a reaction update can be fanned to it)."""
        db = self._get_db()
        try:
            r = db.execute("SELECT channel FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
            return r["channel"] if r else ""
        finally:
            db.close()

    def _check_mentions(self, msg: ChatMessage):
        content_lower = msg.content.lower()
        for agent_nick, handler in self._mention_handlers.items():
            if f"@{agent_nick}" in content_lower:
                try:
                    result = handler(msg)
                    if asyncio.iscoroutine(result):
                        self._schedule(result)
                except Exception as e:
                    logger.debug("Mention handler error for @%s: %s", agent_nick, e)

    def _dispatch_outbound(self, msg: ChatMessage):
        """Fan a posted message to outbound relay subscribers (fire-and-forget).

        Each subscriber is isolated: an error in one never blocks the post or the
        others. Handlers may be sync or async (coroutines are scheduled).
        """
        for name, handler in list(self._outbound_handlers.items()):
            try:
                result = handler(msg)
                if asyncio.iscoroutine(result):
                    self._schedule(result)
            except Exception as e:
                logger.debug("Outbound handler error (%s): %s", name, e)

    def _rename_user(self, old_nick: str, new_nick: str):
        user = self._users.pop(old_nick, None)
        if user:
            user.nick = new_nick
            user.display_name = new_nick
            self._users[new_nick] = user
        ws = self._ws_connections.pop(old_nick, None)
        if ws:
            self._ws_connections[new_nick] = ws
        handler = self._mention_handlers.pop(old_nick.lower(), None)
        if handler:
            self._mention_handlers[new_nick.lower()] = handler

    @staticmethod
    def _dm_key(nick1: str, nick2: str) -> str:
        pair = sorted([nick1.lower(), nick2.lower()])
        return f"dm:{pair[0]}:{pair[1]}"

    # ── Status ───────────────────────────────────────────────────────

    def status(self) -> dict:
        db = self._get_db()
        try:
            msg_count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            db.close()
        return {
            "node_id": self.node_id,
            "channels": len(self._channels),
            "users_online": sum(1 for u in self._users.values() if u.status != "offline"),
            "agents_online": sum(1 for u in self._users.values()
                                 if u.is_agent and u.status != "offline"),
            "ws_connections": len(self._ws_connections),
            "total_messages": msg_count,
            "channel_list": list(self._channels.keys()),
        }

    def irc_status(self) -> dict | None:
        """Return IRC server status, or None if not running."""
        if hasattr(self, "_irc_server") and self._irc_server:
            return self._irc_server.status()
        return None

    # ── IRC Protocol Server ───────────────────────────────────────────

    async def start_irc_server(self, port: int = 6667, host: str = "0.0.0.0") -> bool:
        """Start the raw IRC protocol listener.

        Runs as an asyncio task alongside FastAPI. Real IRC clients
        (mIRC, WeeChat, HexChat, irssi) can connect directly.

        Args:
            port: TCP port to listen on (default 6667).
            host: Bind address (default 0.0.0.0).

        Returns:
            True if the server started successfully.
        """
        try:
            self._irc_server = IRCServer(self, host=host, port=port)
            await self._irc_server.start()
            logger.info("IRC server started on %s:%d", host, port)
            return True
        except Exception as exc:
            logger.warning("Failed to start IRC server on %s:%d: %s", host, port, exc)
            self._irc_server = None
            return False

    async def stop_irc_server(self):
        """Stop the IRC protocol listener gracefully."""
        if hasattr(self, "_irc_server") and self._irc_server:
            await self._irc_server.stop()
            self._irc_server = None
            logger.info("IRC server stopped")


# ── IRC Protocol Server (RFC 2812 subset) ────────────────────────────────

_IRC_SERVER_NAME = "AitherNet"
_IRC_VERSION = "adk-irc-1.0"


class _IRCClient:
    """State for a single connected IRC client."""

    __slots__ = ("reader", "writer", "nick", "user", "realname",
                 "registered", "channels", "addr", "_host")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.nick: str = ""
        self.user: str = ""
        self.realname: str = ""
        self.registered: bool = False
        self.channels: list[str] = []
        peername = writer.get_extra_info("peername")
        self.addr: str = f"{peername[0]}:{peername[1]}" if peername else "unknown"
        self._host: str = peername[0] if peername else "unknown"

    @property
    def hostmask(self) -> str:
        """Return nick!user@host for IRC message prefixes."""
        u = self.user or self.nick
        return f"{self.nick}!{u}@{self._host}"

    async def send(self, line: str):
        """Send a single IRC line to the client."""
        try:
            self.writer.write((line + "\r\n").encode("utf-8", errors="replace"))
            await self.writer.drain()
        except (ConnectionError, OSError):
            pass

    async def send_numeric(self, server: str, numeric: str, target: str, text: str):
        """Send a numeric reply: :server NUMERIC target :text"""
        await self.send(f":{server} {numeric} {target} {text}")


class IRCServer:
    """Raw IRC protocol listener implementing an RFC 2812 subset.

    Wraps asyncio.start_server and delegates all chat operations to
    a ChatRelay instance. Supports NICK, USER, JOIN, PART, PRIVMSG,
    QUIT, WHO, LIST, TOPIC, PING/PONG, MODE (stub), and NAMES.

    All messages flow through the same ChatRelay channels, users, and
    persistence layer used by WebSocket and REST clients.
    """

    def __init__(self, relay: ChatRelay, host: str = "0.0.0.0", port: int = 6667):
        self._relay = relay
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None
        self._clients: dict[str, _IRCClient] = {}  # nick -> client
        self._server_name = _IRC_SERVER_NAME

        # Register event handlers on the relay so that messages from
        # WebSocket/REST/federation are broadcast to IRC clients.
        self._relay.on("message", self._on_relay_message)
        self._relay.on("join", self._on_relay_join)
        self._relay.on("part", self._on_relay_part)
        self._relay.on("topic", self._on_relay_topic)
        self._relay.on("dm", self._on_relay_dm)
        self._relay.on("action", self._on_relay_action)

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self):
        """Start listening for IRC connections."""
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port,
        )

    async def stop(self):
        """Stop the IRC server and disconnect all clients."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Disconnect all clients
        for nick in list(self._clients):
            client = self._clients.pop(nick, None)
            if client:
                try:
                    client.writer.close()
                except Exception:
                    pass

    def status(self) -> dict:
        """Return IRC server status."""
        return {
            "listening": self._server is not None and self._server.is_serving(),
            "host": self._host,
            "port": self._port,
            "clients": len(self._clients),
            "nicks": list(self._clients.keys()),
        }

    # ── Client Connection Handler ─────────────────────────────────────

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        """Handle a single IRC client connection."""
        client = _IRCClient(reader, writer)
        logger.info("IRC client connected from %s", client.addr)

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(reader.readline(), timeout=300)
                except asyncio.TimeoutError:
                    # Send PING to check if client is alive
                    await client.send(f"PING :{self._server_name}")
                    try:
                        raw = await asyncio.wait_for(reader.readline(), timeout=60)
                    except asyncio.TimeoutError:
                        break  # Client unresponsive

                if not raw:
                    break  # EOF — client disconnected

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                await self._process_line(client, line)

        except (ConnectionError, OSError):
            pass
        except Exception as exc:
            logger.debug("IRC client error (%s): %s", client.addr, exc)
        finally:
            await self._client_disconnect(client)

    async def _client_disconnect(self, client: _IRCClient):
        """Clean up when a client disconnects."""
        nick = client.nick
        if nick:
            self._clients.pop(nick, None)
            # Part all channels via ChatRelay
            for ch in list(client.channels):
                self._relay.part(ch, nick)
            logger.info("IRC client disconnected: %s (%s)", nick, client.addr)
        else:
            logger.debug("IRC client disconnected (unregistered): %s", client.addr)

        try:
            client.writer.close()
        except Exception:
            pass

    # ── IRC Command Processor ─────────────────────────────────────────

    async def _process_line(self, client: _IRCClient, line: str):
        """Parse and dispatch a single IRC protocol line."""
        # Strip optional prefix (some clients send :prefix)
        if line.startswith(":"):
            _, _, line = line.partition(" ")
            if not line:
                return

        # Split into command + params
        if " :" in line:
            head, _, trailing = line.partition(" :")
            parts = head.split()
            parts.append(trailing)
        else:
            parts = line.split()

        if not parts:
            return

        cmd = parts[0].upper()
        params = parts[1:]

        handler = getattr(self, f"_cmd_{cmd.lower()}", None)
        if handler:
            await handler(client, params)
        else:
            # Unknown command — send ERR_UNKNOWNCOMMAND (421)
            if client.nick:
                await client.send_numeric(
                    self._server_name, "421", client.nick,
                    f"{cmd} :Unknown command",
                )

    # ── Registration Commands ─────────────────────────────────────────

    async def _cmd_nick(self, client: _IRCClient, params: list[str]):
        """Handle NICK command."""
        if not params:
            await client.send_numeric(
                self._server_name, "431", "*",
                ":No nickname given",
            )
            return

        new_nick = params[0].strip()

        # Validate nick format
        if not _NICK_RE.match(new_nick):
            await client.send_numeric(
                self._server_name, "432", client.nick or "*",
                f"{new_nick} :Erroneous nickname",
            )
            return

        # Check collision
        if new_nick.lower() in {n.lower() for n in self._clients if n != client.nick}:
            await client.send_numeric(
                self._server_name, "433", client.nick or "*",
                f"{new_nick} :Nickname is already in use",
            )
            return

        # Check collision with non-IRC relay users
        existing = self._relay._users.get(new_nick)
        if existing and new_nick not in self._clients:
            await client.send_numeric(
                self._server_name, "433", client.nick or "*",
                f"{new_nick} :Nickname is already in use",
            )
            return

        old_nick = client.nick
        if old_nick:
            # Nick change — notify channels
            self._clients.pop(old_nick, None)
            self._relay._rename_user(old_nick, new_nick)
            # Notify all channels this user is in
            for ch in client.channels:
                await self._broadcast_to_channel(
                    ch, f":{old_nick}!{client.user or old_nick}@{client._host} NICK {new_nick}",
                    exclude=None,
                )

        client.nick = new_nick
        self._clients[new_nick] = client

        if not client.registered and client.user:
            await self._complete_registration(client)

    async def _cmd_user(self, client: _IRCClient, params: list[str]):
        """Handle USER command."""
        if client.registered:
            await client.send_numeric(
                self._server_name, "462", client.nick,
                ":You may not reregister",
            )
            return

        if len(params) < 4:
            await client.send_numeric(
                self._server_name, "461", client.nick or "*",
                "USER :Not enough parameters",
            )
            return

        client.user = params[0]
        # params[1] and params[2] are mode and unused in modern IRC
        client.realname = params[3] if len(params) > 3 else params[0]

        if client.nick:
            await self._complete_registration(client)

    async def _complete_registration(self, client: _IRCClient):
        """Send the RPL_WELCOME (001-004) burst after NICK+USER received."""
        client.registered = True
        nick = client.nick

        # 001 RPL_WELCOME
        await client.send_numeric(
            self._server_name, "001", nick,
            f":Welcome to {self._server_name} {client.hostmask}",
        )
        # 002 RPL_YOURHOST
        await client.send_numeric(
            self._server_name, "002", nick,
            f":Your host is {self._server_name}, running version {_IRC_VERSION}",
        )
        # 003 RPL_CREATED
        await client.send_numeric(
            self._server_name, "003", nick,
            f":This server was created by AitherADK",
        )
        # 004 RPL_MYINFO
        await client.send_numeric(
            self._server_name, "004", nick,
            f"{self._server_name} {_IRC_VERSION} o o",
        )

        # Register the user in ChatRelay
        self._relay.join("#general", nick)
        client.channels.append("#general")

        # Send MOTD (short)
        await client.send_numeric(
            self._server_name, "375", nick,
            f":- {self._server_name} Message of the Day -",
        )
        await client.send_numeric(
            self._server_name, "372", nick,
            ":- Welcome to AitherNet IRC. Type /list for channels.",
        )
        await client.send_numeric(
            self._server_name, "376", nick,
            ":End of /MOTD command.",
        )

        # Auto-join #general — send JOIN + topic + names
        await self._send_join_burst(client, "#general")

    # ── Channel Commands ──────────────────────────────────────────────

    async def _cmd_join(self, client: _IRCClient, params: list[str]):
        """Handle JOIN command."""
        if not client.registered:
            await client.send_numeric(
                self._server_name, "451", "*",
                ":You have not registered",
            )
            return

        if not params:
            await client.send_numeric(
                self._server_name, "461", client.nick,
                "JOIN :Not enough parameters",
            )
            return

        # Support comma-separated channels: JOIN #a,#b,#c
        channels = params[0].split(",")
        for ch_name in channels:
            ch_name = ch_name.strip()
            if not ch_name:
                continue
            if not ch_name.startswith("#"):
                ch_name = f"#{ch_name}"

            if not _CHANNEL_RE.match(ch_name):
                await client.send_numeric(
                    self._server_name, "403", client.nick,
                    f"{ch_name} :No such channel",
                )
                continue

            if ch_name in client.channels:
                continue  # Already in channel

            ok = self._relay.join(ch_name, client.nick)
            if ok:
                client.channels.append(ch_name)
                await self._send_join_burst(client, ch_name)
            else:
                await client.send_numeric(
                    self._server_name, "403", client.nick,
                    f"{ch_name} :Cannot join channel",
                )

    async def _send_join_burst(self, client: _IRCClient, channel: str):
        """Send the JOIN + topic + names burst to a client and notify others."""
        nick = client.nick

        # Notify everyone in channel (including self)
        await self._broadcast_to_channel(
            channel, f":{client.hostmask} JOIN {channel}",
            exclude=None,
        )

        # 332 RPL_TOPIC (if topic is set)
        ch_obj = self._relay._channels.get(channel)
        if ch_obj and ch_obj.topic:
            await client.send_numeric(
                self._server_name, "332", nick,
                f"{channel} :{ch_obj.topic}",
            )

        # 353 RPL_NAMREPLY + 366 RPL_ENDOFNAMES
        await self._send_names(client, channel)

    async def _cmd_part(self, client: _IRCClient, params: list[str]):
        """Handle PART command."""
        if not client.registered:
            return

        if not params:
            await client.send_numeric(
                self._server_name, "461", client.nick,
                "PART :Not enough parameters",
            )
            return

        channels = params[0].split(",")
        reason = params[1] if len(params) > 1 else "Leaving"

        for ch_name in channels:
            ch_name = ch_name.strip()
            if ch_name not in client.channels:
                await client.send_numeric(
                    self._server_name, "442", client.nick,
                    f"{ch_name} :You're not on that channel",
                )
                continue

            # Notify channel before removing
            await self._broadcast_to_channel(
                ch_name, f":{client.hostmask} PART {ch_name} :{reason}",
                exclude=None,
            )
            client.channels.remove(ch_name)
            self._relay.part(ch_name, client.nick)

    async def _cmd_topic(self, client: _IRCClient, params: list[str]):
        """Handle TOPIC command (query or set)."""
        if not client.registered:
            return

        if not params:
            await client.send_numeric(
                self._server_name, "461", client.nick,
                "TOPIC :Not enough parameters",
            )
            return

        channel = params[0]
        if len(params) < 2:
            # Query topic
            ch_obj = self._relay._channels.get(channel)
            if ch_obj and ch_obj.topic:
                await client.send_numeric(
                    self._server_name, "332", client.nick,
                    f"{channel} :{ch_obj.topic}",
                )
            else:
                await client.send_numeric(
                    self._server_name, "331", client.nick,
                    f"{channel} :No topic is set",
                )
        else:
            # Set topic
            new_topic = params[1]
            self._relay.set_topic(channel, new_topic, client.nick)
            await self._broadcast_to_channel(
                channel, f":{client.hostmask} TOPIC {channel} :{new_topic}",
                exclude=None,
            )

    async def _cmd_names(self, client: _IRCClient, params: list[str]):
        """Handle NAMES command."""
        if not client.registered:
            return

        channel = params[0] if params else None
        if channel:
            await self._send_names(client, channel)
        else:
            # Send names for all channels the client is in
            for ch in client.channels:
                await self._send_names(client, ch)

    async def _send_names(self, client: _IRCClient, channel: str):
        """Send RPL_NAMREPLY (353) + RPL_ENDOFNAMES (366)."""
        users = self._relay.who(channel)
        nicks = " ".join(u["nick"] for u in users)
        await client.send_numeric(
            self._server_name, "353", client.nick,
            f"= {channel} :{nicks}",
        )
        await client.send_numeric(
            self._server_name, "366", client.nick,
            f"{channel} :End of /NAMES list",
        )

    # ── Messaging Commands ────────────────────────────────────────────

    async def _cmd_privmsg(self, client: _IRCClient, params: list[str]):
        """Handle PRIVMSG command."""
        if not client.registered:
            return

        if len(params) < 2:
            await client.send_numeric(
                self._server_name, "411", client.nick,
                ":No recipient given (PRIVMSG)",
            )
            return

        target = params[0]
        content = params[1]

        if not content:
            await client.send_numeric(
                self._server_name, "412", client.nick,
                ":No text to send",
            )
            return

        # Check for CTCP ACTION (/me)
        if content.startswith("\x01ACTION ") and content.endswith("\x01"):
            action_text = content[8:-1]
            if target.startswith("#"):
                self._relay.post_action(target, client.nick, action_text)
            return

        if target.startswith("#"):
            # Channel message
            msg = self._relay.post(target, client.nick, content)
            if msg:
                # Relay already emits event which triggers _on_relay_message,
                # but that will re-broadcast to IRC. We need to prevent echo
                # to the sender, so broadcast here excluding the sender and
                # let _on_relay_message skip IRC-originated messages.
                pass
            else:
                await client.send_numeric(
                    self._server_name, "404", client.nick,
                    f"{target} :Cannot send to channel",
                )
        else:
            # Private message to a user
            self._relay.post_dm(client.nick, target, content)

    async def _cmd_notice(self, client: _IRCClient, params: list[str]):
        """Handle NOTICE command (same as PRIVMSG but never auto-replied)."""
        # Per RFC, NOTICE should never generate an automatic reply.
        # We treat it the same as PRIVMSG for relay purposes.
        await self._cmd_privmsg(client, params)

    # ── Query Commands ────────────────────────────────────────────────

    async def _cmd_who(self, client: _IRCClient, params: list[str]):
        """Handle WHO command."""
        if not client.registered:
            return

        mask = params[0] if params else "*"

        if mask.startswith("#"):
            # Channel WHO
            users = self._relay.who(mask)
            for u in users:
                # 352 RPL_WHOREPLY: <channel> <user> <host> <server> <nick> <H|G> :<hopcount> <realname>
                status_flag = "H" if u.get("status") == "online" else "G"
                await client.send_numeric(
                    self._server_name, "352", client.nick,
                    f"{mask} {u['nick']} {self._server_name} {self._server_name} "
                    f"{u['nick']} {status_flag} :0 {u.get('display_name', u['nick'])}",
                )
        else:
            # Global WHO (match nick)
            for u in self._relay.online_users():
                if mask == "*" or mask.lower() in u["nick"].lower():
                    ch = u["channels"][0] if u.get("channels") else "*"
                    status_flag = "H" if u.get("status") == "online" else "G"
                    await client.send_numeric(
                        self._server_name, "352", client.nick,
                        f"{ch} {u['nick']} {self._server_name} {self._server_name} "
                        f"{u['nick']} {status_flag} :0 {u['nick']}",
                    )

        # 315 RPL_ENDOFWHO
        await client.send_numeric(
            self._server_name, "315", client.nick,
            f"{mask} :End of /WHO list",
        )

    async def _cmd_list(self, client: _IRCClient, params: list[str]):
        """Handle LIST command."""
        if not client.registered:
            return

        channels = self._relay.list_channels()
        for ch in channels:
            # 322 RPL_LIST: <channel> <visible> :<topic>
            await client.send_numeric(
                self._server_name, "322", client.nick,
                f"{ch['name']} {ch['users']} :{ch.get('topic', '')}",
            )

        # 323 RPL_LISTEND
        await client.send_numeric(
            self._server_name, "323", client.nick,
            ":End of /LIST",
        )

    async def _cmd_whois(self, client: _IRCClient, params: list[str]):
        """Handle WHOIS command."""
        if not client.registered or not params:
            return

        target_nick = params[0]
        user = self._relay._users.get(target_nick)

        if not user:
            await client.send_numeric(
                self._server_name, "401", client.nick,
                f"{target_nick} :No such nick/channel",
            )
            return

        # 311 RPL_WHOISUSER
        await client.send_numeric(
            self._server_name, "311", client.nick,
            f"{user.nick} {user.nick} {self._server_name} * :{user.display_name}",
        )
        # 319 RPL_WHOISCHANNELS
        if user.channels:
            await client.send_numeric(
                self._server_name, "319", client.nick,
                f"{user.nick} :{' '.join(user.channels)}",
            )
        # 312 RPL_WHOISSERVER
        await client.send_numeric(
            self._server_name, "312", client.nick,
            f"{user.nick} {self._server_name} :AitherNet IRC",
        )
        # 318 RPL_ENDOFWHOIS
        await client.send_numeric(
            self._server_name, "318", client.nick,
            f"{user.nick} :End of /WHOIS list",
        )

    # ── Utility Commands ──────────────────────────────────────────────

    async def _cmd_ping(self, client: _IRCClient, params: list[str]):
        """Handle PING command."""
        token = params[0] if params else self._server_name
        await client.send(f":{self._server_name} PONG {self._server_name} :{token}")

    async def _cmd_pong(self, client: _IRCClient, params: list[str]):
        """Handle PONG — client responding to our PING. Nothing to do."""
        pass

    async def _cmd_quit(self, client: _IRCClient, params: list[str]):
        """Handle QUIT command."""
        reason = params[0] if params else "Client quit"

        # Notify all channels
        for ch in list(client.channels):
            await self._broadcast_to_channel(
                ch, f":{client.hostmask} QUIT :{reason}",
                exclude=client.nick,
            )

        # Clean up
        await self._client_disconnect(client)

    async def _cmd_mode(self, client: _IRCClient, params: list[str]):
        """Handle MODE command (stub — accept but mostly ignore)."""
        if not client.registered:
            return

        if not params:
            return

        target = params[0]

        if target.startswith("#"):
            # Channel mode query — return empty mode
            await client.send_numeric(
                self._server_name, "324", client.nick,
                f"{target} +",
            )
        else:
            # User mode query — return empty mode
            await client.send_numeric(
                self._server_name, "221", client.nick,
                "+",
            )

    async def _cmd_cap(self, client: _IRCClient, params: list[str]):
        """Handle CAP command (IRCv3 capability negotiation stub).

        Most modern clients send CAP LS/REQ during registration.
        We respond minimally to avoid hanging the registration flow.
        """
        if not params:
            return

        sub = params[0].upper()
        if sub == "LS":
            # No capabilities supported — send empty list
            await client.send(f":{self._server_name} CAP * LS :")
        elif sub == "REQ":
            # Deny all requested capabilities
            req = params[1] if len(params) > 1 else ""
            await client.send(f":{self._server_name} CAP * NAK :{req}")
        elif sub == "END":
            # Client finished negotiation — nothing to do
            pass

    async def _cmd_userhost(self, client: _IRCClient, params: list[str]):
        """Handle USERHOST command."""
        if not client.registered or not params:
            return

        replies = []
        for nick in params[:5]:  # Max 5 per RFC
            user = self._relay._users.get(nick)
            if user:
                replies.append(f"{nick}=+{nick}@{self._server_name}")

        await client.send_numeric(
            self._server_name, "302", client.nick,
            f":{' '.join(replies)}",
        )

    # ── Broadcast Helpers ─────────────────────────────────────────────

    async def _broadcast_to_channel(self, channel: str, line: str,
                                    exclude: str | None = None):
        """Send a raw IRC line to all IRC clients in a channel."""
        for nick, irc_client in list(self._clients.items()):
            if nick == exclude:
                continue
            if channel in irc_client.channels:
                await irc_client.send(line)

    async def _send_to_nick(self, nick: str, line: str):
        """Send a raw IRC line to a specific nick if they are an IRC client."""
        client = self._clients.get(nick)
        if client:
            await client.send(line)

    # ── Relay Event Handlers (ChatRelay → IRC clients) ────────────────

    def _fire_and_forget(self, coro):
        """Schedule a coroutine on the running event loop, or discard if none."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            # No running event loop — close the coroutine to avoid warnings
            coro.close()

    def _on_relay_message(self, data: dict):
        """Handle message events from ChatRelay — broadcast to IRC clients."""
        nick = data.get("nick", "")
        channel = data.get("channel", "")
        content = data.get("content", "")

        if not channel.startswith("#") or not content or not nick:
            return

        # Skip if the sender is an IRC client (they already see their own messages
        # in their client — no server echo needed per IRC convention)
        if nick in self._clients:
            return

        line = f":{nick}!{nick}@{self._server_name} PRIVMSG {channel} :{content}"
        self._fire_and_forget(self._broadcast_to_channel(channel, line))

    def _on_relay_join(self, data: dict):
        """Handle join events from ChatRelay — notify IRC clients."""
        nick = data.get("nick", "")
        channel = data.get("channel", "")

        if nick in self._clients:
            return  # IRC client already got the JOIN echo

        line = f":{nick}!{nick}@{self._server_name} JOIN {channel}"
        self._fire_and_forget(self._broadcast_to_channel(channel, line))

    def _on_relay_part(self, data: dict):
        """Handle part events from ChatRelay — notify IRC clients."""
        nick = data.get("nick", "")
        channel = data.get("channel", "")

        if nick in self._clients:
            return

        line = f":{nick}!{nick}@{self._server_name} PART {channel} :Left"
        self._fire_and_forget(self._broadcast_to_channel(channel, line))

    def _on_relay_topic(self, data: dict):
        """Handle topic change events from ChatRelay."""
        nick = data.get("nick", "")
        channel = data.get("channel", "")
        topic = data.get("topic", "")

        if nick in self._clients:
            return

        line = f":{nick}!{nick}@{self._server_name} TOPIC {channel} :{topic}"
        self._fire_and_forget(self._broadcast_to_channel(channel, line))

    def _on_relay_dm(self, data: dict):
        """Handle DM events from ChatRelay — deliver to IRC clients."""
        nick = data.get("nick", "")
        content = data.get("content", "")
        # DM channel format is "dm:alice:bob" — extract the other party
        channel = data.get("channel", "")

        if nick in self._clients or not content:
            return

        if channel.startswith("dm:"):
            parts = channel.split(":")
            if len(parts) == 3:
                # Determine recipient — the one that is NOT the sender
                recipient = parts[2] if parts[1] == nick.lower() else parts[1]
                # Find actual nick casing
                for real_nick in self._clients:
                    if real_nick.lower() == recipient:
                        line = f":{nick}!{nick}@{self._server_name} PRIVMSG {real_nick} :{content}"
                        self._fire_and_forget(self._send_to_nick(real_nick, line))
                        break

    def _on_relay_action(self, data: dict):
        """Handle action events from ChatRelay — send CTCP ACTION to IRC."""
        nick = data.get("nick", "")
        channel = data.get("channel", "")
        content = data.get("content", "")

        if nick in self._clients or not channel.startswith("#"):
            return

        # Extract action text: content is "* nick does something"
        if content.startswith(f"* {nick} "):
            action_text = content[len(f"* {nick} "):]
        else:
            action_text = content

        line = f":{nick}!{nick}@{self._server_name} PRIVMSG {channel} :\x01ACTION {action_text}\x01"
        self._fire_and_forget(self._broadcast_to_channel(channel, line))


# ── Singleton ────────────────────────────────────────────────────────────

_chat_relay: ChatRelay | None = None


def get_chat_relay(**kwargs) -> ChatRelay:
    """Get or create the singleton ChatRelay instance."""
    global _chat_relay
    if _chat_relay is None:
        _chat_relay = ChatRelay(**kwargs)
    return _chat_relay
