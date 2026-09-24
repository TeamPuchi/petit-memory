"""段0: 保管層の抽象（MemoryStoreBackend）まわりの回帰テスト。

- SQLite 実装が抽象越しに同じ結果を返すこと
- 実装の選択が環境変数 1 つで決まること
- DynamoDB 実装が骨組み（NotImplementedError）であること
"""

from __future__ import annotations

import inspect

import pytest

from memory_mcp.config import MemoryConfig
from memory_mcp.dual_backend import DualWriteMemoryStore
from memory_mcp.dynamo_backend import DynamoMemoryStore
from memory_mcp.sqlite_backend import SqliteMemoryStore
from memory_mcp.store import MemoryStore
from memory_mcp.store_backend import (
    MemoryRecord,
    MemoryStoreBackend,
    create_backend,
)
from memory_mcp.types import Episode, Memory
from memory_mcp.vector import encode_vector

# ──────────────────────────────────────────────
# 実装の選択
# ──────────────────────────────────────────────


def test_create_backend_defaults_to_sqlite(temp_db_path: str) -> None:
    config = MemoryConfig(db_path=temp_db_path, collection_name="t")
    assert config.store_backend == "sqlite"
    assert isinstance(create_backend(config), SqliteMemoryStore)


def test_create_backend_selects_dynamo(temp_db_path: str) -> None:
    config = MemoryConfig(db_path=temp_db_path, collection_name="t", store_backend="dynamo")
    assert isinstance(create_backend(config), DynamoMemoryStore)


def test_create_backend_selects_dual(temp_db_path: str) -> None:
    config = MemoryConfig(db_path=temp_db_path, collection_name="t", store_backend="dual")
    backend = create_backend(config)
    assert isinstance(backend, DualWriteMemoryStore)
    assert isinstance(backend.primary, SqliteMemoryStore)
    assert isinstance(backend.secondary, DynamoMemoryStore)


def test_create_backend_rejects_unknown_name(temp_db_path: str) -> None:
    config = MemoryConfig(db_path=temp_db_path, collection_name="t", store_backend="postgres")
    with pytest.raises(ValueError, match="PETIT_MEMORY_STORE"):
        create_backend(config)


def test_all_backends_satisfy_the_protocol(temp_db_path: str) -> None:
    config = MemoryConfig(db_path=temp_db_path, collection_name="t")
    assert isinstance(SqliteMemoryStore(config), MemoryStoreBackend)
    assert isinstance(DynamoMemoryStore(config), MemoryStoreBackend)
    assert isinstance(
        DualWriteMemoryStore(SqliteMemoryStore(config), DynamoMemoryStore(config)),
        MemoryStoreBackend,
    )


def test_every_backend_mirrors_the_protocol_signatures() -> None:
    """抽象のメソッドが 3 つの実装に揃っていること（署名も含めて）。"""
    protocol_methods = [
        name
        for name, member in inspect.getmembers(MemoryStoreBackend, inspect.isfunction)
        if not name.startswith("_")
    ]
    assert len(protocol_methods) >= 20

    for name in protocol_methods:
        sqlite_sig = inspect.signature(getattr(SqliteMemoryStore, name))
        for implementation in (DynamoMemoryStore, DualWriteMemoryStore):
            other_sig = inspect.signature(getattr(implementation, name))
            assert sqlite_sig == other_sig, (
                f"{name} の署名が {implementation.__name__} でずれている"
            )


async def test_dynamo_backend_refuses_work_before_connect(temp_db_path: str) -> None:
    """接続前に使うと RuntimeError（段1 で実装が入ったので NotImplementedError ではない）。"""
    backend = DynamoMemoryStore(
        MemoryConfig(db_path=temp_db_path, collection_name="t", house_id="h1", petit_id="p1")
    )
    with pytest.raises(RuntimeError, match="not connected"):
        await backend.fetch_memory("some-id")
    with pytest.raises(RuntimeError, match="not connected"):
        await backend.fetch_all_memories()


def test_dynamo_key_prefixes(temp_db_path: str) -> None:
    backend = DynamoMemoryStore(
        MemoryConfig(db_path=temp_db_path, collection_name="t", house_id="h1", petit_id="p1")
    )
    assert backend.partition_key == "H#h1#P#p1"
    assert backend.memory_sk("2026-09-22T10:00:00", "m1") == "MEM#2026-09-22T10:00:00#m1"
    assert backend.vector_sk("m1") == "VEC#m1"
    assert backend.episode_sk("2026-09-20T09:00:00", "e1") == "EPI#2026-09-20T09:00:00#e1"
    assert backend.coactivation_sk("m1", "m2") == "COACT#m1#m2"
    assert backend.pointer_sk("m1") == "IDX#m1"
    assert backend.episode_pointer_sk("e1") == "IDX#EPI#e1"


async def test_memory_store_exposes_only_its_backend(temp_db_path: str) -> None:
    """計算層には「生の接続」を取る口が無い（段1 でテスト用ヘルパに移した）。"""
    config = MemoryConfig(db_path=temp_db_path, collection_name="t", store_backend="dynamo")
    store = MemoryStore(config)
    assert not hasattr(store, "_ensure_connected")
    assert isinstance(store.backend, DynamoMemoryStore)


# ──────────────────────────────────────────────
# SQLite 実装の往復（埋め込みモデル不要）
# ──────────────────────────────────────────────


@pytest.fixture
def sample_memory() -> Memory:
    return Memory(
        id="mem-1",
        content="長門の海で金魚の話をした",
        timestamp="2026-09-01T12:00:00",
        emotion="happy",
        importance=4,
        category="daily",
        tags=("海", "金魚"),
    )


def _record(memory: Memory) -> MemoryRecord:
    return MemoryRecord(
        memory=memory,
        normalized_content=memory.content,
        reading=None,
        vector=encode_vector([0.1, 0.2, 0.3]),
    )


async def test_backend_roundtrip_insert_fetch_update_delete(
    memory_config: MemoryConfig, sample_memory: Memory
) -> None:
    backend = create_backend(memory_config)
    await backend.connect()
    try:
        await backend.insert_memory(_record(sample_memory))

        fetched = await backend.fetch_memory("mem-1")
        assert fetched is not None
        assert fetched.content == sample_memory.content
        assert fetched.emotion == "happy"
        assert fetched.importance == 4
        assert fetched.tags == ("海", "金魚")

        assert await backend.update_memory_fields("mem-1", {"importance": 2}) is True
        assert (await backend.fetch_memory("mem-1")).importance == 2
        assert await backend.update_memory_fields("missing", {"importance": 2}) is False

        await backend.increment_access("mem-1", "2026-09-02T09:00:00")
        after_access = await backend.fetch_memory("mem-1")
        assert after_access.access_count == 1
        assert after_access.last_accessed == "2026-09-02T09:00:00"

        assert len(await backend.fetch_all_memories()) == 1
        assert await backend.delete_memory("mem-1") is True
        assert await backend.fetch_memory("mem-1") is None
        assert await backend.delete_memory("mem-1") is False
    finally:
        await backend.disconnect()


async def test_backend_returns_vectors_it_stored(
    memory_config: MemoryConfig, sample_memory: Memory
) -> None:
    backend = create_backend(memory_config)
    await backend.connect()
    try:
        record = _record(sample_memory)
        await backend.insert_memory(record)

        assert await backend.fetch_vectors(["mem-1"]) == {"mem-1": record.vector}
        assert await backend.fetch_vectors([]) == {}

        with_vectors = await backend.fetch_memories_with_vectors()
        assert len(with_vectors) == 1
        assert with_vectors[0].vector == record.vector

        rows = await backend.fetch_all_vectors()
        assert [r.memory_id for r in rows] == ["mem-1"]
        assert rows[0].normalized_content == sample_memory.content
    finally:
        await backend.disconnect()


async def test_backend_filters_and_recency(memory_config: MemoryConfig) -> None:
    backend = create_backend(memory_config)
    await backend.connect()
    try:
        for idx, (emotion, category, ts) in enumerate(
            [
                ("happy", "daily", "2026-09-01T00:00:00"),
                ("sad", "technical", "2026-09-02T00:00:00"),
                ("happy", "technical", "2026-09-03T00:00:00"),
            ]
        ):
            await backend.insert_memory(
                _record(
                    Memory(
                        id=f"m{idx}",
                        content=f"記憶 {idx}",
                        timestamp=ts,
                        emotion=emotion,
                        importance=3,
                        category=category,
                    )
                )
            )

        happy = await backend.fetch_memories_with_vectors(emotion="happy")
        assert {c.memory.id for c in happy} == {"m0", "m2"}

        technical = await backend.fetch_memories_with_vectors(category="technical")
        assert {c.memory.id for c in technical} == {"m1", "m2"}

        windowed = await backend.fetch_memories_with_vectors(
            date_from="2026-09-02T00:00:00", date_to="2026-09-02T23:59:59"
        )
        assert {c.memory.id for c in windowed} == {"m1"}

        recent = await backend.fetch_recent_memories(limit=2)
        assert [m.id for m in recent] == ["m2", "m1"]

        facets = await backend.fetch_memory_facets()
        assert len(facets.rows) == 3
        assert facets.oldest_timestamp == "2026-09-01T00:00:00"
        assert facets.newest_timestamp == "2026-09-03T00:00:00"
    finally:
        await backend.disconnect()


async def test_backend_coactivation_and_links(
    memory_config: MemoryConfig, sample_memory: Memory
) -> None:
    backend = create_backend(memory_config)
    await backend.connect()
    try:
        other = Memory(
            id="mem-2",
            content="やきとりを食べた",
            timestamp="2026-09-02T12:00:00",
            emotion="neutral",
            importance=3,
            category="daily",
        )
        await backend.insert_memory(_record(sample_memory))
        await backend.insert_memory(_record(other))

        assert await backend.fetch_coactivation_weight("mem-1", "mem-2") is None
        await backend.put_coactivation("mem-1", "mem-2", 0.4)
        assert await backend.fetch_coactivation_weight("mem-1", "mem-2") == pytest.approx(0.4)
        assert await backend.fetch_coactivation("mem-1") == (("mem-2", 0.4),)

        await backend.add_bidirectional_link("mem-1", "mem-2")
        await backend.add_bidirectional_link("mem-1", "mem-2")  # 二度目は増えない
        assert (await backend.fetch_memory("mem-1")).linked_ids == ("mem-2",)
        assert (await backend.fetch_memory("mem-2")).linked_ids == ("mem-1",)

        # 消すと逆参照も共活性も消える
        assert await backend.delete_memory("mem-2") is True
        assert (await backend.fetch_memory("mem-1")).linked_ids == ()
        assert await backend.fetch_coactivation("mem-1") == ()
    finally:
        await backend.disconnect()


async def test_backend_episode_crud(memory_config: MemoryConfig) -> None:
    backend = create_backend(memory_config)
    await backend.connect()
    try:
        episode = Episode(
            id="ep-1",
            title="金魚の水換え",
            start_time="2026-09-01T10:00:00",
            end_time="2026-09-01T11:00:00",
            memory_ids=("mem-1",),
            participants=("なぎ", "ぷち"),
            location_context=None,
            summary="水を換えた",
            emotion="happy",
            importance=3,
        )
        await backend.insert_episode(episode)

        assert (await backend.fetch_episode("ep-1")).title == "金魚の水換え"
        assert [e.id for e in await backend.search_episodes("金魚", 5)] == ["ep-1"]
        assert await backend.search_episodes("和太鼓", 5) == []
        assert len(await backend.fetch_all_episodes()) == 1

        await backend.delete_episode("ep-1")
        assert await backend.fetch_episode("ep-1") is None
    finally:
        await backend.disconnect()


async def test_backend_forgetting_a_memory_clears_its_episode_summary(memory_config: MemoryConfig) -> None:
    """K22: memory_ids に含む記憶を忘れたら EPI# の題・要約も空にして stale にする（枠は残す）。"""
    backend = create_backend(memory_config)
    await backend.connect()
    try:
        mem1 = Memory(id="mem-1", content="金魚に餌をやった", timestamp="2026-09-01T09:50:00",
                      emotion="happy", importance=3, category="daily")
        mem2 = Memory(id="mem-2", content="水を換えた", timestamp="2026-09-01T10:10:00",
                      emotion="happy", importance=3, category="daily")
        await backend.insert_memory(_record(mem1))
        await backend.insert_memory(_record(mem2))
        episode = Episode(
            id="ep-1", title="金魚の世話", start_time="2026-09-01T09:00:00", end_time="2026-09-01T11:00:00",
            memory_ids=("mem-1", "mem-2"), participants=("なぎ",), location_context=None,
            summary="餌をやって水を換えた", emotion="happy", importance=3,
        )
        await backend.insert_episode(episode)

        assert await backend.delete_memory("mem-1") is True

        epi = await backend.fetch_episode("ep-1")
        assert epi is not None
        assert epi.title == "" and epi.summary == "" and epi.stale is True
        assert epi.memory_ids == ("mem-1", "mem-2")  # memory_ids 自体は書き換えない
        assert epi.start_time == "2026-09-01T09:00:00" and epi.emotion == "happy" and epi.importance == 3

        # mem-1 と無関係の記憶を忘れても ep-1 には触らない
        mem3 = Memory(id="mem-3", content="無関係の記憶", timestamp="2026-09-01T12:00:00",
                      emotion="neutral", importance=2, category="daily")
        await backend.insert_memory(_record(mem3))
        await backend.delete_memory("mem-3")
        still = await backend.fetch_episode("ep-1")
        assert still.stale is True and still.title == ""
    finally:
        await backend.disconnect()


# ──────────────────────────────────────────────
# MemoryStore（計算層）が抽象越しに同じ結果を返す
# ──────────────────────────────────────────────


async def test_remember_then_search_through_the_seam(memory_store: MemoryStore) -> None:
    saved = await memory_store.save(
        content="長門の海で金魚の話をした",
        emotion="happy",
        importance=4,
        category="daily",
    )
    results = await memory_store.search(query="金魚", n_results=3)
    assert saved.id in {r.memory.id for r in results}


async def test_recall_through_the_seam(memory_store: MemoryStore) -> None:
    await memory_store.save(content="和太鼓の練習に行った", emotion="excited", importance=4)
    await memory_store.save(content="家庭菜園のトマトを収穫した", emotion="happy", importance=3)

    results = await memory_store.recall(context="和太鼓", n_results=2)
    assert results
    assert results[0].memory.content == "和太鼓の練習に行った"


async def test_time_decay_still_lowers_old_memories(
    memory_store: MemoryStore, set_memory_timestamp
) -> None:
    """減衰の計算は計算層に残っている（保管層は素の行を返すだけ）。"""
    await memory_store.save(content="金魚の水換えをした", emotion="neutral", importance=3)

    await set_memory_timestamp(memory_store, "2025-09-01T00:00:00")

    fresh = await memory_store.search_with_scoring(
        query="金魚", n_results=1, use_time_decay=False, use_emotion_boost=False
    )
    decayed = await memory_store.search_with_scoring(
        query="金魚", n_results=1, use_time_decay=True, use_emotion_boost=False
    )
    assert fresh and decayed
    assert decayed[0].time_decay_factor < 0.01
    assert decayed[0].final_score > fresh[0].final_score


async def test_association_links_survive_the_seam(memory_store: MemoryStore) -> None:
    first = await memory_store.save(content="長門の海は青かった", emotion="moved", importance=4)
    second = await memory_store.save_with_auto_link(
        content="長門の海でまた散歩した", link_threshold=2.0, max_links=5
    )

    assert first.id in second.linked_ids
    # depth は起点そのものを 1 段目に数える（server.py の既定も 2）。段0 でこの仕様は変えていない。
    linked = await memory_store.get_linked_memories(second.id, depth=2)
    assert first.id in {m.id for m in linked}
    assert await memory_store.get_linked_memories(second.id, depth=1) == []

    assert await memory_store.bump_coactivation(first.id, second.id, delta=0.7) is True
    refreshed = await memory_store.get_by_id(first.id)
    assert dict(refreshed.coactivation_weights)[second.id] == pytest.approx(0.7)


async def test_delete_through_the_seam_cleans_links(memory_store: MemoryStore) -> None:
    first = await memory_store.save(content="リルと散歩した", emotion="happy", importance=3)
    second = await memory_store.save_with_auto_link(
        content="リルと海辺を散歩した", link_threshold=2.0, max_links=5
    )
    assert first.id in second.linked_ids

    assert await memory_store.delete_memory(first.id) is True
    assert await memory_store.get_by_id(first.id) is None

    remaining = await memory_store.get_by_id(second.id)
    assert first.id not in remaining.linked_ids
    assert await memory_store.delete_memory(first.id) is False


async def test_get_vectors_feeds_the_sleep_merge_phase(memory_store: MemoryStore) -> None:
    """sleep.py が生の SQL をやめて使うようになった経路。"""
    saved = await memory_store.save(content="温泉に行った", emotion="happy", importance=3)
    vectors = await memory_store.get_vectors([saved.id])
    assert set(vectors) == {saved.id}
    assert len(vectors[saved.id]) > 0
