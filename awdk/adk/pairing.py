"""Cross-platform user pairing -- link identities across Telegram, Discord, Slack, etc.

Clean-room port of the AitherOS UserPairing concept for standalone ADK use.
SQLite-backed, no AitherDirectory dependency, zero external dependencies.

Usage:
    from adk.pairing import get_pairing_manager

    pm = get_pairing_manager()

    # Platform A generates a code
    code = pm.generate_code("telegram", "tg_12345", display_name="Alice")

    # Platform B redeems it to link identities
    result = pm.redeem_code(code, "discord", "dc_67890", display_name="Alice#1234")
    assert result.success
    assert result.platforms_linked == 2

    # Canonical session ID for conversation continuity
    sid = pm.get_session_id("telegram", "tg_12345")
    # "user-<unified-id>"

    # Unlinked users get platform-scoped IDs
    sid2 = pm.get_session_id("matrix", "mx_unknown")
    # "matrix-mx_unknown"
"""

from __future__ import annotations

import logging
import os
import random
import sqlite3
import string
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("adk.pairing")

_CODE_LENGTH = 6
_CODE_TTL_SECONDS = 600  # 10 minutes
_CODE_CHARS = string.ascii_uppercase + string.digits


@dataclass
class PlatformIdentity:
    """A linked platform identity."""
    platform: str
    platform_user_id: str
    display_name: str = ""
    linked_at: str = ""


@dataclass
class PairingResult:
    """Result of a pairing code redemption."""
    success: bool
    user_id: str = ""
    message: str = ""
    platforms_linked: int = 0


@dataclass
class _PendingCode:
    """In-memory pending pairing code with TTL."""
    code: str
    platform: str
    platform_user_id: str
    display_name: str
    created_at: float
    ttl: float = _CODE_TTL_SECONDS


class PairingManager:
    """SQLite-backed cross-platform identity pairing manager.

    Links users across multiple platforms (Telegram, Discord, Slack, WhatsApp,
    Matrix, etc.) using short-lived 6-character pairing codes.

    Args:
        db_path: Path to the SQLite database file. If None, uses
            ``~/.aither/pairing/pairing.db``.
    """

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            data_dir = Path(
                os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))
            ) / "pairing"
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = data_dir / "pairing.db"

        self._db_path = str(db_path)
        self._pending: dict[str, _PendingCode] = {}
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pairing_users (
                    user_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pairing_links (
                    user_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    platform_user_id TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    linked_at TEXT NOT NULL,
                    UNIQUE(platform, platform_user_id),
                    FOREIGN KEY (user_id) REFERENCES pairing_users(user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_links_user
                    ON pairing_links(user_id);
                CREATE INDEX IF NOT EXISTS idx_links_platform
                    ON pairing_links(platform, platform_user_id);
            """)

    def _connect(self) -> sqlite3.Connection:
        """Open a WAL-mode connection."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ─── Code generation & redemption ─────────────────────────────────

    def _purge_expired(self):
        """Remove expired pending codes."""
        now = time.time()
        expired = [
            code for code, pc in self._pending.items()
            if now - pc.created_at > pc.ttl
        ]
        for code in expired:
            del self._pending[code]

    def generate_code(
        self,
        platform: str,
        platform_user_id: str,
        display_name: str = "",
    ) -> str:
        """Generate a 6-character pairing code with 10-minute TTL.

        Args:
            platform: Platform name (e.g. "telegram", "discord").
            platform_user_id: User's ID on that platform.
            display_name: Optional display name for the user.

        Returns:
            A 6-character alphanumeric pairing code.
        """
        self._purge_expired()

        # If this platform identity already has a pending code, revoke it
        for existing_code, pc in list(self._pending.items()):
            if pc.platform == platform and pc.platform_user_id == platform_user_id:
                del self._pending[existing_code]
                break

        # Generate unique code
        for _ in range(100):
            code = "".join(random.choices(_CODE_CHARS, k=_CODE_LENGTH))
            if code not in self._pending:
                break

        self._pending[code] = _PendingCode(
            code=code,
            platform=platform,
            platform_user_id=platform_user_id,
            display_name=display_name,
            created_at=time.time(),
        )
        logger.debug("Generated pairing code %s for %s:%s", code, platform, platform_user_id)
        return code

    def redeem_code(
        self,
        code: str,
        platform: str,
        platform_user_id: str,
        display_name: str = "",
    ) -> PairingResult:
        """Redeem a pairing code to link two platform identities.

        If the code creator is already paired, the redeemer joins that user.
        Otherwise a new unified user is created and both platforms are linked.

        Args:
            code: The 6-character pairing code.
            platform: Redeemer's platform name.
            platform_user_id: Redeemer's platform user ID.
            display_name: Optional display name for the redeemer.

        Returns:
            PairingResult with success status and linked platform count.
        """
        self._purge_expired()

        code = code.upper().strip()
        pending = self._pending.pop(code, None)
        if pending is None:
            return PairingResult(
                success=False,
                message="Invalid or expired pairing code",
            )

        # Prevent self-pairing
        if pending.platform == platform and pending.platform_user_id == platform_user_id:
            return PairingResult(
                success=False,
                message="Cannot pair the same platform identity with itself",
            )

        # Check if creator already has a unified user
        creator_user_id = self.resolve_user(pending.platform, pending.platform_user_id)

        # Check if redeemer already has a unified user
        redeemer_user_id = self.resolve_user(platform, platform_user_id)

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        with self._connect() as conn:
            if creator_user_id and redeemer_user_id:
                if creator_user_id == redeemer_user_id:
                    count = self._count_links(conn, creator_user_id)
                    return PairingResult(
                        success=True,
                        user_id=creator_user_id,
                        message="Platforms already linked under same user",
                        platforms_linked=count,
                    )
                # Merge: move redeemer's links to creator's user
                conn.execute(
                    "UPDATE pairing_links SET user_id = ? WHERE user_id = ?",
                    (creator_user_id, redeemer_user_id),
                )
                conn.execute(
                    "DELETE FROM pairing_users WHERE user_id = ?",
                    (redeemer_user_id,),
                )
                user_id = creator_user_id

            elif creator_user_id:
                # Link redeemer to creator's existing user
                self._ensure_link(
                    conn, creator_user_id, platform, platform_user_id,
                    display_name, now,
                )
                user_id = creator_user_id

            elif redeemer_user_id:
                # Link creator to redeemer's existing user
                self._ensure_link(
                    conn, redeemer_user_id, pending.platform,
                    pending.platform_user_id, pending.display_name, now,
                )
                user_id = redeemer_user_id

            else:
                # New unified user
                user_id = uuid.uuid4().hex[:16]
                conn.execute(
                    "INSERT INTO pairing_users (user_id, created_at) VALUES (?, ?)",
                    (user_id, now),
                )
                self._ensure_link(
                    conn, user_id, pending.platform,
                    pending.platform_user_id, pending.display_name, now,
                )
                self._ensure_link(
                    conn, user_id, platform, platform_user_id,
                    display_name, now,
                )

            count = self._count_links(conn, user_id)

        logger.info("Paired %s:%s + %s:%s -> user %s (%d platforms)",
                     pending.platform, pending.platform_user_id,
                     platform, platform_user_id, user_id, count)

        return PairingResult(
            success=True,
            user_id=user_id,
            message=f"Successfully linked {count} platforms",
            platforms_linked=count,
        )

    # ─── Query helpers ────────────────────────────────────────────────

    def resolve_user(self, platform: str, platform_user_id: str) -> Optional[str]:
        """Resolve a platform identity to its unified user ID.

        Args:
            platform: Platform name.
            platform_user_id: User's ID on that platform.

        Returns:
            The unified user_id if paired, None otherwise.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM pairing_links "
                "WHERE platform = ? AND platform_user_id = ?",
                (platform, platform_user_id),
            ).fetchone()
        return row[0] if row else None

    def get_session_id(self, platform: str, platform_user_id: str) -> str:
        """Get a canonical session ID for conversation continuity.

        Returns ``"user-{id}"`` for paired users, ``"{platform}-{id}"``
        for unlinked users.

        Args:
            platform: Platform name.
            platform_user_id: User's ID on that platform.

        Returns:
            A canonical session identifier string.
        """
        user_id = self.resolve_user(platform, platform_user_id)
        if user_id:
            return f"user-{user_id}"
        return f"{platform}-{platform_user_id}"

    def get_linked_platforms(self, user_id: str) -> list[PlatformIdentity]:
        """Get all platforms linked to a unified user.

        Args:
            user_id: The unified user ID.

        Returns:
            List of PlatformIdentity objects.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT platform, platform_user_id, display_name, linked_at "
                "FROM pairing_links WHERE user_id = ? ORDER BY linked_at",
                (user_id,),
            ).fetchall()
        return [
            PlatformIdentity(
                platform=r[0],
                platform_user_id=r[1],
                display_name=r[2] or "",
                linked_at=r[3] or "",
            )
            for r in rows
        ]

    def unlink_platform(self, user_id: str, platform: str) -> bool:
        """Remove a platform link from a unified user.

        Args:
            user_id: The unified user ID.
            platform: Platform name to unlink.

        Returns:
            True if a link was removed, False if not found.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM pairing_links WHERE user_id = ? AND platform = ?",
                (user_id, platform),
            )
            if cursor.rowcount == 0:
                return False

            # If no links remain, remove the user record too
            remaining = conn.execute(
                "SELECT COUNT(*) FROM pairing_links WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            if remaining == 0:
                conn.execute(
                    "DELETE FROM pairing_users WHERE user_id = ?",
                    (user_id,),
                )
        return True

    # ─── Internal helpers ─────────────────────────────────────────────

    @staticmethod
    def _ensure_link(
        conn: sqlite3.Connection,
        user_id: str,
        platform: str,
        platform_user_id: str,
        display_name: str,
        linked_at: str,
    ):
        """Insert a platform link if it doesn't already exist."""
        conn.execute(
            "INSERT OR IGNORE INTO pairing_links "
            "(user_id, platform, platform_user_id, display_name, linked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, platform, platform_user_id, display_name, linked_at),
        )

    @staticmethod
    def _count_links(conn: sqlite3.Connection, user_id: str) -> int:
        """Count the number of platform links for a user."""
        row = conn.execute(
            "SELECT COUNT(*) FROM pairing_links WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row[0] if row else 0


# ─────────────────────────────────────────────────────────────────────────────
# Module singleton
# ─────────────────────────────────────────────────────────────────────────────

_instance: PairingManager | None = None


def get_pairing_manager(db_path: str | Path | None = None) -> PairingManager:
    """Get or create the module-level PairingManager singleton.

    Args:
        db_path: Optional path to the SQLite database file.

    Returns:
        The global PairingManager instance.
    """
    global _instance
    if _instance is None:
        _instance = PairingManager(db_path=db_path)
    return _instance
