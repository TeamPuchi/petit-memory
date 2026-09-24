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

## 暗号シュレッダー（K20）

`MemoryConfig.keys_table` と `kms_key_id` を両方与えると、記憶 1 件ごとの DEK で
本文（`SECRET_MEMORY_ATTRIBUTES`）とベクトルを AES-256-GCM で暗号化して置く。
DEK は KMS で包んで別の「鍵の表」に置き、忘れるときは鍵から消す（`crypto_shred.py`）。

- `MEM#` / `PRIV#` — 本文類は `sealed`（Binary）1 本にまとめ、`enc_v` = 1 を平文で付ける。
  id・日時・感情・種類・重要度・タグ・index/private フラグ・リンク先 id は平文のまま
  （絞り込み・保守・消した跡のため）。
- `VEC#` — `vector` の代わりに `vector_sealed`。
- `FORGET#` — 従来どおり平文（本文は元から持たない）。

鍵の無い暗号文（忘れた項目を PITR / S3 から戻したもの）は、どの読みでも「無いもの」として扱う。
復号はこのプロセスの中だけ・その場だけで、平文 DEK はメモリにだけ持つ。
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

from .config import MemoryConfig
from .crypto_shred import CryptoShredder, KmsDataKeyWrapper, open_json, open_sealed, seal, seal_json
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

# K20: 暗号化する記憶の属性（本文をある程度復元できるもの）。残りは平文のメタデータ。
SECRET_MEMORY_ATTRIBUTES: tuple[str, ...] = (
    "content",
    "normalized_content",
    "reading",
    "sensory_data",
    "links",  # リンクの note に本文が混ざりうる
)
SEALED_ATTRIBUTE = "sealed"
SEALED_VERSION_ATTRIBUTE = "enc_v"
SEALED_VECTOR_ATTRIBUTE = "vector_sealed"


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


def _as_bytes(value: Any) -> bytes:
    """boto3 の `Binary` か bytes を bytes にする。"""
    return bytes(value.value) if hasattr(value, "value") else bytes(value)


def _is_sealed(item: dict[str, Any]) -> bool:
    return SEALED_VERSION_ATTRIBUTE in item


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
        self._shredder: CryptoShredder | None = None
        if bool(config.keys_table) != bool(config.kms_key_id):
            raise ValueError("keys_table と kms_key_id は両方そろえて設定する（K20 暗号シュレッダー）")

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
            shredder = None
            if self._config.keys_table:
                shredder = CryptoShredder(
                    self._petit_id,
                    self._config.keys_table,
                    KmsDataKeyWrapper(self._config.kms_key_id, boto3.client("kms")),
                    dynamodb_client=client,
                )
            return table, client, shredder

        self._table, self._client, self._shredder = await asyncio.to_thread(_open)

    async def disconnect(self) -> None:
        """リソースを手放す（DynamoDB に閉じる接続は無い）。"""
        self._table = None
        self._client = None
        if self._shredder is not None:
            self._shredder.clear_cache()
        self._shredder = None

    @property
    def encrypted(self) -> bool:
        """暗号シュレッダーが効いているか。"""
        return self._shredder is not None

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

    # ── 暗号シュレッダー（K20）───────────────────

    def _seal_memory_attrs(self, memory_id: str, attrs: dict[str, Any], dek: bytes) -> dict[str, Any]:
        """本文類を `sealed` 1 本にまとめて暗号化し、平文の属性から外す。"""
        assert self._shredder is not None
        secret = {name: attrs.get(name) for name in SECRET_MEMORY_ATTRIBUTES}
        sealed = {k: v for k, v in attrs.items() if k not in SECRET_MEMORY_ATTRIBUTES}
        sealed[SEALED_ATTRIBUTE] = seal_json(dek, secret, self._shredder.aad(memory_id, "mem"))
        sealed[SEALED_VERSION_ATTRIBUTE] = 1
        return sealed

    def _open_memory_items_sync(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """暗号化された記憶を復号して平文の属性に戻す。鍵の無いもの（忘れた項目）は落とす。

        暗号化していない行（段2 までの行・暗号シュレッダー無効の環境）はそのまま通す。
        """
        sealed_ids = [str(item["id"]) for item in items if _is_sealed(item)]
        if not sealed_ids:
            return items
        keys = self._shredder.keys_for(sealed_ids) if self._shredder is not None else {}
        opened: list[dict[str, Any]] = []
        for item in items:
            if not _is_sealed(item):
                opened.append(item)
                continue
            dek = keys.get(str(item["id"]))
            if dek is None or SEALED_ATTRIBUTE not in item:
                continue
            assert self._shredder is not None
            secret = open_json(dek, _as_bytes(item[SEALED_ATTRIBUTE]), self._shredder.aad(str(item["id"]), "mem"))
            plain = {k: v for k, v in item.items() if k not in (SEALED_ATTRIBUTE, SEALED_VERSION_ATTRIBUTE)}
            plain.update(secret)
            opened.append(plain)
        return opened

    def _readable_sync(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """鍵のある（読める）記憶だけ残す。復号はしない（射影した読みの絞り込み用）。"""
        sealed_ids = [str(item["id"]) for item in items if _is_sealed(item)]
        if not sealed_ids:
            return items
        keys = self._shredder.keys_for(sealed_ids) if self._shredder is not None else {}
        return [item for item in items if not _is_sealed(item) or str(item["id"]) in keys]

    def _vector_item_sync(self, memory_id: str, vector: bytes, dek: bytes | None) -> dict[str, Any]:
        base = {**self._key(self.vector_sk(memory_id)), "entity": "vector", "memory_id": memory_id}
        if dek is None:
            return {**base, "vector": vector}
        assert self._shredder is not None
        return {
            **base,
            SEALED_VECTOR_ATTRIBUTE: seal(dek, vector, self._shredder.aad(memory_id, "vec")),
            SEALED_VERSION_ATTRIBUTE: 1,
        }

    def _open_vectors_sync(self, items: list[dict[str, Any]]) -> dict[str, bytes]:
        """`VEC#` の行から memory_id → ベクトルの bytes。鍵の無いものは落とす。"""
        sealed_ids = [str(item["memory_id"]) for item in items if SEALED_VECTOR_ATTRIBUTE in item]
        keys = self._shredder.keys_for(sealed_ids) if (sealed_ids and self._shredder is not None) else {}
        result: dict[str, bytes] = {}
        for item in items:
            memory_id = str(item["memory_id"])
            if SEALED_VECTOR_ATTRIBUTE not in item:
                if "vector" in item:
                    result[memory_id] = _as_bytes(item["vector"])
                continue
            dek = keys.get(memory_id)
            if dek is None:
                continue
            assert self._shredder is not None
            result[memory_id] = open_sealed(
                dek, _as_bytes(item[SEALED_VECTOR_ATTRIBUTE]), self._shredder.aad(memory_id, "vec")
            )
        return result

    def _write_secret_fields_sync(self, item: dict[str, Any], updates: dict[str, Any]) -> None:
        """記憶 1 件の属性を書き換える。暗号化された行の本文類は復号→差し替え→再暗号化する。"""
        table = self._ensure_connected()
        plain_updates = {k: v for k, v in updates.items() if k not in SECRET_MEMORY_ATTRIBUTES}
        secret_updates = {k: v for k, v in updates.items() if k in SECRET_MEMORY_ATTRIBUTES}

        if secret_updates and _is_sealed(item):
            opened = self._open_memory_items_sync([item])
            if not opened:
                # 鍵が無い＝忘れた記憶。書き換える中身も無い
                return
            assert self._shredder is not None
            memory_id = str(item["id"])
            dek = self._shredder.require_key(memory_id)
            secret = {name: opened[0].get(name) for name in SECRET_MEMORY_ATTRIBUTES}
            secret.update(secret_updates)
            plain_updates[SEALED_ATTRIBUTE] = seal_json(dek, secret, self._shredder.aad(memory_id, "mem"))
        else:
            plain_updates.update(secret_updates)

        if not plain_updates:
            return
        names = {f"#f{i}": name for i, name in enumerate(plain_updates)}
        values = {f":v{i}": _to_attribute(value) for i, value in enumerate(plain_updates.values())}
        table.update_item(
            Key=self._key(str(item["sk"])),
            UpdateExpression="SET " + ", ".join(f"{n} = {v}" for n, v in zip(names, values)),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

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
            opened = self._open_memory_items_sync([item])
            if not opened:
                return None
            return decode_memory(_plain(opened[0]), self._coactivation_for_sync(memory_id))

        return await asyncio.to_thread(_fetch)

    async def fetch_memories(self, memory_ids: list[str]) -> list[Memory]:
        if not memory_ids:
            return []

        def _fetch() -> list[Memory]:
            pointers = self._batch_get_sync([self.pointer_sk(mid) for mid in memory_ids])
            target_sks = [p["target_sk"] for p in pointers if p.get("target_sk")]
            if not target_sks:
                return []
            items = self._open_memory_items_sync(self._batch_get_sync(target_sks))
            coactivation = self._coactivation_map_sync()
            return [
                decode_memory(_plain(item), coactivation.get(item["id"], ()))
                for item in items
            ]

        return await asyncio.to_thread(_fetch)

    async def fetch_all_memories(self) -> list[Memory]:
        def _fetch() -> list[Memory]:
            items = self._open_memory_items_sync(self._query_memories_sync())
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

            # 復号はここで（鍵の無い＝忘れた記憶は落ちる）
            selected = self._open_memory_items_sync(selected)
            if not selected:
                return []
            vectors = self._open_vectors_sync(
                self._batch_get_sync([self.vector_sk(i["id"]) for i in selected])
            )
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
            items = self._readable_sync(items)[: max(0, limit)]
            items = self._open_memory_items_sync(items)
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
            selected = self._open_memory_items_sync(self._readable_sync(selected)[: max(0, limit)])
            coactivation = self._coactivation_map_sync()
            return [
                decode_memory(_plain(item), coactivation.get(item["id"], ()))
                for item in selected
            ]

        return await asyncio.to_thread(_fetch)

    async def fetch_memory_facets(self) -> MemoryFacets:
        def _fetch() -> MemoryFacets:
            items = self._readable_sync(
                self._query_memories_sync(
                    projection=["id", "emotion", "category", "timestamp", SEALED_VERSION_ATTRIBUTE]
                )
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
            dek: bytes | None = None
            body = attrs
            if self._shredder is not None:
                # 鍵を先に置く（本体だけ残って鍵が無い、は「読めない」側なので安全）
                dek = self._shredder.new_key(memory.id)
                body = self._seal_memory_attrs(memory.id, attrs, dek)
            self._transact_write_sync(
                [
                    {"Put": {**self._key(memory_sk), "entity": "memory", **body}},
                    {"Put": self._vector_item_sync(memory.id, record.vector, dek)},
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
            if any(name in SECRET_MEMORY_ATTRIBUTES for name in fields):
                # 本文類を含む書き換えは、暗号化された行なら復号→差し替え→再暗号化
                item = self._get_item_sync(sk)
                if item is None:
                    return False
            else:
                item = {"id": memory_id, "sk": sk}
            self._write_secret_fields_sync(item, dict(fields))
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

            # K20: 先に鍵を消す。ここから先で落ちても、残った暗号文は誰にも読めない
            if self._shredder is not None:
                self._shredder.shred(memory_id)

            # 他の記憶の linked_ids / links から消す（links は暗号化されているので開いて見る）
            raw_items = {str(raw["id"]): raw for raw in self._query_memories_sync()}
            for item in self._open_memory_items_sync(list(raw_items.values())):
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
                # 封をしたままの行を渡す（暗号化された行なら links を再暗号化して書く）
                self._write_secret_fields_sync(raw_items[str(item["id"])], updates)

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
            items = self._readable_sync(
                self._query_memories_sync(projection=["id", "indexed", SEALED_VERSION_ATTRIBUTE])
            )
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
                        # 錨 1 件＋忘れた記憶を戻したもの（鍵が無い）を読み飛ばす余裕
                        limit=None if self._shredder is not None else 2,
                    )
                    for item in self._readable_sync(items):
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
                opened = self._open_memory_items_sync([item])
                if not opened:
                    return None
                return decode_memory(_plain(opened[0]), coactivation.get(item["id"], ()))

            return (_decode(previous_item), _decode(next_item))

        return await asyncio.to_thread(_fetch)

    # ── ベクトル ────────────────────────────────

    async def fetch_vectors(self, memory_ids: list[str]) -> dict[str, bytes]:
        if not memory_ids:
            return {}

        def _fetch() -> dict[str, bytes]:
            items = self._batch_get_sync([self.vector_sk(mid) for mid in memory_ids])
            return self._open_vectors_sync(items)

        return await asyncio.to_thread(_fetch)

    async def fetch_all_vectors(self) -> list[VectorRow]:
        def _fetch() -> list[VectorRow]:
            vector_items = self._query_prefix_sync(VECTOR_PREFIX)
            # 段2: index:false は Hopfield の母集団にも載せない
            memory_items = [
                item
                for item in self._query_memories_sync(
                    projection=["id", "normalized_content", "indexed", SEALED_ATTRIBUTE, SEALED_VERSION_ATTRIBUTE]
                )
                if _is_indexed(item)
            ]
            contents = {
                item["id"]: item.get("normalized_content", "")
                for item in self._open_memory_items_sync(memory_items)
            }
            vectors = self._open_vectors_sync([i for i in vector_items if i["memory_id"] in contents])
            rows: list[VectorRow] = []
            for memory_id, vector in vectors.items():
                rows.append(
                    VectorRow(
                        memory_id=memory_id,
                        vector=vector,
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
