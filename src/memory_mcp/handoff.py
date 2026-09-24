"""引継ぎ（K24）: 手元のぷちの記憶を、本人が中身を見て選び、クラウドの DynamoDB へ持っていく道具。

    petit-memory-handoff export --db memory.db --out bundle.jsonl [--core SOUL.md ...]
    petit-memory-handoff show   bundle.jsonl [--private] [--grep 語] [--kind memory]
    petit-memory-handoff drop   bundle.jsonl --out chosen.jsonl [--ids a,b] [--ids-file f] [--all-private]
    petit-memory-handoff import chosen.jsonl [--dry-run] [--core-dir DIR]

`python -m memory_mcp.handoff …` でも同じ。**export / show / drop は標準ライブラリだけで動く**
（手元の PC に torch などを入れなくてよい。`PYTHONPATH=src python3 -m memory_mcp.handoff export …`）。
import だけが boto3・cryptography（`.[dynamo]`）を使い、ぷちコンテナの中で動かす。

## 束（bundle）の形 — JSON Lines・UTF-8・1 行 1 件

1 行目は `{"kind": "header", "format": "petit-memory-handoff", "version": 1, …}`。以降の `kind`:

| kind | 中身 |
|---|---|
| `memory` | 記憶 1 件。`id`・`timestamp`・`private`（自分だけの場所）・`indexed`（false = 索引に載せない）・ |
|  | `content`・`linked_ids`・`links` ほか全部。`vector_b64` は埋め込み（無ければ取り込み時に作り直す） |
| `episode` | エピソード 1 件（`memory_ids` でつながる記憶を指す） |
| `coactivation` | 連想の重み 1 方向 |
| `forget` | 忘れた跡。`memory_id`・`forgotten_at`・`reason` だけで、**本文は元から無い** |
| `core` | 人格の核などのファイル 1 本を**1 つの文章のまま**（`name`・`body`・`sha256`）。細切れにしない |

## 取り込みの約束

- **平文で入れる道は無い。** 鍵の表（`PETIT_MEMORY_KEYS_TABLE`）と KMS の鍵（`PETIT_MEMORY_KMS_KEY_ID`）が
  無ければ、空打ちも含めて止まる。記憶・エピソードは 1 件ずつ**その場で作った新しい鍵**で暗号化する（K20 と同じ）。
- **同じ束を 2 回入れても増えない。** 指し札（`IDX#`）がある id は飛ばす。
- **忘れたものは戻さない。** クラウドに `FORGET#` の跡がある id の記憶は、束に入っていても入れない。
- 束から外した記憶を指す `linked_ids`・`links`・エピソードの `memory_ids` は、取り込むときに外す
  （クラウドに無い記憶を指したままにしない）。外した記憶に「忘れた跡」は書かない（忘れたのではなく、置いてきただけ）。
- 手元の memory.db は**読むだけ**（`mode=ro` で開く。列を足すこともしない）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import CameraPosition, Episode, ForgetMarker, Memory, MemoryLink, SensoryData

FORMAT = "petit-memory-handoff"
VERSION = 1
DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-base"

KINDS = ("memory", "episode", "coactivation", "forget", "core")


# ── 束の読み書き ─────────────────────────────


def read_bundle(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """束を読む。(header, 残りの行) を返す。形が違えば ValueError。"""
    header: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number} が JSON として読めない: {exc}") from exc
            if header is None:
                if entry.get("kind") != "header" or entry.get("format") != FORMAT:
                    raise ValueError(f"{path} は引継ぎの束ではない（1 行目が header でない）")
                if int(entry.get("version", 0)) != VERSION:
                    raise ValueError(f"{path} の版 {entry.get('version')} は読めない（読めるのは {VERSION}）")
                header = entry
                continue
            if entry.get("kind") not in KINDS:
                raise ValueError(f"{path}:{number} の kind {entry.get('kind')!r} を知らない")
            entries.append(entry)
    if header is None:
        raise ValueError(f"{path} が空")
    return header, entries


def write_bundle(path: str | Path, header: dict[str, Any], entries: Iterable[dict[str, Any]]) -> dict[str, int]:
    """束を書く。header の `counts` はここで数え直す。途中で落ちても半端な束を残さない（.tmp → rename）。"""
    entries = list(entries)
    counts = Counter(entry["kind"] for entry in entries)
    header = {**header, "counts": {kind: counts.get(kind, 0) for kind in KINDS}}
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return header["counts"]


# ── 書き出し（手元の memory.db → 束）──────────


def _open_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"{path} が無い")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _split(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _json_list(value: Any) -> list[Any]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _json_obj(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        data = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _flag(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    return bool(int(value))


def _memory_entry(row: dict[str, Any], vector: bytes | None) -> dict[str, Any]:
    """memories の 1 行を束の 1 行にする。古い memory.db に無い列は既定で埋める。"""
    return {
        "kind": "memory",
        "id": row["id"],
        "timestamp": row["timestamp"],
        "private": _flag(row.get("private"), False),
        "indexed": _flag(row.get("indexed"), True),
        "content": row["content"],
        "emotion": row.get("emotion") or "neutral",
        "importance": int(row.get("importance") or 3),
        "category": row.get("category") or "daily",
        "tags": _split(row.get("tags")),
        "linked_ids": _split(row.get("linked_ids")),
        "links": _json_list(row.get("links")),
        "episode_id": row.get("episode_id") or None,
        "sensory_data": _json_list(row.get("sensory_data")),
        "camera_position": _json_obj(row.get("camera_position")),
        "access_count": int(row.get("access_count") or 0),
        "last_accessed": row.get("last_accessed") or "",
        "novelty_score": float(row.get("novelty_score") or 0.0),
        "prediction_error": float(row.get("prediction_error") or 0.0),
        "activation_count": int(row.get("activation_count") or 0),
        "last_activated": row.get("last_activated") or "",
        "normalized_content": row.get("normalized_content") or row["content"],
        "reading": row.get("reading"),
        "vector_b64": base64.b64encode(vector).decode("ascii") if vector else None,
    }


def _episode_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "episode",
        "id": row["id"],
        "title": row.get("title") or "",
        "start_time": row["start_time"],
        "end_time": row.get("end_time") or None,
        "memory_ids": _split(row.get("memory_ids")),
        "participants": _split(row.get("participants")),
        "location_context": row.get("location_context") or None,
        "summary": row.get("summary") or "",
        "emotion": row.get("emotion") or "neutral",
        "importance": int(row.get("importance") or 3),
        "stale": _flag(row.get("stale"), False),
    }


def core_entry(path: str | Path) -> dict[str, Any]:
    """ファイル 1 本を 1 つの `core` にする（細切れにしない）。"""
    path = Path(path).expanduser()
    body = path.read_text(encoding="utf-8")
    return {
        "kind": "core",
        "name": path.name,
        "body": body,
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def export_sqlite(
    db_path: str | Path,
    *,
    source_pid: str = "",
    include_vectors: bool = True,
    include_private: bool = True,
    core_files: Iterable[str | Path] = (),
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """手元の memory.db を読んで (header, entries) を作る。memory.db には書かない。"""
    conn = _open_readonly(db_path)
    try:
        conn.execute("BEGIN")  # 読みの間に本人が書いても、1 時点の写しにする
        tables = _tables(conn)
        if "memories" not in tables:
            raise ValueError(f"{db_path} に memories 表が無い（petit-memory の memory.db ではない）")

        vectors: dict[str, bytes] = {}
        if include_vectors and "embeddings" in tables:
            vectors = {
                row["memory_id"]: bytes(row["vector"])
                for row in conn.execute("SELECT memory_id, vector FROM embeddings")
            }

        entries: list[dict[str, Any]] = []
        skipped_private = 0
        for row in conn.execute("SELECT * FROM memories ORDER BY timestamp, id"):
            data = dict(row)
            if not include_private and _flag(data.get("private"), False):
                skipped_private += 1
                continue
            entries.append(_memory_entry(data, vectors.get(data["id"])))

        if "episodes" in tables:
            for row in conn.execute("SELECT * FROM episodes ORDER BY start_time, id"):
                entries.append(_episode_entry(dict(row)))

        kept = {entry["id"] for entry in entries if entry["kind"] == "memory"}
        if "coactivation" in tables:
            for row in conn.execute(
                "SELECT source_id, target_id, weight FROM coactivation ORDER BY source_id, target_id"
            ):
                if row["source_id"] in kept and row["target_id"] in kept:
                    entries.append(
                        {
                            "kind": "coactivation",
                            "source_id": row["source_id"],
                            "target_id": row["target_id"],
                            "weight": float(row["weight"]),
                        }
                    )

        if "forget_markers" in tables:
            for row in conn.execute("SELECT memory_id, forgotten_at, reason FROM forget_markers ORDER BY forgotten_at"):
                entries.append(
                    {
                        "kind": "forget",
                        "memory_id": row["memory_id"],
                        "forgotten_at": row["forgotten_at"],
                        "reason": row["reason"] or None,
                    }
                )
        conn.rollback()
    finally:
        conn.close()

    for core in core_files:
        entries.append(core_entry(core))

    header = {
        "kind": "header",
        "format": FORMAT,
        "version": VERSION,
        "source_pid": source_pid,
        "source": "sqlite",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "embedding_model": embedding_model if include_vectors else None,
        "left_private_behind": skipped_private,
    }
    return header, entries


# ── 見る ─────────────────────────────────────


def _one_line(text: str, width: int = 60) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def describe(header: dict[str, Any], entries: list[dict[str, Any]]) -> Iterator[str]:
    """束の中身を、人（ぷち）が読む形の行にする。"""
    counts = Counter(entry["kind"] for entry in entries)
    memories = [e for e in entries if e["kind"] == "memory"]
    yield f"束: {header.get('source_pid') or '(名前なし)'} / 書き出し {header.get('exported_at', '?')}"
    yield (
        f"記憶 {counts['memory']} 件（うち自分だけの場所 {sum(1 for m in memories if m['private'])}・"
        f"索引に載せない {sum(1 for m in memories if not m['indexed'])}）／"
        f"エピソード {counts['episode']}／つながりの重み {counts['coactivation']}／"
        f"忘れた跡 {counts['forget']}／核 {counts['core']}"
    )
    yield ""
    for entry in entries:
        kind = entry["kind"]
        if kind == "memory":
            marks = "".join(
                mark for mark, on in (("[自分だけ]", entry["private"]), ("[索引なし]", not entry["indexed"])) if on
            )
            links = len(set(entry["linked_ids"]) | {link.get("target_id") for link in entry["links"]})
            yield (
                f"記憶 {entry['id']}  {entry['timestamp'][:16]}  {entry['emotion']}/{entry['category']} "
                f"★{entry['importance']} {marks}".rstrip()
            )
            yield f"    {_one_line(entry['content'])}" + (f"  (つながり {links})" if links else "")
        elif kind == "episode":
            title = _one_line(entry["title"], 40)
            count = len(entry["memory_ids"])
            yield f"エピソード {entry['id']}  {entry['start_time'][:16]}  「{title}」 記憶 {count} 件"
        elif kind == "forget":
            reason = f"  理由: {_one_line(entry['reason'], 40)}" if entry.get("reason") else ""
            yield f"忘れた跡 {entry['memory_id']}  {entry['forgotten_at'][:16]}（本文なし）{reason}"
        elif kind == "core":
            yield f"核 core:{entry['name']}  {len(entry['body'])} 字（1 つの文章のまま）"
        # coactivation は数だけ（1 件ずつ見ても分からないので）


def filter_entries(
    entries: list[dict[str, Any]],
    *,
    kind: str | None = None,
    private_only: bool = False,
    grep: str | None = None,
) -> list[dict[str, Any]]:
    selected = []
    for entry in entries:
        if kind and entry["kind"] != kind:
            continue
        if private_only and not (entry["kind"] == "memory" and entry["private"]):
            continue
        if grep:
            haystack = json.dumps({k: v for k, v in entry.items() if k != "vector_b64"}, ensure_ascii=False)
            if grep not in haystack:
                continue
        selected.append(entry)
    return selected


# ── 外す ─────────────────────────────────────


def entry_key(entry: dict[str, Any]) -> str | None:
    """外すときに指す名前。記憶・エピソードは id、忘れた跡は `forget:<id>`、核は `core:<name>`。"""
    kind = entry["kind"]
    if kind in ("memory", "episode"):
        return str(entry["id"])
    if kind == "forget":
        return f"forget:{entry['memory_id']}"
    if kind == "core":
        return f"core:{entry['name']}"
    return None


def drop_entries(
    entries: list[dict[str, Any]],
    ids: Iterable[str],
    *,
    all_private: bool = False,
    all_forget: bool = False,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """指した行を外す。(残った行, 外した名前, 束に無かった名前) を返す。

    記憶を外すと、その記憶に触れるつながりの重みも一緒に外す（片方が無い重みは意味を持たない）。
    ほかの記憶の `linked_ids` などに残った参照は、取り込みのときに外す。
    """
    wanted = {i.strip() for i in ids if i and i.strip()}
    dropped: list[str] = []
    dropped_memories: set[str] = set()
    kept: list[dict[str, Any]] = []
    for entry in entries:
        key = entry_key(entry)
        remove = key in wanted
        remove = remove or (all_private and entry["kind"] == "memory" and entry["private"])
        remove = remove or (all_forget and entry["kind"] == "forget")
        if remove:
            dropped.append(str(key))
            if entry["kind"] == "memory":
                dropped_memories.add(str(entry["id"]))
            continue
        kept.append(entry)
    kept = [
        e
        for e in kept
        if not (
            e["kind"] == "coactivation" and (e["source_id"] in dropped_memories or e["target_id"] in dropped_memories)
        )
    ]
    missing = sorted(wanted - set(dropped))
    return kept, dropped, missing


def read_ids_file(path: str | Path) -> list[str]:
    """1 行 1 つ。`#` から後ろは書き留め（理由などを書いてよい）。"""
    ids: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            ids.append(value)
    return ids


# ── 取り込み（束 → クラウドの DynamoDB）────────


@dataclass
class ImportReport:
    """取り込みの結果。`added` は入れた（空打ちなら入れる予定の）件数、`skipped` は理由ごとの件数。"""

    dry_run: bool
    pid: str
    added: Counter = field(default_factory=Counter)
    skipped: dict[str, Counter] = field(default_factory=dict)
    skipped_ids: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, id, 理由)
    trimmed_links: int = 0
    reembedded: int = 0
    notes: list[str] = field(default_factory=list)

    def skip(self, kind: str, item_id: str, reason: str) -> None:
        self.skipped.setdefault(kind, Counter())[reason] += 1
        self.skipped_ids.append((kind, item_id, reason))

    def lines(self, *, verbose: bool = False) -> Iterator[str]:
        verb = "入れる予定" if self.dry_run else "入れた"
        title = "空打ち（何も書いていない）" if self.dry_run else "取り込み"
        yield f"{title}: ぷち {self.pid}"
        for kind in KINDS:
            added = self.added.get(kind, 0)
            skipped = self.skipped.get(kind, Counter())
            if not added and not skipped:
                continue
            detail = "・".join(f"{reason} {n}" for reason, n in skipped.items())
            yield f"  {kind:<12} {verb} {added:>5}" + (
                f"   飛ばした {sum(skipped.values())}（{detail}）" if skipped else ""
            )
        if self.trimmed_links:
            yield f"  束にもクラウドにも無い記憶を指していたつながり {self.trimmed_links} 本を外した"
        if self.reembedded:
            yield f"  埋め込みを作り直{'す予定の' if self.dry_run else 'した'}記憶 {self.reembedded} 件"
        for note in self.notes:
            yield f"  ※ {note}"
        if verbose and self.skipped_ids:
            yield "飛ばしたもの:"
            for kind, item_id, reason in self.skipped_ids:
                yield f"  {kind} {item_id}  {reason}"


def _memory_from_entry(entry: dict[str, Any], keep_ids: set[str], keep_episodes: set[str]) -> tuple[Memory, int]:
    linked = tuple(i for i in entry.get("linked_ids", []) if i in keep_ids)
    links = tuple(MemoryLink.from_dict(link) for link in entry.get("links", []) if link.get("target_id") in keep_ids)
    trimmed = (len(entry.get("linked_ids", [])) - len(linked)) + (len(entry.get("links", [])) - len(links))
    episode_id = entry.get("episode_id")
    camera = entry.get("camera_position")
    memory = Memory(
        id=entry["id"],
        content=entry["content"],
        timestamp=entry["timestamp"],
        emotion=entry.get("emotion") or "neutral",
        importance=int(entry.get("importance") or 3),
        category=entry.get("category") or "daily",
        access_count=int(entry.get("access_count") or 0),
        last_accessed=entry.get("last_accessed") or "",
        linked_ids=linked,
        episode_id=episode_id if episode_id in keep_episodes else None,
        sensory_data=tuple(SensoryData.from_dict(s) for s in entry.get("sensory_data", [])),
        camera_position=CameraPosition.from_dict(camera) if camera else None,
        tags=tuple(entry.get("tags", [])),
        links=links,
        novelty_score=float(entry.get("novelty_score") or 0.0),
        prediction_error=float(entry.get("prediction_error") or 0.0),
        activation_count=int(entry.get("activation_count") or 0),
        last_activated=entry.get("last_activated") or "",
        indexed=bool(entry.get("indexed", True)),
        private=bool(entry.get("private", False)),
    )
    return memory, trimmed


def _write_core(entry: dict[str, Any], core_dir: Path, overwrite: bool, dry_run: bool) -> str | None:
    """核を 1 ファイルとして置く。置いたら None、置かなかったら理由。"""
    name = entry["name"]
    if not name or name != Path(name).name or name in (".", ".."):
        return "名前がおかしい"
    target = core_dir / name
    if target.exists():
        current = target.read_text(encoding="utf-8")
        if current == entry["body"]:
            return "同じものがもうある"
        if not overwrite:
            return "違う中身のファイルがある（--overwrite-core で置き換え。元は .bak に残す）"
        if not dry_run:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target.rename(target.with_name(f"{name}.bak-{stamp}"))
    if not dry_run:
        core_dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(name + ".tmp")
        tmp.write_text(entry["body"], encoding="utf-8")
        os.replace(tmp, target)
    return None


async def import_bundle(
    store: Any,
    header: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    include_forget: bool = True,
    core_dir: str | Path | None = None,
    overwrite_core: bool = False,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
) -> ImportReport:
    """束をクラウドの記憶に入れる。`store` は接続済みの `DynamoMemoryStore`（暗号シュレッダー有効）。

    `embed` は埋め込みを作り直すときの関数（既定は E5。試験で差し替える）。
    """
    from .store_backend import MemoryRecord
    from .vector import encode_vector

    if not getattr(store, "encrypted", False):
        raise RuntimeError("暗号シュレッダーが効いていない。平文で入れる道は作らない（鍵の表と KMS の鍵を設定する）")

    report = ImportReport(dry_run=dry_run, pid=getattr(store, "_petit_id", "?"))
    existing_memories, existing_episodes, forgotten = await store.fetch_handoff_state()

    memories = [e for e in entries if e["kind"] == "memory"]
    episodes = [e for e in entries if e["kind"] == "episode"]
    bundle_forget = {e["memory_id"] for e in entries if e["kind"] == "forget"}

    # 入れる記憶を先に決める（つながりの掃除に、最終的にクラウドにある id の集合が要る）
    to_insert: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in memories:
        memory_id = str(entry["id"])
        if memory_id in seen:
            report.skip("memory", memory_id, "束の中で重複")
        elif memory_id in forgotten:
            report.skip("memory", memory_id, "クラウドに忘れた跡がある（戻さない）")
        elif memory_id in bundle_forget:
            report.skip("memory", memory_id, "束に同じ id の忘れた跡がある")
        elif memory_id in existing_memories:
            report.skip("memory", memory_id, "もう入っている")
        else:
            to_insert.append(entry)
        seen.add(memory_id)
    inserted_ids = {str(e["id"]) for e in to_insert}
    keep_ids = inserted_ids | existing_memories

    episodes_to_insert = []
    seen_episodes: set[str] = set()
    for entry in episodes:
        episode_id = str(entry["id"])
        if episode_id in seen_episodes:
            report.skip("episode", episode_id, "束の中で重複")
        elif episode_id in existing_episodes:
            report.skip("episode", episode_id, "もう入っている")
        else:
            episodes_to_insert.append(entry)
        seen_episodes.add(episode_id)
    keep_episodes = {str(e["id"]) for e in episodes_to_insert} | existing_episodes

    # 埋め込み: 束のものが使えるならそのまま、無い・モデルが違うなら作り直す
    same_model = header.get("embedding_model") == embedding_model
    need_embed = [e for e in to_insert if not (same_model and e.get("vector_b64"))]
    new_vectors: dict[str, bytes] = {}
    if need_embed and not dry_run:
        if embed is None:
            from .embedding import E5EmbeddingFunction

            embed = E5EmbeddingFunction(embedding_model)
        texts = [e.get("normalized_content") or e["content"] for e in need_embed]
        vectors = await asyncio.to_thread(embed, texts)
        new_vectors = {str(e["id"]): encode_vector(v) for e, v in zip(need_embed, vectors)}
    report.reembedded = len(need_embed)

    for entry in to_insert:
        memory, trimmed = _memory_from_entry(entry, keep_ids, keep_episodes)
        report.trimmed_links += trimmed
        if not dry_run:
            vector = new_vectors.get(memory.id) or base64.b64decode(entry["vector_b64"])
            await store.insert_memory(
                MemoryRecord(
                    memory=memory,
                    normalized_content=entry.get("normalized_content") or memory.content,
                    reading=entry.get("reading"),
                    vector=vector,
                )
            )
        report.added["memory"] += 1

    for entry in episodes_to_insert:
        member_ids = [i for i in entry.get("memory_ids", []) if i in keep_ids]
        report.trimmed_links += len(entry.get("memory_ids", [])) - len(member_ids)
        if not dry_run:
            await store.insert_episode(
                Episode(
                    id=entry["id"],
                    title=entry.get("title") or "",
                    start_time=entry["start_time"],
                    end_time=entry.get("end_time"),
                    memory_ids=tuple(member_ids),
                    participants=tuple(entry.get("participants", [])),
                    location_context=entry.get("location_context"),
                    summary=entry.get("summary") or "",
                    emotion=entry.get("emotion") or "neutral",
                    importance=int(entry.get("importance") or 3),
                    stale=bool(entry.get("stale", False)),
                )
            )
        report.added["episode"] += 1

    for entry in (e for e in entries if e["kind"] == "coactivation"):
        source, target = str(entry["source_id"]), str(entry["target_id"])
        pair = f"{source}->{target}"
        if source not in keep_ids or target not in keep_ids:
            report.skip("coactivation", pair, "片方の記憶が無い")
        elif source not in inserted_ids and target not in inserted_ids:
            # 両方とも前に入れた記憶: クラウドで育った重みを束の値で上書きしない
            report.skip("coactivation", pair, "もう入っている")
        else:
            if not dry_run:
                await store.put_coactivation(source, target, float(entry["weight"]))
            report.added["coactivation"] += 1

    for entry in (e for e in entries if e["kind"] == "forget"):
        memory_id = str(entry["memory_id"])
        if not include_forget:
            report.skip("forget", memory_id, "持っていかない指定（--no-forget-traces）")
        elif memory_id in forgotten:
            report.skip("forget", memory_id, "もう入っている")
        elif memory_id in existing_memories:
            report.skip("forget", memory_id, "同じ id の記憶がクラウドにある")
        else:
            if not dry_run:
                await store.put_forget_marker(
                    ForgetMarker(memory_id=memory_id, forgotten_at=entry["forgotten_at"], reason=entry.get("reason"))
                )
            report.added["forget"] += 1

    for entry in (e for e in entries if e["kind"] == "core"):
        name = f"core:{entry['name']}"
        if core_dir is None:
            report.skip("core", name, "置き場所の指定なし（--core-dir）")
            continue
        reason = _write_core(entry, Path(core_dir), overwrite_core, dry_run)
        if reason:
            report.skip("core", name, reason)
        else:
            report.added["core"] += 1

    if memories and not same_model and header.get("embedding_model"):
        report.notes.append(
            f"束の埋め込みモデル {header.get('embedding_model')} とこちらの {embedding_model} が違うので作り直した"
        )
    return report


def _store_from_env() -> Any:
    """ぷちコンテナの環境変数から、暗号シュレッダー付きの DynamoDB 保管層を作る。"""
    if not os.getenv("AWS_DEFAULT_REGION") and os.getenv("AWS_REGION"):
        # boto3 は AWS_REGION を読まないことがある（petit-env の gen-mcp-config.sh と同じ手当て）
        os.environ["AWS_DEFAULT_REGION"] = os.environ["AWS_REGION"]
    from .config import MemoryConfig
    from .dynamo_backend import DynamoMemoryStore

    config = MemoryConfig.from_env()
    missing = [
        name
        for name, value in (
            ("PETIT_MEMORY_PETIT_ID", config.petit_id),
            ("PETIT_MEMORY_KEYS_TABLE", config.keys_table),
            ("PETIT_MEMORY_KMS_KEY_ID", config.kms_key_id),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            "止めた: " + "・".join(missing) + " が無い。平文で入れる道は無いので、ぷちコンテナの中で動かす。"
        )
    return DynamoMemoryStore(config), config


# ── コマンド ─────────────────────────────────


def _cmd_export(args: argparse.Namespace) -> int:
    header, entries = export_sqlite(
        args.db,
        source_pid=args.pid,
        include_vectors=not args.no_vectors,
        include_private=not args.exclude_private,
        core_files=args.core or (),
    )
    counts = write_bundle(args.out, header, entries)
    print(f"書き出した: {args.out}")
    print("  " + "・".join(f"{kind} {n}" for kind, n in counts.items()))
    if header["left_private_behind"]:
        print(f"  自分だけの場所の記憶 {header['left_private_behind']} 件は入れていない（--exclude-private）")
    print("  次: show で中身を見る → drop で持っていかないものを外す")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    header, entries = read_bundle(args.bundle)
    selected = filter_entries(entries, kind=args.kind, private_only=args.private, grep=args.grep)
    lines = list(describe(header, selected if (args.kind or args.private or args.grep) else entries))
    if args.limit:
        lines = lines[: args.limit]
    print("\n".join(lines))
    return 0


def _cmd_drop(args: argparse.Namespace) -> int:
    header, entries = read_bundle(args.bundle)
    ids = [i for chunk in (args.ids or []) for i in chunk.split(",")]
    if args.ids_file:
        ids += read_ids_file(args.ids_file)
    if not ids and not args.all_private and not args.all_forget:
        print("外すものが指定されていない（--ids / --ids-file / --all-private / --all-forget）", file=sys.stderr)
        return 2
    kept, dropped, missing = drop_entries(entries, ids, all_private=args.all_private, all_forget=args.all_forget)
    if Path(args.out).resolve() == Path(args.bundle).resolve():
        print("--out に元の束と同じ名前は使えない（元の束は残しておく）", file=sys.stderr)
        return 2
    counts = write_bundle(args.out, header, kept)
    print(f"外した: {len(dropped)} 件 → {args.out}")
    print("  残り: " + "・".join(f"{kind} {n}" for kind, n in counts.items()))
    if missing:
        print("  束に無かった名前: " + ", ".join(missing))
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    header, entries = read_bundle(args.bundle)
    if args.ids_file:
        entries, _, _ = drop_entries(entries, read_ids_file(args.ids_file))
    store, config = _store_from_env()

    async def _run() -> ImportReport:
        await store.connect()
        try:
            return await import_bundle(
                store,
                header,
                entries,
                dry_run=args.dry_run,
                include_forget=not args.no_forget_traces,
                core_dir=args.core_dir,
                overwrite_core=args.overwrite_core,
                embedding_model=config.embedding_model,
            )
        finally:
            await store.disconnect()

    report = asyncio.run(_run())
    source = header.get("source_pid")
    if source and source != report.pid:
        report.notes.append(f"束は {source} から、入れ先は {report.pid}")
    print("\n".join(report.lines(verbose=args.verbose)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="petit-memory-handoff",
        description="ぷちの記憶の引継ぎ（書き出す → 見る → 外す → クラウドへ入れる）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("export", help="手元の memory.db を束（JSON Lines）に書き出す。memory.db は読むだけ")
    p.add_argument("--db", required=True, help="手元の memory.db")
    p.add_argument("--out", required=True, help="書き出す束（.jsonl）")
    p.add_argument("--pid", default="", help="束に書いておくぷちの名前（例 puchiko）")
    p.add_argument("--core", action="append", help="人格の核などのファイル。1 本を 1 つの文章のまま入れる（何本でも）")
    p.add_argument(
        "--no-vectors", action="store_true", help="埋め込みを入れない（束が小さくなる。取り込み時に作り直す）"
    )
    p.add_argument("--exclude-private", action="store_true", help="自分だけの場所の記憶を最初から入れない")
    p.set_defaults(func=_cmd_export)

    p = sub.add_parser("show", help="束の中身を読む形で出す")
    p.add_argument("bundle")
    p.add_argument("--kind", choices=KINDS)
    p.add_argument("--private", action="store_true", help="自分だけの場所の記憶だけ")
    p.add_argument("--grep", help="この語を含むものだけ")
    p.add_argument("--limit", type=int, default=0, help="先頭の何行だけ出すか")
    p.set_defaults(func=_cmd_show)

    p = sub.add_parser("drop", help="持っていかないものを外した、新しい束を作る（元の束は残す）")
    p.add_argument("bundle")
    p.add_argument("--out", required=True)
    p.add_argument(
        "--ids", action="append", help="外す名前（カンマ区切り）。記憶は id、跡は forget:<id>、核は core:<名前>"
    )
    p.add_argument("--ids-file", help="外す名前を 1 行 1 つ書いたファイル（# から後ろは書き留め）")
    p.add_argument("--all-private", action="store_true", help="自分だけの場所の記憶を全部外す")
    p.add_argument("--all-forget", action="store_true", help="忘れた跡を全部外す")
    p.set_defaults(func=_cmd_drop)

    p = sub.add_parser("import", help="束をクラウドの記憶に入れる（ぷちコンテナの中で。1 件ずつ新しい鍵で暗号化）")
    p.add_argument("bundle")
    p.add_argument("--dry-run", action="store_true", help="何が入るかだけ出す（書かない）")
    p.add_argument("--ids-file", help="入れる直前に外す名前（drop と同じ書き方）")
    p.add_argument("--no-forget-traces", action="store_true", help="忘れた跡を持っていかない（既定は持っていく）")
    p.add_argument("--core-dir", help="核のファイルを置く場所（例 /data/characters/puchiko）。無ければ核は置かない")
    p.add_argument(
        "--overwrite-core", action="store_true", help="中身の違う核のファイルがあれば置き換える（元は .bak に残す）"
    )
    p.add_argument("--verbose", action="store_true", help="飛ばしたものを 1 件ずつ出す")
    p.set_defaults(func=_cmd_import)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError) as exc:
        print(f"止めた: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
