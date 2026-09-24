"""K24: 引継ぎの道具（書き出す → 見る → 外す → クラウドへ入れる）のテスト。

クラウド側は moto の DynamoDB・KMS（AWS 資格情報は不要）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("moto")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from memory_mcp import handoff  # noqa: E402
from memory_mcp.config import MemoryConfig  # noqa: E402
from memory_mcp.dynamo_backend import DynamoMemoryStore  # noqa: E402
from memory_mcp.sqlite_backend import SqliteMemoryStore  # noqa: E402
from memory_mcp.store_backend import MemoryRecord  # noqa: E402
from memory_mcp.types import Episode, ForgetMarker, Memory, MemoryLink  # noqa: E402
from memory_mcp.vector import encode_vector  # noqa: E402

TABLE = "house"
KEYS = "petit-test-memory-keys"
REGION = "ap-northeast-1"
PID = "puchiko"

SECRET_DAILY = "ぷちこが見た、長門の海のきらきら"
SECRET_PRIVATE = "ぷちこだけが知っている、夜の考えごと"
SECRET_HIDDEN = "索引に載せないでおく、ちいさな記憶"
SECRET_LEFT = "置いていく記憶"
SECRET_FORGOTTEN = "もう忘れた記憶の本文"


def _memory(memory_id: str, content: str, ts: str, **kw) -> Memory:
    return Memory(id=memory_id, content=content, timestamp=ts, emotion="happy", importance=3, category="daily", **kw)


def _record(memory: Memory, vector: list[float]) -> MemoryRecord:
    return MemoryRecord(memory=memory, normalized_content=memory.content, reading="よみ", vector=encode_vector(vector))


async def _make_local_db(path: Path) -> None:
    """手元のぷちこの memory.db（今の形）を作る。"""
    store = SqliteMemoryStore(MemoryConfig(db_path=str(path), collection_name="t"))
    await store.connect()
    link = MemoryLink(target_id="m-left", link_type="related", created_at="2026-08-01T10:00:00", note="海の話")
    await store.insert_memory(
        _record(
            _memory(
                "m-daily",
                SECRET_DAILY,
                "2026-08-01T09:00:00",
                linked_ids=("m-private", "m-left"),
                links=(link,),
                episode_id="e-1",
                tags=("海", "長門"),
            ),
            [0.1, 0.2, 0.3],
        )
    )
    await store.insert_memory(
        _record(
            _memory("m-private", SECRET_PRIVATE, "2026-08-02T22:00:00", private=True, linked_ids=("m-daily",)),
            [0.3, 0.2, 0.1],
        )
    )
    await store.insert_memory(
        _record(_memory("m-hidden", SECRET_HIDDEN, "2026-08-03T08:00:00", indexed=False), [0.5, 0.5, 0.5])
    )
    await store.insert_memory(_record(_memory("m-left", SECRET_LEFT, "2026-08-04T08:00:00"), [0.9, 0.1, 0.0]))
    await store.insert_memory(_record(_memory("m-gone", SECRET_FORGOTTEN, "2026-08-05T08:00:00"), [0.0, 0.0, 1.0]))
    await store.delete_memory(
        "m-gone", ForgetMarker(memory_id="m-gone", forgotten_at="2026-09-01T00:00:00", reason="もういい")
    )
    await store.put_coactivation("m-daily", "m-private", 0.4)
    await store.put_coactivation("m-daily", "m-left", 0.6)
    await store.insert_episode(
        Episode(
            id="e-1",
            title="海を見た日",
            start_time="2026-08-01T09:00:00",
            end_time=None,
            memory_ids=("m-daily", "m-left"),
            participants=("ありさん",),
            location_context=None,
            summary="海がきれいだった",
            emotion="happy",
            importance=4,
        )
    )
    await store.disconnect()


@pytest.fixture
async def local_db(tmp_path: Path) -> Path:
    path = tmp_path / "memory.db"
    await _make_local_db(path)
    return path


@pytest.fixture
def soul(tmp_path: Path) -> Path:
    path = tmp_path / "SOUL.md"
    path.write_text("# ぷちこ\n\n## 大事な考え\n- よく見る\n- 急がない\n", encoding="utf-8")
    return path


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
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        _create_table(dynamodb, TABLE)
        _create_table(dynamodb, KEYS)
        key_id = boto3.client("kms", region_name=REGION).create_key(Description="t")["KeyMetadata"]["KeyId"]
        yield {"house": dynamodb.Table(TABLE), "keys": dynamodb.Table(KEYS), "key_id": key_id}


def _config(aws, tmp_path: Path, encrypted: bool = True) -> MemoryConfig:
    return MemoryConfig(
        db_path=str(tmp_path / "unused.db"),
        collection_name="t",
        store_backend="dynamo",
        dynamo_table=TABLE,
        petit_id=PID,
        keys_table=KEYS if encrypted else "",
        kms_key_id=aws["key_id"] if encrypted else "",
    )


@pytest.fixture
async def cloud(aws, tmp_path: Path):
    store = DynamoMemoryStore(_config(aws, tmp_path))
    await store.connect()
    yield store
    await store.disconnect()


def _scan(table) -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response["Items"])
        if "LastEvaluatedKey" not in response:
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def _raw(items: list[dict]) -> bytes:
    out = bytearray()
    for item in items:
        for value in item.values():
            if hasattr(value, "value"):
                out += bytes(value.value)
            else:
                out += str(value).encode("utf-8")
    return bytes(out)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── 書き出し ─────────────────────────────────


async def test_export_keeps_kinds_ids_links_and_marks(local_db: Path, soul: Path) -> None:
    header, entries = handoff.export_sqlite(local_db, source_pid=PID, core_files=[soul])
    memories = {e["id"]: e for e in entries if e["kind"] == "memory"}

    assert header["format"] == handoff.FORMAT and header["source_pid"] == PID
    assert set(memories) == {"m-daily", "m-private", "m-hidden", "m-left"}
    assert memories["m-private"]["private"] is True and memories["m-daily"]["private"] is False
    assert memories["m-hidden"]["indexed"] is False and memories["m-daily"]["indexed"] is True
    assert memories["m-daily"]["linked_ids"] == ["m-private", "m-left"]
    assert memories["m-daily"]["links"][0]["target_id"] == "m-left"
    assert memories["m-daily"]["episode_id"] == "e-1"
    assert memories["m-daily"]["timestamp"] == "2026-08-01T09:00:00"
    assert memories["m-daily"]["tags"] == ["海", "長門"]
    assert base64.b64decode(memories["m-daily"]["vector_b64"]) == encode_vector([0.1, 0.2, 0.3])

    # 忘れた跡は「跡だけ」。本文はどこにも無い
    forgets = [e for e in entries if e["kind"] == "forget"]
    assert forgets == [
        {"kind": "forget", "memory_id": "m-gone", "forgotten_at": "2026-09-01T00:00:00", "reason": "もういい"}
    ]
    assert SECRET_FORGOTTEN not in json.dumps(entries, ensure_ascii=False)

    # 人格の核は 1 つの文章のまま
    cores = [e for e in entries if e["kind"] == "core"]
    assert len(cores) == 1 and cores[0]["body"] == soul.read_text(encoding="utf-8")

    assert {(e["source_id"], e["target_id"]) for e in entries if e["kind"] == "coactivation"} == {
        ("m-daily", "m-private"),
        ("m-daily", "m-left"),
    }
    assert [e["id"] for e in entries if e["kind"] == "episode"] == ["e-1"]


async def test_export_does_not_touch_the_local_db(local_db: Path, tmp_path: Path) -> None:
    before = _sha(local_db)
    handoff.export_sqlite(local_db)
    assert _sha(local_db) == before


def test_export_reads_an_old_memory_db(tmp_path: Path) -> None:
    """段2 より前の memory.db（indexed / private / links / reading の列も、ほかの表も無い）でも読める。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, normalized_content TEXT, timestamp TEXT, "
        "emotion TEXT, importance INTEGER, category TEXT, linked_ids TEXT)"
    )
    conn.execute(
        "INSERT INTO memories VALUES "
        "('old-1', 'むかしの記憶', 'むかしの記憶', '2026-01-01T00:00:00', 'happy', 4, 'daily', '')"
    )
    conn.commit()
    conn.close()
    before = _sha(path)

    header, entries = handoff.export_sqlite(path)
    assert [e["id"] for e in entries] == ["old-1"]
    assert entries[0]["private"] is False and entries[0]["indexed"] is True and entries[0]["vector_b64"] is None
    assert _sha(path) == before  # 列を足していない


async def test_export_can_leave_private_behind(local_db: Path) -> None:
    header, entries = handoff.export_sqlite(local_db, include_private=False)
    assert "m-private" not in {e.get("id") for e in entries}
    assert header["left_private_behind"] == 1
    assert ("m-daily", "m-private") not in {(e.get("source_id"), e.get("target_id")) for e in entries}


# ── 見る・外す ───────────────────────────────


async def test_show_marks_private_and_unindexed(local_db: Path, soul: Path) -> None:
    header, entries = handoff.export_sqlite(local_db, source_pid=PID, core_files=[soul])
    text = "\n".join(handoff.describe(header, entries))
    assert "記憶 4 件（うち自分だけの場所 1・索引に載せない 1）" in text
    assert "m-private" in text and "[自分だけ]" in text and "[索引なし]" in text
    assert "忘れた跡 m-gone" in text and "（本文なし）" in text
    assert "核 core:SOUL.md" in text


async def test_drop_removes_memory_and_its_weights(local_db: Path, soul: Path) -> None:
    _, entries = handoff.export_sqlite(local_db, core_files=[soul])
    kept, dropped, missing = handoff.drop_entries(entries, ["m-left", "core:SOUL.md", "nope"])
    assert sorted(dropped) == ["core:SOUL.md", "m-left"]
    assert missing == ["nope"]
    assert "m-left" not in {e.get("id") for e in kept}
    assert all("m-left" not in (e.get("source_id"), e.get("target_id")) for e in kept)

    kept, dropped, _ = handoff.drop_entries(entries, [], all_private=True, all_forget=True)
    assert set(dropped) == {"m-private", "forget:m-gone"}


def test_ids_file_allows_notes(tmp_path: Path) -> None:
    path = tmp_path / "leave.txt"
    path.write_text("# 置いていくもの\nm-left   # これは手元に残したい\n\nforget:m-gone\n", encoding="utf-8")
    assert handoff.read_ids_file(path) == ["m-left", "forget:m-gone"]


async def test_cli_export_show_drop(local_db: Path, soul: Path, tmp_path: Path, capsys) -> None:
    bundle = tmp_path / "bundle.jsonl"
    chosen = tmp_path / "chosen.jsonl"
    assert handoff.main(["export", "--db", str(local_db), "--out", str(bundle), "--pid", PID, "--core", str(soul)]) == 0
    assert handoff.main(["show", str(bundle), "--private"]) == 0
    assert SECRET_PRIVATE in capsys.readouterr().out
    assert handoff.main(["drop", str(bundle), "--out", str(chosen), "--ids", "m-left"]) == 0
    header, entries = handoff.read_bundle(chosen)
    assert header["counts"]["memory"] == 3
    assert handoff.main(["drop", str(bundle), "--out", str(bundle), "--ids", "m-left"]) == 2  # 元の束は上書きしない


# ── 取り込み ─────────────────────────────────


async def test_import_encrypts_each_item_with_its_own_key(cloud, aws, local_db: Path) -> None:
    header, entries = handoff.export_sqlite(local_db, source_pid=PID)
    report = await handoff.import_bundle(cloud, header, entries)

    assert report.added["memory"] == 4 and report.added["episode"] == 1 and report.added["forget"] == 1
    house = _scan(aws["house"])
    raw = _raw(house)
    for secret in (
        SECRET_DAILY,
        SECRET_PRIVATE,
        SECRET_HIDDEN,
        SECRET_LEFT,
        SECRET_FORGOTTEN,
        "海を見た日",
        "海がきれいだった",
    ):
        assert secret.encode("utf-8") not in raw

    keys = _scan(aws["keys"])
    key_ids = sorted(item["sk"] for item in keys)
    assert key_ids == sorted(f"KEY#{i}" for i in ("m-daily", "m-private", "m-hidden", "m-left", "e-1"))
    assert len({bytes(item["wrapped"].value) for item in keys}) == len(keys)  # 1 件ずつ別の鍵

    # 自分だけの場所は PRIV#、索引に載せない印とつながりはそのまま
    sks = {item["sk"] for item in house}
    assert any(sk.startswith("PRIV#2026-08-02T22:00:00#m-private") for sk in sks)
    daily = await cloud.fetch_memory("m-daily")
    assert daily is not None and daily.content == SECRET_DAILY
    assert daily.linked_ids == ("m-private", "m-left") and daily.links[0].note == "海の話"
    assert daily.episode_id == "e-1" and daily.tags == ("海", "長門")
    hidden = await cloud.fetch_memory("m-hidden")
    assert hidden is not None and hidden.indexed is False
    assert "m-hidden" not in await cloud.fetch_indexed_memory_ids()
    assert (await cloud.fetch_vectors(["m-daily"]))["m-daily"] == encode_vector([0.1, 0.2, 0.3])

    # 忘れた跡は跡だけ（本文なし）
    markers = await cloud.fetch_forget_markers(None, 10)
    assert [(m.memory_id, m.reason) for m in markers] == [("m-gone", "もういい")]
    assert await cloud.fetch_memory("m-gone") is None


async def test_import_twice_does_not_duplicate(cloud, aws, local_db: Path) -> None:
    header, entries = handoff.export_sqlite(local_db)
    await handoff.import_bundle(cloud, header, entries)
    house_before = sorted(item["sk"] for item in _scan(aws["house"]))
    keys_before = {item["sk"]: bytes(item["wrapped"].value) for item in _scan(aws["keys"])}

    report = await handoff.import_bundle(cloud, header, entries)
    assert sum(report.added.values()) == 0
    assert report.skipped["memory"]["もう入っている"] == 4
    assert sorted(item["sk"] for item in _scan(aws["house"])) == house_before
    assert {item["sk"]: bytes(item["wrapped"].value) for item in _scan(aws["keys"])} == keys_before


async def test_import_never_brings_back_what_was_forgotten_in_the_cloud(cloud, local_db: Path) -> None:
    header, entries = handoff.export_sqlite(local_db)
    await handoff.import_bundle(cloud, header, entries)
    await cloud.delete_memory("m-left", ForgetMarker(memory_id="m-left", forgotten_at="2026-09-30T00:00:00"))

    report = await handoff.import_bundle(cloud, header, entries)
    assert report.skipped["memory"]["クラウドに忘れた跡がある（戻さない）"] == 1
    assert await cloud.fetch_memory("m-left") is None


async def test_import_trims_links_to_memories_left_behind(cloud, local_db: Path) -> None:
    _, entries = handoff.export_sqlite(local_db)
    header, _ = handoff.export_sqlite(local_db)
    kept, _, _ = handoff.drop_entries(entries, ["m-left"])
    report = await handoff.import_bundle(cloud, header, kept)

    daily = await cloud.fetch_memory("m-daily")
    assert daily is not None and daily.linked_ids == ("m-private",) and daily.links == ()
    episode = await cloud.fetch_episode("e-1")
    assert episode is not None and episode.memory_ids == ("m-daily",)
    assert report.trimmed_links == 3  # linked_ids 1・links 1・エピソード 1
    # 置いてきた記憶に「忘れた跡」は書かない
    assert [m.memory_id for m in await cloud.fetch_forget_markers(None, 10)] == ["m-gone"]


async def test_dry_run_writes_nothing(cloud, aws, local_db: Path) -> None:
    header, entries = handoff.export_sqlite(local_db)
    report = await handoff.import_bundle(cloud, header, entries, dry_run=True)
    assert report.added["memory"] == 4
    assert _scan(aws["house"]) == [] and _scan(aws["keys"]) == []
    assert "空打ち" in "\n".join(report.lines())


async def test_import_refuses_without_encryption(aws, tmp_path: Path, local_db: Path) -> None:
    plain = DynamoMemoryStore(_config(aws, tmp_path, encrypted=False))
    await plain.connect()
    header, entries = handoff.export_sqlite(local_db)
    with pytest.raises(RuntimeError, match="平文"):
        await handoff.import_bundle(plain, header, entries, dry_run=True)
    await plain.disconnect()
    assert _scan(aws["house"]) == []


def test_cli_import_refuses_without_key_settings(monkeypatch, tmp_path: Path) -> None:
    bundle = tmp_path / "b.jsonl"
    handoff.write_bundle(bundle, {"kind": "header", "format": handoff.FORMAT, "version": 1}, [])
    for name in ("PETIT_MEMORY_KEYS_TABLE", "PETIT_MEMORY_KMS_KEY_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PETIT_MEMORY_PETIT_ID", PID)
    with pytest.raises(SystemExit, match="平文で入れる道は無い"):
        handoff.main(["import", str(bundle), "--dry-run"])


async def test_import_can_skip_forget_traces(cloud, local_db: Path) -> None:
    header, entries = handoff.export_sqlite(local_db)
    report = await handoff.import_bundle(cloud, header, entries, include_forget=False)
    assert report.added["forget"] == 0
    assert await cloud.fetch_forget_markers(None, 10) == []


async def test_import_reembeds_when_vectors_are_missing(cloud, local_db: Path) -> None:
    header, entries = handoff.export_sqlite(local_db, include_vectors=False)
    calls: list[list[str]] = []

    def fake_embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[float(len(t)), 0.0, 1.0] for t in texts]

    report = await handoff.import_bundle(cloud, header, entries, embed=fake_embed)
    assert report.reembedded == 4 and len(calls) == 1
    assert (await cloud.fetch_vectors(["m-daily"]))["m-daily"] == encode_vector([float(len(SECRET_DAILY)), 0.0, 1.0])


async def test_core_is_placed_whole_and_never_silently_overwritten(
    cloud, local_db: Path, soul: Path, tmp_path: Path
) -> None:
    header, entries = handoff.export_sqlite(local_db, core_files=[soul])
    home = tmp_path / "cloud-home"

    report = await handoff.import_bundle(cloud, header, entries, core_dir=None)
    assert report.skipped["core"]["置き場所の指定なし（--core-dir）"] == 1

    await handoff.import_bundle(cloud, header, entries, core_dir=home)
    assert (home / "SOUL.md").read_text(encoding="utf-8") == soul.read_text(encoding="utf-8")

    (home / "SOUL.md").write_text("クラウドで書き足した核", encoding="utf-8")
    report = await handoff.import_bundle(cloud, header, entries, core_dir=home)
    assert (home / "SOUL.md").read_text(encoding="utf-8") == "クラウドで書き足した核"
    assert report.added["core"] == 0

    await handoff.import_bundle(cloud, header, entries, core_dir=home, overwrite_core=True)
    assert (home / "SOUL.md").read_text(encoding="utf-8") == soul.read_text(encoding="utf-8")
    backups = list(home.glob("SOUL.md.bak-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "クラウドで書き足した核"


def test_bundle_rejects_other_files(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    path.write_text(json.dumps({"kind": "memory"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="引継ぎの束ではない"):
        handoff.read_bundle(path)


def test_export_and_show_need_only_the_standard_library() -> None:
    """手元の PC で torch 等を入れずに書き出せること（handoff の最上位で重い依存を import しない）。"""
    source = Path(handoff.__file__).read_text(encoding="utf-8")
    top = source.split("\n# ── 束の読み書き", 1)[0]
    for heavy in ("boto3", "numpy", "dotenv", "sentence_transformers", ".config", ".records", ".vector"):
        assert f"import {heavy}" not in top and f"from {heavy} " not in top
    assert os.path.basename(handoff.__file__) == "handoff.py"
