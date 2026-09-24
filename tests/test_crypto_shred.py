"""K20: 暗号シュレッダーのテスト（moto の DynamoDB と KMS で偽装、AWS 資格情報は不要）。

見たいこと:
- house 表には本文・ベクトルの平文が 1 byte も載らない（メタデータは平文のまま）。
- 忘れると鍵の表から DEK が消える。
- **本体の表を PITR / S3 相当で戻しても、鍵が無いので読めない**（どの読みの経路でも出てこない）。
- KMS の暗号化コンテキスト（pid）が違えば DEK は解けない。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("moto")

import boto3  # noqa: E402
from cryptography.exceptions import InvalidTag  # noqa: E402
from moto import mock_aws  # noqa: E402

from memory_mcp.config import MemoryConfig  # noqa: E402
from memory_mcp.crypto_shred import (  # noqa: E402
    CryptoShredder,
    KmsDataKeyWrapper,
    ShreddedError,
    open_json,
    open_sealed,
    seal,
    seal_json,
)
from memory_mcp.dynamo_backend import DynamoMemoryStore  # noqa: E402
from memory_mcp.store_backend import MemoryRecord  # noqa: E402
from memory_mcp.types import ForgetMarker, Memory, MemoryLink, SensoryData  # noqa: E402
from memory_mcp.vector import encode_vector  # noqa: E402

TABLE_NAME = "house"
KEYS_TABLE_NAME = "petit-test-memory-keys"
REGION = "ap-northeast-1"
PID = "mio"
SECRET = "澪がはじめて覚えた、長門の夕焼けの秘密"


def memory(memory_id: str = "mem-1", content: str = SECRET, timestamp: str = "2026-09-25T12:00:00", **kw) -> Memory:
    return Memory(
        id=memory_id,
        content=content,
        timestamp=timestamp,
        emotion=kw.pop("emotion", "happy"),
        importance=kw.pop("importance", 3),
        category=kw.pop("category", "daily"),
        **kw,
    )


def record(mem: Memory, vector: list[float] | None = None) -> MemoryRecord:
    return MemoryRecord(
        memory=mem,
        normalized_content=mem.content,
        reading="ながとのゆうやけ",
        vector=encode_vector(vector or [0.25, 0.5, 0.75]),
    )


def _create_table(dynamodb, name: str) -> None:
    dynamodb.create_table(
        TableName=name,
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


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch):
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(key, value)
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        _create_table(dynamodb, TABLE_NAME)
        _create_table(dynamodb, KEYS_TABLE_NAME)
        key_id = boto3.client("kms", region_name=REGION).create_key(Description="petit-test-memory")["KeyMetadata"][
            "KeyId"
        ]
        yield {
            "house": dynamodb.Table(TABLE_NAME),
            "keys": dynamodb.Table(KEYS_TABLE_NAME),
            "key_id": key_id,
        }


@pytest.fixture
def config(aws, temp_db_path: str) -> MemoryConfig:
    return MemoryConfig(
        db_path=temp_db_path,
        collection_name="t",
        store_backend="dynamo",
        dynamo_table=TABLE_NAME,
        petit_id=PID,
        keys_table=KEYS_TABLE_NAME,
        kms_key_id=aws["key_id"],
    )


@pytest.fixture
async def store(config: MemoryConfig):
    backend = DynamoMemoryStore(config)
    await backend.connect()
    yield backend
    await backend.disconnect()


def scan_all(table) -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response["Items"])
        if "LastEvaluatedKey" not in response:
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def raw_bytes(items: list[dict]) -> bytes:
    """表の中身を全部 1 本の bytes にする（平文が混ざっていないかを探すため）。"""
    out = bytearray()
    for item in items:
        for value in item.values():
            if hasattr(value, "value"):
                out += bytes(value.value)
            else:
                out += str(value).encode("utf-8")
    return bytes(out)


# ──────────────────────────────────────────────
# 部品（他リポも使う）
# ──────────────────────────────────────────────


def test_seal_roundtrip_and_aad_binding() -> None:
    dek = os.urandom(32)
    blob = seal(dek, SECRET.encode(), "mio|m1|mem")
    assert SECRET.encode() not in blob
    assert blob[:1] == b"\x01"
    assert open_sealed(dek, blob, "mio|m1|mem") == SECRET.encode()
    # 別の項目・別のぷちへ貼り替えた暗号文は開かない
    with pytest.raises(InvalidTag):
        open_sealed(dek, blob, "mio|m2|mem")
    with pytest.raises(InvalidTag):
        open_sealed(os.urandom(32), blob, "mio|m1|mem")
    assert open_json(dek, seal_json(dek, {"a": "澪"}, "x"), "x") == {"a": "澪"}


def test_shredder_new_key_then_shred(aws) -> None:
    shredder = CryptoShredder(PID, KEYS_TABLE_NAME, KmsDataKeyWrapper(aws["key_id"]))
    dek = shredder.new_key("msg-1")
    assert len(dek) == 32

    stored = aws["keys"].get_item(Key={"pk": "P#mio", "sk": "KEY#msg-1"})["Item"]
    assert dek not in bytes(stored["wrapped"].value)  # 包んだ形でしか置かない

    # キャッシュを捨てても KMS で解ける
    shredder.clear_cache()
    assert shredder.key_for("msg-1") == dek
    assert shredder.keys_for(["msg-1", "nope"]) == {"msg-1": dek}

    shredder.shred("msg-1")
    assert "Item" not in aws["keys"].get_item(Key={"pk": "P#mio", "sk": "KEY#msg-1"})
    assert shredder.key_for("msg-1") is None
    with pytest.raises(ShreddedError):
        shredder.require_key("msg-1")


def test_wrapped_key_cannot_be_opened_under_another_pid(aws) -> None:
    """KMS の暗号化コンテキスト pid が違えば解けない（鍵ポリシーの条件と同じ軸）。"""
    mine = CryptoShredder(PID, KEYS_TABLE_NAME, KmsDataKeyWrapper(aws["key_id"]))
    mine.new_key("m1")
    wrapped = bytes(aws["keys"].get_item(Key={"pk": "P#mio", "sk": "KEY#m1"})["Item"]["wrapped"].value)
    with pytest.raises(Exception):  # noqa: B017 - moto は InvalidCiphertextException
        KmsDataKeyWrapper(aws["key_id"]).unwrap(wrapped, "someone-else")


def test_from_env_needs_both_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PETIT_MEMORY_KEYS_TABLE", raising=False)
    monkeypatch.delenv("PETIT_MEMORY_KMS_KEY_ID", raising=False)
    assert CryptoShredder.from_env(PID) is None
    monkeypatch.setenv("PETIT_MEMORY_KEYS_TABLE", KEYS_TABLE_NAME)
    with pytest.raises(ValueError):
        CryptoShredder.from_env(PID)


def test_backend_refuses_half_configuration(temp_db_path: str) -> None:
    with pytest.raises(ValueError):
        DynamoMemoryStore(
            MemoryConfig(db_path=temp_db_path, collection_name="t", petit_id=PID, keys_table=KEYS_TABLE_NAME)
        )


# ──────────────────────────────────────────────
# DynamoDB 実装
# ──────────────────────────────────────────────


async def test_house_table_holds_no_plaintext_body_or_vector(store: DynamoMemoryStore, aws) -> None:
    rec = record(
        memory(
            tags=("夕焼け",),
            sensory_data=(
                SensoryData(
                    sensory_type="visual",
                    file_path=None,
                    metadata={},
                    description="橙色の空",
                    timestamp="2026-09-25T12:00:00",
                ),
            ),
        )
    )
    await store.insert_memory(rec)
    assert store.encrypted

    items = scan_all(aws["house"])
    blob = raw_bytes(items)
    assert SECRET.encode() not in blob
    assert "ながとのゆうやけ".encode() not in blob
    assert "橙色の空".encode() not in blob
    assert rec.vector not in blob

    body = next(i for i in items if i["sk"].startswith("MEM#"))
    # メタデータは平文のまま（絞り込み・保守のため）
    assert body["id"] == "mem-1"
    assert body["emotion"] == "happy"
    assert body["category"] == "daily"
    assert body["tags"] == "夕焼け"
    assert int(body["indexed"]) == 1
    assert "content" not in body and "normalized_content" not in body
    vec = next(i for i in items if i["sk"].startswith("VEC#"))
    assert "vector" not in vec and "vector_sealed" in vec

    # 本人（ぷちコンテナの処理）からは普通に読める
    fetched = await store.fetch_memory("mem-1")
    assert fetched is not None
    assert fetched.content == SECRET
    assert fetched.sensory_data[0].description == "橙色の空"
    assert await store.fetch_vectors(["mem-1"]) == {"mem-1": rec.vector}
    rows = await store.fetch_all_vectors()
    assert rows[0].normalized_content == SECRET and rows[0].vector == rec.vector


async def test_forget_deletes_the_key_and_keeps_the_trace(store: DynamoMemoryStore, aws) -> None:
    await store.insert_memory(record(memory()))
    assert "Item" in aws["keys"].get_item(Key={"pk": "P#mio", "sk": "KEY#mem-1"})

    marker = ForgetMarker(memory_id="mem-1", forgotten_at="2026-09-26T00:00:00", reason="本人の希望")
    assert await store.delete_memory("mem-1", forget_marker=marker) is True

    assert "Item" not in aws["keys"].get_item(Key={"pk": "P#mio", "sk": "KEY#mem-1"})
    markers = await store.fetch_forget_markers(None, 10)
    assert [(m.memory_id, m.reason) for m in markers] == [("mem-1", "本人の希望")]


async def test_restoring_the_house_table_does_not_bring_the_memory_back(
    store: DynamoMemoryStore, config: MemoryConfig, aws
) -> None:
    """PITR / 毎晩の S3 バックアップ相当で本体の表を戻しても、鍵の表は戻らないので読めない。"""
    await store.insert_memory(record(memory("keep", content="残す記憶", timestamp="2026-09-25T10:00:00")))
    await store.insert_memory(record(memory("gone", timestamp="2026-09-25T11:00:00")))
    await store.insert_memory(record(memory("keep2", content="残す記憶その2", timestamp="2026-09-25T12:00:00")))

    backup = scan_all(aws["house"])  # 「忘れる」前のバックアップ（PITR の戻り先）
    assert "Item" not in aws["keys"].get_item(Key={"pk": "P#mio", "sk": "KEY#nothing"})

    await store.delete_memory("gone", forget_marker=ForgetMarker(memory_id="gone", forgotten_at="2026-09-26T00:00:00"))

    # 復元: 本体の表だけを「忘れる前」に戻す（鍵の表は PITR 無効・バックアップ対象外なので戻らない）
    with aws["house"].batch_writer() as writer:
        for item in backup:
            writer.put_item(Item=item)
    restored_skinds = {i["sk"].split("#", 1)[0] for i in scan_all(aws["house"]) if "gone" in i["sk"]}
    assert {"MEM", "VEC", "IDX"} <= restored_skinds  # 暗号文も指し札も物理的には戻っている

    # 別プロセス（キャッシュなし）で開き直しても、どの経路からも出てこない
    fresh = DynamoMemoryStore(config)
    await fresh.connect()
    try:
        for backend in (store, fresh):
            assert await backend.fetch_memory("gone") is None
            assert await backend.fetch_memories(["gone", "keep"]) != []
            assert {m.id for m in await backend.fetch_memories(["gone", "keep"])} == {"keep"}
            assert {m.id for m in await backend.fetch_all_memories()} == {"keep", "keep2"}
            assert {c.memory.id for c in await backend.fetch_memories_with_vectors()} == {"keep", "keep2"}
            assert {m.id for m in await backend.fetch_recent_memories(10)} == {"keep", "keep2"}
            assert {m.id for m in await backend.fetch_important_memories(0, 0, None, 10)} == {"keep", "keep2"}
            assert set(await backend.fetch_indexed_memory_ids()) == {"keep", "keep2"}
            assert await backend.fetch_vectors(["gone"]) == {}
            assert {r.memory_id for r in await backend.fetch_all_vectors()} == {"keep", "keep2"}
            assert len((await backend.fetch_memory_facets()).rows) == 2
            before, after = await backend.fetch_neighbors("keep")
            assert before is None and after is not None and after.id == "keep2"  # 間の "gone" を飛ばす

        # 暗号文を直接つかんでも、鍵の表に DEK が無い
        shredder = CryptoShredder(PID, KEYS_TABLE_NAME, KmsDataKeyWrapper(aws["key_id"]))
        assert shredder.key_for("gone") is None
    finally:
        await fresh.disconnect()


async def test_updates_and_link_cleanup_stay_encrypted(store: DynamoMemoryStore, aws) -> None:
    await store.insert_memory(record(memory("a", content="記憶A", timestamp="2026-09-25T10:00:00")))
    await store.insert_memory(record(memory("b", content="記憶B", timestamp="2026-09-25T11:00:00")))

    link = MemoryLink(target_id="b", link_type="related", created_at="2026-09-25T12:00:00", note="Bとの秘密のつながり")
    import json

    await store.update_memory_fields("a", {"links": json.dumps([link.to_dict()]), "importance": 5})
    await store.add_bidirectional_link("a", "b")
    assert "秘密のつながり".encode() not in raw_bytes(scan_all(aws["house"]))

    a = await store.fetch_memory("a")
    assert a is not None and a.importance == 5 and a.links[0].note == "Bとの秘密のつながり"
    assert a.content == "記憶A"

    # b を忘れると、a の links / linked_ids から b が抜け、a は暗号化されたまま読める
    await store.delete_memory("b")
    a = await store.fetch_memory("a")
    assert a is not None and a.links == () and "b" not in a.linked_ids and a.content == "記憶A"
    assert "記憶A".encode() not in raw_bytes(scan_all(aws["house"]))


async def test_plaintext_rows_still_read_when_encryption_is_on(aws, config: MemoryConfig) -> None:
    """暗号化を入れる前の行（平文）も読める。暗号化の有無は環境変数だけで切り替わる。"""
    plain_config = MemoryConfig(
        db_path=config.db_path, collection_name="t", store_backend="dynamo", dynamo_table=TABLE_NAME, petit_id=PID
    )
    plain = DynamoMemoryStore(plain_config)
    await plain.connect()
    await plain.insert_memory(record(memory("old", content="平文の古い記憶", timestamp="2026-09-01T00:00:00")))
    assert not plain.encrypted

    enc = DynamoMemoryStore(config)
    await enc.connect()
    await enc.insert_memory(record(memory("new", timestamp="2026-09-25T00:00:00")))
    assert {m.id: m.content for m in await enc.fetch_all_memories()} == {"old": "平文の古い記憶", "new": SECRET}

    # 暗号化を切った環境からは暗号化された記憶は見えない（鍵を持たない＝読めない）
    assert {m.id for m in await plain.fetch_all_memories()} == {"old"}
    await plain.disconnect()
    await enc.disconnect()


async def test_episode_title_and_summary_are_sealed_and_stay_gone_after_restore(
    store: DynamoMemoryStore, config: MemoryConfig, aws
) -> None:
    """K21: `EPI#` の題・要約も暗号化し、忘れた後に本体の表を戻しても読めない。"""
    from memory_mcp.types import Episode

    def episode(episode_id: str, title: str, start: str) -> Episode:
        return Episode(
            id=episode_id,
            title=title,
            start_time=start,
            end_time=None,
            memory_ids=("mem-1",),
            participants=("なぎ",),
            location_context="青海島の見える窓辺",
            summary=f"{title}の要約",
            emotion="happy",
            importance=4,
        )

    await store.insert_episode(episode("epi-keep", "残すエピソード", "2026-09-25T09:00:00"))
    await store.insert_episode(episode("epi-gone", SECRET, "2026-09-25T10:00:00"))

    rows = [i for i in scan_all(aws["house"]) if i["sk"].startswith("EPI#")]
    dump = raw_bytes(rows)
    for secret in (SECRET, "残すエピソード", "青海島", "なぎ"):
        assert secret.encode("utf-8") not in dump
    assert all(int(r["enc_v"]) == 1 and r["emotion"] == "happy" for r in rows)  # メタは平文
    assert (await store.fetch_episode("epi-gone")).title == SECRET

    backup = scan_all(aws["house"])
    await store.delete_episode("epi-gone")
    assert "Item" not in aws["keys"].get_item(Key={"pk": "P#mio", "sk": "KEY#epi-gone"})

    with aws["house"].batch_writer() as writer:
        for item in backup:
            writer.put_item(Item=item)
    assert any("epi-gone" in i["sk"] for i in scan_all(aws["house"]))  # 暗号文は物理的に戻っている

    fresh = DynamoMemoryStore(config)
    await fresh.connect()
    try:
        for backend in (store, fresh):
            assert await backend.fetch_episode("epi-gone") is None
            assert [e.id for e in await backend.fetch_all_episodes()] == ["epi-keep"]
            assert await backend.search_episodes(SECRET[:4], 10) == []
            assert [e.title for e in await backend.search_episodes("残す", 10)] == ["残すエピソード"]
    finally:
        await fresh.disconnect()


async def test_forgetting_a_memory_clears_its_episodes_title_and_summary(store: DynamoMemoryStore, aws) -> None:
    """K22: memory_ids に含む記憶を忘れたら EPI# の題・要約も消える（枠と memory_ids は残る）。

    要約は記憶の中身から作るので、記憶だけ忘れても要約に中身が残ってしまう問題への v0 の対処。
    """
    from memory_mcp.types import Episode

    await store.insert_memory(record(memory("mem-1", content=SECRET, timestamp="2026-09-25T09:30:00")))
    await store.insert_memory(record(memory("mem-2", content="無事な記憶", timestamp="2026-09-25T09:40:00")))
    await store.insert_episode(
        Episode(
            id="epi-1", title=f"{SECRET}の日", start_time="2026-09-25T09:00:00", end_time="2026-09-25T11:00:00",
            memory_ids=("mem-1", "mem-2"), participants=("なぎ",), location_context="青海島の見える窓辺",
            summary=f"{SECRET}をした一日", emotion="happy", importance=4,
        )
    )

    assert await store.delete_memory("mem-1")

    # 鍵は消えている（もう復号できない）
    assert "Item" not in aws["keys"].get_item(Key={"pk": "P#mio", "sk": "KEY#epi-1"})

    epi = await store.fetch_episode("epi-1")
    assert epi is not None and epi.title == "" and epi.summary == "" and epi.stale is True
    # 枠（id・時刻・memory_ids・感情・重要度）は残る。mem-2 を忘れたわけではないので消さない
    assert epi.memory_ids == ("mem-1", "mem-2")
    assert epi.start_time == "2026-09-25T09:00:00" and epi.end_time == "2026-09-25T11:00:00"
    assert epi.emotion == "happy" and epi.importance == 4

    rows = [i for i in scan_all(aws["house"]) if i["sk"] == "EPI#2026-09-25T09:00:00#epi-1"]
    assert len(rows) == 1 and "sealed" not in rows[0] and "enc_v" not in rows[0]
    assert SECRET.encode("utf-8") not in raw_bytes(rows)  # 平文で残った題・要約に中身が漏れていない

    # 別の記憶を忘れても、もう関わりの無いこのエピソードは触らない
    await store.insert_memory(record(memory("mem-3", content="他人の記憶", timestamp="2026-09-25T09:50:00")))
    await store.delete_memory("mem-3")
    epi_again = await store.fetch_episode("epi-1")
    assert epi_again.stale is True and epi_again.memory_ids == ("mem-1", "mem-2")
