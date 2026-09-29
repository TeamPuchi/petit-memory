"""記憶の感情タグ（強さを含んだ、決まった日本語のタグ）.

ぷちは記憶に、下の表の日本語のタグを一つ付ける（気持ちが無ければ付けない）。
タグは「どの感情か（ID）」と「強さ（1〜3）」を持つ。形はプルチックの感情の輪
（基本 8 感情 × 強さ 3 段。例: 平穏 → 喜び → 恍惚）に倣い、言葉はぷちの話し方に合わせた。

- 保存するのはタグの言葉そのもの（例: "嬉しい"）。ID と強さは表から引く。
  あとで感情どうしの近さ（その子ごとの感情距離。驚きと喜びが近い子は、喜びのとき驚きも思い出す）
  を足すときは、ID（``EmotionTag.emotion_id``）で引けばよい。いまは実装しない。
- 強さが、思い出しやすさ・寝ている間の整理で残る点数（``emotion_strength``）と、
  消されない記憶（``is_protected_emotion``）を決める。元の作り（embodied-claude の memory-mcp）の
  ``EMOTION_BOOST_MAP``（0.0〜0.4）と ``protected_emotions`` の趣旨「強い気持ちの記憶は残る」を、強さの段で表す。
  ただし悲しみ・嫌悪・怒り・恐れは消えやすい（守らない・加点は半分。なぎさん 2026-09-29）。
- 古い英語の値（元の作りの 8 語・それより前の joy など）は保存を変えず、読むときに ``LEGACY_EMOTION_TAGS`` で
  新しいタグに読み替える（強さ・消されない・表示・絞り込みのどれも読み替えたタグで決まる）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmotionTag:
    """感情タグ一つ。``label`` を保存し、``emotion_id``・``level`` は表から引く."""

    label: str  # 保存・表示する日本語（例: "嬉しい"）
    emotion_id: str  # どの感情か（プルチックの基本 8 感情の英名。例: "joy"）
    level: int  # 強さ 1（弱い）〜 3（強い）


# 基本 8 感情（ID → 日本語の名前）。並びはプルチックの輪の順（隣どうしが近い）
EMOTIONS: dict[str, str] = {
    "joy": "喜び",
    "trust": "信頼",
    "fear": "恐れ",
    "surprise": "驚き",
    "sadness": "悲しみ",
    "disgust": "嫌悪",
    "anger": "怒り",
    "anticipation": "期待",
}

# 感情 × 強さ の表（弱い → 強い）。コメントはプルチックの言葉
_TABLE: dict[str, tuple[str, str, str]] = {
    "joy": ("穏やか", "嬉しい", "感動"),  # 平穏 → 喜び → 恍惚
    "trust": ("安心", "好き", "大好き"),  # 受容 → 信頼 → 敬愛
    "fear": ("不安", "怖い", "すごく怖い"),  # 不安 → 恐れ → 恐怖
    "surprise": ("きょとん", "驚いた", "びっくり"),  # 放心 → 驚き → 驚嘆
    "sadness": ("しんみり", "悲しい", "すごく悲しい"),  # 哀愁 → 悲しみ → 悲嘆
    "disgust": ("退屈", "嫌", "大嫌い"),  # 退屈 → 嫌悪 → 憎悪
    "anger": ("むっとした", "怒った", "すごく怒った"),  # 苛立ち → 怒り → 激怒
    "anticipation": ("気になる", "楽しみ", "わくわく"),  # 関心 → 期待 → 警戒（ぷちに合わせて「わくわく」）
}

EMOTION_TAGS: dict[str, EmotionTag] = {
    label: EmotionTag(label=label, emotion_id=emotion_id, level=level)
    for emotion_id, labels in _TABLE.items()
    for level, label in enumerate(labels, start=1)
}

# remember などの入力で選べるタグ（表の順）
TAG_LABELS: tuple[str, ...] = tuple(EMOTION_TAGS)

# 強さ → 加点（元の作りの EMOTION_BOOST_MAP と同じ 0.0〜0.4 の幅）
LEVEL_BOOST: dict[int, float] = {0: 0.0, 1: 0.1, 2: 0.25, 3: 0.4}

# 消えやすい気持ち（悲しみ・嫌悪・怒り・恐れ）。整理で守らず、加点も控えめ（LEVEL_BOOST × FADING_BOOST_SCALE）
FADING_EMOTIONS: frozenset[str] = frozenset({"sadness", "disgust", "anger", "fear"})
FADING_BOOST_SCALE: float = 0.5

# 整理で消されない気持ち（喜び・信頼・驚き・期待）と、その強さの下限（元の protected_emotions の趣旨）
PROTECTED_EMOTIONS: tuple[str, ...] = ("joy", "trust", "surprise", "anticipation")
PROTECTED_LEVEL: int = 2

# 古い英語の値 → 新しいタグ（保存は変えず、読むときに読み替える）
LEGACY_EMOTION_TAGS: dict[str, str] = {
    # 元の作り（embodied-claude memory-mcp）の 8 語
    "happy": "嬉しい",
    "excited": "わくわく",
    "surprised": "驚いた",
    "moved": "感動",
    "sad": "悲しい",
    "nostalgic": "しんみり",
    "curious": "気になる",
    "neutral": "",
    # それより前に使っていた値
    "joy": "嬉しい",
    "calm": "穏やか",
    "curiosity": "気になる",
    "surprise": "驚いた",
    "sadness": "悲しい",
    "anger": "むっとした",
    "fear": "怖い",
    "lonely": "悲しい",
}

# 元の作りの強さの表（参考に残す。いまの加点は LEVEL_BOOST で決まる）
EMOTION_BOOST_MAP: dict[str, float] = {
    "excited": 0.4,
    "surprised": 0.35,
    "moved": 0.3,
    "sad": 0.25,
    "happy": 0.2,
    "nostalgic": 0.15,
    "curious": 0.1,
    "neutral": 0.0,
}


def _clean(emotion: str | None) -> str:
    return (emotion or "").strip()


def is_neutral(emotion: str | None) -> bool:
    """気持ちが付いていない（空・neutral）か。表に無い値は「付いている」扱い（元の作りと同じ）."""
    e = _clean(emotion)
    return not e or (e.isascii() and e.lower() == "neutral")


def to_tag(emotion: str | None) -> EmotionTag | None:
    """保存値を感情タグに。古い英語の値は読み替える。気持ちが無い・表に無い値は None."""
    e = _clean(emotion)
    if e in EMOTION_TAGS:
        return EMOTION_TAGS[e]
    legacy = LEGACY_EMOTION_TAGS.get(e.lower()) if e.isascii() else None
    return EMOTION_TAGS[legacy] if legacy else None


def normalize_emotion(emotion: str | None) -> str | None:
    """保存する値に整える。タグ（古い英語の値も）ならタグの言葉、空なら ""、表に無ければ None."""
    if is_neutral(emotion):
        return ""
    tag = to_tag(emotion)
    return tag.label if tag else None


def emotion_level(emotion: str | None) -> int:
    """強さ（0＝無し・表に無い、1〜3）."""
    tag = to_tag(emotion)
    return tag.level if tag else 0


def emotion_strength(emotion: str | None) -> float:
    """思い出しやすさ・残りやすさの加点（0.0〜0.4）。強さの段で決まり、消えやすい気持ちは控えめ."""
    tag = to_tag(emotion)
    if tag is None:
        return 0.0
    boost = LEVEL_BOOST[tag.level]
    return boost * FADING_BOOST_SCALE if tag.emotion_id in FADING_EMOTIONS else boost


def is_protected_emotion(
    emotion: str | None,
    protected_level: int = PROTECTED_LEVEL,
    protected_emotions: tuple[str, ...] = PROTECTED_EMOTIONS,
) -> bool:
    """寝ている間の整理で消されない気持ちか（守る気持ちで、強さが ``protected_level`` 以上）."""
    tag = to_tag(emotion)
    return tag is not None and tag.emotion_id in protected_emotions and tag.level >= protected_level


def emotion_label(emotion: str | None) -> str:
    """表示用の日本語。古い英語の値は読み替えたタグ。気持ちが無い・表に無い英語は空文字."""
    e = _clean(emotion)
    tag = to_tag(e)
    if tag:
        return tag.label
    if not e or e.isascii():
        return ""
    return e  # 表に無い日本語はそのまま


def emotion_filter_values(emotion: str | None) -> tuple[str, ...] | None:
    """感情で絞るときに一致させる保存値の組.

    タグで探すと、そのタグに読み替わる古い英語の記憶も拾う（「嬉しい」→ happy・joy も）。
    """
    e = _clean(emotion)
    if not e:
        return None
    if is_neutral(e):
        return ("", "neutral")
    tag = to_tag(e)
    if tag is None:
        return (e,)
    values = {e, tag.label}
    values.update(k for k, v in LEGACY_EMOTION_TAGS.items() if v == tag.label)
    return tuple(sorted(values))
