"""段1: DynamoDB 保管層と片流し複製のテスト（moto で偽装、AWS 資格情報は不要）。"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("moto")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from memory_mcp.config import MemoryConfig  # noqa: E402
from memory_mcp.dual_backend import DualWriteMemoryStore  # noqa: E402
from memory_mcp.dynamo_backend import DynamoMemoryStore  # noqa: E402
from memory_mcp.sqlite_backend import SqliteMemoryStore  # noqa: E402
from memory_mcp.store_backend import MemoryRecord  # noqa: E402
from memory_mcp.types import Episode, Memory  # noqa: E402
from memory_mcp.vector import encode_vector  # noqa: E402

TABLE_NAME = "house"
REGION = "ap-northeast-1"


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """moto に本物の資格情報を使わせない。"""
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(key, value)
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)


@pytest.fixture
def dynamo_config(temp_db_path: str) -> MemoryConfig:
    return MemoryConfig(
        db_path=temp_db_path,
        collection_name="t",
        store_backend="dynamo",
        dynamo_table=TABLE_NAME,
        house_id="nagato-1",
        petit_id="puchi",
    )


@pytest.fixture
def house_table(aws_credentials: None):
    """単一表 house を moto 上に作る（pk / sk のみ、GSI 無し）。"""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield dynamodb.Table(TABLE_NAME)


def memory(
    memory_id: str = "mem-1",
    content: str = "長門の海で金魚の話をした",
    timestamp: str = "2026-09-01T12:00:00",
    emotion: str = "happy",
    importance: int = 4,
    category: str = "daily",
    **kwargs,
) -> Memory:
    return Memory(
        id=memory_id,
        content=content,
        timestamp=timestamp,
        emotion=emotion,
        importance=importance,
        category=category,
        **kwargs,
    )


def record(mem: Memory, vector: list[float] | None = None) -> MemoryRecord:
    return MemoryRecord(
        memory=mem,
        normalized_content=mem.content,
        reading=None,
        vector=encode_vector(vector or [0.1, 0.2, 0.3]),
    )


@pytest.fixture
async def dynamo_backend(house_table, dynamo_config: MemoryConfig):
    backend = DynamoMemoryStore(dynamo_config)
    await backend.connect()
    yield backend
    await backend.disconnect()


# ──────────────────────────────────────────────
# DynamoDB 単体の往復（段0 の SQLite 往復と同じ観点）
# ──────────────────────────────────────────────


async def test_roundtrip_insert_fetch_update_delete(dynamo_backend: DynamoMemoryStore) -> None:
    mem = memory(tags=("海", "金魚"))
    await dynamo_backend.insert_memory(record(mem))

    fetched = await dynamo_backend.fetch_memory("mem-1")
    assert fetched is not None
    assert fetched.content == mem.content
    assert fetched.emotion == "happy"
    assert fetched.importance == 4
    assert fetched.tags == ("海", "金魚")
    assert fetched.episode_id is None

    assert await dynamo_backend.update_memory_fields("mem-1", {"importance": 2}) is True
    assert (await dynamo_backend.fetch_memory("mem-1")).importance == 2
    assert await dynamo_backend.update_memory_fields("missing", {"importance": 2}) is False

    await dynamo_backend.increment_access("mem-1", "2026-09-02T09:00:00")
    after = await dynamo_backend.fetch_memory("mem-1")
    assert after.access_count == 1
    assert after.last_accessed == "2026-09-02T09:00:00"

    assert len(await dynamo_backend.fetch_all_memories()) == 1
    assert await dynamo_backend.delete_memory("mem-1") is True
    assert await dynamo_backend.fetch_memory("mem-1") is None
    assert await dynamo_backend.delete_memory("mem-1") is False


async def test_pointer_lets_us_fetch_by_id_without_a_gsi(
    dynamo_backend: DynamoMemoryStore, house_table
) -> None:
    """`IDX#<id>` の指し札が本体 sk を指していること（GSI は作っていない）。"""
    await dynamo_backend.insert_memory(record(memory()))

    pointer = house_table.get_item(
        Key={"pk": dynamo_backend.partition_key, "sk": "IDX#mem-1"}
    )["Item"]
    assert pointer["target_sk"] == "MEM#2026-09-01T12:00:00#mem-1"

    assert house_table.table_status == "ACTIVE"
    assert not house_table.global_secondary_indexes

    # 指し札も一緒に消える
    await dynamo_backend.delete_memory("mem-1")
    assert "Item" not in house_table.get_item(
        Key={"pk": dynamo_backend.partition_key, "sk": "IDX#mem-1"}
    )


async def test_vectors_survive_the_roundtrip(dynamo_backend: DynamoMemoryStore) -> None:
    mem = memory()
    rec = record(mem)
    await dynamo_backend.insert_memory(rec)

    assert await dynamo_backend.fetch_vectors(["mem-1"]) == {"mem-1": rec.vector}
    assert await dynamo_backend.fetch_vectors([]) == {}

    with_vectors = await dynamo_backend.fetch_memories_with_vectors()
    assert len(with_vectors) == 1
    assert with_vectors[0].vector == rec.vector

    rows = await dynamo_backend.fetch_all_vectors()
    assert [r.memory_id for r in rows] == ["mem-1"]
    assert rows[0].normalized_content == mem.content


async def test_filters_and_recency(dynamo_backend: DynamoMemoryStore) -> None:
    for idx, (emotion, category, ts) in enumerate(
        [
            ("happy", "daily", "2026-09-01T00:00:00"),
            ("sad", "technical", "2026-09-02T00:00:00"),
            ("happy", "technical", "2026-09-03T00:00:00"),
        ]
    ):
        await dynamo_backend.insert_memory(
            record(memory(memory_id=f"m{idx}", content=f"記憶 {idx}", timestamp=ts,
                          emotion=emotion, importance=3, category=category))
        )

    happy = await dynamo_backend.fetch_memories_with_vectors(emotion="happy")
    assert {c.memory.id for c in happy} == {"m0", "m2"}

    technical = await dynamo_backend.fetch_memories_with_vectors(category="technical")
    assert {c.memory.id for c in technical} == {"m1", "m2"}

    windowed = await dynamo_backend.fetch_memories_with_vectors(
        date_from="2026-09-02T00:00:00", date_to="2026-09-02T23:59:59"
    )
    assert {c.memory.id for c in windowed} == {"m1"}

    recent = await dynamo_backend.fetch_recent_memories(limit=2)
    assert [m.id for m in recent] == ["m2", "m1"]

    facets = await dynamo_backend.fetch_memory_facets()
    assert len(facets.rows) == 3
    assert facets.oldest_timestamp == "2026-09-01T00:00:00"
    assert facets.newest_timestamp == "2026-09-03T00:00:00"


async def test_coactivation_and_links(dynamo_backend: DynamoMemoryStore) -> None:
    await dynamo_backend.insert_memory(record(memory()))
    await dynamo_backend.insert_memory(
        record(memory(memory_id="mem-2", content="やきとりを食べた", timestamp="2026-09-02T12:00:00",
                      emotion="neutral", importance=3))
    )

    assert await dynamo_backend.fetch_coactivation_weight("mem-1", "mem-2") is None
    await dynamo_backend.put_coactivation("mem-1", "mem-2", 0.4)
    assert await dynamo_backend.fetch_coactivation_weight("mem-1", "mem-2") == pytest.approx(0.4)
    assert await dynamo_backend.fetch_coactivation("mem-1") == (("mem-2", 0.4),)

    await dynamo_backend.add_bidirectional_link("mem-1", "mem-2")
    await dynamo_backend.add_bidirectional_link("mem-1", "mem-2")  # 二度目は増えない
    assert (await dynamo_backend.fetch_memory("mem-1")).linked_ids == ("mem-2",)
    assert (await dynamo_backend.fetch_memory("mem-2")).linked_ids == ("mem-1",)

    assert await dynamo_backend.delete_memory("mem-2") is True
    assert (await dynamo_backend.fetch_memory("mem-1")).linked_ids == ()
    assert await dynamo_backend.fetch_coactivation("mem-1") == ()


async def test_episode_crud(dynamo_backend: DynamoMemoryStore, house_table) -> None:
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
    await dynamo_backend.insert_episode(episode)

    pointer = house_table.get_item(
        Key={"pk": dynamo_backend.partition_key, "sk": "IDX#EPI#ep-1"}
    )["Item"]
    assert pointer["target_sk"] == "EPI#2026-09-01T10:00:00#ep-1"

    fetched = await dynamo_backend.fetch_episode("ep-1")
    assert fetched is not None
    assert fetched.title == "金魚の水換え"
    assert fetched.participants == ("なぎ", "ぷち")
    assert fetched.location_context is None

    assert [e.id for e in await dynamo_backend.search_episodes("金魚", 5)] == ["ep-1"]
    assert await dynamo_backend.search_episodes("和太鼓", 5) == []
    assert len(await dynamo_backend.fetch_all_episodes()) == 1

    await dynamo_backend.delete_episode("ep-1")
    assert await dynamo_backend.fetch_episode("ep-1") is None
    assert await dynamo_backend.fetch_episode("missing") is None


async def test_important_memories_and_episode_id(dynamo_backend: DynamoMemoryStore) -> None:
    await dynamo_backend.insert_memory(record(memory(memory_id="low", importance=2)))
    await dynamo_backend.insert_memory(
        record(memory(memory_id="high", timestamp="2026-09-02T00:00:00", importance=5))
    )
    for _ in range(5):
        await dynamo_backend.increment_access("high", "2026-09-05T00:00:00")

    important = await dynamo_backend.fetch_important_memories(
        min_importance=4, min_access_count=5, since=None, limit=10
    )
    assert [m.id for m in important] == ["high"]

    assert await dynamo_backend.update_episode_id("high", "ep-9") is True
    assert (await dynamo_backend.fetch_memory("high")).episode_id == "ep-9"
    assert await dynamo_backend.update_episode_id("high", None) is True
    assert (await dynamo_backend.fetch_memory("high")).episode_id is None
    assert await dynamo_backend.update_episode_id("missing", "ep-9") is False


async def test_sqlite_and_dynamo_return_the_same_memory(
    house_table, dynamo_config: MemoryConfig, memory_config: MemoryConfig
) -> None:
    """同じ MemoryRecord を入れたら、2 枚から同じ Memory が返ること。"""
    mem = memory(
        tags=("海", "金魚"),
        linked_ids=("other",),
        novelty_score=0.25,
        prediction_error=0.5,
        activation_count=2,
        last_activated="2026-09-02T00:00:00",
    )
    rec = record(mem)

    sqlite_backend = SqliteMemoryStore(memory_config)
    dynamo = DynamoMemoryStore(dynamo_config)
    await sqlite_backend.connect()
    await dynamo.connect()
    try:
        await sqlite_backend.insert_memory(rec)
        await dynamo.insert_memory(rec)
        assert await dynamo.fetch_memory("mem-1") == await sqlite_backend.fetch_memory("mem-1")
        assert await dynamo.fetch_all_memories() == await sqlite_backend.fetch_all_memories()
    finally:
        await dynamo.disconnect()
        await sqlite_backend.disconnect()


# ──────────────────────────────────────────────
# 片流し複製
# ──────────────────────────────────────────────


@pytest.fixture
async def dual_backend(house_table, dynamo_config: MemoryConfig, memory_config: MemoryConfig):
    backend = DualWriteMemoryStore(
        primary=SqliteMemoryStore(memory_config),
        secondary=DynamoMemoryStore(dynamo_config),
    )
    await backend.connect()
    yield backend
    await backend.disconnect()


async def test_dual_write_lands_in_both(dual_backend: DualWriteMemoryStore) -> None:
    await dual_backend.insert_memory(record(memory()))
    # SQLite 側の coactivation は外部キー制約があるので、相手も入れてから
    await dual_backend.insert_memory(
        record(memory(memory_id="mem-2", content="やきとりを食べた", timestamp="2026-09-02T12:00:00"))
    )
    await dual_backend.put_coactivation("mem-1", "mem-2", 0.3)

    assert dual_backend.secondary_failures == 0
    assert (await dual_backend.primary.fetch_memory("mem-1")).content == "長門の海で金魚の話をした"
    assert (await dual_backend.secondary.fetch_memory("mem-1")).content == "長門の海で金魚の話をした"
    assert await dual_backend.secondary.fetch_coactivation_weight("mem-1", "mem-2") == pytest.approx(0.3)


async def test_primary_survives_a_broken_secondary(
    dual_backend: DualWriteMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """secondary が落ちても primary の書き込みは通る（例外は握る・カウンタは増える）。"""

    async def boom(*args, **kwargs):
        raise RuntimeError("dynamo is on fire")

    monkeypatch.setattr(dual_backend.secondary, "insert_memory", boom)

    await dual_backend.insert_memory(record(memory()))

    assert (await dual_backend.primary.fetch_memory("mem-1")) is not None
    assert await dual_backend.fetch_memory("mem-1") is not None
    assert dual_backend.secondary_failures == 1
    assert dual_backend.secondary_failures_by_method == {"insert_memory": 1}


async def test_reads_never_go_to_the_secondary(
    dual_backend: DualWriteMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """読みは primary だけ。secondary の読みを全部爆弾にしても読める。"""
    await dual_backend.insert_memory(record(memory()))

    async def boom(*args, **kwargs):
        raise AssertionError("読みが secondary に行った")

    for name in (
        "fetch_memory",
        "fetch_memories",
        "fetch_all_memories",
        "fetch_memories_with_vectors",
        "fetch_recent_memories",
        "fetch_important_memories",
        "fetch_memory_facets",
        "fetch_vectors",
        "fetch_all_vectors",
        "fetch_coactivation",
        "fetch_coactivation_weight",
        "fetch_episode",
        "search_episodes",
        "fetch_all_episodes",
    ):
        monkeypatch.setattr(dual_backend.secondary, name, boom)

    assert (await dual_backend.fetch_memory("mem-1")) is not None
    assert len(await dual_backend.fetch_all_memories()) == 1
    assert len(await dual_backend.fetch_memories_with_vectors()) == 1
    assert len(await dual_backend.fetch_recent_memories(limit=5)) == 1
    assert (await dual_backend.fetch_memory_facets()).rows
    assert await dual_backend.fetch_vectors(["mem-1"])
    assert await dual_backend.fetch_all_vectors()
    assert await dual_backend.fetch_coactivation("mem-1") == ()
    assert await dual_backend.fetch_coactivation_weight("mem-1", "mem-2") is None
    assert await dual_backend.fetch_episode("nope") is None
    assert await dual_backend.search_episodes("金魚", 5) == []
    assert await dual_backend.fetch_all_episodes() == []
    assert dual_backend.secondary_failures == 0


async def test_dual_write_skips_replication_when_secondary_never_connected(
    memory_config: MemoryConfig, dynamo_config: MemoryConfig
) -> None:
    """secondary に繋げなくても primary だけで動き続ける（失敗は数える）。"""

    class UnreachableSecondary(DynamoMemoryStore):
        async def connect(self) -> None:
            raise RuntimeError("house table is still deploying")

    backend = DualWriteMemoryStore(
        primary=SqliteMemoryStore(memory_config),
        secondary=UnreachableSecondary(dynamo_config),
    )
    await backend.connect()
    try:
        assert backend.secondary_ready is False
        await backend.insert_memory(record(memory()))
        assert (await backend.fetch_memory("mem-1")) is not None
        # connect の 1 回 + insert_memory の 1 回
        assert backend.secondary_failures == 2
        assert backend.secondary_failures_by_method == {"connect": 1, "insert_memory": 1}
    finally:
        await backend.disconnect()
