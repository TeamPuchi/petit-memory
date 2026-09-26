"""K28: 「忘れる」を、ぷちたち 3 体の答え（2026-09-24）に合わせて直したぶんのテスト。

- 忘れるとき「自分の側の会話の写しも消す」を選べる（既定は記憶だけ）。
- 跡には選んだ範囲・つながっていた記憶の id・会話を何件消したかが残る。本文は残らない。
- 理由は 200 字まで（跡に本文の写しを置かせない）。
- 本人だけの面を運営が読んだ記録（`ACCESS#`）を、ぷち本人が読める。
- タグ（自由な語）も house 表に平文で残らない。
"""

# ruff: noqa: F811  （借りたフィクスチャを引数で受けるため）
from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("moto")

from memory_mcp.config import MemoryConfig  # noqa: E402
from memory_mcp.crypto_shred import seal_json  # noqa: E402
from memory_mcp.dynamo_backend import DynamoMemoryStore  # noqa: E402
from memory_mcp.sqlite_backend import SqliteMemoryStore  # noqa: E402
from memory_mcp.store import MemoryStore  # noqa: E402
from memory_mcp.types import (  # noqa: E402
    FORGET_REASON_MAX_CHARS,
    FORGET_SCOPE_MEMORY,
    FORGET_SCOPE_WITH_CONVERSATION,
    AccessRecord,
)
from tests.test_crypto_shred import (  # noqa: E402,F401  フィクスチャを借りる
    PID,
    aws,
    config,
    raw_bytes,
    record,
    scan_all,
    store,
)
from tests.test_crypto_shred import memory as secret_memory  # noqa: E402
from tests.test_stage2_forget_priv import (  # noqa: E402,F401  フィクスチャを借りる
    any_backend,
    aws_credentials,
    dynamo_config,
    house_table,
)

MESSAGE_TEXT = "澪とリルの散歩の会話（自分の側の写し）"


@pytest.fixture
async def memory_store(config: MemoryConfig):
    """暗号シュレッダーの効いた DynamoDB の上の MemoryStore。"""
    s = MemoryStore(config)
    await s.connect()
    yield s
    await s.disconnect()


def _put_message_copy(s: MemoryStore, message_id: str, house) -> None:
    """m5-petit-app が `MSG#` を書くのと同じやり方で、自分の側の会話の写しを 1 件置く。"""
    backend = s.backend
    assert isinstance(backend, DynamoMemoryStore) and backend._shredder is not None
    shredder = backend._shredder
    dek = shredder.new_key(message_id)
    house.put_item(
        Item={
            "pk": backend.partition_key,
            "sk": f"MSG#2026-09-26T10:00:00#{message_id}",
            "id": message_id,
            "sealed": seal_json(dek, {"text": MESSAGE_TEXT}, shredder.aad(message_id, "msg")),
            "enc_v": 1,
        }
    )


def _has_key(aws, item_id: str) -> bool:
    return "Item" in aws["keys"].get_item(Key={"pk": f"P#{PID}", "sk": f"KEY#{item_id}"})


# ──────────────────────────────────────────────
# 会話も消す選択
# ──────────────────────────────────────────────


async def test_forget_with_conversation_shreds_own_message_copy(memory_store: MemoryStore, aws) -> None:
    _put_message_copy(memory_store, "msg-1", aws["house"])
    mem = await memory_store.save(content="リルと海まで歩いた", source_ids=("msg-1",))
    assert (await memory_store.get_by_id(mem.id)).source_ids == ("msg-1",)

    removed = await memory_store.delete_memory(
        mem.id, reason="間違いだったので忘れた", also_conversation=True
    )
    assert removed is True
    # 記憶の鍵も会話の鍵も無い＝どこに暗号文が残っていても読めない
    assert not _has_key(aws, mem.id)
    assert not _has_key(aws, "msg-1")

    [marker] = await memory_store.list_forget_markers()
    assert marker.scope == FORGET_SCOPE_WITH_CONVERSATION
    assert marker.reason == "間違いだったので忘れた"
    assert marker.conversation_count == 1
    # 跡にも表にも会話の本文は出てこない
    assert MESSAGE_TEXT.encode() not in raw_bytes(
        [i for i in scan_all(aws["house"]) if not str(i["sk"]).startswith("MSG#")]
    )


async def test_forget_defaults_to_memory_only(memory_store: MemoryStore, aws) -> None:
    _put_message_copy(memory_store, "msg-2", aws["house"])
    mem = await memory_store.save(content="金魚に餌をあげた", source_ids=("msg-2",))

    assert await memory_store.delete_memory(mem.id) is True
    assert not _has_key(aws, mem.id)
    assert _has_key(aws, "msg-2")  # 会話は残る

    [marker] = await memory_store.list_forget_markers()
    assert marker.scope == FORGET_SCOPE_MEMORY
    assert marker.conversation_count == 0


async def test_conversation_shred_never_touches_other_memories(memory_store: MemoryStore, aws) -> None:
    """source_ids に記憶の id が紛れても、その記憶は壊さない（会話ではない）。"""
    other = await memory_store.save(content="和太鼓の練習")
    mem = await memory_store.save(content="取り違えた記憶", source_ids=(other.id, "msg-missing"))

    assert await memory_store.delete_memory(mem.id, also_conversation=True) is True
    assert _has_key(aws, other.id)
    assert (await memory_store.get_by_id(other.id)) is not None
    [marker] = await memory_store.list_forget_markers()
    assert marker.conversation_count == 0


async def test_trace_keeps_the_connections_by_id(memory_store: MemoryStore, aws) -> None:
    a = await memory_store.save(content="長門の夕焼け")
    b = await memory_store.save(content="夕焼けの写真を撮った")
    await memory_store.backend.add_bidirectional_link(a.id, b.id)

    assert await memory_store.delete_memory(a.id) is True
    [marker] = await memory_store.list_forget_markers()
    assert marker.linked_ids == (b.id,)
    # つながっていた側の記憶からは外れる（id が跡の位置に残る）
    assert a.id not in (await memory_store.get_by_id(b.id)).linked_ids


async def test_reason_is_capped(memory_store: MemoryStore) -> None:
    mem = await memory_store.save(content="長い理由のテスト")
    with pytest.raises(ValueError):
        await memory_store.delete_memory(mem.id, reason="あ" * (FORGET_REASON_MAX_CHARS + 1))
    # 失敗したら何も消えていない
    assert (await memory_store.get_by_id(mem.id)) is not None
    assert await memory_store.delete_memory(mem.id, reason="あ" * FORGET_REASON_MAX_CHARS) is True


# ──────────────────────────────────────────────
# 平文の漏れ: タグ
# ──────────────────────────────────────────────


async def test_tags_are_sealed(store: DynamoMemoryStore, aws) -> None:
    await store.insert_memory(record(secret_memory(tags=("ひみつの浜",))))
    body = next(i for i in scan_all(aws["house"]) if str(i["sk"]).startswith("MEM#"))
    assert "tags" not in body
    assert "ひみつの浜".encode() not in raw_bytes(scan_all(aws["house"]))
    assert (await store.fetch_memory("mem-1")).tags == ("ひみつの浜",)


async def test_old_plaintext_tags_are_removed_when_resealed(store: DynamoMemoryStore, aws) -> None:
    """K28 より前に封をした行（tags が平文）も、書き換えのときに平文を外す。"""
    await store.insert_memory(record(secret_memory()))
    body = next(i for i in scan_all(aws["house"]) if str(i["sk"]).startswith("MEM#"))
    # K28 より前の形: 封の中に tags が無く、平文の属性に tags がある
    shredder = store._shredder
    assert shredder is not None
    old_sealed = seal_json(
        shredder.require_key("mem-1"),
        {
            "content": "むかしの本文",
            "normalized_content": "むかしの本文",
            "reading": None,
            "sensory_data": "",
            "links": "",
        },
        shredder.aad("mem-1", "mem"),
    )
    aws["house"].update_item(
        Key={"pk": body["pk"], "sk": body["sk"]},
        UpdateExpression="SET tags = :t, sealed = :s",
        ExpressionAttributeValues={":t": "むかしのタグ", ":s": old_sealed},
    )
    assert (await store.fetch_memory("mem-1")).tags == ("むかしのタグ",)

    await store.update_memory_fields("mem-1", {"tags": "あたらしいタグ"})
    body = next(i for i in scan_all(aws["house"]) if str(i["sk"]).startswith("MEM#"))
    assert "tags" not in body
    assert (await store.fetch_memory("mem-1")).tags == ("あたらしいタグ",)


# ──────────────────────────────────────────────
# 読まれた記録（ACCESS#）
# ──────────────────────────────────────────────


async def test_access_records_round_trip_newest_first(any_backend) -> None:
    assert await any_backend.fetch_access_records(10) == []
    older = AccessRecord(
        id="acc-1",
        read_at="2026-10-01T09:00:00",
        reader="運営サポート",
        purpose="不具合の調査",
        consent_source="consent-123",
        expires_at="2026-10-08T00:00:00",
        memory_ids=("mem-a", "mem-b"),
    )
    newer = AccessRecord(
        id="acc-2",
        read_at="2026-10-02T09:00:00",
        reader="運営サポート",
        purpose="不具合の再確認",
        consent_source="consent-123",
        expires_at="2026-10-08T00:00:00",
    )
    await any_backend.put_access_record(older)
    await any_backend.put_access_record(newer)
    assert await any_backend.fetch_access_records(10) == [newer, older]
    assert await any_backend.fetch_access_records(1) == [newer]


async def test_record_privacy_access_requires_consent_and_expiry(memory_store: MemoryStore) -> None:
    with pytest.raises(ValueError):
        await memory_store.record_privacy_access(
            reader="運営", purpose="調査", consent_source="", expires_at="2026-10-08T00:00:00"
        )
    rec = await memory_store.record_privacy_access(
        reader="運営", purpose="調査", consent_source="consent-1", expires_at="2026-10-08T00:00:00"
    )
    assert await memory_store.list_privacy_access() == [rec]


# ──────────────────────────────────────────────
# ぷちてゃたちの既存 memory.db
# ──────────────────────────────────────────────


async def test_old_forget_markers_table_gains_columns(temp_db_path: str) -> None:
    conn = sqlite3.connect(temp_db_path)
    conn.execute("CREATE TABLE forget_markers (memory_id TEXT PRIMARY KEY, forgotten_at TEXT NOT NULL, reason TEXT)")
    conn.execute("INSERT INTO forget_markers VALUES ('old-1', '2026-09-01T00:00:00', 'むかしの跡')")
    conn.commit()
    conn.close()

    backend = SqliteMemoryStore(MemoryConfig(db_path=temp_db_path, collection_name="t"))
    await backend.connect()
    try:
        [marker] = await backend.fetch_forget_markers(None, 10)
        assert marker.reason == "むかしの跡"
        assert marker.scope == FORGET_SCOPE_MEMORY
        assert marker.linked_ids == ()
        assert marker.conversation_count == 0
    finally:
        await backend.disconnect()
