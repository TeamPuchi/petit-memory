"""Sleep engine — memory consolidation, decay, and forgetting."""

from __future__ import annotations

import logging
import math
from datetime import datetime

import numpy as np

from .config import SleepConfig
from .store import EMOTION_BOOST_MAP, MemoryStore
from .types import Memory, SleepStats
from .vector import cosine_similarity, decode_vector

logger = logging.getLogger(__name__)

# 「初めて」を示すキーワード — 初体験の記憶は保護
_FIRST_TIME_KEYWORDS = ("初めて", "はじめて", "初の", "first time", "first")


def _is_first_experience(content: str) -> bool:
    """記憶が「初めての体験」であるかを判定する."""
    lower = content.lower()
    return any(kw in lower for kw in _FIRST_TIME_KEYWORDS)


def calculate_retention_score(memory: Memory, now: datetime | None = None) -> float:
    """保持スコアを計算する (0.0–1.0).

    retention = (importance / 5) * 0.3
              + emotion_strength * 0.2
              + recency * 0.3
              + access_frequency * 0.2
    """
    if now is None:
        now = datetime.now()

    # importance component
    importance_component = (memory.importance / 5.0) * 0.3

    # emotion component
    emotion_strength = EMOTION_BOOST_MAP.get(memory.emotion, 0.0)
    emotion_component = emotion_strength * 0.2

    # recency component (half-life 30 days)
    try:
        memory_time = datetime.fromisoformat(memory.timestamp)
        age_days = max(0.0, (now - memory_time).total_seconds() / 86400)
    except ValueError:
        age_days = 0.0
    recency = math.exp(-age_days / 30.0)
    recency_component = recency * 0.3

    # access frequency component
    access_frequency = min(1.0, memory.access_count / 10.0)
    access_component = access_frequency * 0.2

    return importance_component + emotion_component + recency_component + access_component


def _is_protected(memory: Memory, config: SleepConfig) -> bool:
    """保護対象かどうか判定する."""
    if memory.importance >= config.protected_importance:
        return True
    if memory.emotion in config.protected_emotions:
        return True
    if _is_first_experience(memory.content):
        return True
    return False


class SleepEngine:
    """三段階の記憶整理: 圧縮 → 減衰 → 忘却."""

    def __init__(self, store: MemoryStore, config: SleepConfig | None = None):
        self._store = store
        self._config = config or SleepConfig()

    async def run(self, dry_run: bool = True) -> SleepStats:
        """Sleep を実行する.

        Args:
            dry_run: True なら実行せずに対象一覧のみ返す.
        """
        now = datetime.now()
        all_memories = await self._store.get_all()

        # min_age_days 以上の記憶だけ対象
        candidates: list[Memory] = []
        for m in all_memories:
            try:
                age = (now - datetime.fromisoformat(m.timestamp)).total_seconds() / 86400
            except ValueError:
                continue
            if age >= self._config.min_age_days:
                candidates.append(m)

        protected_count = 0

        # Phase 1: 圧縮 (merge)
        merged_results = await self._phase_merge(candidates, now, dry_run)
        merged_ids: set[str] = set()
        for entry in merged_results:
            merged_ids.update(entry["group"])

        # 圧縮された記憶は後続フェーズから除外
        remaining = [m for m in candidates if m.id not in merged_ids]

        # Phase 2: 減衰 (decay)
        decayed_results: list[dict] = []
        for m in remaining:
            if _is_protected(m, self._config):
                protected_count += 1
                continue
            retention = calculate_retention_score(m, now)
            if retention < self._config.decay_retention_threshold and m.importance > 1:
                old_imp = m.importance
                new_imp = max(1, old_imp - 1)
                decayed_results.append({
                    "id": m.id,
                    "importance": f"{old_imp}→{new_imp}",
                    "retention_score": round(retention, 3),
                })
                if not dry_run:
                    await self._store.update_memory_fields(
                        m.id, importance=new_imp,
                    )

        # Phase 3: 忘却 (forget)
        forgotten_results: list[dict] = []
        for m in remaining:
            if _is_protected(m, self._config):
                continue
            if m.id in {d["id"] for d in decayed_results}:
                # 今回減衰したばかりの記憶は忘却しない
                continue
            if self._should_forget(m, now):
                forgotten_results.append({
                    "id": m.id,
                    "content": m.content[:80],
                })
                if not dry_run:
                    await self._store.delete_memory(m.id)

        # 保護対象をカウント（全候補中）
        for m in candidates:
            if m.id not in merged_ids and _is_protected(m, self._config):
                # 既にカウント済みの分は phase2 で数えた
                pass

        return SleepStats(
            merged=merged_results,
            decayed=decayed_results,
            forgotten=forgotten_results,
            protected=protected_count,
            dry_run=dry_run,
        )

    def _should_forget(self, memory: Memory, now: datetime) -> bool:
        """忘却条件を満たすか判定する."""
        if memory.importance != 1:
            return False
        if memory.emotion != "neutral":
            return False
        if memory.episode_id:
            return False
        try:
            age = (now - datetime.fromisoformat(memory.timestamp)).total_seconds() / 86400
        except ValueError:
            return False
        if age < self._config.forget_min_age_days:
            return False
        if memory.access_count >= self._config.forget_max_access:
            return False
        return True

    async def _phase_merge(
        self,
        candidates: list[Memory],
        now: datetime,
        dry_run: bool,
    ) -> list[dict]:
        """Phase 1: 類似記憶を圧縮する."""
        # Group by category
        by_category: dict[str, list[Memory]] = {}
        for m in candidates:
            if _is_protected(m, self._config):
                continue
            by_category.setdefault(m.category, []).append(m)

        merged_results: list[dict] = []
        db = self._store._ensure_connected()

        for category, mems in by_category.items():
            if len(mems) < 2:
                continue

            # Load embeddings
            mem_ids = [m.id for m in mems]
            id_to_mem = {m.id: m for m in mems}

            placeholders = ",".join("?" * len(mem_ids))
            rows = db.execute(
                f"SELECT memory_id, vector FROM embeddings WHERE memory_id IN ({placeholders})",
                mem_ids,
            ).fetchall()

            if len(rows) < 2:
                continue

            id_to_vec: dict[str, np.ndarray] = {}
            for row in rows:
                id_to_vec[row["memory_id"]] = decode_vector(bytes(row["vector"]))

            # Greedy grouping by cosine similarity
            available = set(id_to_vec.keys())
            groups: list[list[str]] = []

            for mid in list(available):
                if mid not in available:
                    continue
                group = [mid]
                available.remove(mid)
                vec = id_to_vec[mid]

                for other_id in list(available):
                    other_vec = id_to_vec[other_id]
                    sim = float(cosine_similarity(vec, other_vec.reshape(1, -1))[0])
                    if sim >= self._config.similarity_threshold:
                        group.append(other_id)
                        available.remove(other_id)

                if len(group) >= 2:
                    groups.append(group)

            # Merge each group
            for group in groups:
                group_mems = [id_to_mem[gid] for gid in group if gid in id_to_mem]
                if len(group_mems) < 2:
                    continue

                # Sort by timestamp
                group_mems.sort(key=lambda m: m.timestamp)
                oldest_date = group_mems[0].timestamp[:10]
                newest_date = group_mems[-1].timestamp[:10]
                first_content = group_mems[0].content

                merged_content = (
                    f"{category}の記録 {len(group_mems)}件"
                    f"（{oldest_date}〜{newest_date}）。"
                    f"主な内容: {first_content[:100]}"
                )

                # Best importance and strongest emotion
                best_importance = max(m.importance for m in group_mems)
                strongest_emotion = max(
                    (m.emotion for m in group_mems),
                    key=lambda e: EMOTION_BOOST_MAP.get(e, 0.0),
                )

                merged_results.append({
                    "group": [m.id for m in group_mems],
                    "into": merged_content[:120],
                })

                if not dry_run:
                    await self._store.merge_memories(
                        source_ids=[m.id for m in group_mems],
                        merged_content=merged_content,
                        importance=best_importance,
                        emotion=strongest_emotion,
                        category=category,
                    )

        return merged_results
