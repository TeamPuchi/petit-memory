"""SQLite + numpy の保管層。

段0 で store.py から移設し、段1 で値の直列化を records.py に寄せた
（DynamoDB 実装と同じ属性名・同じ表現を使うため）。SQL 自体は変えていない。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

from .config import MemoryConfig
from .records import (
    EPISODE_ATTRIBUTES,
    FORGET_ATTRIBUTES,
    MEMORY_ATTRIBUTES,
    decode_episode,
    decode_forget_marker,
    decode_memory,
    encode_episode,
    encode_forget_marker,
    encode_memory,
    parse_linked_ids,
    parse_links,
)
from .store_backend import (
    MemoryFacets,
    MemoryRecord,
    MemoryWithVector,
    VectorRow,
)
from .types import Episode, ForgetMarker, Memory

# ──────────────────────────────────────────────
# DDL
# ──────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    normalized_content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    emotion TEXT NOT NULL DEFAULT 'neutral',
    importance INTEGER NOT NULL DEFAULT 3,
    category TEXT NOT NULL DEFAULT 'daily',
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed TEXT NOT NULL DEFAULT '',
    linked_ids TEXT NOT NULL DEFAULT '',
    episode_id TEXT,
    sensory_data TEXT NOT NULL DEFAULT '',
    camera_position TEXT,
    tags TEXT NOT NULL DEFAULT '',
    links TEXT NOT NULL DEFAULT '',
    novelty_score REAL NOT NULL DEFAULT 0.0,
    prediction_error REAL NOT NULL DEFAULT 0.0,
    activation_count INTEGER NOT NULL DEFAULT 0,
    last_activated TEXT NOT NULL DEFAULT '',
    reading TEXT,
    indexed INTEGER NOT NULL DEFAULT 1,
    private INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_emotion    ON memories(emotion);
CREATE INDEX IF NOT EXISTS idx_memories_category   ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_timestamp  ON memories(timestamp);
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance);

CREATE TABLE IF NOT EXISTS embeddings (
    memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    vector BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS coactivation (
    source_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    weight REAL NOT NULL CHECK(weight >= 0.0 AND weight <= 1.0),
    PRIMARY KEY (source_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_coactivation_source ON coactivation(source_id);
CREATE INDEX IF NOT EXISTS idx_coactivation_target ON coactivation(target_id);

-- 段2: 消した跡。本文は持たない（跡から中身が読めてはいけない）。
-- memories への外部キーは張らない。参照先はもう無いのが前提。
CREATE TABLE IF NOT EXISTS forget_markers (
    memory_id TEXT PRIMARY KEY,
    forgotten_at TEXT NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_forget_markers_at ON forget_markers(forgotten_at);

CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    memory_ids TEXT NOT NULL DEFAULT '',
    participants TEXT NOT NULL DEFAULT '',
    location_context TEXT,
    summary TEXT NOT NULL DEFAULT '',
    emotion TEXT NOT NULL DEFAULT 'neutral',
    importance INTEGER NOT NULL DEFAULT 3
);
"""

# 段2 より前に作られた memory.db には無い列。connect() のたびに足りない分だけ埋める。
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("indexed", "INTEGER NOT NULL DEFAULT 1"),
    ("private", "INTEGER NOT NULL DEFAULT 0"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """ぷちてゃたちの既存 memory.db をそのまま読めるようにする。"""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(memories)")}
    for name, ddl in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {ddl}")


# ──────────────────────────────────────────────
# Row → Memory / Episode
# ──────────────────────────────────────────────


def _row_to_memory(row: sqlite3.Row, coactivation: tuple[tuple[str, float], ...] = ()) -> Memory:
    """Convert a SQLite Row from the memories table to a Memory object."""
    return decode_memory(dict(row), coactivation)


def _row_to_episode(row: sqlite3.Row) -> Episode:
    """Convert a SQLite Row from the episodes table to an Episode object."""
    return decode_episode(dict(row))


# ──────────────────────────────────────────────
# SqliteMemoryStore
# ──────────────────────────────────────────────


class SqliteMemoryStore:
    """家コンテナ内の SQLite ファイルに記憶を置く保管層。

    `MemoryStoreBackend` の実装。SQL はすべてここにある。
    """

    def __init__(self, config: MemoryConfig):
        self._config = config
        self._db: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ── 接続 ────────────────────────────────────

    async def connect(self) -> None:
        """Open SQLite database and create tables."""
        async with self._lock:
            if self._db is None:
                db_path = self._config.db_path

                def _open() -> sqlite3.Connection:
                    conn = sqlite3.connect(db_path, check_same_thread=False)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.execute("PRAGMA journal_mode = WAL")
                    for stmt in _DDL.strip().split(";"):
                        stmt = stmt.strip()
                        if stmt:
                            conn.execute(stmt)
                    _add_missing_columns(conn)
                    conn.commit()
                    return conn

                self._db = await asyncio.to_thread(_open)

    async def disconnect(self) -> None:
        """Close the SQLite connection."""
        async with self._lock:
            if self._db is not None:
                await asyncio.to_thread(self._db.close)
                self._db = None

    @property
    def connection(self) -> sqlite3.Connection:
        """生の接続。SQLite 実装だけが持つ逃げ道（テスト用）。"""
        return self._ensure_connected()

    def _ensure_connected(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("MemoryStore not connected. Call connect() first.")
        return self._db

    # ── 共活性の同期ヘルパ ──────────────────────

    def _get_coactivation(self, db: sqlite3.Connection, memory_id: str) -> tuple[tuple[str, float], ...]:
        rows = db.execute(
            "SELECT target_id, weight FROM coactivation WHERE source_id = ?",
            (memory_id,),
        ).fetchall()
        return tuple((row["target_id"], float(row["weight"])) for row in rows)

    def _get_coactivation_map(
        self, db: sqlite3.Connection, memory_ids: list[str]
    ) -> dict[str, tuple[tuple[str, float], ...]]:
        """複数 ID ぶんの共活性をまとめて引く（1 件ずつの往復を避けるため）。"""
        grouped: dict[str, list[tuple[str, float]]] = {}
        chunk_size = 500  # SQLite のバインド変数上限（既定 999）に収める
        for start in range(0, len(memory_ids), chunk_size):
            chunk = memory_ids[start : start + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = db.execute(
                f"SELECT source_id, target_id, weight FROM coactivation WHERE source_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                grouped.setdefault(row["source_id"], []).append(
                    (row["target_id"], float(row["weight"]))
                )
        return {mid: tuple(pairs) for mid, pairs in grouped.items()}

    def _rows_to_memories(self, db: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[Memory]:
        memories: list[Memory] = []
        for row in rows:
            coactivation = self._get_coactivation(db, row["id"])
            memories.append(_row_to_memory(row, coactivation))
        return memories

    # ── 記憶: 取る ──────────────────────────────

    async def fetch_memory(self, memory_id: str) -> Memory | None:
        db = self._ensure_connected()

        def _fetch() -> Memory | None:
            row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                return None
            coactivation = self._get_coactivation(db, memory_id)
            return _row_to_memory(row, coactivation)

        return await asyncio.to_thread(_fetch)

    async def fetch_memories(self, memory_ids: list[str]) -> list[Memory]:
        if not memory_ids:
            return []
        db = self._ensure_connected()

        def _fetch() -> list[Memory]:
            placeholders = ",".join("?" * len(memory_ids))
            rows = db.execute(
                f"SELECT * FROM memories WHERE id IN ({placeholders})", memory_ids
            ).fetchall()
            return self._rows_to_memories(db, rows)

        return await asyncio.to_thread(_fetch)

    async def fetch_all_memories(self) -> list[Memory]:
        db = self._ensure_connected()

        def _fetch() -> list[Memory]:
            rows = db.execute("SELECT * FROM memories").fetchall()
            return self._rows_to_memories(db, rows)

        return await asyncio.to_thread(_fetch)

    async def fetch_memories_with_vectors(
        self,
        emotion: str | None = None,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[MemoryWithVector]:
        db = self._ensure_connected()

        conditions: list[str] = []
        params: list[Any] = []
        if emotion:
            conditions.append("m.emotion = ?")
            params.append(emotion)
        if category:
            conditions.append("m.category = ?")
            params.append(category)
        if date_from:
            conditions.append("m.timestamp >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("m.timestamp <= ?")
            params.append(date_to)

        # 段2: index:false の記憶は意味検索・recall の母集団に入れない
        conditions.append("m.indexed = 1")

        where_clause = "WHERE " + " AND ".join(conditions)
        sql = f"SELECT m.*, e.vector FROM memories m JOIN embeddings e ON m.id = e.memory_id {where_clause}"

        def _fetch() -> list[MemoryWithVector]:
            rows = db.execute(sql, params).fetchall()
            coactivation_map = self._get_coactivation_map(db, [row["id"] for row in rows])
            return [
                MemoryWithVector(
                    memory=_row_to_memory(row, coactivation_map.get(row["id"], ())),
                    vector=bytes(row["vector"]),
                )
                for row in rows
            ]

        return await asyncio.to_thread(_fetch)

    async def fetch_recent_memories(self, limit: int, category: str | None = None) -> list[Memory]:
        db = self._ensure_connected()

        def _fetch() -> list[Memory]:
            if category:
                rows = db.execute(
                    "SELECT * FROM memories WHERE category = ? ORDER BY timestamp DESC LIMIT ?",
                    (category, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM memories ORDER BY timestamp DESC LIMIT ?", (limit,)
                ).fetchall()
            return self._rows_to_memories(db, rows)

        return await asyncio.to_thread(_fetch)

    async def fetch_important_memories(
        self,
        min_importance: int,
        min_access_count: int,
        since: str | None,
        limit: int,
    ) -> list[Memory]:
        db = self._ensure_connected()

        def _fetch() -> list[Memory]:
            conditions = [
                "importance >= ?",
                "access_count >= ?",
            ]
            params: list[Any] = [min_importance, min_access_count]
            if since:
                conditions.append("last_accessed >= ?")
                params.append(since)
            where = " AND ".join(conditions)
            rows = db.execute(
                f"SELECT * FROM memories WHERE {where} ORDER BY last_accessed DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            return self._rows_to_memories(db, rows)

        return await asyncio.to_thread(_fetch)

    async def fetch_memory_facets(self) -> MemoryFacets:
        db = self._ensure_connected()

        def _fetch() -> MemoryFacets:
            rows = db.execute("SELECT emotion, category, timestamp FROM memories").fetchall()
            oldest = db.execute("SELECT MIN(timestamp) FROM memories").fetchone()[0]
            newest = db.execute("SELECT MAX(timestamp) FROM memories").fetchone()[0]
            return MemoryFacets(
                rows=tuple(
                    (row["emotion"] or "neutral", row["category"] or "daily", row["timestamp"])
                    for row in rows
                ),
                oldest_timestamp=oldest,
                newest_timestamp=newest,
            )

        return await asyncio.to_thread(_fetch)

    # ── 記憶: 入れる・直す・消す ────────────────

    async def insert_memory(self, record: MemoryRecord) -> None:
        db = self._ensure_connected()
        attrs = encode_memory(record)
        columns = ", ".join(MEMORY_ATTRIBUTES)
        placeholders = ",".join("?" * len(MEMORY_ATTRIBUTES))
        values = [attrs[name] for name in MEMORY_ATTRIBUTES]

        def _insert() -> None:
            db.execute(f"INSERT INTO memories ({columns}) VALUES ({placeholders})", values)
            db.execute(
                "INSERT INTO embeddings (memory_id, vector) VALUES (?,?)",
                (record.memory.id, record.vector),
            )
            db.commit()

        await asyncio.to_thread(_insert)

    async def update_memory_fields(self, memory_id: str, fields: dict[str, Any]) -> bool:
        if not fields:
            return True
        db = self._ensure_connected()

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [memory_id]

        def _update() -> bool:
            result = db.execute(f"UPDATE memories SET {set_clause} WHERE id = ?", values)
            db.commit()
            return result.rowcount > 0

        return await asyncio.to_thread(_update)

    async def update_episode_id(self, memory_id: str, episode_id: str | None) -> bool:
        db = self._ensure_connected()

        def _update() -> bool:
            result = db.execute(
                "UPDATE memories SET episode_id = ? WHERE id = ?",
                (episode_id, memory_id),
            )
            if result.rowcount == 0:
                return False
            db.commit()
            return True

        return await asyncio.to_thread(_update)

    async def increment_access(self, memory_id: str, last_accessed: str) -> None:
        db = self._ensure_connected()

        def _update() -> None:
            db.execute(
                """UPDATE memories
                   SET access_count = access_count + 1,
                       last_accessed = ?
                   WHERE id = ?""",
                (last_accessed, memory_id),
            )
            db.commit()

        await asyncio.to_thread(_update)

    async def delete_memory(self, memory_id: str, forget_marker: ForgetMarker | None = None) -> bool:
        """Delete a memory and clean up references.

        Embeddings and coactivation rows are CASCADE-deleted by SQLite.
        linked_ids and links JSON in other memories are cleaned up manually.

        段2: `forget_marker` を渡すと同じコミットで `forget_markers` に跡を残す。
        """
        db = self._ensure_connected()

        def _delete() -> bool:
            # Check existence
            row = db.execute("SELECT id FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                return False

            # Remove from other memories' linked_ids
            referencing = db.execute(
                "SELECT id, linked_ids FROM memories WHERE linked_ids LIKE ?",
                (f"%{memory_id}%",),
            ).fetchall()
            for ref_row in referencing:
                current = parse_linked_ids(ref_row["linked_ids"] or "")
                updated = tuple(lid for lid in current if lid != memory_id)
                db.execute(
                    "UPDATE memories SET linked_ids = ? WHERE id = ?",
                    (",".join(updated), ref_row["id"]),
                )

            # Remove from other memories' links JSON
            linking = db.execute(
                "SELECT id, links FROM memories WHERE links LIKE ?",
                (f"%{memory_id}%",),
            ).fetchall()
            for link_row in linking:
                links = parse_links(link_row["links"] or "")
                updated_links = tuple(lk for lk in links if lk.target_id != memory_id)
                links_json = json.dumps([lk.to_dict() for lk in updated_links])
                db.execute(
                    "UPDATE memories SET links = ? WHERE id = ?",
                    (links_json, link_row["id"]),
                )

            # Delete the memory (CASCADE handles embeddings & coactivation)
            db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))

            if forget_marker is not None:
                attrs = encode_forget_marker(forget_marker)
                columns = ", ".join(FORGET_ATTRIBUTES)
                placeholders = ",".join("?" * len(FORGET_ATTRIBUTES))
                db.execute(
                    f"INSERT OR REPLACE INTO forget_markers ({columns}) VALUES ({placeholders})",
                    [attrs[name] for name in FORGET_ATTRIBUTES],
                )

            db.commit()
            return True

        return await asyncio.to_thread(_delete)

    async def add_bidirectional_link(self, source_id: str, target_id: str) -> None:
        db = self._ensure_connected()

        def _link() -> None:
            for mem_id, other_id in [(source_id, target_id), (target_id, source_id)]:
                row = db.execute("SELECT linked_ids FROM memories WHERE id = ?", (mem_id,)).fetchone()
                if row is None:
                    continue
                current = parse_linked_ids(row["linked_ids"] or "")
                if other_id not in current:
                    new_linked = ",".join(current + (other_id,))
                    db.execute("UPDATE memories SET linked_ids = ? WHERE id = ?", (new_linked, mem_id))
            db.commit()

        await asyncio.to_thread(_link)

    # ── 消した跡 ────────────────────────────────

    async def fetch_forget_markers(self, since: str | None, limit: int) -> list[ForgetMarker]:
        db = self._ensure_connected()

        def _fetch() -> list[ForgetMarker]:
            if since is not None:
                rows = db.execute(
                    "SELECT * FROM forget_markers WHERE forgotten_at >= ?"
                    " ORDER BY forgotten_at DESC LIMIT ?",
                    (since, max(0, limit)),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM forget_markers ORDER BY forgotten_at DESC LIMIT ?",
                    (max(0, limit),),
                ).fetchall()
            return [decode_forget_marker(dict(row)) for row in rows]

        return await asyncio.to_thread(_fetch)

    # ── 一覧の材料（段2）──────────────────────

    async def fetch_indexed_memory_ids(self) -> list[str]:
        db = self._ensure_connected()

        def _fetch() -> list[str]:
            rows = db.execute("SELECT id FROM memories WHERE indexed = 1").fetchall()
            return [row["id"] for row in rows]

        return await asyncio.to_thread(_fetch)

    async def fetch_neighbors(self, memory_id: str) -> tuple[Memory | None, Memory | None]:
        """timestamp 順で 1 つ前・1 つ後。同時刻は id で並びを決める（DynamoDB の sk と同じ規則）。"""
        db = self._ensure_connected()

        def _fetch() -> tuple[Memory | None, Memory | None]:
            anchor = db.execute(
                "SELECT timestamp, id FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if anchor is None:
                return (None, None)
            ts, mid = anchor["timestamp"], anchor["id"]
            previous = db.execute(
                "SELECT * FROM memories WHERE (timestamp, id) < (?, ?)"
                " ORDER BY timestamp DESC, id DESC LIMIT 1",
                (ts, mid),
            ).fetchone()
            following = db.execute(
                "SELECT * FROM memories WHERE (timestamp, id) > (?, ?)"
                " ORDER BY timestamp ASC, id ASC LIMIT 1",
                (ts, mid),
            ).fetchone()
            rows = [row for row in (previous, following) if row is not None]
            decoded = {
                row["id"]: memory
                for row, memory in zip(rows, self._rows_to_memories(db, rows))
            }
            return (
                decoded.get(previous["id"]) if previous is not None else None,
                decoded.get(following["id"]) if following is not None else None,
            )

        return await asyncio.to_thread(_fetch)

    # ── ベクトル ────────────────────────────────

    async def fetch_vectors(self, memory_ids: list[str]) -> dict[str, bytes]:
        if not memory_ids:
            return {}
        db = self._ensure_connected()

        def _fetch() -> dict[str, bytes]:
            placeholders = ",".join("?" * len(memory_ids))
            rows = db.execute(
                f"SELECT memory_id, vector FROM embeddings WHERE memory_id IN ({placeholders})",
                memory_ids,
            ).fetchall()
            return {row["memory_id"]: bytes(row["vector"]) for row in rows}

        return await asyncio.to_thread(_fetch)

    async def fetch_all_vectors(self) -> list[VectorRow]:
        db = self._ensure_connected()

        def _fetch() -> list[VectorRow]:
            # 段2: index:false は Hopfield の母集団にも載せない
            sql = (
                "SELECT e.memory_id, e.vector, m.normalized_content"
                " FROM embeddings e JOIN memories m ON m.id = e.memory_id"
                " WHERE m.indexed = 1"
            )
            rows = db.execute(sql).fetchall()
            return [
                VectorRow(
                    memory_id=row["memory_id"],
                    vector=bytes(row["vector"]),
                    normalized_content=row["normalized_content"],
                )
                for row in rows
            ]

        return await asyncio.to_thread(_fetch)

    # ── 共活性 ──────────────────────────────────

    async def fetch_coactivation(self, memory_id: str) -> tuple[tuple[str, float], ...]:
        db = self._ensure_connected()
        return await asyncio.to_thread(self._get_coactivation, db, memory_id)

    async def fetch_coactivation_weight(self, source_id: str, target_id: str) -> float | None:
        db = self._ensure_connected()

        def _fetch() -> float | None:
            row = db.execute(
                "SELECT weight FROM coactivation WHERE source_id = ? AND target_id = ?",
                (source_id, target_id),
            ).fetchone()
            return float(row["weight"]) if row is not None else None

        return await asyncio.to_thread(_fetch)

    async def put_coactivation(self, source_id: str, target_id: str, weight: float) -> None:
        db = self._ensure_connected()

        def _put() -> None:
            db.execute(
                """INSERT INTO coactivation (source_id, target_id, weight)
                   VALUES (?, ?, ?)
                   ON CONFLICT(source_id, target_id) DO UPDATE SET weight = excluded.weight""",
                (source_id, target_id, weight),
            )
            db.commit()

        await asyncio.to_thread(_put)

    # ── エピソード ──────────────────────────────

    async def insert_episode(self, episode: Episode) -> None:
        db = self._ensure_connected()
        attrs = encode_episode(episode)
        columns = ", ".join(EPISODE_ATTRIBUTES)
        placeholders = ",".join("?" * len(EPISODE_ATTRIBUTES))
        values = [attrs[name] for name in EPISODE_ATTRIBUTES]

        def _insert() -> None:
            db.execute(f"INSERT INTO episodes ({columns}) VALUES ({placeholders})", values)
            db.commit()

        await asyncio.to_thread(_insert)

    async def fetch_episode(self, episode_id: str) -> Episode | None:
        db = self._ensure_connected()

        def _fetch() -> Episode | None:
            row = db.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
            if row is None:
                return None
            return _row_to_episode(row)

        return await asyncio.to_thread(_fetch)

    async def search_episodes(self, query: str, limit: int) -> list[Episode]:
        """Search episodes by title/summary (LIKE search, good enough for few episodes)."""
        db = self._ensure_connected()
        pattern = f"%{query}%"

        def _fetch() -> list[Episode]:
            rows = db.execute(
                """SELECT * FROM episodes
                   WHERE title LIKE ? OR summary LIKE ?
                   ORDER BY start_time DESC LIMIT ?""",
                (pattern, pattern, limit),
            ).fetchall()
            return [_row_to_episode(row) for row in rows]

        return await asyncio.to_thread(_fetch)

    async def fetch_all_episodes(self) -> list[Episode]:
        db = self._ensure_connected()

        def _fetch() -> list[Episode]:
            rows = db.execute("SELECT * FROM episodes ORDER BY start_time DESC").fetchall()
            return [_row_to_episode(row) for row in rows]

        return await asyncio.to_thread(_fetch)

    async def delete_episode(self, episode_id: str) -> None:
        db = self._ensure_connected()

        def _delete() -> None:
            db.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
            db.commit()

        await asyncio.to_thread(_delete)
