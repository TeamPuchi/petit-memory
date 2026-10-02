"""記憶の本文にある言葉を数える・書き換える（運営の道具。ぷちコンテナの中で動かす）。

覚えさせるつもりのなかった呼び名（実在の人の名前など）を、はじめから別の呼び名だったことにする。
「忘れる」（鍵を消す）とは違い、記憶は残して本文の中の言葉だけを置き換える。
家 API（petit-api）の `scripts/rename_words.py` の記憶の分。会話・日記・エピソードなどはそちらが書き換える。

- **既定は数えるだけ**（書かない）。出すのは件数だけで、本文は出さない
- `--apply` で、記憶（`MEM#`・`PRIV#`）の本文・タグ・リンクの添え書き・感覚データの言葉を置き換え、
  本文から作るもの（正規化した本文・読み・埋め込みベクトル `VEC#`）を作り直す
- **同じ鍵・同じ AAD で閉じ直す**。鍵の表には触れない。id・日時・感情・重要度・想起の回数などは変えない
- 本体とベクトルは 1 つのトランザクションで書く。本体は条件付き（読んだときの中身のままなら書く）なので、
  途中で落ちても、もう一度走らせれば残りだけが当たる
- 指し札（`IDX#`）・共活性（`COACT#`）は id しか持たないので触らない。BM25 の索引はメモリの中だけで、
  記憶 MCP が起動するたびに作り直される

DynamoDB の家（`PETIT_MEMORY_STORE=dynamo`）だけ。設定は記憶 MCP と同じ環境変数から読む。

`--from` はいくつも書ける。長い言葉から先に当てる。SSM 越しで日本語が化けるときは
`\\u5c71\\u7530\\u3055\\u3093`（山田さん）の形でも渡せる。

Usage:
    python -m memory_mcp.rename_words <pid> --from 山田さん --from はなさん --to はなちゃん          # 数えるだけ
    python -m memory_mcp.rename_words <pid> --from 山田さん --from はなさん --to はなちゃん --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from typing import Any

from .config import MemoryConfig
from .crypto_shred import seal_json
from .dynamo_backend import (
    SEALED_ATTRIBUTE,
    SECRET_MEMORY_ATTRIBUTES,
    DynamoMemoryStore,
    _as_bytes,
    _is_sealed,
    _to_attribute,
)
from .normalizer import get_reading, normalize_japanese
from .vector import encode_vector

# 言葉を探す属性。`normalized_content`・`reading` は本文から作り直すので探さない
TEXT_ATTRIBUTES: tuple[str, ...] = ("content", "tags", "links", "sensory_data")

Embed = Callable[[list[str]], list[list[float]]]


def _unescape_arg(value: str) -> str:
    """`\\u5c71` の形で渡された引数を文字に戻す。"""
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), value)


def _variants(word: str) -> list[str]:
    """その言葉の書かれ方: そのまま・JSON の `\\uXXXX` 形（小文字／大文字）。

    `links`・`sensory_data` は `json.dumps` の既定（ASCII だけ）で書かれ、日本語が `\\uXXXX` になっている。"""
    out = [word]
    if not word.isascii():
        esc = json.dumps(word)[1:-1]
        out.append(esc)
        upper = re.sub(r"\\u([0-9a-f]{4})", lambda m: "\\u" + m.group(1).upper(), esc)
        if upper != esc:
            out.append(upper)
    return out


class Words:
    """置き換える言葉（`froms` → `to`）。長い言葉から先に当てる。"""

    def __init__(self, froms: list[str], to: str | None):
        froms = list(dict.fromkeys(w for w in froms if w))
        if not froms:
            raise ValueError("--from を1つは渡す")
        for word in froms:
            if to is not None and word in to:
                raise ValueError(f"--to に --from の言葉（{word}）が入っている（走らせるたびに置き換わってしまう）")
        self.froms = sorted(froms, key=len, reverse=True)
        self.to = to
        self._canon: dict[str, str] = {}
        self._swap: dict[str, str] = {}
        to_forms = _variants(to) if to is not None else []
        for word in self.froms:
            for i, form in enumerate(_variants(word)):
                self._canon[form] = word
                if to_forms:
                    self._swap[form] = to_forms[min(i, len(to_forms) - 1)]
        forms = sorted(self._canon, key=len, reverse=True)
        self._re = re.compile("|".join(re.escape(f) for f in forms))

    def scan(self, text: str, hits: Counter) -> str:
        """当たりを `hits` に足し、置き換えた文字列を返す（`to` が無ければそのまま）。"""
        found = [self._canon[m.group()] for m in self._re.finditer(text)]
        hits.update(found)
        if not found or not self._swap:
            return text
        return self._re.sub(lambda m: self._swap[m.group()], text)


def _new_tally() -> dict[str, Any]:
    return {"seen": 0, "hit": 0, "hits": Counter(), "changed": 0, "raced": 0, "no_key": 0, "broken": 0}


def _write_sync(
    backend: DynamoMemoryStore,
    item: dict[str, Any],
    row: dict[str, Any],
    updates: dict[str, Any],
    vector: bytes,
) -> bool:
    """記憶 1 件の本文類とベクトルを 1 トランザクションで書く。読んだあとに変わっていたら書かずに False。"""
    from boto3.dynamodb.types import TypeSerializer
    from botocore.exceptions import ClientError

    serializer = TypeSerializer()
    memory_id = str(item["id"])
    dek: bytes | None = None
    remove: list[str] = []
    if _is_sealed(item):
        assert backend._shredder is not None
        dek = backend._shredder.require_key(memory_id)
        secret = {name: row.get(name) for name in SECRET_MEMORY_ATTRIBUTES}
        secret.update(updates)
        sets: dict[str, Any] = {SEALED_ATTRIBUTE: seal_json(dek, secret, backend._shredder.aad(memory_id, "mem"))}
        guard = (SEALED_ATTRIBUTE, _as_bytes(item[SEALED_ATTRIBUTE]))
        # 封をする前の行に平文で残っていた本文類（K28 で tags を足す前の行など）は外す
        remove = [name for name in SECRET_MEMORY_ATTRIBUTES if name in item]
    else:
        sets = {name: _to_attribute(value) for name, value in updates.items()}
        guard = ("content", item["content"])

    names = {f"#s{i}": name for i, name in enumerate(sets)}
    values = {f":s{i}": value for i, value in enumerate(sets.values())}
    expression = "SET " + ", ".join(f"{n} = {v}" for n, v in zip(names, values))
    if remove:
        removed = {f"#r{i}": name for i, name in enumerate(remove)}
        names.update(removed)
        expression += " REMOVE " + ", ".join(removed)
    names["#g"] = guard[0]
    values[":g"] = guard[1]

    def serialize(data: dict[str, Any]) -> dict[str, Any]:
        return {k: serializer.serialize(v) for k, v in data.items()}

    try:
        backend._ensure_client().transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": backend._table_name,
                        "Key": serialize(backend._key(str(item["sk"]))),
                        "UpdateExpression": expression,
                        "ConditionExpression": "#g = :g",
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": serialize(values),
                    }
                },
                {
                    "Put": {
                        "TableName": backend._table_name,
                        "Item": serialize(backend._vector_item_sync(memory_id, vector, dek)),
                    }
                },
            ]
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = exc.response.get("CancellationReasons") or []
        if reasons and not any(r.get("Code") == "ConditionalCheckFailed" for r in reasons):
            raise
        return False


def _rename_sync(backend: DynamoMemoryStore, words: Words, apply: bool, embed: Embed | None) -> dict[str, Any]:
    tallies: dict[str, dict[str, Any]] = {}
    todo: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for item in backend._query_memories_sync():
        tally = tallies.setdefault(str(item["sk"]).split("#", 1)[0], _new_tally())
        tally["seen"] += 1
        try:
            opened = backend._open_memory_items_sync([item])
        except Exception:  # noqa: BLE001 — 鍵・AAD が合わない行。中身は出さずに数だけ残す
            tally["broken"] += 1
            continue
        if not opened:
            tally["no_key"] += 1  # 忘れた記憶
            continue
        row = opened[0]
        hits: Counter = Counter()
        updates: dict[str, Any] = {}
        for name in TEXT_ATTRIBUTES:
            old = row.get(name)
            if isinstance(old, str) and old:
                new = words.scan(old, hits)
                if new != old:
                    updates[name] = new
        if not hits:
            continue
        tally["hit"] += 1
        tally["hits"].update(hits)
        if apply and updates:
            todo.append((item, row, updates))

    if todo:
        if embed is None:
            from .embedding import E5EmbeddingFunction

            embed = E5EmbeddingFunction(backend._config.embedding_model)
        contents = [updates.get("content", row.get("content") or "") for _, row, updates in todo]
        normalized = [normalize_japanese(content) for content in contents]
        vectors = embed(normalized)
        for (item, row, updates), content, norm, vector in zip(todo, contents, normalized, vectors):
            tally = tallies[str(item["sk"]).split("#", 1)[0]]
            full = {**updates, "normalized_content": norm, "reading": get_reading(content) or ""}
            if _write_sync(backend, item, row, full, encode_vector(vector)):
                tally["changed"] += 1
            else:
                tally["raced"] += 1

    return {prefix: {**tally, "hits": dict(tally["hits"])} for prefix, tally in sorted(tallies.items())}


async def rename_words(
    config: MemoryConfig,
    froms: list[str],
    to: str | None,
    *,
    apply: bool = False,
    embed: Embed | None = None,
) -> dict[str, Any]:
    """記憶の言葉を数える（`apply` なら書き換える）。戻り値は `MEM`・`PRIV` ごとの数。

    `embed` は正規化した本文の並び → ベクトルの並び（省けば記憶 MCP と同じ埋め込みモデル）。
    """
    if apply and to is None:
        raise ValueError("--apply には --to が要る")
    words = Words(froms, to)
    backend = DynamoMemoryStore(config)
    await backend.connect()
    try:
        return await asyncio.to_thread(_rename_sync, backend, words, apply, embed)
    finally:
        await backend.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pid", help="ぷちの ID（PETIT_MEMORY_PETIT_ID と同じでなければ止まる）")
    parser.add_argument("--from", dest="froms", action="append", default=[], help="置き換える言葉（いくつでも）")
    parser.add_argument("--to", help="置き換える先の言葉")
    parser.add_argument("--apply", action="store_true", help="書き換える（無ければ数えるだけ）")
    parser.add_argument("--json", action="store_true", help="数を JSON で出す")
    args = parser.parse_args(argv)

    # boto3 は AWS_REGION を読まない。ぷちコンテナの env は AWS_REGION だけなので写す
    # （記憶 MCP には petit-env の gen-mcp-config.sh が同じことをして渡している）
    if os.environ.get("AWS_REGION"):
        os.environ.setdefault("AWS_DEFAULT_REGION", os.environ["AWS_REGION"])
    config = MemoryConfig.from_env()
    froms = [_unescape_arg(w) for w in args.froms]
    to = _unescape_arg(args.to) if args.to is not None else None
    try:
        if config.store_backend != "dynamo":
            raise ValueError(f"DynamoDB の家だけ（PETIT_MEMORY_STORE={config.store_backend}）")
        if args.pid != config.petit_id:
            raise ValueError(f"pid が合わない（引数 {args.pid}・PETIT_MEMORY_PETIT_ID {config.petit_id or '無し'}）")
        result = asyncio.run(rename_words(config, froms, to, apply=args.apply))
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"pid": args.pid, "apply": args.apply, "from": froms, "to": to, "memory": result},
                         ensure_ascii=False))
        return 0
    print(f"[rename_words] {args.pid} の記憶: {'書き換えた' if args.apply else '数えただけ（書いていない）'}")
    print(f"  置き換える言葉: {'・'.join(froms)} → {to if to is not None else '（無し）'}")
    labels = (("changed", "書き換えた（ベクトルも作り直した）"), ("raced", "途中で変わったので飛ばした"),
              ("no_key", "鍵が無い（忘れた記憶）"), ("broken", "開けなかった"))
    for prefix, tally in result.items():
        parts = [f"見た {tally['seen']}"]
        if tally["hit"]:
            by_word = "・".join(f"{w} {n}" for w, n in sorted(tally["hits"].items()))
            parts.append(f"当たり {tally['hit']}（{by_word}）")
        parts += [f"{text} {tally[name]}" for name, text in labels if tally[name]]
        print(f"  {prefix}: " + "・".join(parts))
    if not result:
        print("  記憶は無い")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
