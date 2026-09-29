"""感情タグ（ぷちが書く日本語の一語）の扱い."""

from datetime import datetime, timedelta

import pytest

from memory_mcp.config import SleepConfig
from memory_mcp.emotion import (
    EMOTION_BOOST_MAP,
    UNKNOWN_EMOTION_STRENGTH,
    emotion_family,
    emotion_filter_values,
    emotion_label,
    emotion_strength,
    is_neutral,
    is_protected_emotion,
)
from memory_mcp.sleep import SleepEngine, _is_protected
from memory_mcp.store import MemoryStore, calculate_emotion_boost
from memory_mcp.types import Memory


class TestStrength:
    def test_original_table_unchanged(self):
        """元の作りの強さの表は変えない."""
        assert EMOTION_BOOST_MAP == {
            "excited": 0.4, "surprised": 0.35, "moved": 0.3, "sad": 0.25,
            "happy": 0.2, "nostalgic": 0.15, "curious": 0.1, "neutral": 0.0,
        }

    def test_legacy_english_keeps_original_values(self):
        for word, value in EMOTION_BOOST_MAP.items():
            assert emotion_strength(word) == value
        # 元の表に無い英語は元どおり 0
        assert emotion_strength("joy") == 0.0
        assert calculate_emotion_boost("unknown") == 0.0

    @pytest.mark.parametrize(
        ("word", "family"),
        [
            ("わくわく", "excited"),
            ("楽しみ", "excited"),
            ("おどろいた", "surprised"),
            ("びっくりした", "surprised"),
            ("感動", "moved"),
            ("かなしい", "sad"),
            ("さみしい", "sad"),
            ("うれしい", "happy"),
            ("楽しい", "happy"),
            ("愉しい", "happy"),
            ("楽しかった", "happy"),
            ("なつかしい", "nostalgic"),
            ("しみじみ", "nostalgic"),
            ("きになる", "curious"),
            ("", "neutral"),
            ("ふつう", "neutral"),
        ],
    )
    def test_japanese_words_map_to_original_family(self, word: str, family: str):
        assert emotion_family(word) == family
        assert emotion_strength(word) == EMOTION_BOOST_MAP[family]

    def test_unknown_japanese_word_is_middle(self):
        assert emotion_family("こそばゆい") is None
        assert emotion_strength("こそばゆい") == UNKNOWN_EMOTION_STRENGTH
        assert 0.0 < UNKNOWN_EMOTION_STRENGTH < max(EMOTION_BOOST_MAP.values())


class TestProtection:
    def test_japanese_strong_feelings_are_protected(self):
        protected = SleepConfig().protected_emotions
        for word in ("楽しい", "愉しい", "うれしい", "感動", "わくわく", "おどろいた"):
            assert is_protected_emotion(word, protected), word
        for word in ("かなしい", "なつかしい", "きになる", "こそばゆい", ""):
            assert not is_protected_emotion(word, protected), word

    def test_legacy_english_protection_unchanged(self):
        protected = SleepConfig().protected_emotions
        assert is_protected_emotion("happy", protected)
        assert not is_protected_emotion("sad", protected)
        assert not is_protected_emotion("joy", protected)

    def test_is_protected_uses_japanese(self):
        m = Memory(
            id="t", content="x", timestamp=datetime.now().isoformat(),
            emotion="愉しい", importance=1, category="daily",
        )
        assert _is_protected(m, SleepConfig())

    def test_neutral_values(self):
        assert is_neutral("")
        assert is_neutral("neutral")
        assert is_neutral(None)
        assert not is_neutral("こそばゆい")


class TestLabelAndFilter:
    def test_legacy_labels(self):
        assert emotion_label("joy") == "うれしい"
        assert emotion_label("happy") == "うれしい"
        assert emotion_label("sadness") == "かなしい"
        assert emotion_label("curiosity") == "きになる"
        assert emotion_label("neutral") == ""
        assert emotion_label("") == ""
        assert emotion_label("Happy") == "うれしい"
        assert emotion_label("bored") == ""  # 表に無い英語は出さない（petit-app と同じ）

    def test_japanese_label_kept_as_written(self):
        assert emotion_label("楽しい") == "楽しい"
        assert emotion_label("愉しい") == "愉しい"

    def test_filter_values(self):
        assert set(emotion_filter_values("うれしい")) == {"うれしい", "joy", "happy"}
        assert set(emotion_filter_values("happy")) == {"happy", "joy", "うれしい"}
        assert emotion_filter_values("愉しい") == ("愉しい",)
        assert "" in emotion_filter_values("neutral")
        assert "neutral" in emotion_filter_values("ふつう")
        assert emotion_filter_values(None) is None
        assert emotion_filter_values("") is None


@pytest.mark.asyncio
async def test_store_keeps_word_as_written(memory_store: MemoryStore):
    a = await memory_store.save(content="散歩", emotion="楽しい")
    b = await memory_store.save(content="お茶", emotion=" 愉しい ")
    assert (await memory_store.get_by_id(a.id)).emotion == "楽しい"
    assert (await memory_store.get_by_id(b.id)).emotion == "愉しい"


@pytest.mark.asyncio
async def test_search_japanese_filter_finds_legacy(memory_store: MemoryStore):
    await memory_store.save(content="ひなたぼっこした", emotion="happy")
    await memory_store.save(content="ひなたぼっこ気持ちよかった", emotion="うれしい")
    await memory_store.save(content="ひなたぼっこで寝た", emotion="joy")
    await memory_store.save(content="ひなたぼっこ中に雨", emotion="かなしい")

    results = await memory_store.search("ひなたぼっこ", n_results=10, emotion_filter="うれしい")
    assert {r.memory.emotion for r in results} == {"happy", "うれしい", "joy"}


@pytest.mark.asyncio
async def test_sleep_forgets_empty_but_keeps_japanese(memory_store: MemoryStore, set_memory_timestamp):
    # 似た記憶はまとめられるので、種類を分けて一つずつ残す
    old = (datetime.now() - timedelta(days=30)).isoformat()
    await memory_store.save(content="何もない日", emotion="", importance=1, category="daily")
    await set_memory_timestamp(memory_store, old, content="何もない日")
    await memory_store.save(content="ふしぎな音", emotion="こそばゆい", importance=1, category="observation")
    await set_memory_timestamp(memory_store, old, content="ふしぎな音")
    await memory_store.save(content="お茶の時間", emotion="愉しい", importance=1, category="feeling")
    await set_memory_timestamp(memory_store, old, content="お茶の時間")

    await SleepEngine(memory_store).run(dry_run=False)

    remaining = {m.content for m in await memory_store.get_all()}
    assert "何もない日" not in remaining  # 気持ちの無い記憶は元どおり忘れる対象
    assert "ふしぎな音" in remaining  # 気持ちが付いていれば忘れない（元どおり）
    assert "お茶の時間" in remaining  # 「愉しい」は happy の仲間 → 消されない
