"""記憶・エピソードの直列化（保管先に依らない形）。

SQLite の 1 行と DynamoDB の 1 アイテムで、同じ属性名・同じ表現を使う。
どちらの実装も必ずここを通すことで、片流し複製（段1）で
「SQLite に入っている値」と「DynamoDB に入っている値」がずれないようにする。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .store_backend import MemoryRecord
from .types import (
    CameraPosition,
    Episode,
    Memory,
    MemoryLink,
    SensoryData,
)

# 記憶 1 件が持つ属性（SQLite の列名と同じ並び）
MEMORY_ATTRIBUTES: tuple[str, ...] = (
    "id",
    "content",
    "normalized_content",
    "timestamp",
    "emotion",
    "importance",
    "category",
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
)

EPISODE_ATTRIBUTES: tuple[str, ...] = (
    "id",
    "title",
    "start_time",
    "end_time",
    "memory_ids",
    "participants",
    "location_context",
    "summary",
    "emotion",
    "importance",
)


# ── 値のほどき ──────────────────────────────


def parse_linked_ids(linked_ids_str: str) -> tuple[str, ...]:
    if not linked_ids_str:
        return ()
    return tuple(id.strip() for id in linked_ids_str.split(",") if id.strip())


def parse_sensory_data(sensory_data_json: str) -> tuple[SensoryData, ...]:
    if not sensory_data_json:
        return ()
    try:
        data_list = json.loads(sensory_data_json)
        return tuple(SensoryData.from_dict(d) for d in data_list)
    except (json.JSONDecodeError, KeyError, TypeError):
        return ()


def parse_camera_position(camera_position_json: str | None) -> CameraPosition | None:
    if not camera_position_json:
        return None
    try:
        data = json.loads(camera_position_json)
        return CameraPosition.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def parse_tags(tags_str: str) -> tuple[str, ...]:
    if not tags_str:
        return ()
    return tuple(tag.strip() for tag in tags_str.split(",") if tag.strip())


def parse_links(links_json: str) -> tuple[MemoryLink, ...]:
    if not links_json:
        return ()
    try:
        data_list = json.loads(links_json)
        return tuple(MemoryLink.from_dict(d) for d in data_list)
    except (json.JSONDecodeError, KeyError, TypeError):
        return ()


# ── 記憶 ────────────────────────────────────


def encode_memory(record: MemoryRecord) -> dict[str, Any]:
    """保管用の属性辞書にする。値の形は SQLite の列と同じ。"""
    memory = record.memory
    meta = memory.to_metadata()
    return {
        "id": memory.id,
        "content": memory.content,
        "normalized_content": record.normalized_content,
        "timestamp": memory.timestamp,
        "emotion": memory.emotion,
        "importance": memory.importance,
        "category": memory.category,
        "access_count": meta.get("access_count", 0),
        "last_accessed": meta.get("last_accessed", ""),
        "linked_ids": meta.get("linked_ids", ""),
        "episode_id": memory.episode_id or None,
        "sensory_data": meta.get("sensory_data", ""),
        "camera_position": meta.get("camera_position") or None,
        "tags": meta.get("tags", ""),
        "links": meta.get("links", ""),
        "novelty_score": memory.novelty_score,
        "prediction_error": memory.prediction_error,
        "activation_count": memory.activation_count,
        "last_activated": memory.last_activated,
        "reading": record.reading,
    }


def decode_memory(
    attrs: Mapping[str, Any],
    coactivation: tuple[tuple[str, float], ...] = (),
) -> Memory:
    """保管された属性から Memory を作る。

    数値は SQLite なら int/float、DynamoDB なら Decimal で返るため、
    どちらでも同じ型になるように読み替える。
    """
    episode_id_raw = attrs["episode_id"] if "episode_id" in attrs else None
    return Memory(
        id=attrs["id"],
        content=attrs["content"],
        timestamp=attrs["timestamp"],
        emotion=attrs["emotion"],
        importance=int(attrs["importance"]),
        category=attrs["category"],
        access_count=int(attrs["access_count"] or 0),
        last_accessed=attrs["last_accessed"] or "",
        linked_ids=parse_linked_ids(attrs["linked_ids"] or ""),
        episode_id=episode_id_raw if episode_id_raw else None,
        sensory_data=parse_sensory_data(attrs["sensory_data"] or ""),
        camera_position=parse_camera_position(attrs["camera_position"]),
        tags=parse_tags(attrs["tags"] or ""),
        links=parse_links(attrs["links"] or ""),
        novelty_score=float(attrs["novelty_score"] or 0.0),
        prediction_error=float(attrs["prediction_error"] or 0.0),
        activation_count=int(attrs["activation_count"] or 0),
        last_activated=attrs["last_activated"] or "",
        coactivation_weights=coactivation,
    )


# ── エピソード ──────────────────────────────


def encode_episode(episode: Episode) -> dict[str, Any]:
    return {
        "id": episode.id,
        "title": episode.title,
        "start_time": episode.start_time,
        "end_time": episode.end_time or None,
        "memory_ids": ",".join(episode.memory_ids),
        "participants": ",".join(episode.participants),
        "location_context": episode.location_context,
        "summary": episode.summary,
        "emotion": episode.emotion,
        "importance": episode.importance,
    }


def decode_episode(attrs: Mapping[str, Any]) -> Episode:
    memory_ids_raw = attrs["memory_ids"] or ""
    participants_raw = attrs["participants"] or ""
    return Episode(
        id=attrs["id"],
        title=attrs["title"],
        start_time=attrs["start_time"],
        end_time=attrs["end_time"] or None,
        memory_ids=tuple(memory_ids_raw.split(",") if memory_ids_raw else []),
        participants=tuple(participants_raw.split(",") if participants_raw else []),
        location_context=attrs["location_context"] or None,
        summary=attrs["summary"] or "",
        emotion=attrs["emotion"],
        importance=int(attrs["importance"]),
    )
