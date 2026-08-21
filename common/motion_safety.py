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
# スカート姿なので使えない動作。しゃがむ・膝を深く曲げる・跳ぶ系は、
# ARDY が予備動作として「膝を深く曲げて脚を大きく開くしゃがみ」を必ず作り、
# カメラが正面・腰の高さにあるため下着が映る（実測: 2026-08-11 の夜版 19.3秒地点）。
# 腰の上下移動(VRMA_HIPS_Y)を切っても脚のポーズは変わらないので、動作ごと落とす。
# プロンプトでも禁止しているが、LLM が破ったときに事故るのでここでも遮断する
BANNED_MOTION_RE = re.compile(
    r"\b(jump|jumps|jumping|leap|leaps|hop|hops|hopping|squat|squats|squatting|"
    r"crouch|crouches|crouching|kneel|kneels|kneeling|sit|sits|sitting|"
    r"lunge|lunges|spring|springs|knees?|clap|claps|clapping|applaud|applauds)\b", re.I)

# 「A <なにか> stands in place [facing forward] [and|.]」までを丸ごと拾う
_MOTION_PREFIX_RE = re.compile(
    r"^\s*an?\s+\w+(\s+\w+)?\s+stands?\s+in\s+place"
    r"(\s+facing\s+forward)?\s*(and\s+|,\s*|\.\s*)?", re.I)

MOTION_SUBJECT = "A woman stands in place and "

# 文に motion が無い／禁止動作だったときに代わりに使う待機動作。
# ARDY のプールが尽きたときにも使う。
#
# 選定の根拠は弱い。ARDY は拡散モデルでシードによって出力が大きく変わるのに、
# 候補ごとにシード1つでしか測っていない。角度変化の二階差分（＝カクつき）が
# 大きいものを避けたつもりだったが、実際に録画して見比べたところ画面上の差は
# 確認できなかった。数値はあてにせず、実際の動画で問題が出たものだけを外している。
#
# 実際の動画で問題が出て外したもの:
#   claps their hands ...                拍手にならず、手が胸の前で中途半端に往復して
#                                        震えて見える（夜版72秒。独立2サンプルで再現）
#   keeps bouncing lightly on their toes 跳ねる動作。スカートなので下半身は使わない
#
# 2026-08-12: 全文にあった "facing forward"（正面固定の明示）を外した。
# Unity が体の向きを捨てていたので書いても無意味だったが、上限つきで通すようにした
# 以上、正面を明示すると ARDY が体を向けなくなる。
#
# 同じ日に、ARDY の /generate を直接叩いて spec の hips ヨーと上体ロールを実測した
# （3秒生成・独立2シード・振幅[度]。対照は "raises one hand to their chin"）:
#
#   指示                                         hipsヨー幅      上体ロール幅
#   （対照）                                       4.5 /  8.2     6.4 /  5.1
#   turns their upper body to their right,
#     then back to the front                      81.1 / 76.3    25.3 /  7.9
#   leans their upper body to their left,
#     then straightens up                         10.0 /  9.3    25.6 / 27.6
#   slowly sways their upper body from side to
#     side                                         6.4 /  2.7     6.2 /  2.5  ← 効かない
#   shakes their head slowly from side to side     1.2 /  1.5   （首ヨー 4.9 / 1.1）← 効かない
#
# 「…して、正面に戻る」という往復の形だけが効いた。sways / shakes は対照と差が無い
# ので、待機動作からもプロンプトの例からも外した。
#
# 主語は "A person / their" ではなく "A woman / her" にしてある（2026-08-15）。
# 動きが男っぽいという指摘への対処。ARDY はテキスト条件付きの拡散モデルなので、
# 主語の性別で分布が動くことを期待している。上の実測値は "A person / their" 版の
# ものなので、書き換えるときは振幅が落ちていないか測り直すこと。
IDLE_MOTIONS = [
    "A woman stands in place and opens both arms out to the sides at chest height.",
    "A woman stands in place and leans her upper body to her left, then straightens up.",
    "A woman stands in place and repeatedly nods her head down and up.",
    "A woman stands in place and keeps tilting her head from one shoulder to the other.",
    "A woman stands in place and clasps both hands together in front of her chest.",
    "A woman stands in place and brings one hand up to her chin.",
    "A woman stands in place and turns her upper body to her right, then back to the front.",
    "A woman stands in place and raises one hand straight above her head.",
]


def normalize_motion_text(text: str) -> str:
    """モーション指示文の主語を "A woman stands in place and ..." に揃える。

    プロンプトで「必ずこの形で始める」と指示しているが、**LLM は普通に破る**。
    実測（2026-08-12 / 08-15 の朝版）では主語ごと落として
    `raises one hand up to her chin` のような断片を返しており、
    ARDY には主語なしの文が渡っていた。禁止語と同じくコード側を最後の砦にする。

    主語が女性であることは ARDY の条件付けに効かせたい要素なので、
    `A person` と書かれていた場合も含めて書き換える。
    """
    text = (text or "").strip()
    if not text:
        return text
    body = _MOTION_PREFIX_RE.sub("", text).strip()
    if not body:                      # 主語だけで中身が無いなら捨てる
        return ""
    return MOTION_SUBJECT + body[0].lower() + body[1:]


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
