"""片流し複製の保管層（段1）。

書き込みは 2 枚（primary と secondary）へ流し、**読みは primary からしか返さない**。
secondary が落ちても primary の書き込みは落とさない: 例外は握って
警告ログを出し、失敗カウンタを増やすだけにする。

段1 の目的は「DynamoDB に同じものが溜まっていく」ことの確認で、
読みの正しさは引き続き SQLite が担保する。
"""

from __future__ import annotations

import logging
from typing import Any

from .store_backend import (
    MemoryFacets,
    MemoryRecord,
    MemoryStoreBackend,
    MemoryWithVector,
    VectorRow,
)
from .types import AccessRecord, Episode, ForgetMarker, Memory

logger = logging.getLogger(__name__)


class DualWriteMemoryStore:
    """primary に書き、secondary にも同じものを流す保管層。

    読み（`fetch_*` と `search_episodes`）は primary だけを見る。
    secondary で起きた例外は呼び出し元に伝えない。
    """

    def __init__(
        self,
        primary: MemoryStoreBackend,
        secondary: MemoryStoreBackend,
    ):
        self._primary = primary
        self._secondary = secondary
        self._secondary_ready = False
        self._secondary_failures = 0
        self._secondary_failures_by_method: dict[str, int] = {}

    # ── 状態 ────────────────────────────────────

    @property
    def primary(self) -> MemoryStoreBackend:
        return self._primary

    @property
    def secondary(self) -> MemoryStoreBackend:
        return self._secondary

    @property
    def secondary_ready(self) -> bool:
        """secondary への接続が済んでいるか。false の間、複製は黙って飛ばす。"""
        return self._secondary_ready

    @property
    def secondary_failures(self) -> int:
        """secondary への複製が落ちた回数（接続できずに飛ばした分も数える）。"""
        return self._secondary_failures

    @property
    def secondary_failures_by_method(self) -> dict[str, int]:
        return dict(self._secondary_failures_by_method)

    def _record_failure(self, method: str, error: BaseException | None) -> None:
        self._secondary_failures += 1
        self._secondary_failures_by_method[method] = (
            self._secondary_failures_by_method.get(method, 0) + 1
        )
        if error is None:
            logger.warning(
                "dual-write: secondary not ready, skipped %s (failures=%d)",
                method,
                self._secondary_failures,
            )
        else:
            logger.warning(
                "dual-write: secondary %s failed (failures=%d): %s",
                method,
                self._secondary_failures,
                error,
            )

    async def _replicate(self, method: str, *args: Any, **kwargs: Any) -> None:
        """secondary に同じ書き込みを流す。失敗しても呼び出し元には伝えない。"""
        if not self._secondary_ready:
            self._record_failure(method, None)
            return
        try:
            await getattr(self._secondary, method)(*args, **kwargs)
        except Exception as error:  # noqa: BLE001 - primary を巻き込まないため握る
            self._record_failure(method, error)

    # ── 接続 ────────────────────────────────────

    async def connect(self) -> None:
        """primary は必ず繋ぐ。secondary は繋がらなくても先へ進む。"""
        await self._primary.connect()
        try:
            await self._secondary.connect()
        except Exception as error:  # noqa: BLE001
            self._secondary_ready = False
            self._record_failure("connect", error)
            return
        self._secondary_ready = True

    async def disconnect(self) -> None:
        try:
            if self._secondary_ready:
                await self._secondary.disconnect()
        except Exception as error:  # noqa: BLE001
            self._record_failure("disconnect", error)
        finally:
            self._secondary_ready = False
            await self._primary.disconnect()

    # ── 読み: primary だけ ──────────────────────

    async def fetch_memory(self, memory_id: str) -> Memory | None:
        return await self._primary.fetch_memory(memory_id)

    async def fetch_memories(self, memory_ids: list[str]) -> list[Memory]:
        return await self._primary.fetch_memories(memory_ids)

    async def fetch_all_memories(self) -> list[Memory]:
        return await self._primary.fetch_all_memories()

    async def fetch_memories_with_vectors(
        self,
        emotion: str | None = None,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[MemoryWithVector]:
        return await self._primary.fetch_memories_with_vectors(
            emotion=emotion, category=category, date_from=date_from, date_to=date_to
        )

    async def fetch_recent_memories(self, limit: int, category: str | None = None) -> list[Memory]:
        return await self._primary.fetch_recent_memories(limit, category)

    async def fetch_important_memories(
        self,
        min_importance: int,
        min_access_count: int,
        since: str | None,
        limit: int,
    ) -> list[Memory]:
        return await self._primary.fetch_important_memories(
            min_importance, min_access_count, since, limit
        )

    async def fetch_memory_facets(self) -> MemoryFacets:
        return await self._primary.fetch_memory_facets()

    async def fetch_vectors(self, memory_ids: list[str]) -> dict[str, bytes]:
        return await self._primary.fetch_vectors(memory_ids)

    async def fetch_all_vectors(self) -> list[VectorRow]:
        return await self._primary.fetch_all_vectors()

    async def fetch_coactivation(self, memory_id: str) -> tuple[tuple[str, float], ...]:
        return await self._primary.fetch_coactivation(memory_id)

    async def fetch_coactivation_weight(self, source_id: str, target_id: str) -> float | None:
        return await self._primary.fetch_coactivation_weight(source_id, target_id)

    async def fetch_episode(self, episode_id: str) -> Episode | None:
        return await self._primary.fetch_episode(episode_id)

    async def search_episodes(self, query: str, limit: int) -> list[Episode]:
        return await self._primary.search_episodes(query, limit)

    async def fetch_all_episodes(self) -> list[Episode]:
        return await self._primary.fetch_all_episodes()

    async def fetch_forget_markers(self, since: str | None, limit: int) -> list[ForgetMarker]:
        return await self._primary.fetch_forget_markers(since, limit)

    async def fetch_indexed_memory_ids(self) -> list[str]:
        return await self._primary.fetch_indexed_memory_ids()

    async def fetch_access_records(self, limit: int) -> list[AccessRecord]:
        return await self._primary.fetch_access_records(limit)

    async def fetch_neighbors(self, memory_id: str) -> tuple[Memory | None, Memory | None]:
        return await self._primary.fetch_neighbors(memory_id)

    # ── 書き: 2 枚へ ────────────────────────────

    async def insert_memory(self, record: MemoryRecord) -> None:
        await self._primary.insert_memory(record)
        await self._replicate("insert_memory", record)

    async def update_memory_fields(self, memory_id: str, fields: dict[str, Any]) -> bool:
        result = await self._primary.update_memory_fields(memory_id, fields)
        await self._replicate("update_memory_fields", memory_id, fields)
        return result

    async def update_episode_id(self, memory_id: str, episode_id: str | None) -> bool:
        result = await self._primary.update_episode_id(memory_id, episode_id)
        await self._replicate("update_episode_id", memory_id, episode_id)
        return result

    async def increment_access(self, memory_id: str, last_accessed: str) -> None:
        await self._primary.increment_access(memory_id, last_accessed)
        await self._replicate("increment_access", memory_id, last_accessed)

    async def delete_memory(self, memory_id: str, forget_marker: ForgetMarker | None = None) -> bool:
        result = await self._primary.delete_memory(memory_id, forget_marker)
        await self._replicate("delete_memory", memory_id, forget_marker)
        return result

    async def shred_conversation_copies(self, conversation_ids: tuple[str, ...]) -> int:
        # 会話の写しの鍵は secondary（DynamoDB）側にしか無い。primary（SQLite）は 0 を返す
        result = await self._primary.shred_conversation_copies(conversation_ids)
        await self._replicate("shred_conversation_copies", conversation_ids)
        return result

    async def put_access_record(self, record: AccessRecord) -> None:
        await self._primary.put_access_record(record)
        await self._replicate("put_access_record", record)

    async def add_bidirectional_link(self, source_id: str, target_id: str) -> None:
        await self._primary.add_bidirectional_link(source_id, target_id)
        await self._replicate("add_bidirectional_link", source_id, target_id)

    async def put_coactivation(self, source_id: str, target_id: str, weight: float) -> None:
        await self._primary.put_coactivation(source_id, target_id, weight)
        await self._replicate("put_coactivation", source_id, target_id, weight)

    async def insert_episode(self, episode: Episode) -> None:
        await self._primary.insert_episode(episode)
        await self._replicate("insert_episode", episode)

    async def delete_episode(self, episode_id: str) -> None:
        await self._primary.delete_episode(episode_id)
        await self._replicate("delete_episode", episode_id)
