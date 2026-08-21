"""喋っていない間の演出。

Unity 側は、生成モーションが乗っていない区間は Animator の Idle クリップを
流し続けるだけになる。表情も /emotion で最後に指定した値のまま固定される。
初回の配信では「黙っている間ずっと同じ立ち姿・同じ顔」になっていた。

ここでは配信ループとは別スレッドで、

  - 数十秒おきにプールのモーションを1本投げる（しぐさ）
  - 数秒〜十数秒おきに表情を少しだけ振る（valence/arousal のランダムウォーク）

を行う。Unity の /status を見て、喋っている間は何もしない。

モーションはプールから引くので、ARDY が配信中に足していったものも
そのまま待機の動きとして使われる。
"""

import random
import threading
import time

import unity_client
from config import (IDLE_EMOTION_MAX_SEC, IDLE_EMOTION_MIN_SEC,
                    IDLE_MOTION_MAX_SEC, IDLE_MOTION_MIN_SEC)

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


def _weighted_category() -> str:
    names = [c for c, _ in IDLE_CATEGORIES]
    weights = [w for _, w in IDLE_CATEGORIES]
    return random.choices(names, weights=weights, k=1)[0]


class IdleAnimator(threading.Thread):
    """待機中の身振りと表情。配信ループからは start/stop するだけでよい。"""

    def __init__(self, pool, enabled: bool = True):
        super().__init__(daemon=True, name="idle")
        self.pool = pool
        self.enabled = enabled
        self._stop = threading.Event()
        self._hold_until = 0.0
        self._base = (BASE_VALENCE, BASE_AROUSAL)
        self._current = (BASE_VALENCE, BASE_AROUSAL)

    # ── 配信ループから呼ぶ ──

    def hold(self, seconds: float = 3.0) -> None:
        """しばらく手を出さない。

        発話を投げてから Unity が実際に鳴らし始めるまでの数百ミリ秒は
        /status が speaking=false のままなので、その隙に待機モーションが
        割り込むと、喋り出しと同時に別の動きが被る。
        """
        self._hold_until = max(self._hold_until, time.monotonic() + seconds)

    def set_base(self, valence: float, arousal: float) -> None:
        """直前の発話の感情を素の顔として引き継ぐ。"""
        self._base = (max(-1.0, min(1.0, valence)), max(-1.0, min(1.0, arousal)))

    def stop(self) -> None:
        self._stop.set()

    # ── 本体 ──

    def run(self) -> None:
        if not self.enabled:
            return
        next_motion = time.monotonic() + random.uniform(IDLE_MOTION_MIN_SEC,
                                                        IDLE_MOTION_MAX_SEC)
        next_emotion = time.monotonic() + random.uniform(IDLE_EMOTION_MIN_SEC,
                                                         IDLE_EMOTION_MAX_SEC)
        while not self._stop.wait(0.5):
            now = time.monotonic()
            if now < self._hold_until:
                continue
            if self._busy():
                # 喋っている間は触らない。予定は先送りする
                next_motion = max(next_motion, now + IDLE_MOTION_MIN_SEC)
                next_emotion = max(next_emotion, now + IDLE_EMOTION_MIN_SEC)
                continue

            if now >= next_motion:
                self._play_gesture()
                next_motion = now + random.uniform(IDLE_MOTION_MIN_SEC,
                                                   IDLE_MOTION_MAX_SEC)
            if now >= next_emotion:
                self._drift_emotion()
                next_emotion = now + random.uniform(IDLE_EMOTION_MIN_SEC,
                                                    IDLE_EMOTION_MAX_SEC)

    def _busy(self) -> bool:
        """Unity が発話中か。落ちているときは触らない方に倒す。"""
        try:
            st = unity_client.status()
        except unity_client.UnityError:
            return True
        return bool(st.get("speaking")) or int(st.get("speak_queue", 0)) > 0

    def _play_gesture(self) -> None:
        path = self.pool.pick(_weighted_category())
        if not path:
            return
        try:
            unity_client.motion(path)
        except unity_client.UnityError as e:
            print(f"[idle] しぐさを送れません（無視します）: {e}")

    def _drift_emotion(self) -> None:
        bv, ba = self._base
        cv, ca = self._current
        # 素の顔へ半分戻してから振る。放っておくと端に張り付く
        cv = (cv + bv) / 2 + random.uniform(-EMOTION_JITTER, EMOTION_JITTER)
        ca = (ca + ba) / 2 + random.uniform(-EMOTION_JITTER, EMOTION_JITTER)
        cv = max(-1.0, min(1.0, cv))
        ca = max(-1.0, min(1.0, ca))
        self._current = (cv, ca)
        try:
            unity_client.emotion(cv, ca)
        except unity_client.UnityError as e:
            print(f"[idle] 表情を送れません（無視します）: {e}")
