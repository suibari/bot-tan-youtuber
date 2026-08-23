"""アバターの身振りと表情。/motion を投げるのはここだけ。

Unity 側は、生成モーションが乗っていない区間は Animator の Idle クリップを
流し続けるだけになる。表情も /emotion で最後に指定した値のまま固定される。
初回の配信では「黙っている間ずっと同じ立ち姿・同じ顔」になっていた。

ここでは配信ループとは別スレッドで、

  - 発話中: クリップが切れる手前で次を投げ続け、生成モーションを途切れさせない
  - 待機中: 数十秒おきにプールのモーションを1本投げる（しぐさ）
  - 待機中: 数秒おきに表情を動かす。ランダムウォークに加えて、ときどき
    ほほえみや「はっと気づいた顔」の山を作って戻す（IDLE_SMILE_CHANCE 参照）

を行う。

**`/motion` の送出者をこのクラス1つに集約してあること。**
以前は `live.py:_speak` も直接 `unity_client.motion()` を呼んでいて、待機スレッドが
投げた直後に発話側が投げると、Unity 側（VrmaMotionPlayer.EnqueueMotion）が
フェードイン途中＝重み1未満のクリップを打ち切る。すると合成後の重み
`Min(1, wa+wb)` が1に届かず Idle が一瞬混ざり、ポーズが跳ねて見えていた。

発話中に途切れさせないやり方は録画パイプラインと同じ。あちらは
`VRMA_CHUNK_OVERLAP`（=VrmaMotionPlayer.FadeDuration）ぶん重ねた時刻表を
事前に作って渡す。配信は先の尺が分からないので、同じ重なりを時間で刻む。

モーションはプールから引くので、ARDY が配信中に足していったものも
そのまま使われる。
"""

import random
import threading
import time

import unity_client
from config import (IDLE_EMOTION_MAX_SEC, IDLE_EMOTION_MIN_SEC,
                    IDLE_MOTION_MAX_SEC, IDLE_MOTION_MIN_SEC,
                    IDLE_SMILE_HOLD_SEC, VRMA_CHUNK_OVERLAP)

# 待機中に引くカテゴリの重み。大げさな動き（happy の両手上げなど）が
# 黙っているときに出ると唐突なので、落ち着いたものを厚くする
IDLE_CATEGORIES = (
    ("neutral",     5),
    ("thinking",    3),
    ("greeting",    2),
    ("encouraging", 2),
    ("happy",       1),
)

# 表情の揺れ幅。大きくすると百面相になる
EMOTION_JITTER = 0.22
# 素の顔。振れたあとはここへ戻ろうとする
BASE_VALENCE = 0.55
BASE_AROUSAL = 0.15

# 待機中の表情イベントの引き。
#
# Unity が受け取れるのは valence/arousal の2軸だけで、ウィンクのような
# 個別の表情は指定できない（LiveController に /expression が無い）。
# 「表情が豊か」を作れるのは**値の動かし方**だけなので、ただのランダムウォークに
# 加えて、はっきり分かる山をときどき入れる。
#   ほほえみ: valence を大きく上げて arousal を下げると Happy が優勢になる
#   はっと顔: arousal を上げると Surprised が少し混ざる
# 残りは従来どおりのドリフト。
IDLE_SMILE_CHANCE = 0.25
IDLE_ALERT_CHANCE = 0.10
IDLE_ALERT_HOLD_SEC = 0.8

# ループの刻み[秒]。
# 発話の頭のモーションはこのぶん遅れて出るので短くしておく（以前は _speak が
# 直接投げていたので遅れは無かった）。発話中の継ぎ足しが遅れると重なりも減る。
# /status を叩かないので、短くしてもコストはほぼ増えない
TICK_SEC = 0.1


def _weighted_category() -> str:
    names = [c for c, _ in IDLE_CATEGORIES]
    weights = [w for _, w in IDLE_CATEGORIES]
    return random.choices(names, weights=weights, k=1)[0]


class IdleAnimator(threading.Thread):
    """身振りと表情の唯一の送出者。配信ループからは start/stop するだけでよい。"""

    def __init__(self, pool, enabled: bool = True):
        """enabled は「黙っている間も動かすか」。False でも発話中は動かす
        （発話のモーションもこのスレッドが投げているため）。"""
        super().__init__(daemon=True, name="idle")
        self.pool = pool
        self.enabled = enabled
        self._stop = threading.Event()
        self._base = (BASE_VALENCE, BASE_AROUSAL)
        self._current = (BASE_VALENCE, BASE_AROUSAL)
        # 発話中の状態。配信ループのスレッドから書き、このスレッドが読む
        self._lock = threading.Lock()
        self._speaking = False
        self._speak_category = "neutral"
        self._next_at = 0.0          # 次にモーションを投げる時刻（monotonic）
        # 山を作ったあと、次の表情イベントで素の顔へ引き戻すかどうか
        self._releasing = False

    # ── 配信ループから呼ぶ ──

    def speak_begin(self, category: str, valence: float = None,
                    arousal: float = None) -> None:
        """発話の頭で呼ぶ。すぐに1本投げ、以後クリップが切れる手前で継ぎ足す。

        表情は発話側（_speak）が /emotion で決めるので、ここでは振らない。
        """
        if valence is not None and arousal is not None:
            self.set_base(valence, arousal)
        with self._lock:
            self._speaking = True
            self._speak_category = category or "neutral"
            self._next_at = 0.0      # 次のループで即座に1本投げる
        # 発話側が /emotion を決めるので、待機中に作った山は捨てる
        self._releasing = False

    def speak_end(self) -> None:
        """発話が終わったら呼ぶ。待機モードへ戻す。"""
        with self._lock:
            self._speaking = False
            # 喋り終わりの余韻を持たせる。すぐ次の待機モーションを投げると
            # 最後のクリップと二重に掛かる
            self._next_at = time.monotonic() + random.uniform(IDLE_MOTION_MIN_SEC,
                                                              IDLE_MOTION_MAX_SEC)

    def set_base(self, valence: float, arousal: float) -> None:
        """直前の発話の感情を素の顔として引き継ぐ。"""
        self._base = (max(-1.0, min(1.0, valence)), max(-1.0, min(1.0, arousal)))

    def stop(self) -> None:
        self._stop.set()

    # ── 本体 ──

    def run(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._next_at = now + random.uniform(IDLE_MOTION_MIN_SEC,
                                                 IDLE_MOTION_MAX_SEC)
        next_emotion = now + random.uniform(IDLE_EMOTION_MIN_SEC,
                                            IDLE_EMOTION_MAX_SEC)
        while not self._stop.wait(TICK_SEC):
            now = time.monotonic()
            with self._lock:
                speaking = self._speaking
                category = self._speak_category
                due_at = self._next_at
            due = now >= due_at

            if speaking:
                if due:
                    # 次の予定は「実際に投げた時刻」ではなく「予定時刻」から数える。
                    # 実際の時刻を基準にするとループの刻みぶん（最大 TICK_SEC）だけ
                    # 毎回後ろへずれ、重なりがその都度削られていく
                    self._play(category, base=due_at)
                continue

            # 待機中。IDLE_ENABLED=false ならここから先はやらない
            # （発話中のモーションだけを残す）
            if not self.enabled:
                continue
            if due:
                self._play(_weighted_category(),
                           spacing=random.uniform(IDLE_MOTION_MIN_SEC,
                                                  IDLE_MOTION_MAX_SEC))
            if now >= next_emotion:
                next_emotion = now + self._next_expression()

    def _play(self, category: str, spacing: float = None,
              base: float = None) -> None:
        """1本投げて、次に投げる時刻を決める。

        spacing を渡さなければ「クリップの尺 - 重なり」＝途切れない間隔にする。
        VRMA_CHUNK_OVERLAP は VrmaMotionPlayer.FadeDuration と同じ値で、
        Unity 側はこの重なりで2本をクロスフェードする（重みの合計が常に1に
        なるので Idle が顔を出さない）。
        """
        retry = spacing if spacing is not None else IDLE_MOTION_MIN_SEC
        path = self.pool.pick(category)
        if not path:
            # プールが空。Animator の Idle が出るだけなので配信は続く
            self._schedule(retry)
            return
        clip = self.pool.duration_of(path)
        try:
            unity_client.motion(path)
        except unity_client.UnityError as e:
            print(f"[idle] しぐさを送れません（無視します）: {e}")
            self._schedule(retry)
            return
        if spacing is None:
            spacing = max(0.5, clip - VRMA_CHUNK_OVERLAP)
        self._schedule(spacing, base=base)

    def _schedule(self, seconds: float, base: float = None) -> None:
        """次にモーションを投げる時刻を決める。

        base（前回の予定時刻）を渡すとそこから数えるので、ループの刻みぶんの
        遅れが積み上がらない。大きく出遅れていたら現在時刻から数え直す
        （スリープ復帰などで一気にまとめて投げないため）。
        """
        now = time.monotonic()
        if base is None or now - base > 1.0:
            base = now
        with self._lock:
            self._next_at = base + seconds

    def _next_expression(self) -> float:
        """待機中の表情をひとつ動かし、次のイベントまでの秒数を返す。

        山を作った直後は必ず引き戻す。作りっぱなしにすると、次のドリフトまで
        数秒のあいだ笑顔で固まったままになる。
        """
        if self._releasing:
            self._releasing = False
            self._drift_emotion()
            return random.uniform(IDLE_EMOTION_MIN_SEC, IDLE_EMOTION_MAX_SEC)

        roll = random.random()
        if roll < IDLE_SMILE_CHANCE:
            # 何もない時間にふとほほえみかける
            self._set_emotion(random.uniform(0.85, 0.95),
                              random.uniform(0.0, 0.2))
            self._releasing = True
            return IDLE_SMILE_HOLD_SEC
        if roll < IDLE_SMILE_CHANCE + IDLE_ALERT_CHANCE:
            # 何かに気づいたような顔。生きている感じが出る
            base_valence = self._base[0]
            self._set_emotion(max(base_valence, 0.5),
                              random.uniform(0.55, 0.75))
            self._releasing = True
            return IDLE_ALERT_HOLD_SEC

        self._drift_emotion()
        return random.uniform(IDLE_EMOTION_MIN_SEC, IDLE_EMOTION_MAX_SEC)

    def _drift_emotion(self) -> None:
        bv, ba = self._base
        cv, ca = self._current
        # 素の顔へ半分戻してから振る。放っておくと端に張り付く
        cv = (cv + bv) / 2 + random.uniform(-EMOTION_JITTER, EMOTION_JITTER)
        ca = (ca + ba) / 2 + random.uniform(-EMOTION_JITTER, EMOTION_JITTER)
        self._set_emotion(cv, ca)

    def _set_emotion(self, valence: float, arousal: float) -> None:
        """目標値を投げる。実際の見た目は Unity 側の Lerp が滑らかに繋ぐ。"""
        valence = max(-1.0, min(1.0, valence))
        arousal = max(-1.0, min(1.0, arousal))
        self._current = (valence, arousal)
        try:
            unity_client.emotion(valence, arousal)
        except unity_client.UnityError as e:
            print(f"[idle] 表情を送れません（無視します）: {e}")
