import os
import aiosqlite
import json
from typing import Any, Optional, List, Dict

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")


class Database:
    """
    Persistent single-connection SQLite database.

    A new connection is opened once in init() and reused for every query.
    This avoids the overhead of opening/closing a connection on every call,
    which was the primary source of button-response latency.

    WAL journal mode and performance pragmas are applied on startup.
    Guild config results are cached in-memory and invalidated on write.
    """

    def __init__(self, path: str = _DB_PATH):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._guild_config_cache: dict[int, Optional[Dict]] = {}
        self._guild_settings_cache: dict[int, Dict[str, Any]] = {}

    # ── Connection property ───────────────────────────────────────────────────

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialised — call await db.init() first.")
        return self._conn

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def init(self):
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row

        # Performance pragmas — applied once on the persistent connection.
        await self._conn.executescript("""
            PRAGMA journal_mode  = WAL;
            PRAGMA synchronous   = NORMAL;
            PRAGMA cache_size    = -32000;
            PRAGMA temp_store    = MEMORY;
            PRAGMA mmap_size     = 268435456;
        """)

        await self._create_tables()
        await self._migrate()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ── Schema ────────────────────────────────────────────────────────────────

    async def _create_tables(self):
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id           INTEGER PRIMARY KEY,
                management_role_id INTEGER,
                staff_role_id      INTEGER
            );

            CREATE TABLE IF NOT EXISTS self_role_panels (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id      INTEGER NOT NULL,
                channel_id    INTEGER,
                message_id    INTEGER,
                title         TEXT    NOT NULL,
                description   TEXT    NOT NULL DEFAULT '',
                roles         TEXT    NOT NULL DEFAULT '[]',
                enabled       INTEGER NOT NULL DEFAULT 1,
                single_select INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS ticket_panels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                channel_id  INTEGER,
                message_id  INTEGER,
                title       TEXT    NOT NULL,
                description TEXT    NOT NULL DEFAULT '',
                buttons     TEXT    NOT NULL DEFAULT '[]',
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tickets (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id       INTEGER NOT NULL,
                channel_id     INTEGER NOT NULL,
                user_id        INTEGER NOT NULL,
                panel_id       INTEGER NOT NULL,
                category       TEXT    NOT NULL,
                status         TEXT    NOT NULL DEFAULT 'open',
                claimed_by     INTEGER,
                ticket_msg_id  INTEGER,
                close_msg_id   INTEGER,
                closed_at      TEXT,
                created_at     TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER NOT NULL,
                key      TEXT    NOT NULL,
                value    TEXT    NOT NULL,
                PRIMARY KEY (guild_id, key)
            );

            CREATE TABLE IF NOT EXISTS tcwa3_link_requests (
                guild_id         INTEGER NOT NULL,
                discord_id       INTEGER NOT NULL,
                link_id          TEXT    NOT NULL,
                discord_username TEXT    NOT NULL,
                nickname         TEXT,
                status           TEXT    NOT NULL DEFAULT 'pending',
                expires_at       INTEGER,
                created_at       TEXT    DEFAULT (datetime('now')),
                updated_at       TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (guild_id, discord_id)
            );
        """)
        await self.conn.commit()

    async def _migrate(self):
        """Safely add any columns that may be missing from pre-existing databases."""
        migrations = [
            "ALTER TABLE tickets ADD COLUMN claimed_by    INTEGER",
            "ALTER TABLE tickets ADD COLUMN ticket_msg_id INTEGER",
            "ALTER TABLE tickets ADD COLUMN close_msg_id  INTEGER",
            "ALTER TABLE tickets ADD COLUMN closed_at     TEXT",
            "ALTER TABLE self_role_panels ADD COLUMN single_select INTEGER NOT NULL DEFAULT 0",
        ]
        for sql in migrations:
            try:
                await self.conn.execute(sql)
                await self.conn.commit()
            except Exception:
                pass

    # ── Guild config ──────────────────────────────────────────────────────────

    async def get_guild_config(self, guild_id: int) -> Optional[Dict]:
        if guild_id in self._guild_config_cache:
            return self._guild_config_cache[guild_id]
        async with self.conn.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            result = dict(row) if row else None
        self._guild_config_cache[guild_id] = result
        return result

    async def set_management_role(self, guild_id: int, role_id: int):
        await self.conn.execute("""
            INSERT INTO guild_config (guild_id, management_role_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET management_role_id = excluded.management_role_id
        """, (guild_id, role_id))
        await self.conn.commit()
        self._guild_config_cache.pop(guild_id, None)

    async def set_staff_role(self, guild_id: int, role_id: int):
        await self.conn.execute("""
            INSERT INTO guild_config (guild_id, staff_role_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET staff_role_id = excluded.staff_role_id
        """, (guild_id, role_id))
        await self.conn.commit()
        self._guild_config_cache.pop(guild_id, None)

    # ── Guild settings (generic key/value) ────────────────────────────────────
    #
    # A namespaced JSON key/value store keyed by (guild_id, key). Values are
    # JSON-encoded on write and decoded on read, so any JSON-serialisable type
    # (int, float, str, bool, list, dict) round-trips cleanly. Feature cogs that
    # only need a handful of per-guild settings can use this instead of a
    # dedicated table. Results are cached per guild and invalidated on write.

    async def get_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        """Return all settings for a guild as a {key: value} dict (values decoded)."""
        if guild_id in self._guild_settings_cache:
            return self._guild_settings_cache[guild_id]
        async with self.conn.execute(
            "SELECT key, value FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ) as cur:
            rows = await cur.fetchall()
        result: Dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                result[row["key"]] = None
        self._guild_settings_cache[guild_id] = result
        return result

    async def get_guild_setting(self, guild_id: int, key: str, default: Any = None) -> Any:
        """Return a single decoded setting value, or `default` if unset."""
        settings = await self.get_guild_settings(guild_id)
        return settings.get(key, default)

    async def set_guild_setting(self, guild_id: int, key: str, value: Any):
        """Insert or update a single setting (value is JSON-encoded)."""
        await self.conn.execute("""
            INSERT INTO guild_settings (guild_id, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value
        """, (guild_id, key, json.dumps(value)))
        await self.conn.commit()
        self._guild_settings_cache.pop(guild_id, None)

    async def delete_guild_setting(self, guild_id: int, key: str):
        """Delete a single setting if present."""
        await self.conn.execute(
            "DELETE FROM guild_settings WHERE guild_id = ? AND key = ?", (guild_id, key)
        )
        await self.conn.commit()
        self._guild_settings_cache.pop(guild_id, None)

    # TCWA3 Discord bridge link request state.

    async def save_tcwa3_link_request(
        self,
        guild_id: int,
        discord_id: int,
        *,
        link_id: str,
        discord_username: str,
        nickname: Optional[str],
        status: str,
        expires_at: Optional[int],
    ):
        await self.conn.execute("""
            INSERT INTO tcwa3_link_requests
                (guild_id, discord_id, link_id, discord_username, nickname, status, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(guild_id, discord_id) DO UPDATE SET
                link_id = excluded.link_id,
                discord_username = excluded.discord_username,
                nickname = excluded.nickname,
                status = excluded.status,
                expires_at = excluded.expires_at,
                updated_at = datetime('now')
        """, (guild_id, discord_id, link_id, discord_username, nickname, status, expires_at))
        await self.conn.commit()

    async def get_tcwa3_link_request(self, guild_id: int, discord_id: int) -> Optional[Dict]:
        async with self.conn.execute(
            "SELECT * FROM tcwa3_link_requests WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_tcwa3_link_status(self, guild_id: int, discord_id: int, status: str):
        await self.conn.execute("""
            UPDATE tcwa3_link_requests
            SET status = ?, updated_at = datetime('now')
            WHERE guild_id = ? AND discord_id = ?
        """, (status, guild_id, discord_id))
        await self.conn.commit()

    # ── Self-role panels ──────────────────────────────────────────────────────

    async def create_self_role_panel(
        self, guild_id: int, channel_id: int, message_id: int,
        title: str, description: str, roles: list, single_select: bool = False
    ) -> int:
        cur = await self.conn.execute("""
            INSERT INTO self_role_panels
                (guild_id, channel_id, message_id, title, description, roles, single_select)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, channel_id, message_id, title, description, json.dumps(roles), int(single_select)))
        await self.conn.commit()
        return cur.lastrowid

    async def get_self_role_panels(self, guild_id: int) -> List[Dict]:
        async with self.conn.execute(
            "SELECT * FROM self_role_panels WHERE guild_id = ? ORDER BY id", (guild_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_all_self_role_panels(self) -> List[Dict]:
        async with self.conn.execute("SELECT * FROM self_role_panels") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_self_role_panel(self, panel_id: int) -> Optional[Dict]:
        async with self.conn.execute(
            "SELECT * FROM self_role_panels WHERE id = ?", (panel_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_self_role_panel(self, panel_id: int, **kwargs):
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        await self.conn.execute(
            f"UPDATE self_role_panels SET {sets} WHERE id = ?",
            [*kwargs.values(), panel_id]
        )
        await self.conn.commit()

    async def delete_self_role_panel(self, panel_id: int):
        await self.conn.execute("DELETE FROM self_role_panels WHERE id = ?", (panel_id,))
        await self.conn.commit()

    async def toggle_self_role_panel(self, panel_id: int, enabled: bool):
        await self.conn.execute(
            "UPDATE self_role_panels SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, panel_id)
        )
        await self.conn.commit()

    # ── Ticket panels ─────────────────────────────────────────────────────────

    async def create_ticket_panel(
        self, guild_id: int, channel_id: int, message_id: int,
        title: str, description: str, buttons: list
    ) -> int:
        cur = await self.conn.execute("""
            INSERT INTO ticket_panels
                (guild_id, channel_id, message_id, title, description, buttons)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (guild_id, channel_id, message_id, title, description, json.dumps(buttons)))
        await self.conn.commit()
        return cur.lastrowid

    async def get_ticket_panels(self, guild_id: int) -> List[Dict]:
        async with self.conn.execute(
            "SELECT * FROM ticket_panels WHERE guild_id = ? ORDER BY id", (guild_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_all_ticket_panels(self) -> List[Dict]:
        async with self.conn.execute("SELECT * FROM ticket_panels") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_ticket_panel(self, panel_id: int) -> Optional[Dict]:
        async with self.conn.execute(
            "SELECT * FROM ticket_panels WHERE id = ?", (panel_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_ticket_panel(self, panel_id: int, **kwargs):
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        await self.conn.execute(
            f"UPDATE ticket_panels SET {sets} WHERE id = ?",
            [*kwargs.values(), panel_id]
        )
        await self.conn.commit()

    async def delete_ticket_panel(self, panel_id: int):
        await self.conn.execute("DELETE FROM ticket_panels WHERE id = ?", (panel_id,))
        await self.conn.commit()

    async def toggle_ticket_panel(self, panel_id: int, enabled: bool):
        await self.conn.execute(
            "UPDATE ticket_panels SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, panel_id)
        )
        await self.conn.commit()

    # ── Tickets ───────────────────────────────────────────────────────────────

    async def create_ticket(
        self, guild_id: int, channel_id: int, user_id: int,
        panel_id: int, category: str
    ) -> int:
        cur = await self.conn.execute("""
            INSERT INTO tickets (guild_id, channel_id, user_id, panel_id, category)
            VALUES (?, ?, ?, ?, ?)
        """, (guild_id, channel_id, user_id, panel_id, category))
        await self.conn.commit()
        return cur.lastrowid

    async def get_ticket_by_channel(self, channel_id: int) -> Optional[Dict]:
        async with self.conn.execute(
            "SELECT * FROM tickets WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
            (channel_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_open_ticket_for_user(
        self, guild_id: int, user_id: int, panel_id: int
    ) -> Optional[Dict]:
        async with self.conn.execute("""
            SELECT * FROM tickets
            WHERE guild_id = ? AND user_id = ? AND panel_id = ?
              AND status IN ('open', 'closing')
            LIMIT 1
        """, (guild_id, user_id, panel_id)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_closing_tickets(self) -> List[Dict]:
        async with self.conn.execute(
            "SELECT * FROM tickets WHERE status = 'closing'"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def update_ticket_field(self, channel_id: int, **kwargs):
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        await self.conn.execute(
            f"UPDATE tickets SET {sets} WHERE channel_id = ?",
            [*kwargs.values(), channel_id]
        )
        await self.conn.commit()
