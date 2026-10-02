"""記憶の言葉の書き換え（`memory_mcp.rename_words`）。moto の DynamoDB と KMS で偽装する。

見たいこと:
- 既定は数えるだけで、表は 1 byte も変わらない
- `--apply` で本文・タグ・リンクの添え書きが置き換わり、正規化した本文・読み・ベクトルが作り直される
- 鍵の表は変わらない（同じ鍵・同じ AAD で閉じ直す）。id・日時・想起の回数などは変わらない
- 本人だけの面（`PRIV#`）も同じ。忘れた記憶（鍵が無い）は飛ばす
- 読んだあとに変わった記憶には書かない（次の実行で当たる）。何度走らせても同じ
"""

from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("moto")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from memory_mcp import rename_words as rw  # noqa: E402
from memory_mcp.config import MemoryConfig  # noqa: E402
from memory_mcp.dynamo_backend import DynamoMemoryStore  # noqa: E402
from memory_mcp.normalizer import get_reading, normalize_japanese  # noqa: E402
from memory_mcp.store_backend import MemoryRecord  # noqa: E402
from memory_mcp.types import Memory, MemoryLink  # noqa: E402
from memory_mcp.vector import decode_vector, encode_vector  # noqa: E402

TABLE_NAME = "house"
KEYS_TABLE_NAME = "petit-test-memory-keys"
REGION = "ap-northeast-1"
PID = "mio"
A, B, TO = "山田さん", "はなさん", "はなちゃん"


def fake_embed(texts: list[str]) -> list[list[float]]:
    """本文ごとに違うベクトル（長さと、置き換えた先の言葉の数）。"""
    return [[float(len(t)), float(t.count(TO)), 0.5] for t in texts]


def _create_table(dynamodb, name: str) -> None:
    dynamodb.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
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
        key_id = boto3.client("kms", region_name=REGION).create_key()["KeyMetadata"]["KeyId"]
        yield {"house": dynamodb.Table(TABLE_NAME), "keys": dynamodb.Table(KEYS_TABLE_NAME), "key_id": key_id}


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
def plain_config(aws, temp_db_path: str) -> MemoryConfig:
    return MemoryConfig(
        db_path=temp_db_path, collection_name="t", store_backend="dynamo", dynamo_table=TABLE_NAME, petit_id=PID
    )


@pytest.fixture
async def store(config: MemoryConfig):
    backend = DynamoMemoryStore(config)
    await backend.connect()
    yield backend
    await backend.disconnect()


def scan(table) -> list[dict]:
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp["Items"])
        if "LastEvaluatedKey" not in resp:
            return sorted(items, key=lambda it: (it["pk"], it["sk"]))
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


async def remember(backend: DynamoMemoryStore, memory_id: str, content: str, **kw) -> None:
    memory = Memory(
        id=memory_id,
        content=content,
        timestamp=kw.pop("timestamp", "2026-09-25T12:00:00"),
        emotion="happy",
        importance=3,
        category="daily",
        **kw,
    )
    await backend.insert_memory(
        MemoryRecord(
            memory=memory,
            normalized_content=normalize_japanese(content),
            reading=get_reading(content) or "",
            vector=encode_vector([9.0, 9.0, 9.0]),
        )
    )


async def fill(backend: DynamoMemoryStore) -> None:
    await remember(
        backend,
        "m-1",
        f"{A}は里親さんのともだち。{A}はやさしい",
        tags=("ともだち", B),
        links=(
            MemoryLink(target_id="m-2", link_type="related", created_at="2026-09-25T12:00:00", note=f"{B}のこと"),
        ),
        access_count=4,
    )
    await remember(backend, "m-2", f"山田{B}という名前らしい", timestamp="2026-09-25T13:00:00")
    await remember(backend, "m-3", "はなびを見た", timestamp="2026-09-25T14:00:00")
    await remember(backend, "m-4", f"{B}のひみつ", timestamp="2026-09-25T15:00:00", private=True)
    await remember(backend, "m-5", f"{A}のこと（忘れた）", timestamp="2026-09-25T16:00:00")
    backend._shredder.shred("m-5")


async def test_default_only_counts(aws, config, store):
    await fill(store)
    before, keys_before = scan(aws["house"]), scan(aws["keys"])

    result = await rw.rename_words(config, [A, B], TO, embed=fake_embed)

    assert scan(aws["house"]) == before and scan(aws["keys"]) == keys_before
    assert result == {
        # m-1: 本文 2・タグ 1・リンクの添え書き（\uXXXX 形）1。m-2 の「山田はなさん」は はなさん に当たる
        "MEM": {"seen": 4, "hit": 2, "hits": {A: 2, B: 3}, "changed": 0, "raced": 0, "unsettled": 0, "no_key": 1,
                "broken": 0},
        "PRIV": {"seen": 1, "hit": 1, "hits": {B: 1}, "changed": 0, "raced": 0, "unsettled": 0, "no_key": 0,
                 "broken": 0},
    }


async def test_apply_rewrites_text_and_rebuilds_derived_fields(aws, config, store):
    await fill(store)
    before = {r["sk"]: r for r in scan(aws["house"])}
    keys_before = scan(aws["keys"])

    result = await rw.rename_words(config, [A, B], TO, apply=True, embed=fake_embed)
    assert result["MEM"]["changed"] == 2 and result["PRIV"]["changed"] == 1 and result["MEM"]["no_key"] == 1

    m1 = await store.fetch_memory("m-1")
    assert m1.content == f"{TO}は里親さんのともだち。{TO}はやさしい"
    assert m1.tags == ("ともだち", TO)
    assert m1.links[0].note == f"{TO}のこと" and m1.links[0].target_id == "m-2"
    assert (m1.timestamp, m1.importance, m1.emotion, m1.access_count) == ("2026-09-25T12:00:00", 3, "happy", 4)
    assert (await store.fetch_memory("m-2")).content == f"山田{TO}という名前らしい"
    assert (await store.fetch_memory("m-3")).content == "はなびを見た"
    m4 = await store.fetch_memory("m-4")
    assert m4.content == f"{TO}のひみつ" and m4.private

    # 本文から作るものも作り直す（正規化した本文・読み・ベクトル）
    rows = {str(r["id"]): r for r in store._open_memory_items_sync(store._query_memories_sync())}
    assert rows["m-1"]["normalized_content"] == normalize_japanese(m1.content)
    assert rows["m-1"]["reading"] == (get_reading(m1.content) or "")
    vectors = await store.fetch_vectors(["m-1", "m-3", "m-4"])
    assert list(decode_vector(vectors["m-1"])) == fake_embed([normalize_japanese(m1.content)])[0]
    assert list(decode_vector(vectors["m-4"])) == fake_embed([normalize_japanese(m4.content)])[0]
    assert list(decode_vector(vectors["m-3"])) == [9.0, 9.0, 9.0]  # 言葉の無い記憶はそのまま

    # 鍵の表はそのまま。変わった行は当たりのあった記憶の本体（sealed だけ）とベクトルだけ
    assert scan(aws["keys"]) == keys_before
    after = {r["sk"]: r for r in scan(aws["house"])}
    assert after.keys() == before.keys()
    changed = {sk for sk in after if after[sk] != before[sk]}
    assert changed == {
        "MEM#2026-09-25T12:00:00#m-1", "VEC#m-1", "MEM#2026-09-25T13:00:00#m-2", "VEC#m-2",
        "PRIV#2026-09-25T15:00:00#m-4", "VEC#m-4",
    }
    for sk in changed:
        differs = {a for a in after[sk] if after[sk][a] != before[sk][a]}
        assert differs == ({"vector_sealed"} if sk.startswith("VEC#") else {"sealed"})

    # もう一度走らせても同じ
    snapshot = scan(aws["house"])
    again = await rw.rename_words(config, [A, B], TO, apply=True, embed=fake_embed)
    assert again["MEM"]["hit"] == 0 and again["PRIV"]["hit"] == 0 and scan(aws["house"]) == snapshot


async def test_longest_word_wins(aws, config, store):
    await remember(store, "m-1", f"山田{B}と{B}")
    await rw.rename_words(config, [B, f"山田{B}"], TO, apply=True, embed=fake_embed)
    assert (await store.fetch_memory("m-1")).content == f"{TO}と{TO}"


async def test_a_memory_changed_after_reading_is_not_overwritten(aws, config, store, monkeypatch):
    await remember(store, "m-1", f"{A}のこと")
    real = rw._write_sync

    def racing(backend, item, row, updates, vector):
        # 読んだあとに、記憶 MCP が本文を書き直した
        backend._write_secret_fields_sync(backend._get_item_sync(str(item["sk"])), {"content": f"{A}のこと。続き"})
        return real(backend, item, row, updates, vector)

    monkeypatch.setattr(rw, "_write_sync", racing)
    result = await rw.rename_words(config, [A], TO, apply=True, embed=fake_embed)
    assert result["MEM"]["raced"] == 1 and result["MEM"]["changed"] == 0
    assert (await store.fetch_memory("m-1")).content == f"{A}のこと。続き"
    assert list(decode_vector((await store.fetch_vectors(["m-1"]))["m-1"])) == [9.0, 9.0, 9.0]  # ベクトルも書かない

    monkeypatch.setattr(rw, "_write_sync", real)
    result = await rw.rename_words(config, [A], TO, apply=True, embed=fake_embed)
    assert result["MEM"]["changed"] == 1
    assert (await store.fetch_memory("m-1")).content == f"{TO}のこと。続き"


async def test_plain_house_is_rewritten_too(aws, plain_config):
    backend = DynamoMemoryStore(plain_config)
    await backend.connect()
    try:
        await remember(backend, "m-1", f"{A}のこと", tags=(B,))
        result = await rw.rename_words(plain_config, [A, B], TO, apply=True, embed=fake_embed)
        assert result["MEM"]["changed"] == 1
        memory = await backend.fetch_memory("m-1")
        assert memory.content == f"{TO}のこと" and memory.tags == (TO,)
        row = aws["house"].get_item(Key={"pk": f"P#{PID}", "sk": "MEM#2026-09-25T12:00:00#m-1"})["Item"]
        assert row["normalized_content"] == normalize_japanese(f"{TO}のこと")
    finally:
        await backend.disconnect()


async def test_words_are_checked(config):
    with pytest.raises(ValueError):
        await rw.rename_words(config, [A], None, apply=True)  # --to が無い
    with pytest.raises(ValueError):
        await rw.rename_words(config, ["はな"], "はなちゃん")  # 置き換えた先にまた当たる
    with pytest.raises(ValueError):
        await rw.rename_words(config, [], TO)


def test_cli_counts_without_showing_text(aws, config, monkeypatch, capsys):
    for name, value in {
        "PETIT_MEMORY_STORE": "dynamo",
        "PETIT_MEMORY_DYNAMO_TABLE": TABLE_NAME,
        "PETIT_MEMORY_PETIT_ID": PID,
        "PETIT_MEMORY_KEYS_TABLE": KEYS_TABLE_NAME,
        "PETIT_MEMORY_KMS_KEY_ID": aws["key_id"],
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PETIT_MEMORY_HOUSE_ID", raising=False)

    import asyncio

    async def _fill():
        backend = DynamoMemoryStore(config)
        await backend.connect()
        await remember(backend, "m-1", f"{A}は里親さんのともだち")
        await backend.disconnect()

    asyncio.run(_fill())

    assert rw.main(["someone-else", "--from", A, "--to", TO]) == 2  # よそのぷちの pid では動かない
    capsys.readouterr()
    assert rw.main([PID, "--from", "\\u5c71\\u7530\\u3055\\u3093", "--to", TO, "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["memory"]["MEM"]["hits"] == {A: 1}
    assert rw.main([PID, "--from", A, "--to", TO]) == 0
    text = capsys.readouterr().out
    assert "数えただけ" in text and "MEM: 見た 1・当たり 1" in text
    assert "里親さん" not in out and "里親さん" not in text


async def test_derived_fields_match_what_remember_would_store(aws, config, store):
    """書き換えたあとの正規化した本文・読み・ベクトルが、同じ本文を remember したときと同じになる。"""
    from memory_mcp.store import MemoryStore

    await remember(store, "m-1", f"{A}とＡＢＣのサーバ-を見た")
    await rw.rename_words(config, [A], TO, apply=True, embed=fake_embed)

    memory_store = MemoryStore(config)
    memory_store._embedding_fn = fake_embed  # 記憶 MCP が保存のときに呼ぶ埋め込み
    await memory_store.connect()
    try:
        fresh = await memory_store.save(content=f"{TO}とＡＢＣのサーバ-を見た")
        rows = {str(r["id"]): r for r in store._open_memory_items_sync(store._query_memories_sync())}
        for name in ("content", "normalized_content", "reading"):
            assert rows["m-1"][name] == rows[fresh.id][name]
        vectors = await store.fetch_vectors(["m-1", fresh.id])
        assert vectors["m-1"] == vectors[fresh.id]
    finally:
        await memory_store.disconnect()


async def test_a_replacement_that_would_match_again_is_not_written(aws, config, store):
    await remember(store, "m-1", "ややまのこと")  # 「やま」→「ま」で、前の「や」とつながってまた「やま」になる
    before = scan(aws["house"])
    result = await rw.rename_words(config, ["やま"], "ま", apply=True, embed=fake_embed)
    assert result["MEM"]["unsettled"] == 1 and result["MEM"]["changed"] == 0
    assert scan(aws["house"]) == before


async def test_to_must_not_break_json_bodies(config):
    for bad in ('は"な', "は\\な", "は\nな", ""):
        with pytest.raises(ValueError):
            await rw.rename_words(config, [A], bad)
