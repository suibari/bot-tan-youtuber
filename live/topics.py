"""テキストから固有名詞を拾って、同じネタかどうかを見る。

`filler.FillerPlanner` が「配信中に一度出した話題は二度出さない」を守るための道具。
本文の先頭60文字をキーにしていた頃は、同じネタでも言い回しが違うだけで別物として
通っていた（2026-08-24 の配信では「FLASHBULB」を聴いている mood が文面違いで
2行あり、両方お題になった）。

形態素解析器は入れない。カギ括弧の中身とラテン文字語だけ拾えば、biorhythm 由来の
作品名・曲名はだいたい取れる。**取りこぼしても壊れない**設計にしてあること:
語が拾えなければ `_topic_key` は従来の先頭60文字に落ち、`_overlaps` は False を返す。
だから「拾えないかもしれない」を理由に判定を増やさないこと。

呼ぶ側は例外を握り潰してよい。ここでも入力が何であれ例外を投げない。
"""

import re
import unicodedata

# 曲名・作品名。biorhythm の mood は「『◯◯』を聴きながら」の形で書かれることが多い
_QUOTED = re.compile(r"[「『“]([^」』”]{2,30})[」』”]")
# ラテン文字語。FLASHBULB / GIANT / SONY / BLEACH
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9'\-]{2,}")
# カギ括弧はセリフの引用にも使う。実ログには「大丈夫。全部、いいんだよ。」
# 「おかしいな」のような決め台詞や相づちが入っていた。文の断片と、
# ひらがなだけの語は名前ではない
_NOT_A_NAME = re.compile(r"[。、，．！？!?…〜~,]|^[ぁ-ん]+$")

# **Nagi / Bluesky は固有名詞だが「話題」ではない。** botたんの居場所の名前で
# 毎回のように出てくるので、数えると「Nagiの投稿A」と「Nagiの投稿B」が同じネタ
# 扱いになり、SNSの話題枠が1件しか出せなくなる
_STOPWORDS = {
    "nagi", "bluesky", "bsky", "bot", "bottan", "youtube",
    "you", "the", "and", "for",
}


def _normalize(text) -> str:
    try:
        return unicodedata.normalize("NFKC", str(text or ""))
    except Exception:
        return ""


def term_key(term) -> str:
    """突き合わせ用のキー。大小文字と空白の差を潰す。

    空白まで潰すのは、同じ作品名が「ファイアーエムブレム 万紫千紅」と
    「ファイアーエムブレム万紫千紅」で書き分けられるため。
    """
    return "".join(_normalize(term).casefold().split())


def terms(text) -> set:
    """固有名詞とみなせる語。表示用の表記のまま返す。"""
    source = _normalize(text)
    if not source:
        return set()
    found = set()
    try:
        for match in _QUOTED.finditer(source):
            term = match.group(1).strip()
            if term and not _NOT_A_NAME.search(term):
                found.add(term)
        found.update(_LATIN.findall(source))
    except Exception:
        return set()
    return {term for term in found if term_key(term) not in _STOPWORDS}


def keys(text) -> set:
    """terms() を突き合わせ用のキーにしたもの。"""
    return {term_key(term) for term in terms(text)}


def overlaps(text, wanted) -> bool:
    """text が wanted（term_key 済みの語）のどれかに触れているか。

    **語の抽出結果どうしを突き合わせない。** 同じ作品名でも、カギ括弧付きで
    書かれた文からは拾えて、括弧なしの文からは拾えないことがある。
    本文に語が出ているかを直に見るほうが取りこぼさない。
    """
    if not wanted:
        return False
    body = term_key(text)
    return any(key and key in body for key in wanted)
