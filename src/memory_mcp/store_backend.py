"""保管層の抽象（段0: DynamoDB 移行）。

「行を出し入れする」ところだけをこの Protocol に集める。
類似度・減衰・感情ブースト・再ランク・探索といった計算は
`MemoryStore`（store.py）側に残す。

実装:
- `SqliteMemoryStore` (sqlite_backend.py) — 既存の SQLite + numpy 実装
- `DynamoMemoryStore` (dynamo_backend.py) — 段2 以降で中身を入れる骨組み
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .config import MemoryConfig
from .types import Episode, Memory

# `update_memory_fields` で書き換えてよい属性。保管先が変わっても同じ名前を使う。
UPDATABLE_FIELDS: frozenset[str] = frozenset(
    {
        "access_count",
        "last_accessed",
        "linked_ids",
        "episode_id",
        "sensory_data",
        "camera_position",
        "tags",
        "links",
        "novelty_score",
        "prediction_error",
        "activation_count",
        "last_activated",
        "reading",
        "importance",
    }
)


@dataclass(frozen=True)
class MemoryRecord:
    """1 件の記憶を保管するための素材。

    `memory` が記憶そのもの、残りは検索のために一緒に保管する派生値。
    埋め込みの生成（計算）は呼び出し側で済ませてから渡す。
    """

    memory: Memory
    normalized_content: str
    reading: str | None
    vector: bytes


@dataclass(frozen=True)
class MemoryWithVector:
    """記憶と、その埋め込みベクトル（raw bytes）の組。"""

    memory: Memory
    vector: bytes


@dataclass(frozen=True)
class VectorRow:
    """Hopfield へ積むための 1 行。"""

    memory_id: str
    vector: bytes
    normalized_content: str


@dataclass(frozen=True)
class MemoryFacets:
    """統計を数えるための素材。数え上げ自体は呼び出し側で行う。"""

    rows: tuple[tuple[str, str, str], ...]  # (emotion, category, timestamp)
    oldest_timestamp: str | None
    newest_timestamp: str | None


@runtime_checkable
class MemoryStoreBackend(Protocol):
    """記憶の保管層。行を取る・入れる・直す・消す・一覧するだけ。

    スコアや距離は返さない。並べ替えも、保管先が自然に持っている順序
    （timestamp 降順など）以外はしない。
    """

    # ── 接続 ────────────────────────────────────

    async def connect(self) -> None:
        """保管先に接続する（必要なら初期化も）。"""
        ...

    async def disconnect(self) -> None:
        """接続を閉じる。"""
        ...

    # ── 記憶: 取る ──────────────────────────────

    async def fetch_memory(self, memory_id: str) -> Memory | None:
        """ID で 1 件取る。無ければ None。"""
        ...

    async def fetch_memories(self, memory_ids: list[str]) -> list[Memory]:
        """ID の並びで複数件取る（順序は保証しない）。"""
        ...

    async def fetch_all_memories(self) -> list[Memory]:
        """全件取る。"""
        ...

    async def fetch_memories_with_vectors(
        self,
        emotion: str | None = None,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[MemoryWithVector]:
        """絞り込み条件に合う記憶を、埋め込みベクトル付きで取る。

        類似度の計算と上位 n 件の切り出しは呼び出し側の仕事。
        """
        ...

    async def fetch_recent_memories(self, limit: int, category: str | None = None) -> list[Memory]:
        """新しい順に limit 件取る。"""
        ...

    async def fetch_important_memories(
        self,
        min_importance: int,
        min_access_count: int,
        since: str | None,
        limit: int,
    ) -> list[Memory]:
        """重要度とアクセス回数の下限で絞り、最終アクセスの新しい順に取る。"""
        ...

    async def fetch_memory_facets(self) -> MemoryFacets:
        """統計用に (emotion, category, timestamp) の一覧と最古・最新を取る。"""
        ...

    # ── 記憶: 入れる・直す・消す ────────────────

    async def insert_memory(self, record: MemoryRecord) -> None:
        """記憶とベクトルを 1 組で保管する。"""
        ...

    async def update_memory_fields(self, memory_id: str, fields: dict[str, Any]) -> bool:
        """属性を書き換える。対象が無ければ False。

        `fields` のキーは `UPDATABLE_FIELDS` に絞ってから渡すこと。
        """
        ...

    async def update_episode_id(self, memory_id: str, episode_id: str | None) -> bool:
        """所属エピソードを付け替える。対象が無ければ False。"""
        ...

    async def increment_access(self, memory_id: str, last_accessed: str) -> None:
        """アクセス回数を 1 増やし、最終アクセス時刻を更新する。"""
        ...

    async def delete_memory(self, memory_id: str) -> bool:
        """記憶を消し、他の記憶からの逆参照も掃除する。無ければ False。"""
        ...

    async def add_bidirectional_link(self, source_id: str, target_id: str) -> None:
        """双方の `linked_ids` に相手を足す（既にあれば何もしない）。"""
        ...

    # ── ベクトル ────────────────────────────────

    async def fetch_vectors(self, memory_ids: list[str]) -> dict[str, bytes]:
        """指定 ID の埋め込みベクトルだけを取る。"""
        ...

    async def fetch_all_vectors(self) -> list[VectorRow]:
        """全件の (id, ベクトル, 正規化済み本文) を取る。"""
        ...

    # ── 共活性 ──────────────────────────────────

    async def fetch_coactivation(self, memory_id: str) -> tuple[tuple[str, float], ...]:
        """ある記憶から見た共活性の重みを全部取る。"""
        ...

    async def fetch_coactivation_weight(self, source_id: str, target_id: str) -> float | None:
        """1 方向ぶんの重みを取る。無ければ None。"""
        ...

    async def put_coactivation(self, source_id: str, target_id: str, weight: float) -> None:
        """1 方向ぶんの重みを書く（upsert）。対称化は呼び出し側で行う。"""
        ...

    # ── エピソード ──────────────────────────────

    async def insert_episode(self, episode: Episode) -> None:
        """エピソードを保管する。"""
        ...

    async def fetch_episode(self, episode_id: str) -> Episode | None:
        """ID で 1 件取る。無ければ None。"""
        ...

    async def search_episodes(self, query: str, limit: int) -> list[Episode]:
        """タイトル・要約の部分一致で探し、開始時刻の新しい順に返す。"""
        ...

    async def fetch_all_episodes(self) -> list[Episode]:
        """全エピソードを開始時刻の新しい順に取る。"""
        ...

    async def delete_episode(self, episode_id: str) -> None:
        """エピソードを消す（記憶は消さない）。"""
        ...


def create_backend(config: MemoryConfig) -> MemoryStoreBackend:
    """`config.store_backend` で保管層の実装を選ぶ。

    `PETIT_MEMORY_STORE=sqlite|dynamo|dual`（既定 sqlite）。

    - `sqlite` — 家コンテナ内の SQLite ファイル
    - `dynamo` — DynamoDB の単一表だけ
    - `dual`   — 段1 の片流し複製。書きは SQLite と DynamoDB の両方、読みは SQLite
    """
    name = (config.store_backend or "sqlite").strip().lower()
    if name == "sqlite":
        from .sqlite_backend import SqliteMemoryStore

        return SqliteMemoryStore(config)
    if name == "dynamo":
        from .dynamo_backend import DynamoMemoryStore

        return DynamoMemoryStore(config)
    if name == "dual":
        from .dual_backend import DualWriteMemoryStore
        from .dynamo_backend import DynamoMemoryStore
        from .sqlite_backend import SqliteMemoryStore

        return DualWriteMemoryStore(
            primary=SqliteMemoryStore(config),
            secondary=DynamoMemoryStore(config),
        )
    raise ValueError(
        f"Unknown PETIT_MEMORY_STORE: {config.store_backend!r} (expected 'sqlite', 'dynamo' or 'dual')"
    )
