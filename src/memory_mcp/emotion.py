"""記憶の感情タグ（ぷちが書く日本語の一語）の扱い.

感情は、ぷちが自分の言葉で書く日本語の一語（「楽しい」「愉しい」「しみじみ」など）。
英語の決まった語には閉じない。保存するのはぷちが書いた言葉そのもの。

元の作り（lifemate-ai/embodied-claude の memory-mcp → PetitOnes/embodied-claude）では、
感情は英語 8 語で、語ごとに「強さ」があった（``EMOTION_BOOST_MAP``）。
強さは思い出しやすさ・寝ている間の整理で残る点数に効き、
強い気持ち（happy・moved・excited・surprised）の記憶は消されず、
気持ちの無い（neutral）記憶だけが忘れる対象になる。

その趣旨を変えないため、日本語の言葉を元の 8 語の「どの気持ちの仲間か」に割り当て、
強さと「消されない」は元の表（``EMOTION_BOOST_MAP``・``protected_emotions``）から引く
（案 A）。表に無い言葉は中くらいの強さ（消されないの対象ではないが、忘れる対象でもない）。

- 古い英語の値（happy など）は保存を変えず、挙動も元のまま（表の値そのもの）。
- 表示するときは ``emotion_label`` で日本語にする（petit-app の LEGACY_EMOTION_LABELS と同じ訳）。

割り当ての表（``JA_EMOTION_FAMILY``）と、表に無い言葉の強さ（``UNKNOWN_EMOTION_STRENGTH``）を
差し替えれば、別の案に替えられる。
"""

from __future__ import annotations

# 元の作りの強さの表（lifemate-ai/embodied-claude memory-mcp のまま。値を変えない）
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

# 表に無い日本語の言葉の強さ（元の表の気持ちのある 7 語の真ん中＝sad と同じ段）
UNKNOWN_EMOTION_STRENGTH: float = 0.25

# 気持ちが無いことを表す値（保存の既定は空文字。古い記憶は "neutral"）
NEUTRAL_VALUES: frozenset[str] = frozenset({"", "neutral", "ふつう", "普通", "なし", "特になし", "平常"})

# 古い英語の値 → 表示の日本語（petit-app の LEGACY_EMOTION_LABELS と同じ。neutral は出さない）
LEGACY_EMOTION_LABELS: dict[str, str] = {
    "joy": "うれしい",
    "happy": "うれしい",
    "sad": "かなしい",
    "sadness": "かなしい",
    "surprised": "おどろいた",
    "surprise": "おどろいた",
    "moved": "感動",
    "excited": "わくわく",
    "nostalgic": "なつかしい",
    "curious": "きになる",
    "curiosity": "きになる",
    "calm": "おだやか",
    "anger": "むっとした",
    "fear": "こわい",
    "lonely": "さみしい",
    "neutral": "",
}

# 日本語の言葉 → 元の 8 語のどの気持ちの仲間か。
# 完全一致のあと「この書き出しで始まる」でも引く（「楽しかった」→「楽し」）。
JA_EMOTION_FAMILY: dict[str, str] = {
    # excited（0.4・消されない）
    "わくわく": "excited", "ワクワク": "excited", "どきどき": "excited", "ドキドキ": "excited",
    "うきうき": "excited", "ウキウキ": "excited", "興奮": "excited", "高揚": "excited",
    "楽しみ": "excited", "たのしみ": "excited",
    # surprised（0.35・消されない）
    "おどろ": "surprised", "驚": "surprised", "びっくり": "surprised", "ビックリ": "surprised",
    "ぎょっ": "surprised", "はっと": "surprised", "意外": "surprised",
    # moved（0.3・消されない）
    "感動": "moved", "感激": "moved", "感謝": "moved", "じーん": "moved", "じんと": "moved",
    "胸がいっぱい": "moved", "胸が熱": "moved", "ありがた": "moved", "尊い": "moved",
    # sad（0.25）— 元の表で気持ちの沈む側はこれだけなので、沈む・荒れる気持ちはここへ
    "かなし": "sad", "悲し": "sad", "哀し": "sad", "さみし": "sad", "さびし": "sad",
    "寂し": "sad", "淋し": "sad", "せつな": "sad", "切な": "sad", "つら": "sad", "辛い": "sad",
    "くやし": "sad", "悔し": "sad", "しょんぼり": "sad", "落ち込": "sad", "泣": "sad",
    "こわ": "sad", "怖": "sad", "不安": "sad", "心配": "sad", "むっと": "sad", "怒": "sad",
    "腹が立": "sad", "いらいら": "sad", "イライラ": "sad", "もやもや": "sad", "モヤモヤ": "sad",
    # happy（0.2・消されない）— 「楽しい」と「愉しい」は違う言葉のまま残し、強さは同じ段
    "うれし": "happy", "嬉し": "happy", "たのし": "happy", "楽し": "happy", "愉し": "happy",
    "しあわせ": "happy", "幸せ": "happy", "よかった": "happy", "良かった": "happy",
    "喜": "happy", "よろこ": "happy", "にこにこ": "happy", "ほっこり": "happy",
    "満足": "happy", "誇らし": "happy", "大好き": "happy", "いとし": "happy", "愛し": "happy",
    "かわいい": "happy", "可愛": "happy",
    # nostalgic（0.15）
    "なつかし": "nostalgic", "懐かし": "nostalgic", "しみじみ": "nostalgic",
    # curious（0.1）
    "きにな": "curious", "気にな": "curious", "ふしぎ": "curious", "不思議": "curious",
    "知りた": "curious", "興味": "curious", "なんだろう": "curious", "はてな": "curious",
    # 古い値の日本語訳で、元の表に仲間が無いもの（おだやか・むっとした・こわい・さみしい）は
    # 上の書き出しで引ける分だけ引き、残りは表に無い言葉として中くらいになる。
}

# 書き出しで引くときは長い書き出しから（「楽しみ」を「楽し」より先に）
_PREFIXES: tuple[str, ...] = tuple(sorted(JA_EMOTION_FAMILY, key=len, reverse=True))


def _clean(emotion: str | None) -> str:
    return (emotion or "").strip()


def is_neutral(emotion: str | None) -> bool:
    """気持ちが付いていない（空・neutral・ふつう など）か."""
    return _clean(emotion) in NEUTRAL_VALUES


def _is_legacy_english(emotion: str) -> bool:
    return emotion.isascii()


def emotion_family(emotion: str | None) -> str | None:
    """言葉が元の 8 語のどの気持ちの仲間か。わからなければ None."""
    e = _clean(emotion)
    if e in NEUTRAL_VALUES:
        return "neutral"
    if e in EMOTION_BOOST_MAP:
        return e
    if e in JA_EMOTION_FAMILY:
        return JA_EMOTION_FAMILY[e]
    for prefix in _PREFIXES:
        if e.startswith(prefix):
            return JA_EMOTION_FAMILY[prefix]
    return None


def emotion_strength(emotion: str | None) -> float:
    """感情の強さ（思い出しやすさ・残りやすさの加点に使う。元の表と同じ 0.0〜0.4）.

    古い英語の値は元の表のまま（表に無い英語は 0.0）。
    日本語は仲間の気持ちの強さ、表に無い言葉は ``UNKNOWN_EMOTION_STRENGTH``。
    """
    e = _clean(emotion)
    family = emotion_family(e)
    if family is not None:
        return EMOTION_BOOST_MAP[family]
    if _is_legacy_english(e):
        return 0.0
    return UNKNOWN_EMOTION_STRENGTH


def is_protected_emotion(emotion: str | None, protected: tuple[str, ...]) -> bool:
    """寝ている間の整理で消されない気持ちか（``protected`` は元の英語の語の並び）."""
    e = _clean(emotion)
    if e in protected:
        return True
    family = emotion_family(e)
    return family is not None and family in protected


def emotion_label(emotion: str | None) -> str:
    """表示用の日本語。気持ちが無ければ空文字.

    petit-app の emotionLabel と同じ決まり: 英数字だけの値は古い英語の値とみなして訳し、
    表に無い英語は出さない（英語のまま出さない）。日本語を含む値はぷちの言葉なのでそのまま。
    """
    e = _clean(emotion)
    if e in NEUTRAL_VALUES:
        return ""
    if _is_legacy_english(e):
        return LEGACY_EMOTION_LABELS.get(e.lower(), "")
    return e


def emotion_filter_values(emotion: str | None) -> tuple[str, ...] | None:
    """感情で絞るときに一致させる保存値の組.

    日本語で探したとき、同じ訳になる古い英語の記憶も拾う（「うれしい」→ joy・happy も）。
    英語で探したときは、その訳の日本語で書かれた記憶も拾う。
    """
    e = _clean(emotion)
    if not e:
        return None
    if e in NEUTRAL_VALUES:  # neutral・ふつう で探した → 気持ちの無い記憶
        return tuple(sorted(NEUTRAL_VALUES))
    legacy = _is_legacy_english(e)
    label = LEGACY_EMOTION_LABELS.get(e.lower(), e) if legacy else e
    values = {e, e.lower(), label} if legacy else {e}
    values.update(k for k, v in LEGACY_EMOTION_LABELS.items() if v == label)
    return tuple(sorted(values))
