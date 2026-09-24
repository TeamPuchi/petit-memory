"""段2: 消した跡・本人だけの面・非索引・一覧の無作為と前後のテスト。

機能ごとに SQLite と DynamoDB（moto で偽装）の両方を見る。保管層の 3 実装
（`SqliteMemoryStore` / `DynamoMemoryStore` / `DualWriteMemoryStore`）で
同じ振る舞いになっていることを確かめる。
"""

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
from memory_mcp.types import ForgetMarker, Memory  # noqa: E402
from memory_mcp.vector import encode_vector  # noqa: E402

TABLE_NAME = "house"
REGION = "ap-northeast-1"


# ──────────────────────────────────────────────
# 素材
# ──────────────────────────────────────────────


def memory(
    memory_id: str = "mem-1",
    content: str = "長門の海で金魚の話をした",
    timestamp: str = "2026-09-01T12:00:00",
    **kwargs,
) -> Memory:
    return Memory(
        id=memory_id,
        content=content,
        timestamp=timestamp,
        emotion=kwargs.pop("emotion", "happy"),
        importance=kwargs.pop("importance", 3),
        category=kwargs.pop("category", "daily"),
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
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.fixture
async def dynamo_backend(house_table, dynamo_config: MemoryConfig):
    backend = DynamoMemoryStore(dynamo_config)
    await backend.connect()
    yield backend
    await backend.disconnect()


@pytest.fixture
async def sqlite_backend(memory_config: MemoryConfig):
    backend = SqliteMemoryStore(memory_config)
    await backend.connect()
    yield backend
    await backend.disconnect()


@pytest.fixture(params=["sqlite", "dynamo", "dynamo_encrypted"])
async def any_backend(request, house_table, memory_config: MemoryConfig, dynamo_config: MemoryConfig):
    """同じテストを SQLite・DynamoDB・DynamoDB＋暗号シュレッダー（K20）で回すためのフィクスチャ。"""
    if request.param == "sqlite":
        backend = SqliteMemoryStore(memory_config)
    elif request.param == "dynamo":
        backend = DynamoMemoryStore(dynamo_config)
    else:
        backend = DynamoMemoryStore(_encrypted_config(dynamo_config))
    await backend.connect()
    yield backend
    await backend.disconnect()


@pytest.fixture
async def dual_backend(house_table, memory_config: MemoryConfig, dynamo_config: MemoryConfig):
    backend = DualWriteMemoryStore(
        primary=SqliteMemoryStore(memory_config),
        secondary=DynamoMemoryStore(dynamo_config),
    )
    await backend.connect()
    yield backend
    await backend.disconnect()


def _encrypted_config(base: MemoryConfig) -> MemoryConfig:
    """鍵の表と KMS の鍵を moto 上に作り、暗号シュレッダーを効かせた設定を返す。"""
    from dataclasses import replace

    keys_table = "petit-test-memory-keys"
    boto3.resource("dynamodb", region_name=REGION).create_table(
        TableName=keys_table,
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
    key_id = boto3.client("kms", region_name=REGION).create_key()["KeyMetadata"]["KeyId"]
    return replace(base, keys_table=keys_table, kms_key_id=key_id)


def sk_list(table, partition_key: str, prefix: str) -> list[str]:
    """ある前置辞の sk を全部拾う（表の形そのものを見るため）。"""
    from boto3.dynamodb.conditions import Key

    response = table.query(
        KeyConditionExpression=Key("pk").eq(partition_key) & Key("sk").begins_with(prefix)
    )
    return sorted(item["sk"] for item in response["Items"])


# ──────────────────────────────────────────────
# pk の切り替え口
# ──────────────────────────────────────────────


def test_partition_key_switches_on_house_id(temp_db_path: str) -> None:
    """`PETIT_MEMORY_HOUSE_ID` が無ければ `P#<pid>`、あれば従来形。組み立ては 1 か所。"""
    base = dict(db_path=temp_db_path, collection_name="t", store_backend="dynamo", petit_id="puchi")

    without_house = DynamoMemoryStore(MemoryConfig(**base))
    assert without_house.partition_key == "P#puchi"

    with_house = DynamoMemoryStore(MemoryConfig(**base, house_id="nagato-1"))
    assert with_house.partition_key == "H#nagato-1#P#puchi"


async def test_new_shape_partition_key_round_trips(house_table, temp_db_path: str) -> None:
    """`P#<pid>` でも読み書きが通り、旧 pk とは混ざらない。"""
    new_config = MemoryConfig(
        db_path=temp_db_path,
        collection_name="t",
        store_backend="dynamo",
        dynamo_table=TABLE_NAME,
        petit_id="puchi",
    )
    backend = DynamoMemoryStore(new_config)
    await backend.connect()
    await backend.insert_memory(record(memory()))

    assert (await backend.fetch_memory("mem-1")) is not None
    assert sk_list(house_table, "P#puchi", "MEM#") == ["MEM#2026-09-01T12:00:00#mem-1"]
    # 従来形の pk 側には 1 件も無い
    assert sk_list(house_table, "H#nagato-1#P#puchi", "MEM#") == []
    await backend.disconnect()


# ──────────────────────────────────────────────
# FORGET#: 消した跡
# ──────────────────────────────────────────────


async def test_dynamo_forget_marker_keeps_the_trace_but_not_the_content(
    dynamo_backend: DynamoMemoryStore, house_table
) -> None:
    await dynamo_backend.insert_memory(record(memory(content="言いたくなかったこと")))
    marker = ForgetMarker(
        memory_id="mem-1", forgotten_at="2026-09-10T08:00:00", reason="もう抱えていたくない"
    )

    assert await dynamo_backend.delete_memory("mem-1", marker) is True

    assert await dynamo_backend.fetch_memory("mem-1") is None
    assert sk_list(house_table, dynamo_backend.partition_key, "MEM#") == []
    assert sk_list(house_table, dynamo_backend.partition_key, "FORGET#") == [
        "FORGET#2026-09-10T08:00:00#mem-1"
    ]

    stored = house_table.get_item(
        Key={"pk": dynamo_backend.partition_key, "sk": "FORGET#2026-09-10T08:00:00#mem-1"}
    )["Item"]
    # 跡から中身は読めない
    assert "content" not in stored
    assert "言いたくなかったこと" not in str(stored)

    markers = await dynamo_backend.fetch_forget_markers(None, 10)
    assert [m.memory_id for m in markers] == ["mem-1"]
    assert markers[0].reason == "もう抱えていたくない"


async def test_sqlite_forget_marker_keeps_the_trace_but_not_the_content(
    sqlite_backend: SqliteMemoryStore,
) -> None:
    await sqlite_backend.insert_memory(record(memory(content="言いたくなかったこと")))
    marker = ForgetMarker(memory_id="mem-1", forgotten_at="2026-09-10T08:00:00", reason=None)

    assert await sqlite_backend.delete_memory("mem-1", marker) is True
    assert await sqlite_backend.fetch_memory("mem-1") is None

    markers = await sqlite_backend.fetch_forget_markers(None, 10)
    assert len(markers) == 1
    assert markers[0].memory_id == "mem-1"
    assert markers[0].forgotten_at == "2026-09-10T08:00:00"
    assert markers[0].reason is None

    row = sqlite_backend.connection.execute("SELECT * FROM forget_markers").fetchone()
    assert "content" not in row.keys()

    # 跡を残さない消し方（統合など）では 1 件も増えない
    await sqlite_backend.insert_memory(record(memory("mem-2")))
    await sqlite_backend.delete_memory("mem-2")
    assert len(await sqlite_backend.fetch_forget_markers(None, 10)) == 1


async def test_forget_markers_are_newest_first_and_windowed(
    sqlite_backend: SqliteMemoryStore,
) -> None:
    for i, at in enumerate(["2026-09-01T00:00:00", "2026-09-05T00:00:00", "2026-09-09T00:00:00"]):
        mid = f"mem-{i}"
        await sqlite_backend.insert_memory(record(memory(mid)))
        await sqlite_backend.delete_memory(mid, ForgetMarker(memory_id=mid, forgotten_at=at))

    newest_first = await sqlite_backend.fetch_forget_markers(None, 10)
    assert [m.memory_id for m in newest_first] == ["mem-2", "mem-1", "mem-0"]
    assert [m.memory_id for m in await sqlite_backend.fetch_forget_markers("2026-09-05T00:00:00", 10)] == [
        "mem-2",
        "mem-1",
    ]
    assert len(await sqlite_backend.fetch_forget_markers(None, 1)) == 1


async def test_dual_write_sends_the_forget_marker_to_both(
    dual_backend: DualWriteMemoryStore, house_table
) -> None:
    await dual_backend.insert_memory(record(memory()))
    marker = ForgetMarker(memory_id="mem-1", forgotten_at="2026-09-10T08:00:00", reason="test")
    assert await dual_backend.delete_memory("mem-1", marker) is True

    secondary = dual_backend.secondary
    assert isinstance(secondary, DynamoMemoryStore)
    assert sk_list(house_table, secondary.partition_key, "FORGET#") == [
        "FORGET#2026-09-10T08:00:00#mem-1"
    ]
    assert [m.memory_id for m in await dual_backend.fetch_forget_markers(None, 10)] == ["mem-1"]
    assert dual_backend.secondary_failures == 0


# ──────────────────────────────────────────────
# index:false: 保存はするが索引に載せない
# ──────────────────────────────────────────────


async def test_index_false_is_saved_but_left_out_of_the_search_pool(any_backend) -> None:
    backend = any_backend
    await backend.insert_memory(record(memory("mem-1", "索引に載せる話")))
    await backend.insert_memory(
        record(memory("mem-2", "索引に載せない話", timestamp="2026-09-02T12:00:00", indexed=False))
    )

    # ID 指定では取れる
    hidden = await backend.fetch_memory("mem-2")
    assert hidden is not None
    assert hidden.content == "索引に載せない話"
    assert hidden.indexed is False

    # 新着一覧には出る（外すのは意味検索・recall・random だけ）
    assert {m.id for m in await backend.fetch_recent_memories(10)} == {"mem-1", "mem-2"}
    assert {m.id for m in await backend.fetch_all_memories()} == {"mem-1", "mem-2"}

    # 意味検索・Hopfield・無作為の母集団からは外れる
    assert [mv.memory.id for mv in await backend.fetch_memories_with_vectors()] == ["mem-1"]
    assert [row.memory_id for row in await backend.fetch_all_vectors()] == ["mem-1"]
    assert await backend.fetch_indexed_memory_ids() == ["mem-1"]


async def test_existing_memory_db_gains_the_two_columns_on_connect(
    memory_config: MemoryConfig,
) -> None:
    """ぷちてゃたちの既存 memory.db（`indexed` / `private` が無い）をそのまま開ける。"""
    import sqlite3

    legacy = sqlite3.connect(memory_config.db_path)
    legacy.executescript(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, normalized_content TEXT NOT NULL,
            timestamp TEXT NOT NULL, emotion TEXT NOT NULL DEFAULT 'neutral',
            importance INTEGER NOT NULL DEFAULT 3, category TEXT NOT NULL DEFAULT 'daily',
            access_count INTEGER NOT NULL DEFAULT 0, last_accessed TEXT NOT NULL DEFAULT '',
            linked_ids TEXT NOT NULL DEFAULT '', episode_id TEXT,
            sensory_data TEXT NOT NULL DEFAULT '', camera_position TEXT,
            tags TEXT NOT NULL DEFAULT '', links TEXT NOT NULL DEFAULT '',
            novelty_score REAL NOT NULL DEFAULT 0.0, prediction_error REAL NOT NULL DEFAULT 0.0,
            activation_count INTEGER NOT NULL DEFAULT 0, last_activated TEXT NOT NULL DEFAULT '',
            reading TEXT
        );
        CREATE TABLE embeddings (
            memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
            vector BLOB NOT NULL
        );
        INSERT INTO memories (id, content, normalized_content, timestamp)
        VALUES ('old-1', '前からある記憶', '前からある記憶', '2026-01-01T00:00:00');
        """
    )
    legacy.commit()
    legacy.close()

    backend = SqliteMemoryStore(memory_config)
    await backend.connect()
    try:
        fetched = await backend.fetch_memory("old-1")
        assert fetched is not None
        assert fetched.content == "前からある記憶"
        assert fetched.indexed is True
        assert fetched.private is False
        assert await backend.fetch_indexed_memory_ids() == ["old-1"]
    finally:
        await backend.disconnect()


def test_decode_memory_defaults_the_two_flags_when_the_attributes_are_missing() -> None:
    """DynamoDB 側の、段2 より前に書かれたアイテム（属性そのものが無い）。"""
    from memory_mcp.records import decode_memory, encode_memory

    attrs = encode_memory(record(memory()))
    del attrs["indexed"]
    del attrs["private"]

    decoded = decode_memory(attrs)
    assert decoded.indexed is True
    assert decoded.private is False


# ──────────────────────────────────────────────
# PRIV#: 本人だけの面
# ──────────────────────────────────────────────


async def test_dynamo_private_memory_lives_under_its_own_prefix(
    dynamo_backend: DynamoMemoryStore, house_table
) -> None:
    await dynamo_backend.insert_memory(record(memory("mem-1", "みんなの話")))
    await dynamo_backend.insert_memory(
        record(memory("mem-2", "ひとりの話", timestamp="2026-09-02T12:00:00", private=True))
    )

    pk = dynamo_backend.partition_key
    assert sk_list(house_table, pk, "MEM#") == ["MEM#2026-09-01T12:00:00#mem-1"]
    assert sk_list(house_table, pk, "PRIV#") == ["PRIV#2026-09-02T12:00:00#mem-2"]
    # 指し札は置き場所に関わらず IDX#<id>
    assert sk_list(house_table, pk, "IDX#") == ["IDX#mem-1", "IDX#mem-2"]


async def test_private_memory_is_visible_to_the_petit_itself(any_backend) -> None:
    """本人（MCP 経由）の読みでは、本人だけの面も普通の記憶と同じに見える。"""
    backend = any_backend
    await backend.insert_memory(record(memory("mem-1", "みんなの話")))
    await backend.insert_memory(
        record(memory("mem-2", "ひとりの話", timestamp="2026-09-02T12:00:00", private=True))
    )

    fetched = await backend.fetch_memory("mem-2")
    assert fetched is not None
    assert fetched.private is True

    assert {m.id for m in await backend.fetch_all_memories()} == {"mem-1", "mem-2"}
    assert [m.id for m in await backend.fetch_recent_memories(10)] == ["mem-2", "mem-1"]
    assert {mv.memory.id for mv in await backend.fetch_memories_with_vectors()} == {"mem-1", "mem-2"}
    assert {m.id for m in await backend.fetch_memories(["mem-1", "mem-2"])} == {"mem-1", "mem-2"}

    facets = await backend.fetch_memory_facets()
    assert len(facets.rows) == 2
    assert facets.oldest_timestamp == "2026-09-01T12:00:00"
    assert facets.newest_timestamp == "2026-09-02T12:00:00"


async def test_dynamo_private_memory_deletes_cleanly(
    dynamo_backend: DynamoMemoryStore, house_table
) -> None:
    await dynamo_backend.insert_memory(record(memory("mem-2", private=True)))
    assert await dynamo_backend.delete_memory("mem-2") is True

    pk = dynamo_backend.partition_key
    assert sk_list(house_table, pk, "PRIV#") == []
    assert sk_list(house_table, pk, "IDX#") == []
    assert sk_list(house_table, pk, "VEC#") == []


# ──────────────────────────────────────────────
# 一覧: 前後と無作為
# ──────────────────────────────────────────────


async def test_neighbors_follow_the_timeline(any_backend) -> None:
    backend = any_backend
    for i, ts in enumerate(
        ["2026-09-01T09:00:00", "2026-09-01T12:00:00", "2026-09-01T18:00:00"]
    ):
        await backend.insert_memory(record(memory(f"mem-{i}", f"{i} 番目", timestamp=ts)))

    previous, following = await backend.fetch_neighbors("mem-1")
    assert previous is not None and previous.id == "mem-0"
    assert following is not None and following.id == "mem-2"

    # 端は片側だけ
    assert (await backend.fetch_neighbors("mem-0"))[0] is None
    assert (await backend.fetch_neighbors("mem-2"))[1] is None
    # 知らない ID は両方 None
    assert await backend.fetch_neighbors("missing") == (None, None)


async def test_neighbors_cross_the_private_boundary(any_backend) -> None:
    """本人から見れば 1 本の時系列なので、隣が本人だけの面でも前後に出る。"""
    backend = any_backend
    await backend.insert_memory(record(memory("mem-0", timestamp="2026-09-01T09:00:00")))
    await backend.insert_memory(
        record(memory("mem-1", timestamp="2026-09-01T12:00:00", private=True))
    )
    await backend.insert_memory(record(memory("mem-2", timestamp="2026-09-01T18:00:00")))

    previous, following = await backend.fetch_neighbors("mem-2")
    assert previous is not None and previous.id == "mem-1"
    assert following is None

    previous, following = await backend.fetch_neighbors("mem-0")
    assert previous is None
    assert following is not None and following.id == "mem-1"
