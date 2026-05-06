import os
import aiosqlite
from datetime import datetime, timedelta
from typing import Optional


class Database:
    def __init__(self, path: str):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        await self._migrate()

    async def close(self):
        if self._db:
            await self._db.close()

    async def _create_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS filters (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id        TEXT    NOT NULL,
                channel_id      TEXT    NOT NULL,
                name            TEXT    NOT NULL,
                search_text     TEXT    DEFAULT '',
                min_price       REAL    DEFAULT NULL,
                max_price       REAL    DEFAULT NULL,
                brand_ids       TEXT    DEFAULT '',
                brand_titles    TEXT    DEFAULT '',
                size_ids        TEXT    DEFAULT '',
                size_titles     TEXT    DEFAULT '',
                catalog_ids     TEXT    DEFAULT '',
                catalog_titles  TEXT    DEFAULT '',
                condition_ids   TEXT    DEFAULT '',
                domain          TEXT    DEFAULT 'www.vinted.fr',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS seen_items (
                item_id    TEXT    NOT NULL,
                filter_id  INTEGER NOT NULL,
                seen_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (item_id, filter_id)
            );
        """)
        await self._db.commit()

    async def _migrate(self):
        """Ajoute les colonnes manquantes si la BDD est antérieure à cette version."""
        async with self._db.execute("PRAGMA table_info(filters)") as cur:
            existing = {row[1] async for row in cur}

        new_cols = {
            "brand_titles":   "TEXT DEFAULT ''",
            "size_ids":       "TEXT DEFAULT ''",
            "size_titles":    "TEXT DEFAULT ''",
            "catalog_ids":    "TEXT DEFAULT ''",
            "catalog_titles": "TEXT DEFAULT ''",
            "condition_ids":  "TEXT DEFAULT ''",
        }
        for col, definition in new_cols.items():
            if col not in existing:
                await self._db.execute(f"ALTER TABLE filters ADD COLUMN {col} {definition}")
        await self._db.commit()

    # ── Filters ────────────────────────────────────────────────────────────────

    async def add_filter(
        self,
        guild_id: str,
        channel_id: str,
        name: str,
        search_text: str = "",
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        brand_ids: str = "",
        brand_titles: str = "",
        size_ids: str = "",
        size_titles: str = "",
        catalog_ids: str = "",
        catalog_titles: str = "",
        condition_ids: str = "",
        domain: str = "www.vinted.fr",
    ) -> int:
        async with self._db.execute(
            """INSERT INTO filters
               (guild_id, channel_id, name, search_text,
                min_price, max_price,
                brand_ids, brand_titles,
                size_ids, size_titles,
                catalog_ids, catalog_titles,
                condition_ids, domain)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, channel_id, name, search_text,
             min_price, max_price,
             brand_ids, brand_titles,
             size_ids, size_titles,
             catalog_ids, catalog_titles,
             condition_ids, domain),
        ) as cursor:
            filter_id = cursor.lastrowid
        await self._db.commit()
        return filter_id

    async def get_filters(self, guild_id: Optional[str] = None):
        if guild_id:
            async with self._db.execute(
                "SELECT * FROM filters WHERE guild_id = ? ORDER BY id", (guild_id,)
            ) as cur:
                return await cur.fetchall()
        async with self._db.execute("SELECT * FROM filters ORDER BY id") as cur:
            return await cur.fetchall()

    async def get_filter(self, filter_id: int):
        async with self._db.execute(
            "SELECT * FROM filters WHERE id = ?", (filter_id,)
        ) as cur:
            return await cur.fetchone()

    async def delete_filter(self, filter_id: int, guild_id: str):
        await self._db.execute(
            "DELETE FROM filters WHERE id = ? AND guild_id = ?", (filter_id, guild_id)
        )
        await self._db.execute(
            "DELETE FROM seen_items WHERE filter_id = ?", (filter_id,)
        )
        await self._db.commit()

    # ── Seen items ─────────────────────────────────────────────────────────────

    async def is_seen(self, item_id: str, filter_id: int) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM seen_items WHERE item_id = ? AND filter_id = ?",
            (item_id, filter_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def has_any_seen(self, filter_id: int) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM seen_items WHERE filter_id = ? LIMIT 1", (filter_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_seen(self, item_id: str, filter_id: int):
        await self._db.execute(
            "INSERT OR IGNORE INTO seen_items (item_id, filter_id) VALUES (?, ?)",
            (item_id, filter_id),
        )
        await self._db.commit()

    async def mark_seen_bulk(self, item_ids: list[str], filter_id: int):
        await self._db.executemany(
            "INSERT OR IGNORE INTO seen_items (item_id, filter_id) VALUES (?, ?)",
            [(iid, filter_id) for iid in item_ids],
        )
        await self._db.commit()

    async def cleanup_old_seen(self, days: int = 7):
        cutoff = datetime.utcnow() - timedelta(days=days)
        await self._db.execute(
            "DELETE FROM seen_items WHERE seen_at < ?", (cutoff,)
        )
        await self._db.commit()
