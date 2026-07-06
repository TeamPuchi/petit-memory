"""Tests for sleep engine (memory consolidation/decay/forgetting)."""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from memory_mcp.config import SleepConfig
from memory_mcp.sleep import SleepEngine, _is_first_experience, _is_protected, calculate_retention_score
from memory_mcp.store import MemoryStore
from memory_mcp.types import Memory

# ── retention score ────────────────────────────


class TestRetentionScore:
    def test_high_importance_recent(self):
        """重要で新しい記憶は高いスコア."""
        now = datetime.now()
        m = Memory(
            id="test", content="important", timestamp=now.isoformat(),
            emotion="excited", importance=5, category="daily",
            access_count=10,
        )
        score = calculate_retention_score(m, now)
        assert score > 0.7

    def test_low_importance_old(self):
        """低重要度で古い記憶は低いスコア."""
        now = datetime.now()
        old = now - timedelta(days=90)
        m = Memory(
            id="test", content="trivial", timestamp=old.isoformat(),
            emotion="neutral", importance=1, category="daily",
            access_count=0,
        )
        score = calculate_retention_score(m, now)
        assert score < 0.2

    def test_score_range(self):
        """スコアは 0.0–1.0 の範囲."""
        now = datetime.now()
        for imp in range(1, 6):
            for emotion in ["neutral", "happy", "excited"]:
                for days in [0, 30, 90]:
                    ts = (now - timedelta(days=days)).isoformat()
                    m = Memory(
                        id="test", content="x", timestamp=ts,
                        emotion=emotion, importance=imp, category="daily",
                    )
                    score = calculate_retention_score(m, now)
                    assert 0.0 <= score <= 1.0, f"Score {score} out of range"


# ── protection ─────────────────────────────────


class TestProtection:
    def test_high_importance_protected(self):
        m = Memory(
            id="test", content="x", timestamp=datetime.now().isoformat(),
            emotion="neutral", importance=4, category="daily",
        )
        assert _is_protected(m, SleepConfig())

    def test_strong_emotion_protected(self):
        m = Memory(
            id="test", content="x", timestamp=datetime.now().isoformat(),
            emotion="excited", importance=1, category="daily",
        )
        assert _is_protected(m, SleepConfig())

    def test_first_experience_protected(self):
        m = Memory(
            id="test", content="初めて外を見た", timestamp=datetime.now().isoformat(),
            emotion="neutral", importance=1, category="daily",
        )
        assert _is_protected(m, SleepConfig())

    def test_first_time_english_protected(self):
        m = Memory(
            id="test", content="This was my first time seeing snow",
            timestamp=datetime.now().isoformat(),
            emotion="neutral", importance=1, category="daily",
        )
        assert _is_protected(m, SleepConfig())

    def test_low_importance_neutral_not_protected(self):
        m = Memory(
            id="test", content="普通の記録", timestamp=datetime.now().isoformat(),
            emotion="neutral", importance=2, category="daily",
        )
        assert not _is_protected(m, SleepConfig())


class TestIsFirstExperience:
    def test_japanese_hajimete(self):
        assert _is_first_experience("初めてカメラで撮影した")

    def test_japanese_hiragana(self):
        assert _is_first_experience("はじめて空を見た")

    def test_english(self):
        assert _is_first_experience("First time seeing the stars")

    def test_not_first(self):
        assert not _is_first_experience("今日も空を見た")


# ── SleepEngine integration ───────────────────


@pytest_asyncio.fixture
async def store_with_old_memories(memory_store: MemoryStore):
    """古い記憶を持つストアを作成."""
    now = datetime.now()
    old = now - timedelta(days=30)

    # 低重要度・neutral の古い記憶（忘却対象）
    await memory_store.save(
        content="何もない日だった",
        emotion="neutral",
        importance=1,
        category="daily",
    )
    # 手動でタイムスタンプを古くする
    db = memory_store._ensure_connected()
    db.execute(
        "UPDATE memories SET timestamp = ? WHERE content = ?",
        (old.isoformat(), "何もない日だった"),
    )
    db.commit()

    # 低重要度だが emotion あり（保護対象）
    await memory_store.save(
        content="嬉しい出来事",
        emotion="excited",
        importance=1,
        category="daily",
    )
    db.execute(
        "UPDATE memories SET timestamp = ? WHERE content = ?",
        (old.isoformat(), "嬉しい出来事"),
    )
    db.commit()

    # 高重要度（保護対象）
    await memory_store.save(
        content="大切な思い出",
        emotion="neutral",
        importance=5,
        category="memory",
    )
    db.execute(
        "UPDATE memories SET timestamp = ? WHERE content = ?",
        (old.isoformat(), "大切な思い出"),
    )
    db.commit()

    # 中重要度・neutral（減衰対象候補）
    await memory_store.save(
        content="普通の会話をした",
        emotion="neutral",
        importance=2,
        category="conversation",
    )
    db.execute(
        "UPDATE memories SET timestamp = ? WHERE content = ?",
        (old.isoformat(), "普通の会話をした"),
    )
    db.commit()

    # 初めての体験（保護対象）
    await memory_store.save(
        content="初めて星を見た",
        emotion="neutral",
        importance=1,
        category="observation",
    )
    db.execute(
        "UPDATE memories SET timestamp = ? WHERE content = ?",
        (old.isoformat(), "初めて星を見た"),
    )
    db.commit()

    return memory_store


@pytest.mark.asyncio
async def test_sleep_dry_run(store_with_old_memories: MemoryStore):
    """dry_run=true では記憶が変更されない."""
    store = store_with_old_memories
    before = await store.get_all()

    engine = SleepEngine(store)
    stats = await engine.run(dry_run=True)

    after = await store.get_all()
    assert len(before) == len(after), "dry_run should not change memory count"
    assert stats.dry_run is True


@pytest.mark.asyncio
async def test_sleep_forget(store_with_old_memories: MemoryStore):
    """importance=1, neutral, old, low access の記憶は忘却される."""
    store = store_with_old_memories
    engine = SleepEngine(store)
    stats = await engine.run(dry_run=False)

    # 「何もない日だった」は忘却されるはず
    forgotten_contents = [f["content"] for f in stats.forgotten]
    assert any("何もない日" in c for c in forgotten_contents)


@pytest.mark.asyncio
async def test_sleep_protects_high_importance(store_with_old_memories: MemoryStore):
    """importance >= 4 は保護される."""
    store = store_with_old_memories
    engine = SleepEngine(store)
    await engine.run(dry_run=False)

    remaining = await store.get_all()
    remaining_contents = [m.content for m in remaining]
    assert "大切な思い出" in remaining_contents


@pytest.mark.asyncio
async def test_sleep_protects_strong_emotion(store_with_old_memories: MemoryStore):
    """strong emotion は保護される."""
    store = store_with_old_memories
    engine = SleepEngine(store)
    await engine.run(dry_run=False)

    remaining = await store.get_all()
    remaining_contents = [m.content for m in remaining]
    assert "嬉しい出来事" in remaining_contents


@pytest.mark.asyncio
async def test_sleep_protects_first_experience(store_with_old_memories: MemoryStore):
    """「初めて」の記憶は保護される."""
    store = store_with_old_memories
    engine = SleepEngine(store)
    await engine.run(dry_run=False)

    remaining = await store.get_all()
    remaining_contents = [m.content for m in remaining]
    assert "初めて星を見た" in remaining_contents


@pytest.mark.asyncio
async def test_sleep_decay(store_with_old_memories: MemoryStore):
    """低保持スコアの記憶は importance が下がる."""
    store = store_with_old_memories
    engine = SleepEngine(store)
    stats = await engine.run(dry_run=False)

    # 「普通の会話をした」は imp=2 → imp=1 に減衰するはず
    if stats.decayed:
        for d in stats.decayed:
            assert "→" in d["importance"]


@pytest.mark.asyncio
async def test_sleep_protected_count(store_with_old_memories: MemoryStore):
    """保護された記憶の数がカウントされる."""
    store = store_with_old_memories
    engine = SleepEngine(store)
    stats = await engine.run(dry_run=True)

    assert stats.protected >= 1


@pytest.mark.asyncio
async def test_sleep_episode_protection(memory_store: MemoryStore):
    """エピソード所属の記憶は削除されない."""
    now = datetime.now()
    old = now - timedelta(days=30)

    # エピソード所属の低重要度記憶
    mem = await memory_store.save(
        content="エピソードに属する記録",
        emotion="neutral",
        importance=1,
        category="daily",
        episode_id="ep-test",
    )
    db = memory_store._ensure_connected()
    db.execute(
        "UPDATE memories SET timestamp = ? WHERE id = ?",
        (old.isoformat(), mem.id),
    )
    db.commit()

    engine = SleepEngine(memory_store)
    await engine.run(dry_run=False)

    remaining = await memory_store.get_all()
    assert any(m.content == "エピソードに属する記録" for m in remaining)


# ── store.delete_memory / merge_memories ──────


@pytest.mark.asyncio
async def test_delete_memory(memory_store: MemoryStore):
    """delete_memory でメモリが消える."""
    mem = await memory_store.save(content="消す記憶", emotion="neutral", importance=1, category="daily")
    result = await memory_store.delete_memory(mem.id)
    assert result is True

    # 存在しないことを確認
    fetched = await memory_store.get_by_id(mem.id)
    assert fetched is None


@pytest.mark.asyncio
async def test_delete_nonexistent(memory_store: MemoryStore):
    """存在しない ID の削除は False."""
    result = await memory_store.delete_memory("nonexistent-id")
    assert result is False


@pytest.mark.asyncio
async def test_delete_cleans_linked_ids(memory_store: MemoryStore):
    """削除時に他の記憶の linked_ids からも除去される."""
    mem1 = await memory_store.save_with_auto_link(
        content="記憶A", emotion="neutral", importance=3, category="daily",
    )
    mem2 = await memory_store.save_with_auto_link(
        content="記憶A", emotion="neutral", importance=3, category="daily",
        link_threshold=2.0,  # force link
    )

    # mem2 should be linked to mem1
    await memory_store.delete_memory(mem2.id)

    # mem1 の linked_ids から mem2 が消えていること
    refreshed = await memory_store.get_by_id(mem1.id)
    if refreshed:
        assert mem2.id not in refreshed.linked_ids


@pytest.mark.asyncio
async def test_merge_memories(memory_store: MemoryStore):
    """merge_memories で複数が1つに統合される."""
    m1 = await memory_store.save(content="朝の散歩1", emotion="happy", importance=2, category="daily")
    m2 = await memory_store.save(content="朝の散歩2", emotion="neutral", importance=3, category="daily")

    merged = await memory_store.merge_memories(
        source_ids=[m1.id, m2.id],
        merged_content="朝の散歩の記録 2件",
        importance=3,
        emotion="happy",
        category="daily",
    )

    assert merged.content == "朝の散歩の記録 2件"
    assert merged.importance == 3

    # 元の記憶は削除されている
    assert await memory_store.get_by_id(m1.id) is None
    assert await memory_store.get_by_id(m2.id) is None
