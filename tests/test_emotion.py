"""感情タグ（強さを含んだ、決まった日本語のタグ）の扱い."""

from datetime import datetime, timedelta

import pytest

from memory_mcp.config import SleepConfig
from memory_mcp.emotion import (
    EMOTION_TAGS,
    EMOTIONS,
    FADING_EMOTIONS,
    LEGACY_EMOTION_TAGS,
    LEVEL_BOOST,
    TAG_LABELS,
    emotion_filter_values,
    emotion_label,
    emotion_level,
    emotion_strength,
    is_neutral,
    is_protected_emotion,
    normalize_emotion,
    to_tag,
)
from memory_mcp.server import _input_emotion
from memory_mcp.sleep import SleepEngine, _is_protected
from memory_mcp.store import MemoryStore
from memory_mcp.types import Memory


def _protected(label: str) -> bool:
    config = SleepConfig()
    return is_protected_emotion(label, config.protected_emotion_level, config.protected_emotions)


class TestTable:
    def test_eight_emotions_three_levels(self):
        assert len(EMOTIONS) == 8
        assert len(TAG_LABELS) == 24
        for emotion_id in EMOTIONS:
            levels = sorted(t.level for t in EMOTION_TAGS.values() if t.emotion_id == emotion_id)
            assert levels == [1, 2, 3]

    def test_tag_keeps_id_and_level_apart(self):
        tag = to_tag("嬉しい")
        assert (tag.emotion_id, tag.level) == ("joy", 2)
        assert to_tag("わくわく").emotion_id == "anticipation"

    def test_every_legacy_value_maps_to_a_tag(self):
        for english, label in LEGACY_EMOTION_TAGS.items():
            assert label == "" or label in EMOTION_TAGS, english


class TestStrength:
    @pytest.mark.parametrize(("label", "level"), [("穏やか", 1), ("嬉しい", 2), ("感動", 3), ("", 0)])
    def test_strength_follows_level(self, label: str, level: int):
        assert emotion_level(label) == level
        assert emotion_strength(label) == LEVEL_BOOST[level]

    def test_stronger_is_higher(self):
        assert emotion_strength("しんみり") < emotion_strength("悲しい") < emotion_strength("すごく悲しい")
        assert emotion_strength("気になる") < emotion_strength("楽しみ") < emotion_strength("わくわく")

    def test_fading_emotions_boost_less(self):
        assert FADING_EMOTIONS == {"sadness", "disgust", "anger", "fear"}
        for level in (1, 2, 3):
            tags = [t for t in EMOTION_TAGS.values() if t.level == level]
            fading = max(emotion_strength(t.label) for t in tags if t.emotion_id in FADING_EMOTIONS)
            positive = min(emotion_strength(t.label) for t in tags if t.emotion_id not in FADING_EMOTIONS)
            assert fading < positive
        assert emotion_strength("すごく悲しい") == 0.2

    @pytest.mark.parametrize(
        ("english", "label"),
        [("happy", "嬉しい"), ("excited", "わくわく"), ("moved", "感動"), ("nostalgic", "しんみり"),
         ("curious", "気になる"), ("joy", "嬉しい"), ("calm", "穏やか"), ("Happy", "嬉しい"),
         ("lonely", "悲しい"), ("fear", "怖い"), ("surprise", "驚いた")],
    )
    def test_legacy_english_reads_as_tag(self, english: str, label: str):
        assert emotion_label(english) == label
        assert emotion_strength(english) == emotion_strength(label)

    def test_unknown_value_has_no_strength(self):
        assert emotion_strength("楽しい") == 0.0
        assert emotion_strength("bored") == 0.0


class TestProtection:
    def test_positive_level_two_and_up_are_protected(self):
        for label in ("嬉しい", "感動", "好き", "大好き", "驚いた", "びっくり", "楽しみ", "わくわく"):
            assert _protected(label), label
        for label in ("穏やか", "安心", "きょとん", "気になる", "", "楽しい"):
            assert not _protected(label), label

    def test_fading_emotions_are_not_protected(self):
        for tag in EMOTION_TAGS.values():
            if tag.emotion_id in FADING_EMOTIONS:
                assert not _protected(tag.label), tag.label

    def test_legacy_protection(self):
        for english in ("happy", "moved", "excited", "surprised"):  # 元の作りで消されなかった 4 語
            assert _protected(english), english
        for english in ("sad", "nostalgic", "curious", "neutral", "fear"):
            assert not _protected(english), english

    def test_is_protected_uses_tag(self):
        def mem(emotion: str) -> Memory:
            return Memory(
                id="t", content="x", timestamp=datetime.now().isoformat(),
                emotion=emotion, importance=1, category="daily",
            )

        assert _is_protected(mem("大好き"), SleepConfig())
        assert not _is_protected(mem("すごく怒った"), SleepConfig())

    def test_neutral_values(self):
        assert is_neutral("")
        assert is_neutral("neutral")
        assert is_neutral(None)
        assert not is_neutral("しんみり")


class TestInput:
    def test_normalize(self):
        assert normalize_emotion("嬉しい") == "嬉しい"
        assert normalize_emotion(" happy ") == "嬉しい"
        assert normalize_emotion("") == ""
        assert normalize_emotion("neutral") == ""
        assert normalize_emotion("楽しい") is None

    def test_server_input_drops_unknown_with_note(self):
        assert _input_emotion({"emotion": "好き"}) == ("好き", "")
        assert _input_emotion({}) == ("", "")
        value, note = _input_emotion({"emotion": "楽しい"})
        assert value == ""
        assert "楽しい" in note


class TestLabelAndFilter:
    def test_labels(self):
        assert emotion_label("neutral") == ""
        assert emotion_label("") == ""
        assert emotion_label("bored") == ""  # 表に無い英語は出さない

    def test_filter_values(self):
        assert set(emotion_filter_values("嬉しい")) == {"嬉しい", "joy", "happy"}
        assert set(emotion_filter_values("happy")) == {"happy", "joy", "嬉しい"}
        assert emotion_filter_values("大好き") == ("大好き",)
        assert set(emotion_filter_values("neutral")) == {"", "neutral"}
        assert emotion_filter_values(None) is None
        assert emotion_filter_values("") is None


@pytest.mark.asyncio
async def test_search_tag_filter_finds_legacy(memory_store: MemoryStore):
    await memory_store.save(content="ひなたぼっこした", emotion="happy")
    await memory_store.save(content="ひなたぼっこ気持ちよかった", emotion="嬉しい")
    await memory_store.save(content="ひなたぼっこで寝た", emotion="joy")
    await memory_store.save(content="ひなたぼっこ中に雨", emotion="悲しい")

    results = await memory_store.search("ひなたぼっこ", n_results=10, emotion_filter="嬉しい")
    assert {r.memory.emotion for r in results} == {"happy", "嬉しい", "joy"}


@pytest.mark.asyncio
async def test_sleep_uses_levels(memory_store: MemoryStore, set_memory_timestamp):
    # 似た記憶はまとめられるので、種類を分けて一つずつ残す
    old = (datetime.now() - timedelta(days=30)).isoformat()
    await memory_store.save(content="何もない日", emotion="", importance=1, category="daily")
    await set_memory_timestamp(memory_store, old, content="何もない日")
    await memory_store.save(content="ふしぎな音", emotion="気になる", importance=1, category="observation")
    await set_memory_timestamp(memory_store, old, content="ふしぎな音")
    await memory_store.save(content="お茶の時間", emotion="嬉しい", importance=1, category="feeling")
    await set_memory_timestamp(memory_store, old, content="お茶の時間")

    await SleepEngine(memory_store).run(dry_run=False)

    remaining = {m.content for m in await memory_store.get_all()}
    assert "何もない日" not in remaining  # 気持ちの無い記憶は元どおり忘れる対象
    assert "ふしぎな音" in remaining  # 気持ちが付いていれば忘れない（元どおり）
    assert "お茶の時間" in remaining  # 喜びの強さ 2 以上は消されない
