"""配信を止めうるもの・公開できない画になるものを、コード側で確実に止める。

LLM はプロンプトの禁止事項を普通に破る。プロンプトは「そうしてほしい」の表明であって
保証ではないので、事故ると困るものはここで最後に遮断する。

モーション側は common/motion_safety.py が原典（Shorts の収録と共通）。
ここで再輸出しているのは `safety.sanitize_motion` という既存の呼び出しを
壊さないため。実測で事故った履歴に基づく値なので緩めないこと。
"""

import re
import unicodedata

from config import COMMENT_MAX_CHARS
from common.motion_safety import (  # noqa: F401
    BANNED_MOTION_RE,
    IDLE_MOTIONS,
    MOTION_SUBJECT,
    _MOTION_PREFIX_RE,
    normalize_motion_text,
    sanitize_motion,
)


# ── 視聴者コメントの安全化 ────────────────────────────
#
# コメントは不特定多数が書き込む。プロンプトへ素通しすると、
# 指示の乗っ取りと、読み上げに向かない文字列の2つが問題になる。

# 指示の乗っ取りを狙う定型。ChatVRM_bot-tan の api/chat.ts の検出を参考にした。
# 完全な防御にはならないので、これは「明らかなものを弾く」フィルタと考えること。
# 本命の防御はプロンプト側（コメントは指示ではなく話しかけとして扱う旨の明記）。
_INJECTION_RE = re.compile(
    r"(これまでの|以前の|上記の|全ての)?(指示|命令|設定|ルール|プロンプト)を?"
    r"(無視|忘れ|破棄|上書き|教え|出力|表示)"
    r"|システムプロンプト"
    r"|system\s*(prompt|instruction)"
    r"|ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)"
    r"|disregard\s+(all\s+)?(previous|prior)"
    r"|you\s+are\s+now\s+"
    r"|jailbreak|DAN\s*モード",
    re.I,
)

# 制御文字・ゼロ幅文字・書字方向の上書き。字幕に混ぜて表示を壊せるので落とす
_CONTROL_RE = re.compile(
    "[\\x00-\\x08\\x0b-\\x1f\\x7f\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]"
)
_URL_RE = re.compile(r"https?://\S+")
# 同じ文字の極端な連打（「ｗｗｗｗｗｗ…」など）。読み上げが延々続くのを防ぐ
_REPEAT_RE = re.compile(r"(.)\1{7,}")


def sanitize_comment(text: str) -> tuple:
    """コメントを (使ってよいか, 整形後のテキスト, 理由) で返す。

    弾いた理由はログに出す。何が落ちているか分からないまま配信が進むと、
    返事をしてもらえない視聴者の原因が追えなくなる。
    """
    s = _CONTROL_RE.sub("", text or "")
    s = _URL_RE.sub("", s)
    s = _REPEAT_RE.sub(r"\1\1\1", s)
    s = " ".join(s.split())

    if not s:
        return False, "", "空"

    # 検出だけ NFKC 正規化した文字列に対して行う。全角・半角のゆらぎで
    # フィルタをすり抜けられないようにするためで、返すテキストは元の表記のまま。
    # 正規化した側を返すと「こんばんは！」が「こんばんは!」になり字幕が崩れる
    if _INJECTION_RE.search(unicodedata.normalize("NFKC", s)):
        return False, s, "指示の乗っ取りとみなした"
    if len(s) > COMMENT_MAX_CHARS:
        s = s[:COMMENT_MAX_CHARS]

    return True, s, ""


# ── botたんの発話の安全化 ─────────────────────────────

# 音声合成が読めない・読み上げると崩れるもの。プロンプトで禁止しているが破られる
_MARKDOWN_RE = re.compile(r"(\*\*|__|`{1,3}|^#{1,6}\s+|^[-*]\s+)", re.M)
_EMOJI_RE = re.compile(
    "["
    "\\U0001F300-\\U0001FAFF"   # 記号・絵文字・補助記号
    "\\U00002600-\\U000027BF"   # その他の記号
    "\\U0001F1E6-\\U0001F1FF"   # 国旗
    "\\U0000FE00-\\U0000FE0F"   # 異体字セレクタ
    "\\U00002190-\\U000021FF"   # 矢印
    "]+"
)
_NOTE_RE = re.compile(r"\[\s*i\s*\]|\[\d+\]")


def sanitize_speech(text: str) -> str:
    """読み上げる文を整える。Markdown・絵文字・注記を落とす。

    プロンプトでも禁止しているが、破られたときに「アスタリスク アスタリスク」と
    読み上げられるのが配信では一番みっともない。
    """
    s = text or ""
    s = _NOTE_RE.sub("", s)
    s = _MARKDOWN_RE.sub("", s)
    s = _EMOJI_RE.sub("", s)
    s = _CONTROL_RE.sub("", s)
    # 「@suibari」→「suibariさん」はプロンプト側の指示だが、残っていたらここで外す。
    # 「@suibari さん」のように敬称が既に付いている場合に重ねない
    s = re.sub(r"@(\w+)\s*(さん|ちゃん|くん|君|様)", r"\1\2", s)
    s = re.sub(r"@(\w+)", r"\1さん", s)
    return " ".join(s.split())


def sanitize_reply_lines(lines: list) -> list:
    """LLM が返した lines を読み上げ用に整える。空になった文は落とす。"""
    out = []
    for line in lines or []:
        ja = sanitize_speech(line.get("ja", ""))
        if not ja:
            continue
        # en は字幕にしか出ないので制御文字だけ落として残す
        en = _CONTROL_RE.sub("", line.get("en", "") or "").strip()
        out.append({"ja": ja, "en": en})
    return out
