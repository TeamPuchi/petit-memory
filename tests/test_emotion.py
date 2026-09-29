"""感情タグ（強さを含んだ、決まった日本語のタグ）の扱い."""

from datetime import datetime, timedelta

import pytest

from memory_mcp.config import SleepConfig
from memory_mcp.emotion import (
    EMOTION_TAGS,
    EMOTIONS,
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


class TestTable:
    def test_eight_emotions_three_levels(self):
        assert len(EMOTIONS) == 8
        assert len(TAG_LABELS) == 24
        for emotion_id in EMOTIONS:
            levels = sorted(t.level for t in EMOTION_TAGS.values() if t.emotion_id == emotion_id)
            assert levels == [1, 2, 3]

    def test_tag_keeps_id_and_level_apart(self):
        tag = to_tag("うれしい")
        assert (tag.emotion_id, tag.level) == ("joy", 2)
        assert to_tag("わくわく").emotion_id == "anticipation"

    def test_every_legacy_value_maps_to_a_tag(self):
        for english, label in LEGACY_EMOTION_TAGS.items():
            assert label == "" or label in EMOTION_TAGS, english


class TestStrength:
    @pytest.mark.parametrize(("label", "level"), [("おだやか", 1), ("うれしい", 2), ("感動", 3), ("", 0)])
    def test_strength_follows_level(self, label: str, level: int):
        assert emotion_level(label) == level
        assert emotion_strength(label) == LEVEL_BOOST[level]

    def test_stronger_is_higher(self):
        assert emotion_strength("しんみり") < emotion_strength("かなしい") < emotion_strength("すごくかなしい")

    @pytest.mark.parametrize(
        ("english", "label"),
        [("happy", "うれしい"), ("excited", "わくわく"), ("moved", "感動"), ("nostalgic", "しんみり"),
         ("curious", "きになる"), ("joy", "うれしい"), ("calm", "おだやか"), ("Happy", "うれしい")],
    )
    def test_legacy_english_reads_as_tag(self, english: str, label: str):
        assert emotion_label(english) == label
        assert emotion_strength(english) == emotion_strength(label)

    def test_unknown_value_has_no_strength(self):
        assert emotion_strength("楽しい") == 0.0
        assert emotion_strength("bored") == 0.0


class TestProtection:
    def test_level_two_and_up_are_protected(self):
        level = SleepConfig().protected_emotion_level
        for label in ("うれしい", "感動", "わくわく", "おどろいた", "かなしい", "こわい"):
            assert is_protected_emotion(label, level), label
        for label in ("おだやか", "しんみり", "きになる", "きょとん", "", "楽しい"):
            assert not is_protected_emotion(label, level), label

    def test_legacy_protection(self):
        level = SleepConfig().protected_emotion_level
        for english in ("happy", "moved", "excited", "surprised"):  # 元の作りで消されなかった 4 語
            assert is_protected_emotion(english, level), english
        for english in ("nostalgic", "curious", "neutral"):
            assert not is_protected_emotion(english, level), english

    def test_is_protected_uses_tag(self):
        m = Memory(
            id="t", content="x", timestamp=datetime.now().isoformat(),
            emotion="だいすき", importance=1, category="daily",
        )
        assert _is_protected(m, SleepConfig())

    def test_neutral_values(self):
        assert is_neutral("")
        assert is_neutral("neutral")
        assert is_neutral(None)
        assert not is_neutral("しんみり")


class TestInput:
    def test_normalize(self):
        assert normalize_emotion("うれしい") == "うれしい"
        assert normalize_emotion(" happy ") == "うれしい"
        assert normalize_emotion("") == ""
        assert normalize_emotion("neutral") == ""
        assert normalize_emotion("楽しい") is None

    def test_server_input_drops_unknown_with_note(self):
        assert _input_emotion({"emotion": "すき"}) == ("すき", "")
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
        assert set(emotion_filter_values("うれしい")) == {"うれしい", "joy", "happy"}
        assert set(emotion_filter_values("happy")) == {"happy", "joy", "うれしい"}
        assert emotion_filter_values("だいすき") == ("だいすき",)
        assert set(emotion_filter_values("neutral")) == {"", "neutral"}
        assert emotion_filter_values(None) is None
        assert emotion_filter_values("") is None


@pytest.mark.asyncio
async def test_search_tag_filter_finds_legacy(memory_store: MemoryStore):
    await memory_store.save(content="ひなたぼっこした", emotion="happy")
    await memory_store.save(content="ひなたぼっこ気持ちよかった", emotion="うれしい")
    await memory_store.save(content="ひなたぼっこで寝た", emotion="joy")
    await memory_store.save(content="ひなたぼっこ中に雨", emotion="かなしい")

    results = await memory_store.search("ひなたぼっこ", n_results=10, emotion_filter="うれしい")
    assert {r.memory.emotion for r in results} == {"happy", "うれしい", "joy"}


@pytest.mark.asyncio
async def test_sleep_uses_levels(memory_store: MemoryStore, set_memory_timestamp):
    # 似た記憶はまとめられるので、種類を分けて一つずつ残す
    old = (datetime.now() - timedelta(days=30)).isoformat()
    await memory_store.save(content="何もない日", emotion="", importance=1, category="daily")
    await set_memory_timestamp(memory_store, old, content="何もない日")
    await memory_store.save(content="ふしぎな音", emotion="きになる", importance=1, category="observation")
    await set_memory_timestamp(memory_store, old, content="ふしぎな音")
    await memory_store.save(content="お茶の時間", emotion="うれしい", importance=1, category="feeling")
    await set_memory_timestamp(memory_store, old, content="お茶の時間")

    await SleepEngine(memory_store).run(dry_run=False)

    remaining = {m.content for m in await memory_store.get_all()}
    assert "何もない日" not in remaining  # 気持ちの無い記憶は元どおり忘れる対象
    assert "ふしぎな音" in remaining  # 気持ちが付いていれば忘れない（元どおり）
    assert "お茶の時間" in remaining  # 強さ 2 以上は消されない
