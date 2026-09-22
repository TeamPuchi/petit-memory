"""DynamoDB 単一表の保管層（段1: 本体を実装）。

## 表の形

- 表名: `house`（単一表・既定）。pk と sk の 2 本だけで、GSI は使わない。
- `pk` — 環境変数 `PETIT_MEMORY_HOUSE_ID` の有無で 2 通り（`partition_key` 1 か所で組み立てる）:
  - 無ければ `P#<pid>`            アカウントが最上位・家はその下（9/22 の決定。これから作るクラウドぷち）
  - あれば `H#<hid>#P#<pid>`      従来形（ぷちてゃたちと、既に書いた分）
- `sk` の前置辞:
  - `MEM#<ts>#<id>`                  記憶本体（`ts` は ISO 8601 なので sk 昇順＝時系列順）
  - `PRIV#<ts>#<id>`                 本人だけの面。本体と同じ形で、置き場所だけ分ける
  - `FORGET#<ts>#<id>`               消した跡。本文は持たない（復元の道具を作らないため）
  - `VEC#<id>`                       記憶の埋め込みベクトル（Binary）
  - `EPI#<start_time>#<id>`          エピソード（sk 昇順＝開始時刻順）
  - `COACT#<source_id>#<target_id>`  共活性の重み（片方向 1 件。対称化は呼び出し側）
  - `IDX#<memory_id>`                記憶の指し札。属性 `target_sk` に `MEM#…` か `PRIV#…`
  - `IDX#EPI#<episode_id>`           エピソードの指し札。属性 `target_sk` に `EPI#<start_time>#<id>`

`sk` に時刻が入る本体を ID だけで引くために、同じパーティションに「指し札」を置く。
GSI を使わないのは、家の壁を IAM の `dynamodb:LeadingKeys`（pk が `H#<hid>…`）で作るため。
pk が `id` になる GSI はその壁の外に出てしまう。

指し札は本体と同じ pk なので、書き込みは `TransactWriteItems` で本体・ベクトル・指し札を
1 回で束ねる。削除も同じ。

## 本人だけの面（`PRIV#`）の読み

`PRIV#` は「置き場所を分ける」だけで、本人（MCP 経由）からの読みでは `MEM#` と同じに見える。
記憶を一覧・検索する経路はすべて `_query_memories_sync()` を通り、2 つの前置辞を読んで
`<ts>#<id>` 順に並べ直す。里親向けの読み出し経路はこの層には作らない
（家 API に出さないのは呼び出し側の責任で、保管層に「`MEM#` だけ読む口」を生やすと
そこが将来の抜け道になる）。

## `index:false`

`indexed` 属性が 0 の記憶は、意味検索の母集団（`fetch_memories_with_vectors`）と
Hopfield の母集団（`fetch_all_vectors`）、無作為の 1 件の母集団
（`fetch_indexed_memory_ids`）から外す。ID 指定の読みと新着一覧では従来どおり出る。
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

from .config import MemoryConfig
from .records import (
    decode_episode,
    decode_forget_marker,
    decode_memory,
    encode_episode,
    encode_forget_marker,
    encode_memory,
    parse_linked_ids,
    parse_links,
)
from .store_backend import (
    MemoryFacets,
    MemoryRecord,
    MemoryWithVector,
    VectorRow,
)
from .types import Episode, ForgetMarker, Memory

MEMORY_PREFIX = "MEM#"
PRIVATE_PREFIX = "PRIV#"
FORGET_PREFIX = "FORGET#"
VECTOR_PREFIX = "VEC#"
EPISODE_PREFIX = "EPI#"
COACTIVATION_PREFIX = "COACT#"
POINTER_PREFIX = "IDX#"
EPISODE_POINTER_PREFIX = "IDX#EPI#"

# sk の範囲指定に使う終端（`#` の次の文字）
_SK_MAX = "￿"

_BATCH_GET_LIMIT = 100
_TRANSACT_LIMIT = 100


def _to_attribute(value: Any) -> Any:
    """DynamoDB が受け取れる形にする（float は Decimal、None は空文字）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def _from_attribute(value: Any) -> Any:
    """DynamoDB から返る Decimal を素の数値に戻す。"""
    if isinstance(value, Decimal):
        as_float = float(value)
        return int(as_float) if as_float.is_integer() else as_float
    return value


def _plain(item: dict[str, Any]) -> dict[str, Any]:
    return {key: _from_attribute(value) for key, value in item.items()}


def _sk_suffix(sk: str) -> str:
    """`MEM#<ts>#<id>` / `PRIV#<ts>#<id>` から `<ts>#<id>` を取る（並べ替えの鍵）。"""
    return sk.split("#", 1)[1]


def _is_indexed(item: dict[str, Any]) -> bool:
    """`index:false` で保存された記憶か。段2 より前の行には属性が無いので既定は True。"""
    value = item.get("indexed")
    if value is None:
        return True
    return bool(int(value))


class DynamoMemoryStore:
    """DynamoDB の単一表 `house` に記憶を置く保管層。

    `MemoryStoreBackend` の実装。boto3 は任意依存（`pip install -e ".[dynamo]"`）で、
    この実装を選んだときだけ import する。
    """

    def __init__(self, config: MemoryConfig):
        self._config = config
        self._table_name = config.dynamo_table
        self._house_id = config.house_id
        self._petit_id = config.petit_id
        self._table: Any = None
        self._client: Any = None

    # ── キー組み立て ────────────────────────────

    @property
    def partition_key(self) -> str:
        """pk はここ 1 か所でしか組み立てない（切り替えの口）。

        - `PETIT_MEMORY_HOUSE_ID` が無い（`house_id` が空）→ `P#<pid>`
          9/22 の決定「アカウントが最上位、家はその下」に合わせた新しい形。
          これから作るクラウドぷちはこちら。
        - ある → `H#<hid>#P#<pid>`（従来形）

        表の中身は pk ごとに完全に分かれるので、同じ個体で途中から切り替えると
        前の pk に書いたものは読めなくなる。**動いている個体の環境変数は変えない。**
        """
        if not self._house_id:
            return f"P#{self._petit_id}"
        return f"H#{self._house_id}#P#{self._petit_id}"

    @staticmethod
    def memory_sk(timestamp: str, memory_id: str, private: bool = False) -> str:
        """`MEM#<ts>#<id>`。本人だけの面なら `PRIV#<ts>#<id>`。"""
        prefix = PRIVATE_PREFIX if private else MEMORY_PREFIX
        return f"{prefix}{timestamp}#{memory_id}"

    @staticmethod
    def forget_sk(forgotten_at: str, memory_id: str) -> str:
        """`FORGET#<ts>#<id>`。"""
        return f"{FORGET_PREFIX}{forgotten_at}#{memory_id}"

    @staticmethod
    def vector_sk(memory_id: str) -> str:
        """`VEC#<id>`。"""
        return f"{VECTOR_PREFIX}{memory_id}"

    @staticmethod
    def episode_sk(start_time: str, episode_id: str) -> str:
        """`EPI#<start_time>#<id>`。"""
        return f"{EPISODE_PREFIX}{start_time}#{episode_id}"

    @staticmethod
    def coactivation_sk(source_id: str, target_id: str) -> str:
        """`COACT#<source_id>#<target_id>`。"""
        return f"{COACTIVATION_PREFIX}{source_id}#{target_id}"

    @staticmethod
    def pointer_sk(memory_id: str) -> str:
        """`IDX#<memory_id>`。記憶の指し札。"""
        return f"{POINTER_PREFIX}{memory_id}"

    @staticmethod
    def episode_pointer_sk(episode_id: str) -> str:
        """`IDX#EPI#<episode_id>`。エピソードの指し札。"""
        return f"{EPISODE_POINTER_PREFIX}{episode_id}"

    # ── 接続 ────────────────────────────────────

    async def connect(self) -> None:
        """boto3 のリソースを用意する（表そのものは petit-infra 側で作る）。"""
        if self._table is not None:
            return

        def _open() -> tuple[Any, Any]:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - 依存が無い環境向け
                raise ImportError(
                    "boto3 が必要です。`uv sync --extra dynamo` か "
                    '`pip install -e ".[dynamo]"` を実行してください。'
                ) from exc
            table = boto3.resource("dynamodb").Table(self._table_name)
            # 素のクライアント。resource 側のクライアントは Item/Key を自動で
            # 変換するため、自分で型を組み立てる操作（transact / batch_get）には使えない。
            client = boto3.client("dynamodb")
            return table, client

        self._table, self._client = await asyncio.to_thread(_open)

    async def disconnect(self) -> None:
        """リソースを手放す（DynamoDB に閉じる接続は無い）。"""
        self._table = None
        self._client = None

    def _ensure_connected(self) -> Any:
        if self._table is None:
            raise RuntimeError("DynamoMemoryStore not connected. Call connect() first.")
        return self._table

    def _ensure_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("DynamoMemoryStore not connected. Call connect() first.")
        return self._client

    # ── 低レベルの往復 ──────────────────────────

    def _query_prefix_sync(
        self,
        prefix: str,
        *,
        projection: list[str] | None = None,
        forward: bool = True,
        sk_from: str | None = None,
        sk_to: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """`sk` 前置辞（必要なら範囲）で 1 パーティションを読む。ページングも面倒を見る。"""
        from boto3.dynamodb.conditions import Key

        table = self._ensure_connected()
        low = sk_from if sk_from is not None else prefix
        high = sk_to if sk_to is not None else prefix + _SK_MAX
        key_condition = Key("pk").eq(self.partition_key) & Key("sk").between(low, high)

        kwargs: dict[str, Any] = {
            "KeyConditionExpression": key_condition,
            "ScanIndexForward": forward,
        }
        if projection:
            names = {f"#p{i}": name for i, name in enumerate(projection)}
            kwargs["ProjectionExpression"] = ", ".join(names)
            kwargs["ExpressionAttributeNames"] = names
        if limit is not None:
            kwargs["Limit"] = limit

        items: list[dict[str, Any]] = []
        while True:
            response = table.query(**kwargs)
            items.extend(response.get("Items", []))
            if limit is not None and len(items) >= limit:
                return items[:limit]
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return items
            kwargs["ExclusiveStartKey"] = last_key

    def _query_memories_sync(
        self,
        *,
        projection: list[str] | None = None,
        forward: bool = True,
        ts_from: str | None = None,
        ts_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """記憶を `MEM#` と `PRIV#` の両方から読み、`<ts>#<id>` 順に並べ直す。

        本人だけの面も本人から見れば同じ 1 本の時系列なので、記憶を一覧・走査する
        経路はすべてここを通す。`sk` は並べ替えに要るので projection にも必ず足す。
        """
        if projection is not None and "sk" not in projection:
            projection = [*projection, "sk"]

        items: list[dict[str, Any]] = []
        for prefix in (MEMORY_PREFIX, PRIVATE_PREFIX):
            items.extend(
                self._query_prefix_sync(
                    prefix,
                    projection=projection,
                    sk_from=f"{prefix}{ts_from}" if ts_from else None,
                    sk_to=f"{prefix}{ts_to}{_SK_MAX}" if ts_to else None,
                )
            )
        items.sort(key=lambda item: _sk_suffix(str(item["sk"])), reverse=not forward)
        return items

    def _get_item_sync(self, sk: str) -> dict[str, Any] | None:
        table = self._ensure_connected()
        response = table.get_item(Key={"pk": self.partition_key, "sk": sk})
        return response.get("Item")

    def _batch_get_sync(self, sks: list[str]) -> list[dict[str, Any]]:
        client = self._ensure_client()
        items: list[dict[str, Any]] = []
        from boto3.dynamodb.types import TypeDeserializer

        deserializer = TypeDeserializer()
        for start in range(0, len(sks), _BATCH_GET_LIMIT):
            chunk = sks[start : start + _BATCH_GET_LIMIT]
            keys = [{"pk": {"S": self.partition_key}, "sk": {"S": sk}} for sk in chunk]
            request: Any = {self._table_name: {"Keys": keys}}
            while request:
                response = client.batch_get_item(RequestItems=request)
                for raw in response.get("Responses", {}).get(self._table_name, []):
                    items.append({k: deserializer.deserialize(v) for k, v in raw.items()})
                unprocessed = response.get("UnprocessedKeys") or {}
                request = unprocessed if unprocessed else None
        return items

    def _transact_write_sync(self, actions: list[dict[str, Any]]) -> None:
        """Put / Delete をまとめて 1 トランザクションで流す。"""
        from boto3.dynamodb.types import TypeSerializer

        client = self._ensure_client()
        serializer = TypeSerializer()

        def serialize(item: dict[str, Any]) -> dict[str, Any]:
            return {k: serializer.serialize(v) for k, v in item.items()}

        transact_items: list[dict[str, Any]] = []
        for action in actions:
            if "Put" in action:
                transact_items.append(
                    {"Put": {"TableName": self._table_name, "Item": serialize(action["Put"])}}
                )
            else:
                transact_items.append(
                    {"Delete": {"TableName": self._table_name, "Key": serialize(action["Delete"])}}
                )

        for start in range(0, len(transact_items), _TRANSACT_LIMIT):
            client.transact_write_items(TransactItems=transact_items[start : start + _TRANSACT_LIMIT])

    def _key(self, sk: str) -> dict[str, str]:
        return {"pk": self.partition_key, "sk": sk}

    def _resolve_memory_sk_sync(self, memory_id: str) -> str | None:
        pointer = self._get_item_sync(self.pointer_sk(memory_id))
        if pointer is None:
            return None
        return pointer.get("target_sk")

    def _resolve_episode_sk_sync(self, episode_id: str) -> str | None:
        pointer = self._get_item_sync(self.episode_pointer_sk(episode_id))
        if pointer is None:
            return None
        return pointer.get("target_sk")

    # ── 共活性の読み ────────────────────────────

    def _coactivation_map_sync(self) -> dict[str, tuple[tuple[str, float], ...]]:
        items = self._query_prefix_sync(COACTIVATION_PREFIX)
        grouped: dict[str, list[tuple[str, float]]] = {}
        for item in items:
            grouped.setdefault(item["source_id"], []).append(
                (item["target_id"], float(item["weight"]))
            )
        return {source: tuple(pairs) for source, pairs in grouped.items()}

    def _coactivation_for_sync(self, memory_id: str) -> tuple[tuple[str, float], ...]:
        items = self._query_prefix_sync(f"{COACTIVATION_PREFIX}{memory_id}#")
        return tuple((item["target_id"], float(item["weight"])) for item in items)

    # ── 記憶: 取る ──────────────────────────────

    async def fetch_memory(self, memory_id: str) -> Memory | None:
        """指し札（`IDX#<id>`）→ 本体（`MEM#…`）の 2 読み。"""

        def _fetch() -> Memory | None:
            sk = self._resolve_memory_sk_sync(memory_id)
            if sk is None:
                return None
            item = self._get_item_sync(sk)
            if item is None:
                return None
            return decode_memory(_plain(item), self._coactivation_for_sync(memory_id))

        return await asyncio.to_thread(_fetch)

    async def fetch_memories(self, memory_ids: list[str]) -> list[Memory]:
        if not memory_ids:
            return []

        def _fetch() -> list[Memory]:
            pointers = self._batch_get_sync([self.pointer_sk(mid) for mid in memory_ids])
            target_sks = [p["target_sk"] for p in pointers if p.get("target_sk")]
            if not target_sks:
                return []
            items = self._batch_get_sync(target_sks)
            coactivation = self._coactivation_map_sync()
            return [
                decode_memory(_plain(item), coactivation.get(item["id"], ()))
                for item in items
            ]

        return await asyncio.to_thread(_fetch)

    async def fetch_all_memories(self) -> list[Memory]:
        def _fetch() -> list[Memory]:
            items = self._query_memories_sync()
            coactivation = self._coactivation_map_sync()
            return [
                decode_memory(_plain(item), coactivation.get(item["id"], ()))
                for item in items
            ]

        return await asyncio.to_thread(_fetch)

    async def fetch_memories_with_vectors(
        self,
        emotion: str | None = None,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[MemoryWithVector]:
        """`MEM#` を（期間は sk の範囲で）絞って取り、`VEC#<id>` を引き当てて組にする。"""

        def _fetch() -> list[MemoryWithVector]:
            # 期間は sk の範囲で粗く絞り、境界の比較は SQLite と同じ文字列比較で仕上げる
            items = self._query_memories_sync(ts_from=date_from, ts_to=date_to)

            selected = [
                item
                for item in items
                # 段2: index:false は意味検索・recall の母集団に入れない
                if _is_indexed(item)
                and (emotion is None or item.get("emotion") == emotion)
                and (category is None or item.get("category") == category)
                and (date_from is None or str(item["timestamp"]) >= date_from)
                and (date_to is None or str(item["timestamp"]) <= date_to)
            ]
            if not selected:
                return []

            vectors = {
                item["memory_id"]: bytes(item["vector"].value)
                for item in self._batch_get_sync([self.vector_sk(i["id"]) for i in selected])
            }
            coactivation = self._coactivation_map_sync()

            results: list[MemoryWithVector] = []
            for item in selected:
                vector = vectors.get(item["id"])
                if vector is None:
                    # SQLite 側は memories JOIN embeddings なので、ベクトルが無い記憶は出さない
                    continue
                results.append(
                    MemoryWithVector(
                        memory=decode_memory(_plain(item), coactivation.get(item["id"], ())),
                        vector=vector,
                    )
                )
            return results

        return await asyncio.to_thread(_fetch)

    async def fetch_recent_memories(self, limit: int, category: str | None = None) -> list[Memory]:
        """`sk` 降順（`ScanIndexForward=False`）に読んで、先頭 limit 件。"""

        def _fetch() -> list[Memory]:
            items = self._query_memories_sync(forward=False)
            if category:
                items = [item for item in items if item.get("category") == category]
            items = items[: max(0, limit)]
            coactivation = self._coactivation_map_sync()
            return [
                decode_memory(_plain(item), coactivation.get(item["id"], ()))
                for item in items
            ]

        return await asyncio.to_thread(_fetch)

    async def fetch_important_memories(
        self,
        min_importance: int,
        min_access_count: int,
        since: str | None,
        limit: int,
    ) -> list[Memory]:
        def _fetch() -> list[Memory]:
            items = self._query_memories_sync()
            selected = [
                item
                for item in items
                if int(item.get("importance", 0)) >= min_importance
                and int(item.get("access_count", 0)) >= min_access_count
                and (since is None or str(item.get("last_accessed", "")) >= since)
            ]
            selected.sort(key=lambda item: str(item.get("last_accessed", "")), reverse=True)
            selected = selected[: max(0, limit)]
            coactivation = self._coactivation_map_sync()
            return [
                decode_memory(_plain(item), coactivation.get(item["id"], ()))
                for item in selected
            ]

        return await asyncio.to_thread(_fetch)

    async def fetch_memory_facets(self) -> MemoryFacets:
        def _fetch() -> MemoryFacets:
            items = self._query_memories_sync(
                projection=["emotion", "category", "timestamp"]
            )
            rows = tuple(
                (
                    item.get("emotion") or "neutral",
                    item.get("category") or "daily",
                    item["timestamp"],
                )
                for item in items
            )
            timestamps = [row[2] for row in rows]
            return MemoryFacets(
                rows=rows,
                oldest_timestamp=min(timestamps) if timestamps else None,
                newest_timestamp=max(timestamps) if timestamps else None,
            )

        return await asyncio.to_thread(_fetch)

    # ── 記憶: 入れる・直す・消す ────────────────

    async def insert_memory(self, record: MemoryRecord) -> None:
        """`MEM#<ts>#<id>`（本人だけの面なら `PRIV#…`）・`VEC#<id>`・`IDX#<id>` を 1 トランザクションで書く。

        指し札は置き場所に関わらず `IDX#<id>` なので、ID 指定の読みは 2 つの面で同じ。
        """
        memory = record.memory
        attrs = {key: _to_attribute(value) for key, value in encode_memory(record).items()}
        memory_sk = self.memory_sk(memory.timestamp, memory.id, memory.private)

        def _write() -> None:
            self._transact_write_sync(
                [
                    {"Put": {**self._key(memory_sk), "entity": "memory", **attrs}},
                    {
                        "Put": {
                            **self._key(self.vector_sk(memory.id)),
                            "entity": "vector",
                            "memory_id": memory.id,
                            "vector": record.vector,
                        }
                    },
                    {
                        "Put": {
                            **self._key(self.pointer_sk(memory.id)),
                            "entity": "pointer",
                            "memory_id": memory.id,
                            "target_sk": memory_sk,
                        }
                    },
                ]
            )

        await asyncio.to_thread(_write)

    async def update_memory_fields(self, memory_id: str, fields: dict[str, Any]) -> bool:
        if not fields:
            return True

        def _update() -> bool:
            sk = self._resolve_memory_sk_sync(memory_id)
            if sk is None:
                return False
            table = self._ensure_connected()
            names = {f"#f{i}": name for i, name in enumerate(fields)}
            values = {
                f":v{i}": _to_attribute(value) for i, value in enumerate(fields.values())
            }
            expression = "SET " + ", ".join(
                f"{name_key} = {value_key}" for name_key, value_key in zip(names, values)
            )
            table.update_item(
                Key=self._key(sk),
                UpdateExpression=expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return True

        return await asyncio.to_thread(_update)

    async def update_episode_id(self, memory_id: str, episode_id: str | None) -> bool:
        return await self.update_memory_fields(memory_id, {"episode_id": episode_id})

    async def increment_access(self, memory_id: str, last_accessed: str) -> None:
        def _update() -> None:
            sk = self._resolve_memory_sk_sync(memory_id)
            if sk is None:
                return
            table = self._ensure_connected()
            table.update_item(
                Key=self._key(sk),
                UpdateExpression="SET #la = :ts ADD #ac :one",
                ExpressionAttributeNames={"#la": "last_accessed", "#ac": "access_count"},
                ExpressionAttributeValues={":ts": last_accessed, ":one": 1},
            )

        await asyncio.to_thread(_update)

    async def delete_memory(self, memory_id: str, forget_marker: ForgetMarker | None = None) -> bool:
        """`MEM#`（か `PRIV#`）・`VEC#`・`IDX#`・その記憶に触れる `COACT#` を消し、逆参照も掃除する。

        `forget_marker` を渡すと、同じ `TransactWriteItems` で `FORGET#<ts>#<id>` を書く。
        跡には本文を入れないので、これを読んでも記憶は戻らない。
        """

        def _delete() -> bool:
            sk = self._resolve_memory_sk_sync(memory_id)
            if sk is None:
                return False

            table = self._ensure_connected()

            # 他の記憶の linked_ids / links から消す
            for item in self._query_memories_sync():
                if item["id"] == memory_id:
                    continue
                updates: dict[str, Any] = {}
                current = parse_linked_ids(item.get("linked_ids") or "")
                if memory_id in current:
                    updates["linked_ids"] = ",".join(lid for lid in current if lid != memory_id)
                links = parse_links(item.get("links") or "")
                if any(link.target_id == memory_id for link in links):
                    remaining = [link for link in links if link.target_id != memory_id]
                    updates["links"] = json.dumps([link.to_dict() for link in remaining])
                if not updates:
                    continue
                names = {f"#f{i}": name for i, name in enumerate(updates)}
                values = {f":v{i}": value for i, value in enumerate(updates.values())}
                table.update_item(
                    Key=self._key(item["sk"]),
                    UpdateExpression="SET "
                    + ", ".join(f"{n} = {v}" for n, v in zip(names, values)),
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                )

            # 本体・ベクトル・指し札・共活性
            deletes: list[dict[str, Any]] = [
                {"Delete": self._key(sk)},
                {"Delete": self._key(self.vector_sk(memory_id))},
                {"Delete": self._key(self.pointer_sk(memory_id))},
            ]
            for item in self._query_prefix_sync(COACTIVATION_PREFIX):
                if item["source_id"] == memory_id or item["target_id"] == memory_id:
                    deletes.append({"Delete": self._key(item["sk"])})

            if forget_marker is not None:
                marker_attrs = {
                    key: _to_attribute(value)
                    for key, value in encode_forget_marker(forget_marker).items()
                }
                deletes.append(
                    {
                        "Put": {
                            **self._key(
                                self.forget_sk(forget_marker.forgotten_at, memory_id)
                            ),
                            "entity": "forget",
                            **marker_attrs,
                        }
                    }
                )

            self._transact_write_sync(deletes)
            return True

        return await asyncio.to_thread(_delete)

    async def add_bidirectional_link(self, source_id: str, target_id: str) -> None:
        def _link() -> None:
            table = self._ensure_connected()
            for mem_id, other_id in [(source_id, target_id), (target_id, source_id)]:
                sk = self._resolve_memory_sk_sync(mem_id)
                if sk is None:
                    continue
                item = self._get_item_sync(sk)
                if item is None:
                    continue
                current = parse_linked_ids(item.get("linked_ids") or "")
                if other_id in current:
                    continue
                table.update_item(
                    Key=self._key(sk),
                    UpdateExpression="SET #li = :v",
                    ExpressionAttributeNames={"#li": "linked_ids"},
                    ExpressionAttributeValues={":v": ",".join(current + (other_id,))},
                )

        await asyncio.to_thread(_link)

    # ── 消した跡 ────────────────────────────────

    async def fetch_forget_markers(self, since: str | None, limit: int) -> list[ForgetMarker]:
        """`FORGET#` を降順（新しい順）に読む。`since` は sk の下端で切る。"""

        def _fetch() -> list[ForgetMarker]:
            items = self._query_prefix_sync(
                FORGET_PREFIX,
                forward=False,
                sk_from=f"{FORGET_PREFIX}{since}" if since else None,
                limit=max(0, limit) or None,
            )
            return [decode_forget_marker(_plain(item)) for item in items[: max(0, limit)]]

        return await asyncio.to_thread(_fetch)

    # ── 一覧の材料（段2）──────────────────────

    async def fetch_indexed_memory_ids(self) -> list[str]:
        """無作為の 1 件を選ぶための母集団。ID だけを射影して読む。"""

        def _fetch() -> list[str]:
            items = self._query_memories_sync(projection=["id", "indexed"])
            return [item["id"] for item in items if _is_indexed(item)]

        return await asyncio.to_thread(_fetch)

    async def fetch_neighbors(self, memory_id: str) -> tuple[Memory | None, Memory | None]:
        """`<ts>#<id>` 順で 1 つ前・1 つ後。`MEM#` と `PRIV#` の両方を見て近いほうを採る。

        `between` は両端を含むので、錨そのものが混ざる分だけ 2 件ずつ読んで落とす。
        """

        def _fetch() -> tuple[Memory | None, Memory | None]:
            anchor_sk = self._resolve_memory_sk_sync(memory_id)
            if anchor_sk is None:
                return (None, None)
            suffix = _sk_suffix(anchor_sk)

            def _pick(before: bool) -> dict[str, Any] | None:
                best: dict[str, Any] | None = None
                for prefix in (MEMORY_PREFIX, PRIVATE_PREFIX):
                    bound = f"{prefix}{suffix}"
                    items = self._query_prefix_sync(
                        prefix,
                        forward=not before,
                        sk_from=None if before else bound,
                        sk_to=bound if before else None,
                        limit=2,
                    )
                    for item in items:
                        candidate = _sk_suffix(str(item["sk"]))
                        if candidate == suffix:
                            continue
                        if best is None:
                            best = item
                        else:
                            current = _sk_suffix(str(best["sk"]))
                            if (candidate > current) if before else (candidate < current):
                                best = item
                        break
                return best

            previous_item = _pick(before=True)
            next_item = _pick(before=False)
            coactivation = self._coactivation_map_sync()

            def _decode(item: dict[str, Any] | None) -> Memory | None:
                if item is None:
                    return None
                return decode_memory(_plain(item), coactivation.get(item["id"], ()))

            return (_decode(previous_item), _decode(next_item))

        return await asyncio.to_thread(_fetch)

    # ── ベクトル ────────────────────────────────

    async def fetch_vectors(self, memory_ids: list[str]) -> dict[str, bytes]:
        if not memory_ids:
            return {}

        def _fetch() -> dict[str, bytes]:
            items = self._batch_get_sync([self.vector_sk(mid) for mid in memory_ids])
            return {item["memory_id"]: bytes(item["vector"].value) for item in items}

        return await asyncio.to_thread(_fetch)

    async def fetch_all_vectors(self) -> list[VectorRow]:
        def _fetch() -> list[VectorRow]:
            vectors = self._query_prefix_sync(VECTOR_PREFIX)
            # 段2: index:false は Hopfield の母集団にも載せない
            contents = {
                item["id"]: item.get("normalized_content", "")
                for item in self._query_memories_sync(
                    projection=["id", "normalized_content", "indexed"]
                )
                if _is_indexed(item)
            }
            rows: list[VectorRow] = []
            for item in vectors:
                memory_id = item["memory_id"]
                if memory_id not in contents:
                    continue
                rows.append(
                    VectorRow(
                        memory_id=memory_id,
                        vector=bytes(item["vector"].value),
                        normalized_content=contents[memory_id],
                    )
                )
            return rows

        return await asyncio.to_thread(_fetch)

    # ── 共活性 ──────────────────────────────────

    async def fetch_coactivation(self, memory_id: str) -> tuple[tuple[str, float], ...]:
        return await asyncio.to_thread(self._coactivation_for_sync, memory_id)

    async def fetch_coactivation_weight(self, source_id: str, target_id: str) -> float | None:
        def _fetch() -> float | None:
            item = self._get_item_sync(self.coactivation_sk(source_id, target_id))
            if item is None:
                return None
            return float(item["weight"])

        return await asyncio.to_thread(_fetch)

    async def put_coactivation(self, source_id: str, target_id: str, weight: float) -> None:
        def _put() -> None:
            table = self._ensure_connected()
            table.put_item(
                Item={
                    **self._key(self.coactivation_sk(source_id, target_id)),
                    "entity": "coactivation",
                    "source_id": source_id,
                    "target_id": target_id,
                    "weight": _to_attribute(weight),
                }
            )

        await asyncio.to_thread(_put)

    # ── エピソード ──────────────────────────────

    async def insert_episode(self, episode: Episode) -> None:
        """`EPI#<start_time>#<id>` と指し札 `IDX#EPI#<id>` を 1 トランザクションで書く。"""
        attrs = {key: _to_attribute(value) for key, value in encode_episode(episode).items()}
        episode_sk = self.episode_sk(episode.start_time, episode.id)

        def _write() -> None:
            self._transact_write_sync(
                [
                    {"Put": {**self._key(episode_sk), "entity": "episode", **attrs}},
                    {
                        "Put": {
                            **self._key(self.episode_pointer_sk(episode.id)),
                            "entity": "pointer",
                            "episode_id": episode.id,
                            "target_sk": episode_sk,
                        }
                    },
                ]
            )

        await asyncio.to_thread(_write)

    async def fetch_episode(self, episode_id: str) -> Episode | None:
        def _fetch() -> Episode | None:
            sk = self._resolve_episode_sk_sync(episode_id)
            if sk is None:
                return None
            item = self._get_item_sync(sk)
            if item is None:
                return None
            return decode_episode(_plain(item))

        return await asyncio.to_thread(_fetch)

    async def search_episodes(self, query: str, limit: int) -> list[Episode]:
        """タイトル・要約の部分一致。`sk` 降順＝開始時刻の新しい順。"""

        def _fetch() -> list[Episode]:
            items = self._query_prefix_sync(EPISODE_PREFIX, forward=False)
            matched = [
                item
                for item in items
                if query in str(item.get("title", "")) or query in str(item.get("summary", ""))
            ]
            return [decode_episode(_plain(item)) for item in matched[: max(0, limit)]]

        return await asyncio.to_thread(_fetch)

    async def fetch_all_episodes(self) -> list[Episode]:
        def _fetch() -> list[Episode]:
            items = self._query_prefix_sync(EPISODE_PREFIX, forward=False)
            return [decode_episode(_plain(item)) for item in items]

        return await asyncio.to_thread(_fetch)

    async def delete_episode(self, episode_id: str) -> None:
        def _delete() -> None:
            sk = self._resolve_episode_sk_sync(episode_id)
            if sk is None:
                return
            self._transact_write_sync(
                [
                    {"Delete": self._key(sk)},
                    {"Delete": self._key(self.episode_pointer_sk(episode_id))},
                ]
            )

        await asyncio.to_thread(_delete)
