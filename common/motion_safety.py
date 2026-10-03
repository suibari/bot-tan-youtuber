"""生成モーション指示文の安全化。

LLM はプロンプトの禁止事項を普通に破る。プロンプトは「そうしてほしい」の表明であって
保証ではないので、事故ると困るものはここで最後に遮断する。

正規表現・定数は実測で事故った履歴（下着の映り込み・手の震え）に基づく。**値を緩めないこと**。
以前は shorts/core.py と live/safety.py に同じものが2つあった。
"""

import re

# ── 禁止する動作 ─────────────────────────────────────
#
# 実際の動画で破綻が確認できた動作。ここに入れる基準は「録画して目で見て駄目だったもの」。
# ARDY はシードで出力が大きく変わるので、単発生成の数値では判断しないこと。
#
# 拍手: シードを変えた独立2サンプルとも拍手にならず、手が胸の前で中途半端に浮くだけ
#       だった（夜版72秒で発生）。armSpread を 0 にしても変わらなかった。
#
# 座る・ひざまずく・深くしゃがむ: その場に立って話すキャラには不自然で、
#       ARDY が脚を大きく開くので下着も映る（2026-08-11 の夜版 19.3秒地点）。
#
# ジャンプ・脚の動き（jump / hop / leap / knees）は 2026-10-03 に解禁した。
# スカートがめくれて太ももが見える程度は許容し、むしろ歓迎とユーザーが判断した
# （ダンスのジャンプを録画して確認済み）。腰の上下も vrma_style.VRMA_HIPS_Y で通している。
BANNED_MOTION_RE = re.compile(
    r"\b(squat|squats|squatting|crouch|crouches|crouching|kneel|kneels|kneeling|"
    r"sit|sits|sitting|lunge|lunges|clap|claps|clapping|applaud|applauds)\b", re.I)

# ── 指示文の形 ───────────────────────────────────────
#
# 2026-10-03 に "A woman stands in place and <到達点つきの細かい動作>." から
# "A person <短い動作> in a feminine way." に切り替えた。同じ音声・同じ5文で
# ARDY に両方を渡して録画し、見比べて決めた（Kimodo でも同じ傾向）。
#
#   旧: A woman stands in place and brings one hand up to her chin.
#   新: A person thinks while tilting their head in a feminine way.
#
# 旧形式は「手をもぞもぞやっているだけ」に見えていた。理由として分かったこと:
#   - 学習データ（BONES-SEED の説明文）の主語はほぼすべて "A person"。
#     "A woman" はデータに無い書き方で、女性らしさにはつながっていなかった
#   - "stands in place" はデータに大量にある「ただ立っている」動きに引っ張る
#   - データの説明文は短い。到達点や角度まで細かく書くほど固くなる
#   - 女性らしさは "in a feminine way"（データにある書き方）で出る
#
# 旧形式の実測（「…して、正面に戻る」の往復形だけが体を向けた等）は
# git の履歴（このファイルの 2026-08-12〜15 の版）に残っている。

# 先頭の主語（と旧形式の "stands in place [facing forward] [and]"）を拾う
_MOTION_PREFIX_RE = re.compile(
    r"^\s*(?:an?\s+(?:young\s+)?(?:person|woman|girl|lady|character)|she|they)\b"
    r"(?:\s+stands?\s+in\s+place(?:\s+facing\s+forward)?)?\s*(?:and\s+|,\s*|\.\s*)?", re.I)
_HERSELF_RE = re.compile(r"\bherself\b", re.I)
_HER_RE = re.compile(r"\bher\b", re.I)
_SHE_RE = re.compile(r"\bshe\b", re.I)

MOTION_SUBJECT = "A person "
MOTION_STYLE = "in a feminine way"

# 台本・配信の LLM に渡す motion の書き方。夜版・朝版・配信で同じものを使う
# （以前は3か所に別々に書いてあり、片方だけ直して食い違っていた）。
MOTION_PROMPT_RULES = """書き方のルール（2026-10-03 の録画比較に基づく）:
- **"A person" で始め、末尾に "in a feminine way" を付ける**
  （例: A person waves cheerfully in a feminine way.）
- **短く書く。動作は1つ、5〜10語程度。** 何をしているかを動詞で書けば十分で、
  手の到達点や角度まで細かく書かない（細かく書くほど動きが固くなる）
- "stands in place" は書かない（棒立ちに引っ張られる）
- 気持ちを表す副詞を使ってよい: cheerfully / happily / excitedly / shyly / gently
  状態として書いてもよい: is surprised / is excited / is thinking
- **前後左右への移動は書かない**（walk / step など。再生側で
  水平移動を捨てるので、その場で足踏みしているように見える）。その場で回るのはよい
- **跳ねる・弾むのは歓迎**（jumps / hops / bounces）。嬉しい・盛り上がる場面で使う
- **座る・ひざまずく・深くしゃがむ動作は禁止**（立って話しているキャラなので不自然）
  禁止: sit / kneel / squat / crouch / lunge
- **拍手は書かない**（clap / applaud）。生成AIが描けず、手を震わせている画になる
- 表情だけの動詞は使わない（smiles / looks / feels）。体が動かず棒立ちになる
- **話題の形や大きさを手で表すのも良い**（丸い物 → draws a circle in the air /
  大きい → spreads their arms wide / 小さい → shows something tiny with their fingers）
- 同じ動作を何度も使わない

よく使う形（この通りでなくてよい。内容に合わせてアレンジすること）:
  A person waves cheerfully in a feminine way. /
  A person nods happily in a feminine way. /
  A person is surprised in a feminine way. /
  A person thinks while tilting their head in a feminine way. /
  A person talks while gesturing with one hand in a feminine way. /
  A person clasps their hands in front of their chest in a feminine way. /
  A person raises both arms happily in a feminine way. /
  A person points upward while explaining in a feminine way. /
  A person jumps for joy in a feminine way."""

# 文に motion が無い／禁止動作だったときに代わりに使う待機動作。
# ARDY のプールが尽きたときにも使う。上の5文は 2026-10-03 の比較録画で使ったもの。
IDLE_MOTIONS = [
    "A person waves cheerfully in a feminine way.",
    "A person talks while gesturing with one hand in a feminine way.",
    "A person thinks while tilting their head in a feminine way.",
    "A person nods happily in a feminine way.",
    "A person is surprised in a feminine way.",
    "A person clasps their hands in front of their chest in a feminine way.",
    "A person explains something with both hands in a feminine way.",
    "A person sways gently while talking in a feminine way.",
]


def normalize_motion_text(text: str) -> str:
    """モーション指示文を "A person <動作> in a feminine way." の形に揃える。

    プロンプトで形を指示しているが、**LLM は普通に破る**。実測（2026-08-12 / 08-15 の
    朝版）では主語ごと落とした断片を返しており、ARDY には主語なしの文が渡っていた。
    禁止語と同じくコード側を最後の砦にする。旧形式（"A woman stands in place and ..."）
    で書かれていても新しい形に直す。代名詞も学習データに合わせて their に寄せる。
    """
    text = (text or "").strip()
    if not text:
        return text
    body = _MOTION_PREFIX_RE.sub("", text).strip().rstrip(".").strip()
    if not body:                      # 主語だけで中身が無いなら捨てる
        return ""
    body = _HERSELF_RE.sub("themselves", body)
    body = _HER_RE.sub("their", body)
    body = _SHE_RE.sub("they", body)
    if "feminine" not in body.lower():
        body = f"{body} {MOTION_STYLE}"
    return MOTION_SUBJECT + body[0].lower() + body[1:] + "."


def sanitize_motion(text: str) -> str:
    """配信に流してよいモーション指示文を返す。使えなければ空文字。

    落としても穴は空かない。呼び出し側が IDLE_MOTIONS かプールのモーションで埋める。
    """
    normalized = normalize_motion_text(text)
    if not normalized:
        return ""
    if BANNED_MOTION_RE.search(normalized):
        print(f"[safety] モーション除外: {normalized[:70]}")
        return ""
    return normalized


def reject_unsafe_motions(motions: list[dict], label: str = "") -> list[dict]:
    """スカートで破綻する動作を台本の motions から取り除く。

    落とした結果セグメントが足りなくなっても、IDLE_MOTIONS が埋めるので穴は空かない。
    """
    out = []
    for m in (motions or []):
        text = (m.get("text") or "")
        if BANNED_MOTION_RE.search(text):
            print(f"[モーション] 除外{f'({label})' if label else ''}: {text[:70]}")
            continue
        out.append(m)
    return out
